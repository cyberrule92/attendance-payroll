"""Endpoints the shared kiosk tablet at each location talks to.

The kiosk is trusted to tell us *when* a punch happened, because punches queued
while the laptop was offline are uploaded later and their original time is the
only correct one. It is not trusted about anything else: the location comes
from the paired device, the identity comes from the PIN, and the photo is
re-encoded server-side.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    codes_match,
    current_device,
    issue_device_token,
    validate_pin,
    verify_employee_pin,
)
from ..db import get_db
from ..models import Device, Employee, Punch, PunchDirection, PunchSource
from ..schemas import KioskEmployee, PairRequest, PairResponse, PunchResponse
from ..services import photos
from ..services.attendance import (
    next_direction,
    recompute_day,
    store_dt,
    to_local,
    work_date_for,
)
from ..services.payroll import minutes_to_hhmm

router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])

# A punch timestamped further ahead than this is the device clock being wrong,
# not a real punch.
MAX_CLOCK_SKEW = timedelta(hours=2)
# Offline queues should drain in hours, not months. Anything older is suspect.
MAX_BACKDATE = timedelta(days=30)


@router.post("/pair", response_model=PairResponse)
def pair_device(payload: PairRequest, db: Session = Depends(get_db)) -> PairResponse:
    """Exchange a one-time pairing code for a long-lived device token."""
    device = db.scalar(
        select(Device).where(Device.pairing_code == payload.code.strip())
    )
    if device is None or not codes_match(payload.code, device.pairing_code):
        raise HTTPException(status_code=404, detail="That pairing code is not valid.")
    if not device.is_active:
        raise HTTPException(status_code=403, detail="This device has been disabled.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if device.pairing_expires_at and device.pairing_expires_at < now:
        raise HTTPException(
            status_code=410,
            detail="That pairing code has expired. Generate a new one.",
        )

    token = issue_device_token(device)
    device.pairing_code = None
    device.pairing_expires_at = None
    device.paired_at = now
    device.last_seen_at = now
    db.commit()

    return PairResponse(
        device_token=token,
        device_name=device.name,
        location_id=device.location_id,
        location_name=device.location.name,
    )


@router.get("/employees", response_model=list[KioskEmployee])
def kiosk_employees(
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> list[KioskEmployee]:
    """Who this kiosk should show, and whether each is due to punch in or out."""
    now = datetime.now(timezone.utc)
    work_date = work_date_for(now)

    device.last_seen_at = now.replace(tzinfo=None)
    db.commit()

    employees = db.scalars(
        select(Employee)
        .where(
            Employee.location_id == device.location_id,
            Employee.is_active == True,  # noqa: E712
        )
        .order_by(Employee.name)
    ).all()

    out: list[KioskEmployee] = []
    for employee in employees:
        if not employee.employed_on(work_date):
            continue
        punches = db.scalars(
            select(Punch)
            .where(
                Punch.employee_id == employee.id,
                Punch.work_date == work_date,
                Punch.is_voided == False,  # noqa: E712
            )
            .order_by(Punch.captured_at)
        ).all()
        ins = [p for p in punches if p.direction is PunchDirection.IN]
        outs = [p for p in punches if p.direction is PunchDirection.OUT]
        out.append(
            KioskEmployee(
                id=employee.id,
                code=employee.code,
                name=employee.name,
                next_direction=next_direction(db, employee.id, work_date).value,
                first_in=to_local(ins[0].captured_at).strftime("%H:%M") if ins else None,
                last_out=to_local(outs[-1].captured_at).strftime("%H:%M") if outs else None,
            )
        )
    return out


def _parse_captured_at(raw: str | None) -> datetime:
    """Accept the kiosk's own timestamp, within sane bounds."""
    now = datetime.now(timezone.utc)
    if not raw:
        return now
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Unreadable punch timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)

    if parsed > now + MAX_CLOCK_SKEW:
        raise HTTPException(
            status_code=400,
            detail="This device's clock is ahead. Fix the date and time settings.",
        )
    if parsed < now - MAX_BACKDATE:
        raise HTTPException(
            status_code=400,
            detail="This punch is too old to accept. Ask the manager to add it manually.",
        )
    return parsed


@router.post("/punch", response_model=PunchResponse)
def record_punch(
    employee_id: int = Form(...),
    pin: str = Form(...),
    client_uuid: str = Form(...),
    captured_at: str | None = Form(default=None),
    photo: UploadFile | None = File(default=None),
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> PunchResponse:
    """Record one punch. Safe to retry -- ``client_uuid`` makes it idempotent."""
    client_uuid = (client_uuid or "").strip()
    if not client_uuid:
        raise HTTPException(status_code=400, detail="Missing punch id.")

    # An offline queue retrying after a flaky upload must not create a second
    # punch, so an already-seen id is a success, not an error.
    existing = db.scalar(select(Punch).where(Punch.client_uuid == client_uuid))
    if existing is not None:
        return _punch_response(db, existing, duplicate=True)

    validate_pin(pin)
    employee = verify_employee_pin(db, employee_id, pin, location_id=device.location_id)

    when = _parse_captured_at(captured_at)
    work_date = work_date_for(when)
    if not employee.employed_on(work_date):
        raise HTTPException(
            status_code=400,
            detail=f"{employee.name} is not on the payroll for {work_date.isoformat()}.",
        )

    direction = next_direction(db, employee.id, work_date)

    photo_path = None
    if photo is not None:
        photo_path = photos.save_punch_photo(photo.file.read(), work_date, client_uuid)

    punch = Punch(
        employee_id=employee.id,
        location_id=device.location_id,
        device_id=device.id,
        direction=direction,
        captured_at=store_dt(when),
        received_at=datetime.now(timezone.utc).replace(tzinfo=None),
        work_date=work_date,
        photo_path=photo_path,
        client_uuid=client_uuid,
        source=PunchSource.KIOSK,
    )
    db.add(punch)
    device.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    recompute_day(db, employee, work_date)
    return _punch_response(db, punch, duplicate=False)


def _punch_response(db: Session, punch: Punch, *, duplicate: bool) -> PunchResponse:
    employee = punch.employee
    row = recompute_day(db, employee, punch.work_date)

    local_time = to_local(punch.captured_at)
    worked = (
        row.manual_worked_minutes
        if row.manual_worked_minutes is not None
        else row.worked_minutes
    )

    if duplicate:
        message = f"Already recorded at {local_time.strftime('%I:%M %p').lstrip('0')}."
    elif punch.direction is PunchDirection.IN:
        message = f"Good morning {employee.name}. Checked in."
    else:
        message = f"Goodbye {employee.name}. Checked out."

    return PunchResponse(
        accepted=True,
        duplicate=duplicate,
        employee_name=employee.name,
        direction=punch.direction.value,
        punched_at=local_time.strftime("%I:%M %p").lstrip("0"),
        work_date=punch.work_date.isoformat(),
        first_in=to_local(row.first_in).strftime("%I:%M %p").lstrip("0") if row.first_in else None,
        last_out=to_local(row.last_out).strftime("%I:%M %p").lstrip("0") if row.last_out else None,
        worked_label=minutes_to_hhmm(worked) if worked else None,
        message=message,
    )
