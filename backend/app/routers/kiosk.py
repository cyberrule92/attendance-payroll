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
from ..config import settings
from ..db import get_db
from ..models import Device, Employee, FaceCheck, Punch, PunchDirection, PunchSource
from ..schemas import KioskEmployee, PairRequest, PairResponse, PunchResponse
from ..services import face_store, photos
from ..services.attendance import (
    next_direction,
    recompute_day,
    store_dt,
    to_local,
    work_date_for,
)
from ..services.face import VerificationResult as FaceVerification
from ..services.payroll import minutes_to_hhmm

router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])

# A punch timestamped further ahead than this is the device clock being wrong,
# not a real punch.
MAX_CLOCK_SKEW = timedelta(hours=2)
# Offline queues should drain in hours, not months. Anything older is suspect.
MAX_BACKDATE = timedelta(days=30)
# Upper bound on frames accepted per punch, whatever the kiosk sends.
MAX_FRAMES = 12


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
                face_enrolled=face_store.is_enrolled(db, employee.id),
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


def _read_frames(
    frames: list[UploadFile] | None, photo: UploadFile | None
) -> list[bytes]:
    """Collect the captured burst, oldest kiosks' single photo included.

    A kiosk that has not been updated yet still posts one ``photo``. It keeps
    working -- it simply cannot pass the liveness check, which needs at least
    two frames, so its punches are recorded and flagged rather than silently
    trusted.
    """
    collected: list[bytes] = []
    for upload in list(frames or []) + ([photo] if photo is not None else []):
        if upload is None:
            continue
        data = upload.file.read()
        if not data:
            continue
        if len(data) > settings.max_photo_bytes:
            raise HTTPException(
                status_code=413,
                detail="A captured frame was too large. The kiosk should shrink it first.",
            )
        collected.append(data)
        # A tampered kiosk could otherwise push hundreds of frames per punch and
        # tie the server up doing face detection on all of them.
        if len(collected) >= MAX_FRAMES:
            break
    return collected


@router.post("/punch", response_model=PunchResponse)
def record_punch(
    employee_id: int = Form(...),
    pin: str = Form(...),
    client_uuid: str = Form(...),
    captured_at: str | None = Form(default=None),
    # Which capture attempt this is, 1-based. Purely to decide whether to offer
    # another try; a kiosk that lies only makes its own punch flagged sooner.
    attempt: int = Form(default=1),
    # True when this is being drained from the offline queue. The employee is
    # long gone, so there is nobody to re-capture -- the check still runs, but
    # its verdict is recorded rather than retried.
    deferred: bool = Form(default=False),
    photo: UploadFile | None = File(default=None),
    frames: list[UploadFile] | None = File(default=None),
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
) -> PunchResponse:
    """Record one punch. Safe to retry -- ``client_uuid`` makes it idempotent.

    Two factors have to line up before the punch counts as confirmed: the PIN,
    which proves the employee knows their secret, and the face, which proves it
    is actually them standing there. The PIN is checked first so a stranger
    cannot use the camera to probe who works at a location.
    """
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

    captured = _read_frames(frames, photo)
    result = face_store.verify_frames(db, employee.id, captured)

    attempt = max(1, min(attempt, settings.face_max_attempts))
    attempts_left = max(0, settings.face_max_attempts - attempt)

    # A kiosk that sent a single frame is running the page from before liveness
    # checking. It can never satisfy the check however many times it is asked,
    # so retrying it would loop forever -- and the old page treats any 200 as a
    # success, which would show the employee a ticket for a punch that was
    # never written. Record and flag instead.
    supports_burst = len(captured) >= 2

    # Ask for another go while there are attempts left and there is somebody
    # still standing at the screen to take it. Bad light, a hat, a hand in the
    # way -- all of these clear up on a second capture.
    if result.retryable and attempts_left > 0 and not deferred and supports_burst:
        return PunchResponse(
            accepted=False,
            retry=True,
            attempts_left=attempts_left,
            employee_name=employee.name,
            face_status=result.outcome,
            message=result.message,
        )

    # Out of retries. By default the punch is still recorded, flagged for the
    # owner: refusing it outright would leave a real employee with no way to
    # clock in, and an unexplained gap in their attendance is a worse failure
    # than a punch the owner has been told to look at. Sites that would rather
    # refuse can set ATTENDANCE_FACE_STRICT=true.
    if result.retryable and settings.face_strict and not deferred and supports_burst:
        raise HTTPException(status_code=403, detail=result.message)

    direction = next_direction(db, employee.id, work_date)

    photo_path = None
    if captured:
        # Keep the frame the check actually judged, not an arbitrary one.
        best = captured[min(result.best_frame, len(captured) - 1)]
        photo_path = photos.save_punch_photo(best, work_date, client_uuid)

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
        note=face_store.review_note(result) if not result.verified else None,
    )
    face_store.apply_result(punch, result, attempts=attempt)

    db.add(punch)
    device.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    recompute_day(db, employee, work_date)
    return _punch_response(db, punch, duplicate=False, result=result)


# What the employee is told when a punch went through without being confirmed.
# Deliberately calm and non-accusing: the overwhelmingly common cause is a
# camera problem, and the kiosk screen is not the place to accuse anyone.
_WARNINGS = {
    FaceCheck.MISMATCH: "We could not confirm your face. This punch has been saved and the manager will check it.",
    FaceCheck.NOT_LIVE: "We could not confirm a live photo. This punch has been saved and the manager will check it.",
    FaceCheck.NO_FACE: "No face was captured. This punch has been saved and the manager will check it.",
    FaceCheck.NOT_ENROLLED: "Your face is not on file yet. Ask the manager to add it.",
}


def _punch_response(
    db: Session,
    punch: Punch,
    *,
    duplicate: bool,
    result: FaceVerification | None = None,
) -> PunchResponse:
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
        face_status=punch.face_status.value,
        face_verified=punch.face_ok,
        warning=_WARNINGS.get(punch.face_status),
    )
