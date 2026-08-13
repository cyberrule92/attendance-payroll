"""Tests for punch -> daily attendance derivation.

The overnight-shift cases are the important ones: a factory night shift crosses
midnight, and getting the work-date attribution wrong would silently rob people
of night pay.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import (
    AttendanceDay,
    DayStatus,
    Employee,
    LeaveRecord,
    LeaveType,
    Punch,
    PunchDirection,
)
from app.services.attendance import (
    build_month_inputs,
    next_direction,
    recompute_day,
    recompute_range,
    store_dt,
    work_date_for,
)
from app.services.payroll import compute_payroll, month_dates
from tests.conftest import local


def punch(db: Session, employee: Employee, when, direction: PunchDirection) -> Punch:
    row = Punch(
        employee_id=employee.id,
        location_id=employee.location_id,
        direction=direction,
        captured_at=store_dt(when),
        received_at=store_dt(when),
        work_date=work_date_for(when),
        client_uuid=f"{employee.id}-{when.isoformat()}-{direction.value}",
    )
    db.add(row)
    db.commit()
    return row


# --- work date attribution --------------------------------------------------


@pytest.mark.parametrize(
    "when, expected",
    [
        (local(2026, 8, 13, 9, 0), date(2026, 8, 13)),  # ordinary morning
        (local(2026, 8, 13, 20, 0), date(2026, 8, 13)),  # night shift start
        (local(2026, 8, 14, 2, 30), date(2026, 8, 13)),  # still that shift
        (local(2026, 8, 14, 4, 59), date(2026, 8, 13)),  # just before cutover
        (local(2026, 8, 14, 5, 0), date(2026, 8, 14)),  # cutover -> new work day
    ],
)
def test_punches_before_the_cutover_belong_to_the_previous_work_day(when, expected):
    assert work_date_for(when) == expected


def test_overnight_shift_stays_on_one_work_day(db, employee):
    punch(db, employee, local(2026, 8, 12, 20, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 13, 3, 0), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.worked_minutes == 420  # 7h, not split across midnight
    assert row.status is DayStatus.FULL
    assert row.needs_review is False

    # And the following calendar day is untouched -- no orphaned punch-out.
    next_day = recompute_day(db, employee, date(2026, 8, 13))
    assert next_day.punch_count == 0


def test_overnight_night_shift_earns_night_status(db, employee):
    # 14:00 to 04:30 = 14h30m worked -> OT 6h -> above the 5h night bar.
    punch(db, employee, local(2026, 8, 12, 14, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 13, 4, 30), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.worked_minutes == 870
    assert row.ot_minutes == 360
    assert row.is_night is True


# --- deriving worked minutes ------------------------------------------------


def test_first_in_to_last_out_ignores_breaks(db, employee):
    punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 12, 13, 0), PunchDirection.OUT)  # lunch out
    punch(db, employee, local(2026, 8, 12, 14, 0), PunchDirection.IN)  # lunch back
    punch(db, employee, local(2026, 8, 12, 19, 0), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 12))

    # 09:00 -> 19:00 is 10h. The hour of lunch is deliberately not subtracted.
    assert row.worked_minutes == 600
    assert row.punch_count == 4
    assert row.ot_minutes == 90


def test_missing_punch_out_is_flagged_for_review(db, employee):
    punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)

    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.needs_review is True
    assert "never punched out" in row.review_reason
    assert row.worked_minutes == 0
    assert row.status is DayStatus.LEAVE  # until the admin corrects it


def test_punch_out_without_punch_in_is_flagged(db, employee):
    punch(db, employee, local(2026, 8, 12, 19, 0), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.needs_review is True
    assert "without a punch in" in row.review_reason


def test_absurdly_long_span_is_flagged_but_still_counted(db, employee):
    punch(db, employee, local(2026, 8, 12, 6, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 12, 23, 30), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.worked_minutes == 1050  # 17h30m
    assert row.needs_review is True
    assert "missed punch out" in row.review_reason


def test_voided_punches_are_ignored(db, employee):
    punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)
    bad = punch(db, employee, local(2026, 8, 12, 9, 1), PunchDirection.OUT)
    punch(db, employee, local(2026, 8, 12, 19, 0), PunchDirection.OUT)

    bad.is_voided = True
    bad.void_reason = "Double tap on the kiosk"
    db.commit()

    row = recompute_day(db, employee, date(2026, 8, 12))
    assert row.worked_minutes == 600
    assert row.punch_count == 2


def test_no_punches_on_a_working_day_is_a_leave(db, employee):
    row = recompute_day(db, employee, date(2026, 8, 12))  # a Wednesday
    assert row.status is DayStatus.LEAVE


def test_no_punches_on_the_weekly_off_is_not_a_leave(db, employee):
    thursday = date(2026, 8, 13)
    assert thursday.weekday() == 3
    row = recompute_day(db, employee, thursday)
    assert row.status is DayStatus.WEEKOFF


def test_working_the_weekly_off_still_earns_overtime(db, employee):
    """salary.txt is silent here. A Thursday night shift must not be free."""
    punch(db, employee, local(2026, 8, 13, 14, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 14, 4, 30), PunchDirection.OUT)

    row = recompute_day(db, employee, date(2026, 8, 13))

    assert row.status is DayStatus.FULL
    assert row.worked_minutes == 870
    assert row.is_night is True


def test_a_short_stint_on_the_weekly_off_is_never_deducted(db, employee):
    punch(db, employee, local(2026, 8, 13, 9, 0), PunchDirection.IN)
    punch(db, employee, local(2026, 8, 13, 13, 0), PunchDirection.OUT)  # 4h

    row = recompute_day(db, employee, date(2026, 8, 13))

    # Would be a half day on a normal date; on a week off it stays fully paid.
    assert row.status is DayStatus.WEEKOFF


# --- overrides survive recompute --------------------------------------------


def test_manual_correction_survives_a_recompute(db, employee):
    punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)
    row = recompute_day(db, employee, date(2026, 8, 12))
    assert row.status is DayStatus.LEAVE  # no punch out yet

    # Admin corrects it: they actually left at 20:00 (11h -> 2h30m OT).
    row.manual_worked_minutes = 660
    row.manual_note = "Forgot to punch out; confirmed with supervisor"
    db.commit()

    # A later punch arrives and triggers another recompute.
    punch(db, employee, local(2026, 8, 12, 9, 5), PunchDirection.IN)
    row = recompute_day(db, employee, date(2026, 8, 12))

    assert row.manual_worked_minutes == 660
    assert row.status is DayStatus.FULL
    assert row.ot_minutes == 150


def test_manual_status_override_survives_a_recompute(db, employee):
    row = recompute_day(db, employee, date(2026, 8, 12))
    row.manual_status = DayStatus.PAID_LEAVE
    db.commit()

    row = recompute_day(db, employee, date(2026, 8, 12))
    assert row.status is DayStatus.PAID_LEAVE


def test_approved_paid_leave_changes_the_day_status(db, employee):
    db.add(
        LeaveRecord(
            employee_id=employee.id,
            leave_date=date(2026, 8, 12),
            leave_type=LeaveType.PAID,
            reason="Festival",
        )
    )
    db.commit()

    row = recompute_day(db, employee, date(2026, 8, 12))
    assert row.status is DayStatus.PAID_LEAVE


# --- kiosk direction toggle -------------------------------------------------


def test_next_direction_alternates(db, employee):
    work_date = date(2026, 8, 12)
    assert next_direction(db, employee.id, work_date) is PunchDirection.IN

    punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)
    assert next_direction(db, employee.id, work_date) is PunchDirection.OUT

    punch(db, employee, local(2026, 8, 12, 19, 0), PunchDirection.OUT)
    assert next_direction(db, employee.id, work_date) is PunchDirection.IN


def test_next_direction_ignores_voided_punches(db, employee):
    work_date = date(2026, 8, 12)
    bad = punch(db, employee, local(2026, 8, 12, 9, 0), PunchDirection.IN)
    bad.is_voided = True
    db.commit()
    assert next_direction(db, employee.id, work_date) is PunchDirection.IN


# --- assembling a month -----------------------------------------------------


def test_days_before_joining_are_not_charged_as_leave(db, employee):
    employee.joined_on = date(2026, 3, 20)
    db.commit()

    dates = month_dates(2026, 3)
    recompute_range(db, [employee], dates[0], dates[-1])
    inputs = build_month_inputs(db, employee, dates)

    assert len(inputs) == 12  # 20th to 31st only
    assert inputs[0].work_date == date(2026, 3, 20)


def test_days_that_have_not_happened_yet_are_not_counted_as_leave(db, employee):
    """Opening the current month mid-month must not show the rest of it as
    absences and deduct a fortnight of salary."""
    today = date.today()
    dates = month_dates(today.year, today.month)
    recompute_range(db, [employee], dates[0], min(dates[-1], today))

    inputs = build_month_inputs(db, employee, dates)
    assert inputs, "the days so far should still be present"
    assert max(day.work_date for day in inputs) <= today
    if dates[-1] > today:
        assert len(inputs) < len(dates)


def test_a_finished_past_month_still_counts_every_day(db, employee):
    dates = month_dates(2026, 1)
    recompute_range(db, [employee], dates[0], dates[-1])
    inputs = build_month_inputs(db, employee, dates)
    assert len(inputs) == 31


def test_full_month_flows_end_to_end_into_the_payroll_engine(db, employee):
    """A realistic completed month, from punches all the way to a grand total."""
    year, month = 2026, 3  # March 2026: 31 days, and safely in the past
    dates = month_dates(year, month)
    expected_weekoffs = sum(1 for d in dates if d.weekday() == 3)

    for work_date in dates:
        if work_date.weekday() == 3:  # Thursday off
            continue
        if work_date.day in (29, 30, 31):  # absent
            continue
        if work_date.day == 27:  # half day: 09:00-13:00
            punch(db, employee, local(year, month, 27, 9, 0), PunchDirection.IN)
            punch(db, employee, local(year, month, 27, 13, 0), PunchDirection.OUT)
            continue
        if work_date.day == 25:  # night: 14:00 -> 04:30 next day
            punch(db, employee, local(year, month, 25, 14, 0), PunchDirection.IN)
            punch(db, employee, local(year, month, 26, 4, 30), PunchDirection.OUT)
            continue
        start = local(year, month, work_date.day, 9, 0)
        punch(db, employee, start, PunchDirection.IN)
        punch(db, employee, start + timedelta(minutes=510), PunchDirection.OUT)

    recompute_range(db, [employee], dates[0], dates[-1])
    result = compute_payroll(
        build_month_inputs(db, employee, dates), employee_policy_of(employee)
    )

    assert result.days_weekoff == expected_weekoffs
    assert result.nights == 1
    assert result.days_half == 1
    assert result.days_leave == 3
    # Every other day was an exact 8h30m shift, so nothing pools.
    assert result.ot_minutes_paid == 0
    assert result.night_pay == result.daily_rate.quantize(Decimal("0.01"))


def employee_policy_of(employee: Employee):
    from app.services.attendance import employee_policy

    return employee_policy(employee)


def test_recompute_range_covers_every_day(db, employee):
    count = recompute_range(db, [employee], date(2026, 8, 1), date(2026, 8, 31))
    assert count == 31
    rows = db.query(AttendanceDay).filter_by(employee_id=employee.id).count()
    assert rows == 31
