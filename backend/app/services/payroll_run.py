"""Running payroll for a month against the database.

Keeps the pure rule engine (``services.payroll``) separate from persistence:
this module gathers inputs, calls the engine, and freezes the result.

Freezing matters. Once a month is finalised its numbers are copied into
``payroll_lines`` and never recomputed, so correcting a September punch cannot
change an August payslip that has already been handed out and paid.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Adjustment,
    Advance,
    Employee,
    Location,
    PayrollLine,
    PayrollRun,
    PayrollStatus,
)
from .attendance import (
    build_month_inputs,
    employee_policy,
    recompute_range,
    work_date_for,
)
from .payroll import PayrollResult, compute_payroll, minutes_to_hhmm, month_dates

ZERO = Decimal("0.00")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    dates = month_dates(year, month)
    return dates[0], dates[-1]


def advances_for_month(db: Session, employee_id: int, year: int, month: int) -> Decimal:
    start, end = month_bounds(year, month)
    total = db.scalar(
        select(func.coalesce(func.sum(Advance.amount), 0)).where(
            Advance.employee_id == employee_id,
            Advance.advance_date >= start,
            Advance.advance_date <= end,
        )
    )
    return Decimal(total or 0)


def adjustments_for_month(
    db: Session, employee_id: int, year: int, month: int
) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(Adjustment.amount), 0)).where(
            Adjustment.employee_id == employee_id,
            Adjustment.year == year,
            Adjustment.month == month,
        )
    )
    return Decimal(total or 0)


def employees_for_month(db: Session, year: int, month: int) -> list[Employee]:
    """Anyone who was on the payroll for at least one day of the month.

    Includes people who have since left, so their final month still gets paid.
    """
    start, end = month_bounds(year, month)
    rows = db.scalars(select(Employee).order_by(Employee.name)).all()
    return [
        e
        for e in rows
        if (e.joined_on is None or e.joined_on <= end)
        and (e.left_on is None or e.left_on >= start)
    ]


def compute_for_employee(
    db: Session, employee: Employee, year: int, month: int
) -> PayrollResult:
    dates = month_dates(year, month)
    result = compute_payroll(
        build_month_inputs(db, employee, dates),
        employee_policy(employee),
        advances=advances_for_month(db, employee.id, year, month),
        adjustments=adjustments_for_month(db, employee.id, year, month),
    )

    start, end = dates[0], dates[-1]

    today = work_date_for(datetime.now(timezone.utc))
    if end > today:
        remaining = (end - max(start, today)).days
        result.notes.append(
            f"{remaining} day(s) of this month have not happened yet, so these "
            "are running totals. The attendance bonus in particular can still "
            "change."
        )

    if employee.joined_on and start <= employee.joined_on <= end:
        result.flags.append(
            f"Joined on {employee.joined_on.isoformat()} -- full monthly salary "
            "was applied, not prorated. Add an adjustment if it should be."
        )
    if employee.left_on and start <= employee.left_on <= end:
        result.flags.append(
            f"Left on {employee.left_on.isoformat()} -- full monthly salary was "
            "applied, not prorated. Add an adjustment if it should be."
        )

    review_days = [
        d.work_date.isoformat()
        for d in result.days
        if d.status.name == "LEAVE" and d.worked_minutes == 0
    ]
    if len(review_days) > 10:
        result.flags.append(
            f"{len(review_days)} days with no punches at all -- check the kiosk "
            "at this location was working."
        )
    return result


def breakdown_payload(result: PayrollResult) -> list[dict]:
    """Day-by-day derivation, for the 'why is this number what it is' view."""
    return [
        {
            "date": d.work_date.isoformat(),
            "status": d.status.value,
            "worked": minutes_to_hhmm(d.worked_minutes),
            "worked_minutes": d.worked_minutes,
            "ot": minutes_to_hhmm(d.ot_minutes_raw),
            "ot_minutes": d.ot_minutes_raw,
            "counted_ot_minutes": d.ot_minutes,
            "is_night": d.is_night,
        }
        for d in result.days
    ]


def result_payload(employee: Employee, result: PayrollResult) -> dict:
    """Flat, JSON-friendly shape shared by the preview screen and the payslip."""
    return {
        "employee_id": employee.id,
        "employee_code": employee.code,
        "employee_name": employee.name,
        "location": employee.location.name if employee.location else "",
        "monthly_salary": str(result.monthly_salary),
        "daily_rate": str(result.daily_rate.quantize(Decimal("0.01"))),
        "hourly_rate": str(result.hourly_rate.quantize(Decimal("0.01"))),
        "days_full": result.days_full,
        "days_half": result.days_half,
        "days_leave": result.days_leave,
        "days_paid_leave": result.days_paid_leave,
        "days_weekoff": result.days_weekoff,
        "nights": result.nights,
        "ot_hours_raw": minutes_to_hhmm(result.ot_minutes_raw),
        "ot_hours_paid": minutes_to_hhmm(result.ot_minutes_paid),
        "ot_adjustment": minutes_to_hhmm(result.ot_adjustment_minutes),
        "ot_pay": str(result.ot_pay),
        "night_pay": str(result.night_pay),
        "attendance_bonus": str(result.attendance_bonus),
        "bonus_granted": result.bonus_granted,
        "bonus_reason": result.bonus_reason,
        "leave_deduction": str(result.leave_deduction),
        "halfday_deduction": str(result.halfday_deduction),
        "adjustments_total": str(result.adjustments_total),
        "grand_total": str(result.grand_total),
        "advances_deducted": str(result.advances_deducted),
        "net_payable": str(result.net_payable),
        "flags": result.flags,
        "notes": result.notes,
        "breakdown": breakdown_payload(result),
    }


def preview_month(db: Session, year: int, month: int, *, recompute: bool = True) -> dict:
    """Compute the whole month live, without saving anything."""
    employees = employees_for_month(db, year, month)
    if recompute:
        start, end = month_bounds(year, month)
        recompute_range(db, employees, start, end)

    rows = []
    for employee in employees:
        result = compute_for_employee(db, employee, year, month)
        rows.append(result_payload(employee, result))

    run = db.scalar(
        select(PayrollRun).where(PayrollRun.year == year, PayrollRun.month == month)
    )
    return {
        "year": year,
        "month": month,
        "status": run.status.value if run else "NONE",
        "finalized_at": run.finalized_at.isoformat() if run and run.finalized_at else None,
        "totals": _totals(rows),
        "rows": rows,
    }


def _totals(rows: list[dict]) -> dict:
    def total(key: str) -> str:
        return str(sum((Decimal(r[key]) for r in rows), ZERO))

    return {
        "headcount": len(rows),
        "salary": total("monthly_salary"),
        "ot_pay": total("ot_pay"),
        "night_pay": total("night_pay"),
        "attendance_bonus": total("attendance_bonus"),
        "leave_deduction": total("leave_deduction"),
        "halfday_deduction": total("halfday_deduction"),
        "grand_total": total("grand_total"),
        "advances_deducted": total("advances_deducted"),
        "net_payable": total("net_payable"),
    }


def finalize_month(db: Session, year: int, month: int, actor: str) -> PayrollRun:
    """Freeze the month. Refuses to silently overwrite an already-final run."""
    run = db.scalar(
        select(PayrollRun).where(PayrollRun.year == year, PayrollRun.month == month)
    )
    if run and run.status is PayrollStatus.FINAL:
        raise ValueError(
            f"Payroll for {month:02d}/{year} is already finalised. "
            "Reopen it first if you really need to change it."
        )

    employees = employees_for_month(db, year, month)
    start, end = month_bounds(year, month)
    recompute_range(db, employees, start, end)

    if run is None:
        run = PayrollRun(year=year, month=month)
        db.add(run)
        db.flush()

    # A re-finalise after a reopen replaces the old lines wholesale.
    for line in list(run.lines):
        db.delete(line)
    db.flush()

    for employee in employees:
        result = compute_for_employee(db, employee, year, month)
        db.add(
            PayrollLine(
                run_id=run.id,
                employee_id=employee.id,
                employee_code=employee.code,
                employee_name=employee.name,
                location_name=employee.location.name if employee.location else "",
                monthly_salary=result.monthly_salary,
                days_full=result.days_full,
                days_half=result.days_half,
                days_leave=result.days_leave,
                days_paid_leave=result.days_paid_leave,
                days_weekoff=result.days_weekoff,
                nights=result.nights,
                ot_minutes_raw=result.ot_minutes_raw,
                ot_minutes_paid=result.ot_minutes_paid,
                ot_pay=result.ot_pay,
                night_pay=result.night_pay,
                attendance_bonus=result.attendance_bonus,
                leave_deduction=result.leave_deduction,
                halfday_deduction=result.halfday_deduction,
                adjustments_total=result.adjustments_total,
                grand_total=result.grand_total,
                advances_deducted=result.advances_deducted,
                net_payable=result.net_payable,
                breakdown_json=json.dumps(
                    {
                        "flags": result.flags,
                        "notes": result.notes,
                        "bonus_reason": result.bonus_reason,
                        "days": breakdown_payload(result),
                    }
                ),
            )
        )

    run.status = PayrollStatus.FINAL
    run.finalized_at = datetime.now(timezone.utc).replace(tzinfo=None)
    run.note = f"Finalised by {actor}"
    db.commit()
    return run


def reopen_month(db: Session, year: int, month: int) -> PayrollRun:
    run = db.scalar(
        select(PayrollRun).where(PayrollRun.year == year, PayrollRun.month == month)
    )
    if run is None:
        raise ValueError(f"No payroll run exists for {month:02d}/{year}.")
    run.status = PayrollStatus.DRAFT
    run.finalized_at = None
    db.commit()
    return run


def frozen_month(db: Session, year: int, month: int) -> dict | None:
    """Read back a finalised month from its saved lines, not by recomputing."""
    run = db.scalar(
        select(PayrollRun).where(
            PayrollRun.year == year,
            PayrollRun.month == month,
            PayrollRun.status == PayrollStatus.FINAL,
        )
    )
    if run is None:
        return None

    rows = []
    for line in sorted(run.lines, key=lambda x: x.employee_name):
        saved = json.loads(line.breakdown_json or "{}")
        rows.append(
            {
                "employee_id": line.employee_id,
                "employee_code": line.employee_code,
                "employee_name": line.employee_name,
                "location": line.location_name,
                "monthly_salary": str(line.monthly_salary),
                "daily_rate": str(
                    (Decimal(line.monthly_salary) / 30).quantize(Decimal("0.01"))
                ),
                "days_full": line.days_full,
                "days_half": line.days_half,
                "days_leave": line.days_leave,
                "days_paid_leave": line.days_paid_leave,
                "days_weekoff": line.days_weekoff,
                "nights": line.nights,
                "ot_hours_raw": minutes_to_hhmm(line.ot_minutes_raw),
                "ot_hours_paid": minutes_to_hhmm(line.ot_minutes_paid),
                "ot_pay": str(line.ot_pay),
                "night_pay": str(line.night_pay),
                "attendance_bonus": str(line.attendance_bonus),
                "bonus_granted": line.attendance_bonus > 0,
                "bonus_reason": saved.get("bonus_reason", ""),
                "leave_deduction": str(line.leave_deduction),
                "halfday_deduction": str(line.halfday_deduction),
                "adjustments_total": str(line.adjustments_total),
                "grand_total": str(line.grand_total),
                "advances_deducted": str(line.advances_deducted),
                "net_payable": str(line.net_payable),
                "flags": saved.get("flags", []),
                "notes": saved.get("notes", []),
                "breakdown": saved.get("days", []),
            }
        )

    return {
        "year": year,
        "month": month,
        "status": run.status.value,
        "finalized_at": run.finalized_at.isoformat() if run.finalized_at else None,
        "totals": _totals(rows),
        "rows": rows,
    }
