"""ORM models.

Money is stored as Numeric and handled as Decimal everywhere -- payroll numbers
go on payslips and get argued about, so binary floats are not acceptable.
Durations are stored as whole minutes (integers) for the same reason.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

MONEY = Numeric(12, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LocationKind(str, enum.Enum):
    FACTORY = "FACTORY"
    STORE = "STORE"


class PunchDirection(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"


class PunchSource(str, enum.Enum):
    KIOSK = "KIOSK"
    ADMIN = "ADMIN"


class DayStatus(str, enum.Enum):
    """How a single calendar day is classified for payroll."""

    FULL = "FULL"  # present, no deduction
    HALF = "HALF"  # 3h30m-5h00m worked -> half daily rate deducted
    LEAVE = "LEAVE"  # absent or under 3h30m -> full daily rate deducted
    PAID_LEAVE = "PAID_LEAVE"  # admin-approved paid leave, no deduction
    WEEKOFF = "WEEKOFF"  # weekly off (Thursday by default), paid
    HOLIDAY = "HOLIDAY"  # reserved; behaves like WEEKOFF


class LeaveType(str, enum.Enum):
    PAID = "PAID"
    UNPAID = "UNPAID"


class PayrollStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    FINAL = "FINAL"


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[LocationKind] = mapped_column(
        Enum(LocationKind, native_enum=False, length=16), default=LocationKind.STORE
    )
    address: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employees: Mapped[list["Employee"]] = relationship(back_populates="location")
    devices: Mapped[list["Device"]] = relationship(back_populates="location")


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)
    pin_hash: Mapped[str] = mapped_column(String(120))

    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    monthly_salary: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    # Payroll policy, per employee. Sita and Shama get 4.0 / 1.5; everyone
    # else gets 5.0 / 0.0. Kept as data rather than hardcoded names so staff
    # changes never need a code change.
    night_threshold_hours: Mapped[float] = mapped_column(Float, default=5.0)
    ot_adjustment_per_night_hours: Mapped[float] = mapped_column(Float, default=0.0)
    # Monday=0 ... Sunday=6. Thursday=3.
    weekly_off_dow: Mapped[int | None] = mapped_column(Integer, default=3)

    joined_on: Mapped[date | None] = mapped_column(Date)
    left_on: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    photo_path: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    location: Mapped[Location] = relationship(back_populates="employees")

    def employed_on(self, day: date) -> bool:
        if self.joined_on and day < self.joined_on:
            return False
        if self.left_on and day > self.left_on:
            return False
        return True


class Device(Base):
    """A kiosk tablet/phone, bound to one location."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))

    token_hash: Mapped[str | None] = mapped_column(String(120))
    pairing_code: Mapped[str | None] = mapped_column(String(12), index=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    paired_at: Mapped[datetime | None] = mapped_column(DateTime)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    location: Mapped[Location] = relationship(back_populates="devices")


class Punch(Base):
    __tablename__ = "punches"
    __table_args__ = (
        UniqueConstraint("client_uuid", name="uq_punches_client_uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"))

    direction: Mapped[PunchDirection] = mapped_column(
        Enum(PunchDirection, native_enum=False, length=8)
    )
    # When the employee actually stood in front of the camera (UTC).
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # When the server received it (UTC). Later than captured_at for punches
    # that sat in a kiosk's offline queue.
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Local (Asia/Kolkata) calendar date of captured_at, denormalised so the
    # daily grouping never has to do timezone maths in SQL.
    work_date: Mapped[date] = mapped_column(Date, index=True)

    photo_path: Mapped[str | None] = mapped_column(String(300))
    client_uuid: Mapped[str] = mapped_column(String(64))
    source: Mapped[PunchSource] = mapped_column(
        Enum(PunchSource, native_enum=False, length=8), default=PunchSource.KIOSK
    )
    note: Mapped[str | None] = mapped_column(Text)

    is_voided: Mapped[bool] = mapped_column(Boolean, default=False)
    void_reason: Mapped[str | None] = mapped_column(Text)

    employee: Mapped[Employee] = relationship()
    location: Mapped[Location] = relationship()
    device: Mapped[Device | None] = relationship()


class AttendanceDay(Base):
    """One derived row per employee per calendar day.

    Recomputed from punches. Admin overrides live in the manual_* columns so a
    recompute never silently discards a correction.
    """

    __tablename__ = "attendance_days"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_attendance_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)

    first_in: Mapped[datetime | None] = mapped_column(DateTime)
    last_out: Mapped[datetime | None] = mapped_column(DateTime)
    worked_minutes: Mapped[int] = mapped_column(Integer, default=0)
    punch_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[DayStatus] = mapped_column(
        Enum(DayStatus, native_enum=False, length=16), default=DayStatus.LEAVE
    )
    ot_minutes: Mapped[int] = mapped_column(Integer, default=0)
    is_night: Mapped[bool] = mapped_column(Boolean, default=False)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_reason: Mapped[str | None] = mapped_column(String(200))

    # Admin overrides. When manual_status or manual_worked_minutes is set it
    # wins over the derived value.
    manual_status: Mapped[DayStatus | None] = mapped_column(
        Enum(DayStatus, native_enum=False, length=16)
    )
    manual_worked_minutes: Mapped[int | None] = mapped_column(Integer)
    manual_note: Mapped[str | None] = mapped_column(Text)
    manual_by: Mapped[str | None] = mapped_column(String(60))
    manual_at: Mapped[datetime | None] = mapped_column(DateTime)

    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    employee: Mapped[Employee] = relationship()


class LeaveRecord(Base):
    """An admin-approved leave for a single date."""

    __tablename__ = "leaves"
    __table_args__ = (
        UniqueConstraint("employee_id", "leave_date", name="uq_leave_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_date: Mapped[date] = mapped_column(Date, index=True)
    leave_type: Mapped[LeaveType] = mapped_column(
        Enum(LeaveType, native_enum=False, length=10), default=LeaveType.UNPAID
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employee: Mapped[Employee] = relationship()


class Advance(Base):
    __tablename__ = "advances"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    advance_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employee: Mapped[Employee] = relationship()


class Adjustment(Base):
    """A manual add/deduct line applied to one employee for one payroll month."""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(120))
    # Positive adds to the payout, negative deducts.
    amount: Mapped[Decimal] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employee: Mapped[Employee] = relationship()


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("year", "month", name="uq_payroll_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    status: Mapped[PayrollStatus] = mapped_column(
        Enum(PayrollStatus, native_enum=False, length=10), default=PayrollStatus.DRAFT
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["PayrollLine"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class PayrollLine(Base):
    """A frozen per-employee payroll result.

    Snapshots the name and salary so a payslip handed out in August does not
    change because an old punch was corrected in September.
    """

    __tablename__ = "payroll_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_payroll_line"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)

    employee_code: Mapped[str] = mapped_column(String(20))
    employee_name: Mapped[str] = mapped_column(String(120))
    location_name: Mapped[str] = mapped_column(String(120))
    monthly_salary: Mapped[Decimal] = mapped_column(MONEY)

    days_full: Mapped[int] = mapped_column(Integer, default=0)
    days_half: Mapped[int] = mapped_column(Integer, default=0)
    days_leave: Mapped[int] = mapped_column(Integer, default=0)
    days_paid_leave: Mapped[int] = mapped_column(Integer, default=0)
    days_weekoff: Mapped[int] = mapped_column(Integer, default=0)
    nights: Mapped[int] = mapped_column(Integer, default=0)

    ot_minutes_raw: Mapped[int] = mapped_column(Integer, default=0)
    ot_minutes_paid: Mapped[int] = mapped_column(Integer, default=0)

    ot_pay: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    night_pay: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    attendance_bonus: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    leave_deduction: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    halfday_deduction: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    adjustments_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    grand_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    advances_deducted: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    net_payable: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    # Full day-by-day derivation, so the admin UI can show "why is this number
    # what it is" without recomputing against possibly-edited data.
    breakdown_json: Mapped[str | None] = mapped_column(Text)

    run: Mapped[PayrollRun] = relationship(back_populates="lines")
    employee: Mapped[Employee] = relationship()


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    detail: Mapped[str | None] = mapped_column(Text)
