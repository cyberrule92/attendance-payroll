"""Admin sign-in, locations, and kiosk device management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    SESSION_COOKIE,
    authenticate_admin,
    current_admin,
    generate_pairing_code,
    hash_secret,
    issue_session,
    verify_secret,
)
from ..config import settings
from ..db import get_db
from ..models import AdminUser, AuditLog, Device, Employee, Location
from ..schemas import (
    AdminOut,
    ChangePasswordRequest,
    DeviceIn,
    DeviceOut,
    LocationIn,
    LocationOut,
    LoginRequest,
)
from ..services.attendance import to_local

router = APIRouter(prefix="/api", tags=["admin"])

PAIRING_CODE_TTL = timedelta(hours=2)


def audit(db: Session, actor: str, action: str, entity: str, entity_id, detail: str):
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            detail=detail,
        )
    )


# --- session ----------------------------------------------------------------


@router.post("/login", response_model=AdminOut)
def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
) -> AdminUser:
    admin = authenticate_admin(db, payload.username, payload.password)
    if admin is None:
        raise HTTPException(status_code=401, detail="Wrong username or password.")

    response.set_cookie(
        SESSION_COOKIE,
        issue_session(admin),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        # The tunnel always terminates TLS, but a plain-HTTP LAN test would
        # break on a Secure cookie, so follow the actual scheme.
        secure=request.url.scheme == "https",
        path="/",
    )
    return admin


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=AdminOut)
def me(admin: AdminUser = Depends(current_admin)) -> AdminUser:
    return admin


@router.post("/me/password")
def change_password(
    payload: ChangePasswordRequest,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_secret(payload.current_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="Current password is wrong.")
    admin.password_hash = hash_secret(payload.new_password)
    audit(db, admin.username, "password_change", "admin_user", admin.id, "")
    db.commit()
    return {"ok": True}


# --- locations --------------------------------------------------------------


@router.get("/locations", response_model=list[LocationOut])
def list_locations(
    _: AdminUser = Depends(current_admin), db: Session = Depends(get_db)
):
    return db.scalars(select(Location).order_by(Location.name)).all()


@router.post("/locations", response_model=LocationOut, status_code=201)
def create_location(
    payload: LocationIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Location).where(Location.name == payload.name)):
        raise HTTPException(status_code=409, detail="A location with that name exists.")
    row = Location(**payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, admin.username, "create", "location", row.id, payload.name)
    db.commit()
    return row


@router.put("/locations/{location_id}", response_model=LocationOut)
def update_location(
    location_id: int,
    payload: LocationIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Location, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    audit(db, admin.username, "update", "location", row.id, payload.name)
    db.commit()
    return row


@router.delete("/locations/{location_id}")
def delete_location(
    location_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Location, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    staff = db.scalar(
        select(Employee).where(Employee.location_id == location_id).limit(1)
    )
    if staff is not None:
        raise HTTPException(
            status_code=409,
            detail="Move this location's employees elsewhere before deleting it.",
        )
    db.delete(row)
    audit(db, admin.username, "delete", "location", location_id, row.name)
    db.commit()
    return {"ok": True}


# --- kiosk devices ----------------------------------------------------------


def _device_out(device: Device) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        name=device.name,
        location_id=device.location_id,
        location_name=device.location.name if device.location else None,
        is_active=device.is_active,
        paired=bool(device.token_hash),
        pairing_code=device.pairing_code,
        last_seen_at=(
            to_local(device.last_seen_at).strftime("%d %b %Y, %I:%M %p")
            if device.last_seen_at
            else None
        ),
    )


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    _: AdminUser = Depends(current_admin), db: Session = Depends(get_db)
):
    return [
        _device_out(d) for d in db.scalars(select(Device).order_by(Device.name)).all()
    ]


@router.post("/devices", response_model=DeviceOut, status_code=201)
def create_device(
    payload: DeviceIn,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    if db.get(Location, payload.location_id) is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    device = Device(name=payload.name, location_id=payload.location_id)
    db.add(device)
    db.flush()
    audit(db, admin.username, "create", "device", device.id, payload.name)
    db.commit()
    return _device_out(device)


@router.post("/devices/{device_id}/pairing-code", response_model=DeviceOut)
def issue_pairing_code(
    device_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Generate a fresh code to type into the tablet. Valid for two hours."""
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    device.pairing_code = generate_pairing_code()
    device.pairing_expires_at = (
        datetime.now(timezone.utc) + PAIRING_CODE_TTL
    ).replace(tzinfo=None)
    audit(db, admin.username, "pairing_code", "device", device.id, device.name)
    db.commit()
    return _device_out(device)


@router.post("/devices/{device_id}/unpair", response_model=DeviceOut)
def unpair_device(
    device_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Revoke a device's token -- use this if a tablet is lost or stolen."""
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    device.token_hash = None
    device.paired_at = None
    device.pairing_code = None
    audit(db, admin.username, "unpair", "device", device.id, device.name)
    db.commit()
    return _device_out(device)


@router.delete("/devices/{device_id}")
def delete_device(
    device_id: int,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    # Punches reference the device; disable rather than delete so history keeps
    # pointing at something real.
    device.is_active = False
    device.token_hash = None
    audit(db, admin.username, "disable", "device", device_id, device.name)
    db.commit()
    return {"ok": True}
