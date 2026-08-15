"""Request and response models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DayStatus, LeaveType, LocationKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminOut(ORMModel):
    id: int
    username: str
    display_name: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# --- locations --------------------------------------------------------------


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: LocationKind = LocationKind.STORE
    address: str | None = None
    is_active: bool = True


class LocationOut(ORMModel):
    id: int
    name: str
    kind: LocationKind
    address: str | None = None
    is_active: bool


# --- employees --------------------------------------------------------------


class EmployeeIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    location_id: int
    monthly_salary: Decimal = Field(ge=0)
    night_threshold_hours: float = Field(default=5.0, ge=0, le=12)
    ot_adjustment_per_night_hours: float = Field(default=0.0, ge=0, le=8)
    weekly_off_dow: int | None = Field(default=3, ge=0, le=6)
    joined_on: date | None = None
    left_on: date | None = None
    is_active: bool = True
    notes: str | None = None
    # Only required when creating; omit to leave an existing PIN alone.
    pin: str | None = None

    @field_validator("pin")
    @classmethod
    def _pin_shape(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if len(value) != 4 or not value.isdigit():
            raise ValueError("PIN must be exactly 4 digits.")
        return value


class EmployeeOut(ORMModel):
    id: int
    code: str
    name: str
    phone: str | None
    location_id: int
    location_name: str | None = None
    monthly_salary: Decimal
    night_threshold_hours: float
    ot_adjustment_per_night_hours: float
    weekly_off_dow: int | None
    joined_on: date | None
    left_on: date | None
    is_active: bool
    notes: str | None = None
    # Whether a reference face has been enrolled. Drives the "not set up" marker
    # in the employee list -- without it the owner has no way to see who is
    # still punching unverified.
    face_enrolled: bool = False
    # Whether they can sign in to the staff portal yet.
    portal_ready: bool = False


class PinResetRequest(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def _pin_shape(cls, value: str) -> str:
        if len(value) != 4 or not value.isdigit():
            raise ValueError("PIN must be exactly 4 digits.")
        return value


# --- devices ----------------------------------------------------------------


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location_id: int


class DeviceOut(ORMModel):
    id: int
    name: str
    location_id: int
    location_name: str | None = None
    is_active: bool
    paired: bool = False
    pairing_code: str | None = None
    last_seen_at: str | None = None


class PairRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class PairResponse(BaseModel):
    device_token: str
    device_name: str
    location_id: int
    location_name: str


# --- kiosk ------------------------------------------------------------------


class KioskEmployee(BaseModel):
    id: int
    code: str
    name: str
    next_direction: str
    first_in: str | None = None
    last_out: str | None = None
    photo_url: str | None = None
    # False means nobody has enrolled this person's face yet, so their punches
    # go through flagged. The kiosk shows a small marker so it gets noticed.
    face_enrolled: bool = False


class PunchResponse(BaseModel):
    """One punch attempt.

    ``accepted`` false with ``retry`` true is not an error -- it is the face
    check asking for another capture. The punch has not been recorded and the
    kiosk should re-open the camera rather than drop anything.
    """

    accepted: bool
    duplicate: bool = False
    employee_name: str
    # Absent on a retry, because no punch was written.
    direction: str | None = None
    punched_at: str | None = None
    work_date: str | None = None
    first_in: str | None = None
    last_out: str | None = None
    worked_label: str | None = None
    message: str

    # --- face check ---
    retry: bool = False
    attempts_left: int = 0
    face_status: str | None = None
    face_verified: bool = False
    # Set when the punch was recorded but could not be confirmed, so the
    # employee is told it will be reviewed rather than being left to think it
    # passed cleanly.
    warning: str | None = None


# --- face enrolment ---------------------------------------------------------


class FaceEnrollmentOut(BaseModel):
    id: int
    employee_id: int
    photo_url: str | None = None
    added_by: str | None = None
    created_at: str


# --- staff portal -----------------------------------------------------------


class PortalLoginRequest(BaseModel):
    phone: str = Field(min_length=4, max_length=20)
    # Their 6-digit portal PIN, or their own password if they have set one.
    secret: str = Field(min_length=4, max_length=128)


class PortalMe(BaseModel):
    name: str
    code: str
    location: str
    uses_own_password: bool = False
    currency: str = "₹"


class PortalPasswordRequest(BaseModel):
    current_secret: str = Field(min_length=4, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PortalPinRequest(BaseModel):
    """Admin-set 6-digit portal PIN."""

    pin: str

    @field_validator("pin")
    @classmethod
    def _pin_shape(cls, value: str) -> str:
        if len(value) != 6 or not value.isdigit():
            raise ValueError("The portal PIN must be exactly 6 digits.")
        return value


class FaceStatusOut(BaseModel):
    employee_id: int
    employee_name: str
    enrolled: bool
    count: int
    max_allowed: int
    enrollments: list[FaceEnrollmentOut] = []


# --- attendance -------------------------------------------------------------


class DayCorrection(BaseModel):
    status: DayStatus | None = None
    worked_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    note: str = Field(min_length=3, max_length=500)
    clear_override: bool = False


class PunchVoidRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ManualPunchRequest(BaseModel):
    employee_id: int
    at: str  # local ISO datetime, e.g. "2026-08-13T09:05"
    direction: str
    note: str = Field(min_length=3, max_length=300)


# --- leaves, advances, adjustments -----------------------------------------


class LeaveIn(BaseModel):
    employee_id: int
    start_date: date
    end_date: date
    leave_type: LeaveType = LeaveType.UNPAID
    reason: str | None = None


class LeaveOut(ORMModel):
    id: int
    employee_id: int
    employee_name: str | None = None
    leave_date: date
    leave_type: LeaveType
    reason: str | None


class AdvanceIn(BaseModel):
    employee_id: int
    advance_date: date
    amount: Decimal = Field(gt=0)
    note: str | None = None


class AdvanceOut(ORMModel):
    id: int
    employee_id: int
    employee_name: str | None = None
    advance_date: date
    amount: Decimal
    note: str | None


class AdjustmentIn(BaseModel):
    employee_id: int
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    label: str = Field(min_length=1, max_length=120)
    amount: Decimal


class AdjustmentOut(ORMModel):
    id: int
    employee_id: int
    employee_name: str | None = None
    year: int
    month: int
    label: str
    amount: Decimal
