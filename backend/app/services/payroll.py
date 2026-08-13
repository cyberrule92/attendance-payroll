"""The payroll rule engine.

A direct, testable implementation of the rule set in ``salary.txt``. Pure
functions over plain dataclasses -- no database, no HTTP, no globals -- because
this is the part that decides what people get paid and it has to be verifiable
line by line.

Rules implemented (section numbers match salary.txt):

1. Shift is 8h30m of *worked duration*, not a clock window.
2. daily_rate = salary / 30;  hourly_rate = salary / 30 / 8.5.
   Always /30, regardless of how many days the month actually has.
3. OT = worked - 8.5h when positive. A day contributing under 30 minutes of
   OT is ignored entirely. OT Pay = pooled OT hours x hourly_rate.
4. A day is a Night when that day's OT *exceeds* the employee's threshold
   (4h for Sita and Shama, 5h for everyone else). A Night day's OT is fully
   excluded from the pool -- only Night Pay applies, so the same hours are
   never paid three times. Night Pay = nights x daily_rate.
5. Employees on the lower 4h bar have a further 1.5h x nights removed from
   the pooled OT before it is priced. Clamped at zero.
6. 3h30m-5h00m worked = half day: 0.5 x daily_rate deducted, no OT.
7. A blank/absent day that is not a week off = leave: 1 x daily_rate deducted.
8. The weekly off (Thursday by default) is paid and is not a leave.
9. Attendance bonus is a flat 2 x daily_rate, granted when leaves == 0, or
   when nights >= (leaves + half days). All-or-nothing.
10. Grand Total = salary + OT pay + night pay + bonus
                  - leave deduction - half-day deduction.

Resolved gaps (not in salary.txt, decided with the owner):
  - Worked duration is first IN to last OUT; breaks are not subtracted.
  - Under 3h30m worked counts as a full leave.
  - Between 5h00m and 8h30m is a normal full day: no deduction, no OT.
  - Net Payable = Grand Total - advances taken in the same calendar month.
  - The pay period is the calendar month.
  - Working the weekly off: a week off is never deducted, but hours actually
    worked on one still earn OT and can qualify as a night. Without this a
    Thursday night shift would be worked for nothing.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from ..models import DayStatus

# --- fixed constants from the rule sheet ------------------------------------

SHIFT_MINUTES = 510  # 8h30m
SALARY_DIVISOR = Decimal("30")  # rule 2: always 30, never days-in-month
HALF_DAY_MIN_MINUTES = 210  # 3h30m
HALF_DAY_MAX_MINUTES = 300  # 5h00m
OT_MIN_MINUTES = 30  # rule 3: under 30 min of OT is discarded
ATTENDANCE_BONUS_DAYS = Decimal("2")

ZERO = Decimal("0.00")


def _money(value: Decimal) -> Decimal:
    """Round a money amount to paise, half-up (what a human does by hand)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class EmployeePolicy:
    """Everything about an employee that affects their pay."""

    monthly_salary: Decimal
    # Rule 4: OT strictly above this makes the day a Night.
    night_threshold_hours: float = 5.0
    # Rule 5: extra OT removed per night. 1.5 for the 4h-bar employees, else 0.
    ot_adjustment_per_night_hours: float = 0.0

    @property
    def night_threshold_minutes(self) -> int:
        return int(round(self.night_threshold_hours * 60))

    @property
    def ot_adjustment_minutes_per_night(self) -> int:
        return int(round(self.ot_adjustment_per_night_hours * 60))

    @property
    def daily_rate(self) -> Decimal:
        """salary / 30. Unrounded -- rounding happens on final amounts only."""
        return Decimal(self.monthly_salary) / SALARY_DIVISOR

    @property
    def hourly_rate(self) -> Decimal:
        """salary / 30 / 8.5. Unrounded."""
        return self.daily_rate / (Decimal(SHIFT_MINUTES) / Decimal(60))


@dataclass(frozen=True)
class DayInput:
    """One calendar day of raw attendance for one employee."""

    work_date: date
    # First IN to last OUT, in whole minutes. 0 when the employee never punched.
    worked_minutes: int = 0
    # True on the employee's weekly off (Thursday by default).
    is_weekly_off: bool = False
    # True when the admin approved a *paid* leave for this date.
    is_paid_leave: bool = False
    # True when the admin approved an unpaid leave (still deducted, but the
    # reason is on record rather than being an unexplained blank).
    is_unpaid_leave: bool = False
    # Admin override that wins over everything derived above.
    override_status: DayStatus | None = None
    # Admin-corrected worked minutes, replacing the punch-derived value.
    override_worked_minutes: int | None = None


# --- per-day evaluation -----------------------------------------------------


@dataclass(frozen=True)
class DayResult:
    work_date: date
    status: DayStatus
    worked_minutes: int
    ot_minutes: int  # after the 30-minute discard; 0 on night days' pool contribution
    ot_minutes_raw: int  # before the discard, before night exclusion
    is_night: bool
    counts_ot_to_pool: bool


def classify_status(day: DayInput) -> DayStatus:
    """Decide what kind of day this is (rules 6, 7, 8).

    Precedence: an explicit admin override, then the weekly off, then an
    approved paid leave, then the worked-hours bands.
    """
    if day.override_status is not None:
        return day.override_status

    worked = _effective_minutes(day)

    if day.is_weekly_off:
        # salary.txt is silent on someone actually working their week off.
        # Resolution: a week off is never a deduction, but real hours worked on
        # one still earn OT and can qualify as a night -- otherwise a Thursday
        # night shift would be worked for free. Anything at or below the
        # half-day band stays a plain paid week off (no OT is possible there
        # anyway, since it is far short of the 8h30m shift).
        if worked > HALF_DAY_MAX_MINUTES:
            return DayStatus.FULL
        return DayStatus.WEEKOFF

    if day.is_paid_leave:
        return DayStatus.PAID_LEAVE

    if worked < HALF_DAY_MIN_MINUTES:
        # Covers both "never turned up" and "left before 3h30m". Rule 7 plus
        # the owner's decision on the sub-3h30m gap.
        return DayStatus.LEAVE
    if worked <= HALF_DAY_MAX_MINUTES:
        return DayStatus.HALF
    return DayStatus.FULL


def _effective_minutes(day: DayInput) -> int:
    if day.override_worked_minutes is not None:
        return max(0, day.override_worked_minutes)
    return max(0, day.worked_minutes)


def evaluate_day(day: DayInput, policy: EmployeePolicy) -> DayResult:
    """Classify one day and work out its OT and night status."""
    status = classify_status(day)
    worked = _effective_minutes(day)

    # Rule 6: no OT is calculated on half days. Nor on leave/week-off days,
    # which by definition have no qualifying worked time.
    if status is not DayStatus.FULL:
        return DayResult(
            work_date=day.work_date,
            status=status,
            worked_minutes=worked,
            ot_minutes=0,
            ot_minutes_raw=0,
            is_night=False,
            counts_ot_to_pool=False,
        )

    ot_raw = max(0, worked - SHIFT_MINUTES)

    # Rule 4: strictly greater than the threshold. Exactly 5h00m is not a night.
    is_night = ot_raw > policy.night_threshold_minutes

    if is_night:
        # A night day's OT is excluded from the pool entirely -- it is paid via
        # night pay instead, so the hours are not counted twice.
        return DayResult(
            work_date=day.work_date,
            status=status,
            worked_minutes=worked,
            ot_minutes=0,
            ot_minutes_raw=ot_raw,
            is_night=True,
            counts_ot_to_pool=False,
        )

    # Rule 3: a day contributing less than 30 minutes of OT is ignored.
    ot_counted = ot_raw if ot_raw >= OT_MIN_MINUTES else 0
    return DayResult(
        work_date=day.work_date,
        status=status,
        worked_minutes=worked,
        ot_minutes=ot_counted,
        ot_minutes_raw=ot_raw,
        is_night=False,
        counts_ot_to_pool=ot_counted > 0,
    )


# --- month evaluation -------------------------------------------------------


@dataclass
class PayrollResult:
    """The full computation for one employee for one month."""

    monthly_salary: Decimal
    daily_rate: Decimal
    hourly_rate: Decimal

    days_full: int = 0
    days_half: int = 0
    days_leave: int = 0
    days_paid_leave: int = 0
    days_weekoff: int = 0
    nights: int = 0

    ot_minutes_raw: int = 0  # pooled OT before the rule-5 adjustment
    ot_minutes_paid: int = 0  # pooled OT actually priced
    ot_adjustment_minutes: int = 0  # rule 5 deduction that was applied

    ot_pay: Decimal = ZERO
    night_pay: Decimal = ZERO
    attendance_bonus: Decimal = ZERO
    leave_deduction: Decimal = ZERO
    halfday_deduction: Decimal = ZERO
    adjustments_total: Decimal = ZERO

    grand_total: Decimal = ZERO
    advances_deducted: Decimal = ZERO
    net_payable: Decimal = ZERO

    bonus_granted: bool = False
    bonus_reason: str = ""
    days: list[DayResult] = field(default_factory=list)
    # Things the owner should look at before paying.
    flags: list[str] = field(default_factory=list)
    # Context that is useful but needs no action. Kept apart from `flags` so
    # every row does not end up wearing a warning badge for something routine.
    notes: list[str] = field(default_factory=list)

    @property
    def ot_hours_paid(self) -> Decimal:
        return (Decimal(self.ot_minutes_paid) / 60).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


def compute_payroll(
    days: list[DayInput],
    policy: EmployeePolicy,
    *,
    advances: Decimal = ZERO,
    adjustments: Decimal = ZERO,
) -> PayrollResult:
    """Run the whole rule set for one employee over one month.

    ``days`` should contain one entry per calendar day the employee was on the
    payroll. Days before joining or after leaving must simply be omitted.
    ``advances`` is the total taken in the month; ``adjustments`` is the signed
    sum of any manual payslip lines.
    """
    salary = Decimal(policy.monthly_salary)
    result = PayrollResult(
        monthly_salary=salary,
        daily_rate=policy.daily_rate,
        hourly_rate=policy.hourly_rate,
    )

    pool_minutes = 0
    for day in days:
        evaluated = evaluate_day(day, policy)
        result.days.append(evaluated)

        if evaluated.status is DayStatus.FULL:
            result.days_full += 1
        elif evaluated.status is DayStatus.HALF:
            result.days_half += 1
        elif evaluated.status is DayStatus.LEAVE:
            result.days_leave += 1
        elif evaluated.status is DayStatus.PAID_LEAVE:
            result.days_paid_leave += 1
        else:  # WEEKOFF / HOLIDAY -- rule 8: paid, not a leave
            result.days_weekoff += 1

        if evaluated.is_night:
            result.nights += 1
        pool_minutes += evaluated.ot_minutes

    result.ot_minutes_raw = pool_minutes

    # Rule 5: the lower-bar employees give back 1.5h per night from the pool.
    adjustment = policy.ot_adjustment_minutes_per_night * result.nights
    result.ot_adjustment_minutes = min(adjustment, pool_minutes)
    payable_minutes = max(0, pool_minutes - adjustment)
    result.ot_minutes_paid = payable_minutes

    # --- money ----------------------------------------------------------
    # Each component is rounded to paise, and the total is the sum of the
    # rounded components, so a payslip always adds up on paper.
    result.ot_pay = _money(
        (Decimal(payable_minutes) / Decimal(60)) * policy.hourly_rate
    )
    result.night_pay = _money(Decimal(result.nights) * policy.daily_rate)
    result.leave_deduction = _money(Decimal(result.days_leave) * policy.daily_rate)
    result.halfday_deduction = _money(
        Decimal(result.days_half) * policy.daily_rate * Decimal("0.5")
    )

    granted, reason = _bonus_decision(result)
    result.bonus_granted = granted
    result.bonus_reason = reason
    result.attendance_bonus = (
        _money(ATTENDANCE_BONUS_DAYS * policy.daily_rate) if granted else ZERO
    )

    result.adjustments_total = _money(Decimal(adjustments))
    result.advances_deducted = _money(Decimal(advances))

    result.grand_total = _money(
        _money(salary)
        + result.ot_pay
        + result.night_pay
        + result.attendance_bonus
        + result.adjustments_total
        - result.leave_deduction
        - result.halfday_deduction
    )
    result.net_payable = _money(result.grand_total - result.advances_deducted)

    if result.net_payable < 0:
        result.flags.append(
            "Net payable is negative -- advances exceed this month's earnings."
        )

    return result


def _bonus_decision(result: PayrollResult) -> tuple[bool, str]:
    """Rule 9. Flat 2 days' salary, all-or-nothing."""
    if result.days_leave == 0:
        return True, "No leaves taken this month."
    offset = result.days_leave + result.days_half
    if result.nights >= offset:
        return True, (
            f"{result.nights} night(s) offset {result.days_leave} leave(s) "
            f"+ {result.days_half} half day(s)."
        )
    return False, (
        f"{result.nights} night(s) do not cover {result.days_leave} leave(s) "
        f"+ {result.days_half} half day(s)."
    )


# --- helpers ----------------------------------------------------------------


def month_dates(year: int, month: int) -> list[date]:
    """Every calendar date in the pay period (rule: calendar month)."""
    _, last = calendar.monthrange(year, month)
    return [date(year, month, day) for day in range(1, last + 1)]


def minutes_to_hhmm(minutes: int) -> str:
    """Format a duration the way the owner reads it: 8:30, not 8.5."""
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"
