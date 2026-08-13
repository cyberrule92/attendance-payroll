"""Fill the database with a realistic sample month, to try the system out.

Creates six employees (including the two on the 4-hour night bar), a month of
punches with overtime, night shifts, half days and absences, plus a couple of
advances -- so every screen has something real to show before you commit to
entering your own data.

    python backend/scripts/demo_data.py            # current month
    python backend/scripts/demo_data.py --year 2026 --month 7
    python backend/scripts/demo_data.py --wipe     # remove demo data again

The demo employees all use codes starting with DEMO, so --wipe can find and
remove exactly them and nothing of yours.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.auth import hash_secret  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.models import (  # noqa: E402
    Advance,
    AttendanceDay,
    Employee,
    Location,
    Punch,
    PunchDirection,
    PunchSource,
)
from app.services.attendance import recompute_range, store_dt  # noqa: E402
from app.services.payroll import month_dates  # noqa: E402

DEMO_PREFIX = "DEMO"

# (code, name, salary, night bar, give-back per night)
PEOPLE = [
    # Deliberately invented names -- not real staff. Two of them sit on the
    # 4-hour night bar so that code path shows up in the sample month.
    ("DEMO01", "Sample Worker One", "15000", 5.0, 0.0),
    ("DEMO02", "Sample Worker Two", "12000", 4.0, 1.5),
    ("DEMO03", "Sample Worker Three", "12500", 4.0, 1.5),
    ("DEMO04", "Sample Worker Four", "18000", 5.0, 0.0),
    ("DEMO05", "Sample Worker Five", "14000", 5.0, 0.0),
    ("DEMO06", "Sample Worker Six", "16500", 5.0, 0.0),
]


def wipe(db) -> None:
    employees = db.scalars(
        select(Employee).where(Employee.code.like(f"{DEMO_PREFIX}%"))
    ).all()
    if not employees:
        print("No demo employees found.")
        return

    ids = [e.id for e in employees]
    for model in (Punch, AttendanceDay, Advance):
        for row in db.scalars(select(model).where(model.employee_id.in_(ids))).all():
            db.delete(row)
    for employee in employees:
        db.delete(employee)
    db.commit()
    print(f"Removed {len(employees)} demo employees and their records.")


def build(db, year: int, month: int) -> None:
    location = db.scalar(select(Location).order_by(Location.id))
    if location is None:
        print("No locations exist. Run scripts/seed.py first.")
        return

    random.seed(f"{year}-{month}")

    employees = []
    for code, name, salary, night_bar, adjustment in PEOPLE:
        employee = db.scalar(select(Employee).where(Employee.code == code))
        if employee is None:
            employee = Employee(
                code=code,
                name=name,
                phone=None,
                pin_hash=hash_secret("1234"),
                location_id=location.id,
                monthly_salary=Decimal(salary),
                night_threshold_hours=night_bar,
                ot_adjustment_per_night_hours=adjustment,
                weekly_off_dow=3,
                joined_on=date(year - 1, 1, 1),
            )
            db.add(employee)
        employees.append(employee)
    db.commit()

    dates = month_dates(year, month)
    today = date.today()
    created = 0

    for employee in employees:
        for work_date in dates:
            if work_date > today:
                continue  # do not invent attendance for the future
            if work_date.weekday() == 3:  # Thursday off
                continue

            roll = random.random()
            if roll < 0.06:
                continue  # absent
            if roll < 0.10:
                start_hour, minutes = 9, random.choice([210, 240, 285])  # half day
            elif roll < 0.22:
                start_hour, minutes = 14, random.randint(800, 900)  # night shift
            elif roll < 0.45:
                start_hour, minutes = 9, random.randint(540, 700)  # some overtime
            else:
                start_hour, minutes = 9, random.randint(500, 525)  # normal shift

            start = datetime(
                work_date.year, work_date.month, work_date.day,
                start_hour, random.choice([0, 5, 10, 55]),
                tzinfo=settings.tz,
            )
            end = start + timedelta(minutes=minutes)

            for direction, when in (
                (PunchDirection.IN, start),
                (PunchDirection.OUT, end),
            ):
                uuid = f"demo-{employee.code}-{work_date}-{direction.value}"
                if db.scalar(select(Punch).where(Punch.client_uuid == uuid)):
                    continue
                db.add(
                    Punch(
                        employee_id=employee.id,
                        location_id=location.id,
                        direction=direction,
                        captured_at=store_dt(when),
                        received_at=store_dt(when),
                        work_date=work_date,
                        client_uuid=uuid,
                        source=PunchSource.KIOSK,
                    )
                )
                created += 1
    db.commit()

    for employee in employees[:3]:
        advance_date = dates[min(9, len(dates) - 1)]
        if not db.scalar(
            select(Advance).where(
                Advance.employee_id == employee.id,
                Advance.advance_date == advance_date,
            )
        ):
            db.add(
                Advance(
                    employee_id=employee.id,
                    advance_date=advance_date,
                    amount=Decimal(random.choice([1000, 2000, 3000])),
                    note="Sample advance",
                )
            )
    db.commit()

    recompute_range(db, employees, dates[0], dates[-1])
    print(f"Created {created} punches for {len(employees)} employees.")
    print(f"Open /admin and look at {month:02d}/{year}.")


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser(description="Generate sample attendance data.")
    parser.add_argument("--year", type=int, default=today.year)
    parser.add_argument("--month", type=int, default=today.month)
    parser.add_argument("--wipe", action="store_true", help="Remove demo data.")
    args = parser.parse_args()

    create_all()
    with SessionLocal() as db:
        if args.wipe:
            wipe(db)
        else:
            build(db, args.year, args.month)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
