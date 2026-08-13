"""Tests for the payroll rule engine.

The two headline cases are the worked examples agreed with the owner. The rest
pin down the boundaries in salary.txt that are easy to get subtly wrong --
"exceeds" vs "at least", the 30-minute OT discard, and the clamp on rule 5.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import DayStatus
from app.services.payroll import (
    DayInput,
    EmployeePolicy,
    classify_status,
    compute_payroll,
    evaluate_day,
    minutes_to_hhmm,
)

STANDARD = EmployeePolicy(
    monthly_salary=Decimal("15000"),
    night_threshold_hours=5.0,
    ot_adjustment_per_night_hours=0.0,
)

# Sita / Shama: lower night bar, and rule 5's extra give-back.
LOW_BAR = EmployeePolicy(
    monthly_salary=Decimal("12000"),
    night_threshold_hours=4.0,
    ot_adjustment_per_night_hours=1.5,
)


def day(n: int, minutes: int = 0, **kwargs) -> DayInput:
    """A day in August 2026. Week-offs are set explicitly, not by weekday, so
    these tests do not depend on what day of the week the 6th happens to be."""
    return DayInput(work_date=date(2026, 8, n), worked_minutes=minutes, **kwargs)


# --- rates ------------------------------------------------------------------


def test_rates_always_divide_by_thirty():
    # August has 31 days, but rule 2 says /30 regardless.
    assert STANDARD.daily_rate == Decimal("500")
    assert STANDARD.hourly_rate == Decimal("500") / Decimal("8.5")


# --- worked example 1: standard employee ------------------------------------


def standard_month() -> list[DayInput]:
    """31 days: 20 exact shifts, 2 days of 2h30m OT, 1 night, 1 half day,
    4 week-offs, 3 blank days."""
    days = [day(n, 510) for n in range(1, 21)]  # 8h30m exactly -> no OT
    days += [day(21, 660), day(22, 660)]  # 11h -> OT 2h30m each
    days += [day(23, 840)]  # 14h -> OT 5h30m -> night
    days += [day(24, 240)]  # 4h -> half day
    days += [day(n, 0, is_weekly_off=True) for n in range(25, 29)]
    days += [day(n, 0) for n in range(29, 32)]  # blank -> leaves
    assert len(days) == 31
    return days


def test_standard_employee_worked_example():
    result = compute_payroll(standard_month(), STANDARD)

    assert result.days_full == 23  # 20 + 2 OT days + 1 night day
    assert result.days_half == 1
    assert result.days_leave == 3
    assert result.days_weekoff == 4
    assert result.nights == 1

    # The night day's 5h30m is excluded from the pool; only the two 2h30m
    # days survive.
    assert result.ot_minutes_raw == 300
    assert result.ot_minutes_paid == 300

    assert result.ot_pay == Decimal("294.12")  # 5h x 58.8235...
    assert result.night_pay == Decimal("500.00")
    assert result.halfday_deduction == Decimal("250.00")
    assert result.leave_deduction == Decimal("1500.00")

    # 1 night does not cover 3 leaves + 1 half day.
    assert result.bonus_granted is False
    assert result.attendance_bonus == Decimal("0.00")

    assert result.grand_total == Decimal("14044.12")
    assert result.net_payable == Decimal("14044.12")


# --- worked example 2: the 4h-bar employees ---------------------------------


def test_low_bar_employee_worked_example():
    days = [day(1, 780), day(2, 870)]  # OT 4h30m and 6h -> both nights (>4h)
    days += [day(n, 630) for n in range(3, 6)]  # OT 2h each -> pool 6h
    days += [day(n, 510) for n in range(6, 31)]  # plain full days

    result = compute_payroll(days, LOW_BAR)

    assert result.nights == 2
    assert result.ot_minutes_raw == 360  # 6h pooled from the non-night days
    # Rule 5: 1.5h x 2 nights removed before pricing.
    assert result.ot_adjustment_minutes == 180
    assert result.ot_minutes_paid == 180

    assert result.ot_pay == Decimal("141.18")  # 3h x (400/8.5)
    assert result.night_pay == Decimal("800.00")

    # No leaves at all -> bonus granted outright.
    assert result.bonus_granted is True
    assert result.attendance_bonus == Decimal("800.00")  # 2 x daily rate

    assert result.grand_total == Decimal("13741.18")


def test_rule_five_adjustment_never_goes_negative():
    # One night, but only 1h of pooled OT to give back 1h30m from.
    days = [day(1, 780)] + [day(2, 570)] + [day(n, 510) for n in range(3, 31)]
    result = compute_payroll(days, LOW_BAR)

    assert result.nights == 1
    assert result.ot_minutes_raw == 60
    assert result.ot_minutes_paid == 0
    assert result.ot_pay == Decimal("0.00")


def test_standard_employee_gets_no_rule_five_adjustment():
    days = [day(1, 900)] + [day(2, 630)] + [day(n, 510) for n in range(3, 31)]
    result = compute_payroll(days, STANDARD)

    assert result.nights == 1  # OT 6h30m > 5h
    assert result.ot_adjustment_minutes == 0
    assert result.ot_minutes_paid == 120  # the 2h day survives untouched


# --- night threshold: "exceeds", not "at least" -----------------------------


@pytest.mark.parametrize(
    "worked, expect_night",
    [
        (510 + 300, False),  # OT exactly 5h00m -> not a night
        (510 + 301, True),  # OT 5h01m -> night
    ],
)
def test_night_threshold_is_strictly_greater(worked, expect_night):
    result = evaluate_day(day(1, worked), STANDARD)
    assert result.is_night is expect_night


@pytest.mark.parametrize(
    "worked, expect_night",
    [
        (510 + 240, False),  # OT exactly 4h -> not a night for the low bar
        (510 + 241, True),
    ],
)
def test_low_bar_night_threshold(worked, expect_night):
    result = evaluate_day(day(1, worked), LOW_BAR)
    assert result.is_night is expect_night


def test_night_day_contributes_nothing_to_the_ot_pool():
    result = evaluate_day(day(1, 510 + 400), STANDARD)
    assert result.is_night is True
    assert result.ot_minutes == 0  # excluded from the pool
    assert result.ot_minutes_raw == 400  # but still visible for the payslip


# --- the 30-minute OT discard -----------------------------------------------


@pytest.mark.parametrize(
    "worked, expected_ot",
    [
        (510 + 29, 0),  # under 30 min -> ignored entirely
        (510 + 30, 30),  # exactly 30 min -> counted
        (510 + 90, 90),
    ],
)
def test_sub_thirty_minute_overtime_is_discarded(worked, expected_ot):
    assert evaluate_day(day(1, worked), STANDARD).ot_minutes == expected_ot


def test_discarded_minutes_do_not_accumulate_across_days():
    # Ten days of 20 minutes each must stay worthless, not add up to 3h20m.
    days = [day(n, 530) for n in range(1, 11)] + [day(n, 510) for n in range(11, 31)]
    result = compute_payroll(days, STANDARD)
    assert result.ot_minutes_raw == 0
    assert result.ot_pay == Decimal("0.00")


# --- day classification boundaries ------------------------------------------


@pytest.mark.parametrize(
    "worked, expected",
    [
        (0, DayStatus.LEAVE),  # never turned up
        (209, DayStatus.LEAVE),  # 3h29m -> leave (owner's decision)
        (210, DayStatus.HALF),  # 3h30m
        (300, DayStatus.HALF),  # 5h00m
        (301, DayStatus.FULL),  # 5h01m -> full day, no deduction (owner's decision)
        (510, DayStatus.FULL),
        (900, DayStatus.FULL),
    ],
)
def test_worked_hour_bands(worked, expected):
    assert classify_status(day(1, worked)) is expected


def test_half_day_never_earns_overtime():
    result = evaluate_day(day(1, 300), STANDARD)
    assert result.status is DayStatus.HALF
    assert result.ot_minutes == 0
    assert result.is_night is False


def test_five_to_eight_thirty_is_a_normal_paid_day():
    days = [day(1, 360)] + [day(n, 510) for n in range(2, 31)]  # 6h day
    result = compute_payroll(days, STANDARD)
    assert result.days_full == 30
    assert result.leave_deduction == Decimal("0.00")
    assert result.halfday_deduction == Decimal("0.00")
    assert result.ot_pay == Decimal("0.00")


# --- week off (rule 8) ------------------------------------------------------


def test_week_off_is_paid_and_is_not_a_leave():
    days = [day(n, 510) for n in range(1, 28)]
    days += [day(n, 0, is_weekly_off=True) for n in range(28, 32)]
    result = compute_payroll(days, STANDARD)

    assert result.days_weekoff == 4
    assert result.days_leave == 0
    assert result.leave_deduction == Decimal("0.00")
    assert result.grand_total == Decimal("16000.00")  # salary + bonus, no cuts


def test_approved_paid_leave_is_not_deducted_and_does_not_block_the_bonus():
    days = [day(1, 0, is_paid_leave=True)] + [day(n, 510) for n in range(2, 31)]
    result = compute_payroll(days, STANDARD)

    assert result.days_paid_leave == 1
    assert result.days_leave == 0
    assert result.leave_deduction == Decimal("0.00")
    assert result.bonus_granted is True


# --- attendance bonus (rule 9) ----------------------------------------------


def test_bonus_granted_when_there_are_no_leaves():
    days = [day(n, 510) for n in range(1, 31)]
    result = compute_payroll(days, STANDARD)
    assert result.bonus_granted is True
    assert result.attendance_bonus == Decimal("1000.00")  # 2 days' salary


def test_bonus_granted_when_nights_exactly_offset_leaves_and_half_days():
    # 2 nights vs 1 leave + 1 half day -> granted at equality.
    days = [day(1, 900), day(2, 900)]  # OT 6h30m each -> nights
    days += [day(3, 0)]  # leave
    days += [day(4, 240)]  # half day
    days += [day(n, 510) for n in range(5, 31)]

    result = compute_payroll(days, STANDARD)
    assert result.nights == 2
    assert result.days_leave == 1
    assert result.days_half == 1
    assert result.bonus_granted is True
    assert result.attendance_bonus == Decimal("1000.00")


def test_bonus_refused_when_nights_fall_one_short():
    days = [day(1, 900)]  # 1 night
    days += [day(2, 0), day(3, 0)]  # 2 leaves
    days += [day(n, 510) for n in range(4, 31)]

    result = compute_payroll(days, STANDARD)
    assert result.nights == 1
    assert result.days_leave == 2
    assert result.bonus_granted is False
    assert result.attendance_bonus == Decimal("0.00")


def test_bonus_is_flat_not_proportional():
    """Two very different months both get exactly 2 days' salary."""
    clean = compute_payroll([day(n, 510) for n in range(1, 31)], STANDARD)
    offset = compute_payroll(
        [day(1, 900), day(2, 0)] + [day(n, 510) for n in range(3, 31)], STANDARD
    )
    assert clean.attendance_bonus == offset.attendance_bonus == Decimal("1000.00")


# --- admin overrides --------------------------------------------------------


def test_manual_status_override_wins():
    result = evaluate_day(day(1, 0, override_status=DayStatus.FULL), STANDARD)
    assert result.status is DayStatus.FULL


def test_manual_worked_minutes_override_drives_overtime():
    # Someone forgot to punch out; the admin corrects the day to 11 hours.
    result = evaluate_day(day(1, 0, override_worked_minutes=660), STANDARD)
    assert result.status is DayStatus.FULL
    assert result.ot_minutes == 150


# --- advances, adjustments and totals ---------------------------------------


def test_advances_are_deducted_from_the_grand_total():
    days = [day(n, 510) for n in range(1, 31)]
    result = compute_payroll(days, STANDARD, advances=Decimal("3000"))

    assert result.grand_total == Decimal("16000.00")
    assert result.advances_deducted == Decimal("3000.00")
    assert result.net_payable == Decimal("13000.00")
    assert result.flags == []


def test_negative_net_payable_is_flagged():
    days = [day(n, 510) for n in range(1, 31)]
    result = compute_payroll(days, STANDARD, advances=Decimal("20000"))

    assert result.net_payable == Decimal("-4000.00")
    assert result.flags and "negative" in result.flags[0]


def test_manual_adjustment_is_included_in_the_grand_total():
    days = [day(n, 510) for n in range(1, 31)]
    result = compute_payroll(days, STANDARD, adjustments=Decimal("-250"))
    assert result.grand_total == Decimal("15750.00")


def test_payslip_components_sum_exactly_to_the_grand_total():
    """A payslip has to add up when the owner checks it by hand."""
    result = compute_payroll(standard_month(), STANDARD)
    recomputed = (
        result.monthly_salary
        + result.ot_pay
        + result.night_pay
        + result.attendance_bonus
        + result.adjustments_total
        - result.leave_deduction
        - result.halfday_deduction
    )
    assert recomputed == result.grand_total


def test_zero_salary_employee_does_not_blow_up():
    policy = EmployeePolicy(monthly_salary=Decimal("0"))
    result = compute_payroll([day(n, 900) for n in range(1, 31)], policy)
    assert result.grand_total == Decimal("0.00")


def test_empty_day_list_is_degenerate_but_safe():
    """Guard on a degenerate input. Callers always pass every day of the month;
    with no days there are no leaves, so rule 9 grants the bonus. Documented
    here so the behaviour is deliberate rather than a surprise."""
    result = compute_payroll([], STANDARD)
    assert result.days == []
    assert result.bonus_granted is True
    assert result.grand_total == Decimal("16000.00")  # salary + 2 days' bonus


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    "minutes, expected",
    [(0, "0:00"), (510, "8:30"), (75, "1:15"), (-90, "-1:30")],
)
def test_minutes_to_hhmm(minutes, expected):
    assert minutes_to_hhmm(minutes) == expected
