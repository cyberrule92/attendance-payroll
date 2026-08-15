"""Employee records: the list, the salary, the PIN, the night-duty policy."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_admin, hash_secret
from ..config import settings
from ..db import get_db
from ..models import AdminUser, Employee, FaceEnrollment, Location
from ..schemas import (
    EmployeeIn,
    EmployeeOut,
    FaceEnrollmentOut,
    FaceStatusOut,
    PinResetRequest,
    PortalPinRequest,
)
from ..services import face, face_store
from .admin import audit

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _out(db: Session, employee: Employee) -> EmployeeOut:
    data = EmployeeOut.model_validate(employee)
    data.location_name = employee.location.name if employee.location else None
    data.face_enrolled = face_store.is_enrolled(db, employee.id)
    data.portal_ready = employee.portal_ready
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
    return [_out(db, e) for e in db.scalars(query).all()]


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
    return _out(db, employee)


@router.get("/{employee_id}", response_model=EmployeeOut)
def get_employee(
    employee_id: int,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return _out(db, employee)


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
    return _out(db, employee)


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


@router.post("/{employee_id}/portal-pin")
def set_portal_pin(
    employee_id: int,
    payload: PortalPinRequest,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Issue or reset an employee's 6-digit staff-portal PIN.

    Deliberately separate from the 4-digit kiosk PIN: that one is typed in the
    open on a shared tablet all day, and reusing it would mean anyone who
    watched somebody clock in could then read their pay.

    Setting a PIN clears any password the employee had chosen -- that is what
    makes this the reset path when somebody forgets theirs.
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    if not employee.phone:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{employee.name} has no phone number. The portal signs in by "
                "phone number, so add one first."
            ),
        )

    employee.portal_pin_hash = hash_secret(payload.pin)
    had_password = bool(employee.portal_password_hash)
    employee.portal_password_hash = None

    audit(
        db,
        admin.username,
        "portal_pin",
        "employee",
        employee.id,
        f"{employee.name}: portal PIN set" + (" (their password was cleared)" if had_password else ""),
    )
    db.commit()
    return {
        "ok": True,
        "message": (
            f"Portal PIN set for {employee.name}. They sign in at /me with their "
            f"phone number {employee.phone}."
        ),
    }


@router.delete("/{employee_id}/portal-pin")
def revoke_portal_access(
    employee_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Take portal access away. Takes effect on their next request, not their
    next login -- ``current_employee`` re-checks this every time."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")

    employee.portal_pin_hash = None
    employee.portal_password_hash = None
    audit(db, admin.username, "portal_revoke", "employee", employee.id, employee.name)
    db.commit()
    return {"ok": True, "message": f"Portal access removed for {employee.name}."}


# --- face enrolment ---------------------------------------------------------
#
# The reference faces the kiosk matches against. Enrolment is an admin action
# on purpose: whoever adds the photo is asserting "this is that person", and
# that assertion is the root of the whole anti-proxy check. Letting the kiosk
# self-enrol would mean the first person to reach the tablet could register
# their own face against somebody else's name.


def _enrollment_out(row) -> FaceEnrollmentOut:
    return FaceEnrollmentOut(
        id=row.id,
        employee_id=row.employee_id,
        photo_url=f"/api/photo/{row.photo_path}" if row.photo_path else None,
        added_by=row.added_by,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _face_status(db: Session, employee: Employee) -> FaceStatusOut:
    rows = face_store.enrollments_for(db, employee.id)
    return FaceStatusOut(
        employee_id=employee.id,
        employee_name=employee.name,
        enrolled=bool(rows),
        count=len(rows),
        max_allowed=settings.face_max_enrollments,
        enrollments=[_enrollment_out(r) for r in rows],
    )


@router.get("/{employee_id}/faces", response_model=FaceStatusOut)
def list_faces(
    employee_id: int,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return _face_status(db, employee)


@router.post("/{employee_id}/faces", response_model=FaceStatusOut)
def add_faces(
    employee_id: int,
    photos_in: list[UploadFile] = File(..., alias="photos"),
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Add one or more reference photos for an employee.

    Photos are accepted individually: a batch where two are good and one is
    blurred should enrol the two, not fail the lot. Whatever was rejected comes
    back in the message so the admin knows to retake it.
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    if not face.models_installed():
        raise HTTPException(
            status_code=503,
            detail=(
                "Face models are not installed on the server. "
                "Run scripts/fetch_face_models.py, then restart."
            ),
        )

    added, rejected = 0, []
    for upload in photos_in:
        raw = upload.file.read()
        ok, message, _row = face_store.enroll(db, employee, raw, admin.username)
        if ok:
            added += 1
        else:
            rejected.append(f"{upload.filename or 'photo'}: {message}")

    if added:
        audit(
            db,
            admin.username,
            "face_enroll",
            "employee",
            employee.id,
            f"{employee.name}: {added} reference photo(s) added",
        )
    db.commit()

    if not added:
        raise HTTPException(
            status_code=400,
            detail=" ".join(rejected) or "No usable photo was supplied.",
        )

    status = _face_status(db, employee)
    if rejected:
        # Surfaced through the normal payload so the caller sees the partial
        # success rather than an error that hides what did work.
        status.employee_name = f"{employee.name} ({len(rejected)} photo(s) rejected)"
    return status


@router.delete("/{employee_id}/faces/{enrollment_id}", response_model=FaceStatusOut)
def delete_face(
    employee_id: int,
    enrollment_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")

    # Ownership is checked before anything is deleted, so a mistyped employee
    # id cannot remove another person's reference face.
    existing = db.get(FaceEnrollment, enrollment_id)
    if existing is None or existing.employee_id != employee_id:
        raise HTTPException(status_code=404, detail="That face photo was not found.")
    face_store.remove(db, enrollment_id)

    audit(
        db,
        admin.username,
        "face_remove",
        "employee",
        employee.id,
        f"{employee.name}: reference photo {enrollment_id} removed",
    )
    db.commit()
    return _face_status(db, employee)


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
