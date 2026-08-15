"""Authentication for the two very different audiences this app has.

*Admins* (the owner, on a Windows laptop) get a signed session cookie after a
username/password login.

*Kiosks* (a shared tablet at each location) are paired once with a short code
and then hold a long-lived device token. The token identifies the location, not
a person -- the person is identified by tapping their name and entering a PIN.

The device token is stored as ``<device_id>.<secret>`` so a lookup is a single
indexed fetch plus one hash comparison, rather than a bcrypt check against
every device row.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AdminUser, Device, Employee

SESSION_COOKIE = "attendance_session"
STAFF_COOKIE = "attendance_staff"

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="attendance-admin")
# A separate salt, so a staff cookie can never be replayed as an admin one even
# though both are signed with the same secret.
_staff_serializer = URLSafeTimedSerializer(settings.secret_key, salt="attendance-staff")


# --- hashing ----------------------------------------------------------------


def hash_secret(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_secret(raw: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def validate_pin(pin: str) -> str:
    """PINs are exactly 4 digits -- the kiosk keypad cannot produce anything else."""
    pin = (pin or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PIN must be exactly 4 digits.",
        )
    return pin


# --- PIN attempt throttling -------------------------------------------------


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class PinThrottle:
    """In-memory lockout. Single process, single laptop -- no shared store needed.

    Keyed per employee so one person fat-fingering their PIN cannot lock out
    the whole store's kiosk.

    ``max_attempts`` is read from settings at check time rather than captured on
    construction, so a test or a .env change takes effect without rebuilding the
    throttle. ``limit_setting`` names which setting to read, letting the kiosk
    and the staff portal run separate throttles with separate limits.
    """

    limit_setting: str = "pin_max_attempts"
    _state: dict[int, _Attempts] = field(default_factory=dict)

    @property
    def max_attempts(self) -> int:
        return getattr(settings, self.limit_setting)

    def check(self, employee_id: int) -> None:
        entry = self._state.get(employee_id)
        if entry and entry.locked_until > time.monotonic():
            remaining = int(entry.locked_until - time.monotonic())
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Too many wrong PIN attempts. Try again in "
                    f"{remaining // 60 + 1} minute(s), or ask the manager to reset it."
                ),
            )

    def record_failure(self, employee_id: int) -> None:
        entry = self._state.setdefault(employee_id, _Attempts())
        entry.count += 1
        if entry.count >= self.max_attempts:
            entry.locked_until = time.monotonic() + settings.pin_lockout_seconds
            entry.count = 0

    def record_success(self, employee_id: int) -> None:
        self._state.pop(employee_id, None)


pin_throttle = PinThrottle()

# The staff portal gets its own throttle. Sharing one would mean a colleague
# guessing at the web login could lock somebody out of the kiosk, and so stop
# them clocking in for a shift they are actually working.
portal_throttle = PinThrottle(limit_setting="portal_max_attempts")


# --- admin sessions ---------------------------------------------------------


def issue_session(admin: AdminUser) -> str:
    return _serializer.dumps({"uid": admin.id, "u": admin.username})


def authenticate_admin(db: Session, username: str, password: str) -> AdminUser | None:
    admin = db.scalar(
        select(AdminUser).where(
            AdminUser.username == username.strip().lower(),
            AdminUser.is_active == True,  # noqa: E712
        )
    )
    if admin is None:
        # Burn roughly the same time as a real check so a wrong username and a
        # wrong password are not distinguishable by timing.
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
        return None
    if not verify_secret(password, admin.password_hash):
        return None
    return admin


def current_admin(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> AdminUser:
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in."
        )
    try:
        payload = _serializer.loads(
            session, max_age=settings.session_max_age_seconds
        )
    except SignatureExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )
    except BadSignature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session."
        )

    admin = db.get(AdminUser, payload.get("uid"))
    if admin is None or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is disabled."
        )
    return admin


# --- device (kiosk) tokens --------------------------------------------------


def issue_device_token(device: Device) -> str:
    """Mint a token and store only its hash. Returned once, at pairing time."""
    secret = secrets.token_urlsafe(32)
    device.token_hash = hash_secret(secret)
    return f"{device.id}.{secret}"


def current_device(
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> Device:
    if not x_device_token or "." not in x_device_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This device is not paired. Ask the manager for a pairing code.",
        )

    device_id, _, secret = x_device_token.partition(".")
    if not device_id.isdigit():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed device token."
        )

    device = db.get(Device, int(device_id))
    if device is None or not device.is_active or not device.token_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This device is no longer paired.",
        )
    if not verify_secret(secret, device.token_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token."
        )
    return device


def generate_pairing_code() -> str:
    """A 6-digit code that a person can read aloud and type on a tablet."""
    return f"{secrets.randbelow(1_000_000):06d}"


def codes_match(supplied: str, stored: str | None) -> bool:
    if not stored:
        return False
    return hmac.compare_digest(supplied.strip(), stored)


def verify_employee_pin(
    db: Session, employee_id: int, pin: str, location_id: int | None = None
) -> Employee:
    """Check a kiosk PIN, with throttling and location scoping."""
    pin_throttle.check(employee_id)

    employee = db.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise HTTPException(status_code=404, detail="Employee not found.")
    if location_id is not None and employee.location_id != location_id:
        raise HTTPException(
            status_code=403,
            detail="This employee is not assigned to this location.",
        )

    if not verify_secret(pin, employee.pin_hash):
        pin_throttle.record_failure(employee_id)
        raise HTTPException(status_code=401, detail="Wrong PIN.")

    pin_throttle.record_success(employee_id)
    return employee


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- staff portal sessions --------------------------------------------------
#
# Staff sign in to read their own attendance and pay. This is a separate world
# from the admin console: its own cookie, its own signing salt, its own
# throttle, and a dependency that resolves to an Employee rather than an
# AdminUser -- so there is no code path where a staff session can be mistaken
# for an admin one.


def validate_portal_pin(pin: str) -> str:
    """Portal PINs are 6 digits, not the kiosk's 4.

    Two more digits is a hundredfold more work to guess, which matters because
    this login is reachable from the internet rather than from a tablet bolted
    to a shop counter.
    """
    pin = (pin or "").strip()
    if len(pin) != 6 or not pin.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The portal PIN must be exactly 6 digits.",
        )
    return pin


def issue_staff_session(employee: Employee) -> str:
    return _staff_serializer.dumps({"eid": employee.id, "c": employee.code})


def authenticate_employee(db: Session, phone: str, secret: str) -> Employee | None:
    """Check a staff portal login.

    A self-set password replaces the PIN once it exists, so an employee who has
    chosen one cannot still be reached with the PIN the office knows.

    Returns None for every kind of failure -- wrong phone, wrong secret, no
    portal access, left the company -- because telling the two apart would let
    anyone test which phone numbers belong to staff.
    """
    phone = (phone or "").strip()
    employee = db.scalar(select(Employee).where(Employee.phone == phone))

    if employee is None:
        # Spend roughly the time a real check costs, so a valid phone number is
        # not detectable by how fast the answer comes back.
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
        return None

    portal_throttle.check(employee.id)

    if not employee.portal_ready:
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
        return None

    expected = employee.portal_password_hash or employee.portal_pin_hash
    if not verify_secret(secret, expected):
        portal_throttle.record_failure(employee.id)
        return None

    portal_throttle.record_success(employee.id)
    return employee


def current_employee(
    session: str | None = Cookie(default=None, alias=STAFF_COOKIE),
    db: Session = Depends(get_db),
) -> Employee:
    """The signed-in employee. Every portal query is scoped through this."""
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in."
        )
    try:
        payload = _staff_serializer.loads(
            session, max_age=settings.staff_session_max_age_seconds
        )
    except SignatureExpired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )
    except BadSignature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session."
        )

    employee = db.get(Employee, payload.get("eid"))
    # Re-checked on every request, not just at login: revoking access by marking
    # someone inactive or clearing their PIN has to take effect immediately, not
    # whenever their cookie happens to expire.
    if employee is None or not employee.portal_ready:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account no longer has access.",
        )
    return employee
