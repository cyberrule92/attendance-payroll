"""Test fixtures: a throwaway SQLite database per test."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import Base
from app.models import Employee, Location, LocationKind


@pytest.fixture()
def db(tmp_path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def location(db: Session) -> Location:
    row = Location(name="Main Factory", kind=LocationKind.FACTORY)
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def employee(db: Session, location: Location) -> Employee:
    row = Employee(
        code="EMP001",
        name="Ramesh",
        phone="9000000001",
        pin_hash="x",
        location_id=location.id,
        monthly_salary=Decimal("15000"),
        night_threshold_hours=5.0,
        ot_adjustment_per_night_hours=0.0,
        weekly_off_dow=3,  # Thursday
        joined_on=date(2026, 1, 1),
    )
    db.add(row)
    db.commit()
    return row


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build an aware local (Asia/Kolkata) timestamp."""
    return datetime(year, month, day, hour, minute, tzinfo=settings.tz)
