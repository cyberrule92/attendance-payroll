"""Employee records: the list, the salary, the PIN, the night-duty policy."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_admin, hash_secret
from ..db import get_db
from ..models import AdminUser, Employee, Location
from ..schemas import EmployeeIn, EmployeeOut, PinResetRequest
from .admin import audit

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _out(employee: Employee) -> EmployeeOut:
    data = EmployeeOut.model_validate(employee)
    data.location_name = employee.location.name if employee.location else None
    return data


@router.get("", response_model=list[EmployeeOut])
def list_employees(
    include_inactive: bool = False,
    location_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    query = select(Employee).order_by(Employee.name)
    if not include_inactive:
        query = query.where(Employee.is_active == True)  # noqa: E712
    if location_id is not None:
        query = query.where(Employee.location_id == location_id)
    return [_out(e) for e in db.scalars(query).all()]


@router.post("", response_model=EmployeeOut, status_code=201)
def create_employee(
    payload: EmployeeIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    if payload.pin is None:
        raise HTTPException(
            status_code=400, detail="Set a 4-digit PIN for the new employee."
        )
    if db.scalar(select(Employee).where(Employee.code == payload.code)):
        raise HTTPException(status_code=409, detail="That employee code is taken.")
    if payload.phone and db.scalar(
        select(Employee).where(Employee.phone == payload.phone)
    ):
        raise HTTPException(status_code=409, detail="That phone number is taken.")
    if db.get(Location, payload.location_id) is None:
        raise HTTPException(status_code=404, detail="Location not found.")

    data = payload.model_dump(exclude={"pin"})
    employee = Employee(**data, pin_hash=hash_secret(payload.pin))
    db.add(employee)
    db.flush()
    audit(db, admin.username, "create", "employee", employee.id, employee.name)
    db.commit()
    return _out(employee)


@router.get("/{employee_id}", response_model=EmployeeOut)
def get_employee(
    employee_id: int,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return _out(employee)


@router.put("/{employee_id}", response_model=EmployeeOut)
def update_employee(
    employee_id: int,
    payload: EmployeeIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")

    clash = db.scalar(
        select(Employee).where(Employee.code == payload.code, Employee.id != employee_id)
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="That employee code is taken.")

    changes = []
    if employee.monthly_salary != payload.monthly_salary:
        changes.append(f"salary {employee.monthly_salary} -> {payload.monthly_salary}")
    if employee.night_threshold_hours != payload.night_threshold_hours:
        changes.append(
            f"night bar {employee.night_threshold_hours}h -> "
            f"{payload.night_threshold_hours}h"
        )

    for key, value in payload.model_dump(exclude={"pin"}).items():
        setattr(employee, key, value)
    if payload.pin:
        employee.pin_hash = hash_secret(payload.pin)
        changes.append("PIN reset")

    audit(
        db,
        admin.username,
        "update",
        "employee",
        employee.id,
        f"{employee.name}: {'; '.join(changes) if changes else 'details edited'}",
    )
    db.commit()
    return _out(employee)


@router.post("/{employee_id}/pin")
def reset_pin(
    employee_id: int,
    payload: PinResetRequest,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    employee.pin_hash = hash_secret(payload.pin)
    audit(db, admin.username, "pin_reset", "employee", employee.id, employee.name)
    db.commit()
    return {"ok": True, "message": f"PIN updated for {employee.name}."}


@router.delete("/{employee_id}")
def deactivate_employee(
    employee_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Deactivate rather than delete -- their attendance history must survive."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    employee.is_active = False
    audit(db, admin.username, "deactivate", "employee", employee.id, employee.name)
    db.commit()
    return {"ok": True, "message": f"{employee.name} marked inactive."}
