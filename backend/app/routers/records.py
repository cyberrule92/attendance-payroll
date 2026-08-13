"""Leaves, advances and one-off payslip adjustments."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_admin
from ..db import get_db
from ..models import AdminUser, Adjustment, Advance, Employee, LeaveRecord
from ..schemas import (
    AdjustmentIn,
    AdjustmentOut,
    AdvanceIn,
    AdvanceOut,
    LeaveIn,
    LeaveOut,
)
from ..services.attendance import recompute_day
from ..services.payroll_run import month_bounds
from .admin import audit

router = APIRouter(prefix="/api", tags=["records"])

MAX_LEAVE_SPAN_DAYS = 366


def _employee_or_404(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return employee


# --- leaves -----------------------------------------------------------------


@router.get("/leaves", response_model=list[LeaveOut])
def list_leaves(
    year: int | None = None,
    month: int | None = None,
    employee_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    query = select(LeaveRecord).order_by(LeaveRecord.leave_date.desc())
    if year and month:
        start, end = month_bounds(year, month)
        query = query.where(
            LeaveRecord.leave_date >= start, LeaveRecord.leave_date <= end
        )
    if employee_id:
        query = query.where(LeaveRecord.employee_id == employee_id)

    rows = []
    for leave in db.scalars(query.limit(500)).all():
        item = LeaveOut.model_validate(leave)
        item.employee_name = leave.employee.name
        rows.append(item)
    return rows


@router.post("/leaves", status_code=201)
def create_leave(
    payload: LeaveIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Record a leave over a date range. One row per date, so a month grid and
    the payroll engine can both look it up by day."""
    employee = _employee_or_404(db, payload.employee_id)
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date is before start date.")
    if (payload.end_date - payload.start_date).days > MAX_LEAVE_SPAN_DAYS:
        raise HTTPException(status_code=400, detail="That leave range is too long.")

    created = 0
    current = payload.start_date
    while current <= payload.end_date:
        existing = db.scalar(
            select(LeaveRecord).where(
                LeaveRecord.employee_id == employee.id,
                LeaveRecord.leave_date == current,
            )
        )
        if existing is None:
            db.add(
                LeaveRecord(
                    employee_id=employee.id,
                    leave_date=current,
                    leave_type=payload.leave_type,
                    reason=payload.reason,
                )
            )
            created += 1
        else:
            existing.leave_type = payload.leave_type
            existing.reason = payload.reason
        current += timedelta(days=1)

    audit(
        db,
        admin.username,
        "create",
        "leave",
        employee.id,
        f"{employee.name}: {payload.leave_type.value} "
        f"{payload.start_date} to {payload.end_date}",
    )
    db.commit()

    current = payload.start_date
    while current <= payload.end_date:
        recompute_day(db, employee, current, commit=False)
        current += timedelta(days=1)
    db.commit()

    return {"ok": True, "created": created}


@router.delete("/leaves/{leave_id}")
def delete_leave(
    leave_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    leave = db.get(LeaveRecord, leave_id)
    if leave is None:
        raise HTTPException(status_code=404, detail="Leave not found.")
    employee, leave_date = leave.employee, leave.leave_date
    db.delete(leave)
    audit(
        db,
        admin.username,
        "delete",
        "leave",
        leave_id,
        f"{employee.name} {leave_date}",
    )
    db.commit()
    recompute_day(db, employee, leave_date)
    return {"ok": True}


# --- advances ---------------------------------------------------------------


@router.get("/advances", response_model=list[AdvanceOut])
def list_advances(
    year: int | None = None,
    month: int | None = None,
    employee_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    query = select(Advance).order_by(Advance.advance_date.desc())
    if year and month:
        start, end = month_bounds(year, month)
        query = query.where(Advance.advance_date >= start, Advance.advance_date <= end)
    if employee_id:
        query = query.where(Advance.employee_id == employee_id)

    rows = []
    for advance in db.scalars(query.limit(500)).all():
        item = AdvanceOut.model_validate(advance)
        item.employee_name = advance.employee.name
        rows.append(item)
    return rows


@router.get("/advances/summary")
def advances_summary(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Per-employee advance totals for a month -- what payroll will deduct."""
    start, end = month_bounds(year, month)
    rows = db.execute(
        select(
            Advance.employee_id,
            Employee.name,
            func.sum(Advance.amount),
            func.count(Advance.id),
        )
        .join(Employee, Employee.id == Advance.employee_id)
        .where(Advance.advance_date >= start, Advance.advance_date <= end)
        .group_by(Advance.employee_id, Employee.name)
        .order_by(Employee.name)
    ).all()

    return {
        "year": year,
        "month": month,
        "total": str(sum((Decimal(r[2]) for r in rows), Decimal("0.00"))),
        "rows": [
            {
                "employee_id": r[0],
                "employee_name": r[1],
                "total": str(Decimal(r[2])),
                "count": r[3],
            }
            for r in rows
        ],
    }


@router.post("/advances", response_model=AdvanceOut, status_code=201)
def create_advance(
    payload: AdvanceIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = _employee_or_404(db, payload.employee_id)
    advance = Advance(**payload.model_dump())
    db.add(advance)
    db.flush()
    audit(
        db,
        admin.username,
        "create",
        "advance",
        advance.id,
        f"{employee.name}: {payload.amount} on {payload.advance_date}",
    )
    db.commit()

    item = AdvanceOut.model_validate(advance)
    item.employee_name = employee.name
    return item


@router.delete("/advances/{advance_id}")
def delete_advance(
    advance_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    advance = db.get(Advance, advance_id)
    if advance is None:
        raise HTTPException(status_code=404, detail="Advance not found.")
    detail = f"{advance.employee.name}: {advance.amount} on {advance.advance_date}"
    db.delete(advance)
    audit(db, admin.username, "delete", "advance", advance_id, detail)
    db.commit()
    return {"ok": True}


# --- payslip adjustments ----------------------------------------------------


@router.get("/adjustments", response_model=list[AdjustmentOut])
def list_adjustments(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    rows = []
    for adj in db.scalars(
        select(Adjustment)
        .where(Adjustment.year == year, Adjustment.month == month)
        .order_by(Adjustment.id.desc())
    ).all():
        item = AdjustmentOut.model_validate(adj)
        item.employee_name = adj.employee.name
        rows.append(item)
    return rows


@router.post("/adjustments", response_model=AdjustmentOut, status_code=201)
def create_adjustment(
    payload: AdjustmentIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """A signed one-off line: positive adds to the payout, negative deducts."""
    employee = _employee_or_404(db, payload.employee_id)
    adj = Adjustment(**payload.model_dump())
    db.add(adj)
    db.flush()
    audit(
        db,
        admin.username,
        "create",
        "adjustment",
        adj.id,
        f"{employee.name} {payload.month:02d}/{payload.year}: "
        f"{payload.label} {payload.amount}",
    )
    db.commit()

    item = AdjustmentOut.model_validate(adj)
    item.employee_name = employee.name
    return item


@router.delete("/adjustments/{adjustment_id}")
def delete_adjustment(
    adjustment_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    adj = db.get(Adjustment, adjustment_id)
    if adj is None:
        raise HTTPException(status_code=404, detail="Adjustment not found.")
    detail = f"{adj.employee.name}: {adj.label} {adj.amount}"
    db.delete(adj)
    audit(db, admin.username, "delete", "adjustment", adjustment_id, detail)
    db.commit()
    return {"ok": True}
