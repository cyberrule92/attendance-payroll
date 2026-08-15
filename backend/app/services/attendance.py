"""Turning raw punches into daily attendance records.

Two things here are load-bearing:

*Work dates, not calendar dates.* A night shift starting at 20:00 and ending
at 03:00 is one day's work. Grouping punches by calendar date would tear it in
half and produce a day with no punch-out followed by a day with no punch-in --
and, worse, an OT figure of zero on a night the employee is owed night pay for.
So every punch is credited to the work date given by ``settings.day_cutover_hour``
(05:00 local by default): anything before 05:00 belongs to the previous day.

*Derived, but not destructive.* Daily rows are recomputed from punches whenever
punches change, but the admin's manual corrections live in separate ``manual_*``
columns and are re-applied after every recompute, so a correction is never
silently overwritten.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AttendanceDay,
    DayStatus,
    Employee,
    LeaveRecord,
    LeaveType,
    Punch,
    PunchDirection,
)
from .payroll import DayInput, EmployeePolicy, evaluate_day

# A span longer than this almost certainly means someone forgot to punch out.
SUSPICIOUS_SPAN_MINUTES = 16 * 60


# --- time helpers -----------------------------------------------------------


def to_utc(value: datetime) -> datetime:
    """Normalise to an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_local(value: datetime) -> datetime:
    """Render a stored (naive UTC) timestamp in the shop's local timezone."""
    return to_utc(value).astimezone(settings.tz)


def work_date_for(captured_at: datetime) -> date:
    """Which work day a punch belongs to.

    Shifts the local clock back by the cutover hour, so 02:30 on the 14th is
    credited to the 13th's shift.
    """
    local = to_local(captured_at)
    return (local - timedelta(hours=settings.day_cutover_hour)).date()


def store_dt(value: datetime) -> datetime:
    """SQLite columns hold naive UTC; strip the tzinfo on the way in."""
    return to_utc(value).replace(tzinfo=None)


def is_weekly_off(employee: Employee, work_date: date) -> bool:
    if employee.weekly_off_dow is None:
        return False
    return work_date.weekday() == employee.weekly_off_dow


def employee_policy(employee: Employee) -> EmployeePolicy:
    return EmployeePolicy(
        monthly_salary=employee.monthly_salary,
        night_threshold_hours=employee.night_threshold_hours,
        ot_adjustment_per_night_hours=employee.ot_adjustment_per_night_hours,
    )


# --- deriving one day -------------------------------------------------------


class DerivedDay:
    """The punch-derived facts for one employee-day, before overrides."""

    __slots__ = (
        "first_in",
        "last_out",
        "worked_minutes",
        "punch_count",
        "needs_review",
        "review_reason",
    )

    def __init__(self) -> None:
        self.first_in: datetime | None = None
        self.last_out: datetime | None = None
        self.worked_minutes: int = 0
        self.punch_count: int = 0
        self.needs_review: bool = False
        self.review_reason: str | None = None


def derive_from_punches(punches: Sequence[Punch]) -> DerivedDay:
    """First IN to last OUT, with the ways it can go wrong flagged.

    Breaks are deliberately not subtracted -- that is the owner's chosen
    definition of worked duration.
    """
    derived = _derive_times(punches)

    # A punch the face check could not confirm belongs in the same review queue
    # as a missed punch out -- it is another day the system could not work out
    # on its own. A timing problem is reported in preference to it, because that
    # is the one that changes the hours; the face concern is still visible on
    # the punch itself and on the Today board.
    suspect = [p for p in punches if not p.is_voided and p.face_suspect]
    if suspect and not derived.needs_review:
        reasons = {p.face_status.value for p in suspect}
        derived.needs_review = True
        derived.review_reason = (
            "Face not confirmed on "
            f"{len(suspect)} punch{'es' if len(suspect) > 1 else ''} "
            f"({', '.join(sorted(reasons)).lower().replace('_', ' ')})"
            " - check the photo"
        )
    return derived


def _derive_times(punches: Sequence[Punch]) -> DerivedDay:
    """The punch-timing half of the derivation."""
    derived = DerivedDay()
    live = [p for p in punches if not p.is_voided]
    derived.punch_count = len(live)
    if not live:
        return derived

    live = sorted(live, key=lambda p: p.captured_at)
    ins = [p for p in live if p.direction is PunchDirection.IN]
    outs = [p for p in live if p.direction is PunchDirection.OUT]

    derived.first_in = ins[0].captured_at if ins else None
    derived.last_out = outs[-1].captured_at if outs else None

    if not ins:
        derived.needs_review = True
        derived.review_reason = "Punched out without a punch in"
        return derived
    if not outs:
        derived.needs_review = True
        derived.review_reason = "Punched in but never punched out"
        return derived

    span = to_utc(derived.last_out) - to_utc(derived.first_in)
    minutes = int(span.total_seconds() // 60)
    if minutes < 0:
        derived.needs_review = True
        derived.review_reason = "Punch out is earlier than punch in"
        return derived

    derived.worked_minutes = minutes
    if minutes > SUSPICIOUS_SPAN_MINUTES:
        derived.needs_review = True
        derived.review_reason = (
            f"Span of {minutes // 60}h{minutes % 60:02d}m looks too long "
            "- check for a missed punch out"
        )
    return derived


def _leave_for(
    db: Session, employee_id: int, work_date: date
) -> LeaveRecord | None:
    return db.scalar(
        select(LeaveRecord).where(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_date == work_date,
        )
    )


def recompute_day(
    db: Session,
    employee: Employee,
    work_date: date,
    *,
    commit: bool = True,
) -> AttendanceDay:
    """Rebuild one employee-day from its punches, preserving admin overrides."""
    punches = list(
        db.scalars(
            select(Punch).where(
                Punch.employee_id == employee.id,
                Punch.work_date == work_date,
            )
        )
    )
    derived = derive_from_punches(punches)

    row = db.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee.id,
            AttendanceDay.work_date == work_date,
        )
    )
    if row is None:
        row = AttendanceDay(employee_id=employee.id, work_date=work_date)
        db.add(row)

    row.first_in = derived.first_in
    row.last_out = derived.last_out
    row.worked_minutes = derived.worked_minutes
    row.punch_count = derived.punch_count
    row.needs_review = derived.needs_review
    row.review_reason = derived.review_reason

    leave = _leave_for(db, employee.id, work_date)
    day_input = DayInput(
        work_date=work_date,
        worked_minutes=derived.worked_minutes,
        is_weekly_off=is_weekly_off(employee, work_date),
        is_paid_leave=leave is not None and leave.leave_type is LeaveType.PAID,
        is_unpaid_leave=leave is not None and leave.leave_type is LeaveType.UNPAID,
        override_status=row.manual_status,
        override_worked_minutes=row.manual_worked_minutes,
    )
    evaluated = evaluate_day(day_input, employee_policy(employee))

    row.status = evaluated.status
    row.ot_minutes = evaluated.ot_minutes_raw
    row.is_night = evaluated.is_night
    row.computed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if commit:
        db.commit()
    return row


def recompute_range(
    db: Session,
    employees: Iterable[Employee],
    start: date,
    end: date,
) -> int:
    """Rebuild every employee-day in an inclusive date range."""
    count = 0
    for employee in employees:
        current = start
        while current <= end:
            if employee.employed_on(current):
                recompute_day(db, employee, current, commit=False)
                count += 1
            current += timedelta(days=1)
    db.commit()
    return count


# --- assembling a month for the payroll engine ------------------------------


def build_month_inputs(
    db: Session,
    employee: Employee,
    dates: Sequence[date],
) -> list[DayInput]:
    """Assemble the DayInput list the payroll engine expects.

    Two kinds of day are omitted rather than counted as absences:

    * Days outside the employee's service period, so a mid-month joiner is not
      charged leave for days before they were hired.
    * Days that have not happened yet. Without this, opening the current month
      on the 13th would show everyone with eighteen leaves and a gutted salary,
      because the rest of the month has no punches in it yet.
    """
    today = work_date_for(datetime.now(timezone.utc))
    rows = {
        row.work_date: row
        for row in db.scalars(
            select(AttendanceDay).where(
                AttendanceDay.employee_id == employee.id,
                AttendanceDay.work_date >= dates[0],
                AttendanceDay.work_date <= dates[-1],
            )
        )
    }
    leaves = {
        leave.leave_date: leave
        for leave in db.scalars(
            select(LeaveRecord).where(
                LeaveRecord.employee_id == employee.id,
                LeaveRecord.leave_date >= dates[0],
                LeaveRecord.leave_date <= dates[-1],
            )
        )
    }

    inputs: list[DayInput] = []
    for work_date in dates:
        if not employee.employed_on(work_date):
            continue
        if work_date > today:
            continue
        row = rows.get(work_date)
        leave = leaves.get(work_date)
        inputs.append(
            DayInput(
                work_date=work_date,
                worked_minutes=row.worked_minutes if row else 0,
                is_weekly_off=is_weekly_off(employee, work_date),
                is_paid_leave=leave is not None and leave.leave_type is LeaveType.PAID,
                is_unpaid_leave=(
                    leave is not None and leave.leave_type is LeaveType.UNPAID
                ),
                override_status=row.manual_status if row else None,
                override_worked_minutes=row.manual_worked_minutes if row else None,
            )
        )
    return inputs


def next_direction(db: Session, employee_id: int, work_date: date) -> PunchDirection:
    """What the kiosk should record next: the opposite of the last punch."""
    last = db.scalar(
        select(Punch)
        .where(
            and_(
                Punch.employee_id == employee_id,
                Punch.work_date == work_date,
                Punch.is_voided == False,  # noqa: E712  (SQL NOT, not Python not)
            )
        )
        .order_by(Punch.captured_at.desc())
        .limit(1)
    )
    if last is None or last.direction is PunchDirection.OUT:
        return PunchDirection.IN
    return PunchDirection.OUT


def day_summary(row: AttendanceDay | None) -> dict:
    """Compact representation for the kiosk confirmation screen and admin grid."""
    if row is None:
        return {
            "status": DayStatus.LEAVE.value,
            "first_in": None,
            "last_out": None,
            "worked_minutes": 0,
            "ot_minutes": 0,
            "is_night": False,
        }
    return {
        "status": row.status.value,
        "first_in": to_local(row.first_in).isoformat() if row.first_in else None,
        "last_out": to_local(row.last_out).isoformat() if row.last_out else None,
        "worked_minutes": (
            row.manual_worked_minutes
            if row.manual_worked_minutes is not None
            else row.worked_minutes
        ),
        "ot_minutes": row.ot_minutes,
        "is_night": row.is_night,
        "needs_review": row.needs_review,
        "review_reason": row.review_reason,
    }
