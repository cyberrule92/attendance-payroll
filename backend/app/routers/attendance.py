"""Viewing and correcting attendance."""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_admin
from ..config import settings
from ..db import get_db
from ..models import (
    AdminUser,
    AttendanceDay,
    DayStatus,
    Employee,
    Punch,
    PunchDirection,
    PunchSource,
)
from ..schemas import DayCorrection, ManualPunchRequest, PunchVoidRequest
from ..services import photos
from ..services.attendance import (
    is_weekly_off,
    recompute_day,
    store_dt,
    to_local,
    work_date_for,
)
from ..services.payroll import minutes_to_hhmm, month_dates
from .admin import audit

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


def _employee_or_404(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return employee


def _punch_payload(punch: Punch) -> dict:
    return {
        "id": punch.id,
        "direction": punch.direction.value,
        "at": to_local(punch.captured_at).strftime("%I:%M %p").lstrip("0"),
        "at_iso": to_local(punch.captured_at).isoformat(),
        "photo_url": f"/api/photo/{punch.photo_path}" if punch.photo_path else None,
        "source": punch.source.value,
        "is_voided": punch.is_voided,
        "void_reason": punch.void_reason,
        # A punch that reached the server much later than it was taken came out
        # of a kiosk's offline queue.
        "was_queued": (punch.received_at - punch.captured_at).total_seconds() > 300,
        "note": punch.note,
        # Anti-proxy check. `face_suspect` is the one that means "the camera
        # disagreed"; not-enrolled and unavailable are administrative gaps and
        # are shown differently in the UI.
        "face_status": punch.face_status.value,
        "face_verified": punch.face_ok,
        "face_suspect": punch.face_suspect,
        "face_score": punch.face_score,
    }


@router.get("/today")
def today_board(
    on: date | None = None,
    location_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """The live board: who is in, who has left, with their photos."""
    work_date = on or work_date_for(datetime.now(timezone.utc))

    query = select(Employee).where(Employee.is_active == True)  # noqa: E712
    if location_id is not None:
        query = query.where(Employee.location_id == location_id)
    employees = db.scalars(query.order_by(Employee.name)).all()

    rows = []
    for employee in employees:
        if not employee.employed_on(work_date):
            continue
        punches = db.scalars(
            select(Punch)
            .where(Punch.employee_id == employee.id, Punch.work_date == work_date)
            .order_by(Punch.captured_at)
        ).all()
        day = db.scalar(
            select(AttendanceDay).where(
                AttendanceDay.employee_id == employee.id,
                AttendanceDay.work_date == work_date,
            )
        )
        live = [p for p in punches if not p.is_voided]
        present = bool(live) and live[-1].direction is PunchDirection.IN

        rows.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "code": employee.code,
                "location": employee.location.name if employee.location else "",
                "status": day.status.value if day else (
                    DayStatus.WEEKOFF.value
                    if is_weekly_off(employee, work_date)
                    else DayStatus.LEAVE.value
                ),
                "currently_in": present,
                "first_in": (
                    to_local(day.first_in).strftime("%I:%M %p").lstrip("0")
                    if day and day.first_in
                    else None
                ),
                "last_out": (
                    to_local(day.last_out).strftime("%I:%M %p").lstrip("0")
                    if day and day.last_out
                    else None
                ),
                "worked": minutes_to_hhmm(day.worked_minutes) if day else "0:00",
                "ot": minutes_to_hhmm(day.ot_minutes) if day else "0:00",
                "is_night": bool(day and day.is_night),
                "needs_review": bool(day and day.needs_review),
                "review_reason": day.review_reason if day else None,
                "face_suspect": any(p.face_suspect for p in live),
                "punches": [_punch_payload(p) for p in punches],
            }
        )

    return {
        "work_date": work_date.isoformat(),
        "generated_at": to_local(datetime.now(timezone.utc)).strftime("%I:%M %p").lstrip("0"),
        "present_count": sum(1 for r in rows if r["currently_in"]),
        # Drives the "N punches could not be confirmed" banner on the board.
        "face_suspect_count": sum(1 for r in rows if r["face_suspect"]),
        "rows": rows,
    }


@router.get("/month")
def month_grid(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    location_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Employees x days, for the correction screen."""
    dates = month_dates(year, month)
    today = work_date_for(datetime.now(timezone.utc))

    query = select(Employee).order_by(Employee.name)
    if location_id is not None:
        query = query.where(Employee.location_id == location_id)
    employees = [
        e
        for e in db.scalars(query).all()
        if (e.joined_on is None or e.joined_on <= dates[-1])
        and (e.left_on is None or e.left_on >= dates[0])
    ]

    rows = []
    for employee in employees:
        days = {
            d.work_date: d
            for d in db.scalars(
                select(AttendanceDay).where(
                    AttendanceDay.employee_id == employee.id,
                    AttendanceDay.work_date >= dates[0],
                    AttendanceDay.work_date <= dates[-1],
                )
            )
        }
        cells = []
        for work_date in dates:
            # Blank out days outside the employee's service period, and days
            # that have not happened yet -- showing a fortnight of red "Leave"
            # squares for the rest of the current month is alarming and wrong.
            if not employee.employed_on(work_date) or work_date > today:
                cells.append({"date": work_date.isoformat(), "status": None})
                continue
            day = days.get(work_date)
            if day is None:
                cells.append(
                    {
                        "date": work_date.isoformat(),
                        "status": (
                            DayStatus.WEEKOFF.value
                            if is_weekly_off(employee, work_date)
                            else DayStatus.LEAVE.value
                        ),
                        "worked": "0:00",
                        "ot": "0:00",
                        "is_night": False,
                        "needs_review": False,
                        "is_manual": False,
                    }
                )
                continue
            cells.append(
                {
                    "date": work_date.isoformat(),
                    "status": day.status.value,
                    "worked": minutes_to_hhmm(
                        day.manual_worked_minutes
                        if day.manual_worked_minutes is not None
                        else day.worked_minutes
                    ),
                    "ot": minutes_to_hhmm(day.ot_minutes),
                    "is_night": day.is_night,
                    "needs_review": day.needs_review,
                    "is_manual": day.manual_status is not None
                    or day.manual_worked_minutes is not None,
                    "note": day.manual_note,
                }
            )
        rows.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "code": employee.code,
                "location": employee.location.name if employee.location else "",
                "cells": cells,
            }
        )

    return {
        "year": year,
        "month": month,
        "dates": [d.isoformat() for d in dates],
        "weekdays": [d.strftime("%a")[:1] for d in dates],
        "rows": rows,
    }


@router.get("/day")
def day_detail(
    employee_id: int,
    work_date: date,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    employee = _employee_or_404(db, employee_id)
    punches = db.scalars(
        select(Punch)
        .where(Punch.employee_id == employee_id, Punch.work_date == work_date)
        .order_by(Punch.captured_at)
    ).all()
    day = db.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee_id,
            AttendanceDay.work_date == work_date,
        )
    )
    return {
        "employee_id": employee.id,
        "employee_name": employee.name,
        "work_date": work_date.isoformat(),
        "is_weekly_off": is_weekly_off(employee, work_date),
        "status": day.status.value if day else None,
        "worked_minutes": (
            (day.manual_worked_minutes if day.manual_worked_minutes is not None
             else day.worked_minutes)
            if day
            else 0
        ),
        "derived_worked_minutes": day.worked_minutes if day else 0,
        "ot": minutes_to_hhmm(day.ot_minutes) if day else "0:00",
        "is_night": bool(day and day.is_night),
        "needs_review": bool(day and day.needs_review),
        "review_reason": day.review_reason if day else None,
        "manual_status": day.manual_status.value if day and day.manual_status else None,
        "manual_worked_minutes": day.manual_worked_minutes if day else None,
        "manual_note": day.manual_note if day else None,
        "punches": [_punch_payload(p) for p in punches],
    }


@router.post("/day/{employee_id}/{work_date}")
def correct_day(
    employee_id: int,
    work_date: date,
    payload: DayCorrection,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Override a day's status or worked hours.

    The override is stored separately from the punch-derived value, so a later
    recompute cannot quietly undo it.
    """
    employee = _employee_or_404(db, employee_id)

    day = db.scalar(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee_id,
            AttendanceDay.work_date == work_date,
        )
    )
    if day is None:
        day = AttendanceDay(employee_id=employee_id, work_date=work_date)
        db.add(day)
        db.flush()

    if payload.clear_override:
        day.manual_status = None
        day.manual_worked_minutes = None
        detail = "override cleared"
    else:
        if payload.status is None and payload.worked_minutes is None:
            raise HTTPException(
                status_code=400,
                detail="Set a status, set the worked minutes, or clear the override.",
            )
        day.manual_status = payload.status
        day.manual_worked_minutes = payload.worked_minutes
        detail = (
            f"status={payload.status.value if payload.status else '-'} "
            f"worked={payload.worked_minutes}"
        )

    day.manual_note = payload.note
    day.manual_by = admin.username
    day.manual_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()

    audit(
        db,
        admin.username,
        "correct_day",
        "attendance_day",
        f"{employee_id}/{work_date}",
        f"{employee.name} {work_date}: {detail} ({payload.note})",
    )
    db.commit()

    row = recompute_day(db, employee, work_date)
    return {
        "ok": True,
        "status": row.status.value,
        "worked": minutes_to_hhmm(
            row.manual_worked_minutes
            if row.manual_worked_minutes is not None
            else row.worked_minutes
        ),
        "ot": minutes_to_hhmm(row.ot_minutes),
        "is_night": row.is_night,
    }


@router.post("/punch")
def add_manual_punch(
    payload: ManualPunchRequest,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Add a punch by hand -- for a forgotten punch-out, or a broken kiosk."""
    employee = _employee_or_404(db, payload.employee_id)
    try:
        direction = PunchDirection(payload.direction.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="Direction must be IN or OUT.")

    try:
        naive_local = datetime.fromisoformat(payload.at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unreadable date and time.")
    aware_local = naive_local.replace(tzinfo=settings.tz)
    work_date = work_date_for(aware_local)

    punch = Punch(
        employee_id=employee.id,
        location_id=employee.location_id,
        direction=direction,
        captured_at=store_dt(aware_local),
        received_at=datetime.now(timezone.utc).replace(tzinfo=None),
        work_date=work_date,
        client_uuid=f"manual-{employee.id}-{aware_local.isoformat()}-{direction.value}",
        source=PunchSource.ADMIN,
        note=payload.note,
    )
    db.add(punch)
    audit(
        db,
        admin.username,
        "manual_punch",
        "punch",
        employee.id,
        f"{employee.name} {direction.value} at {payload.at}: {payload.note}",
    )
    db.commit()

    recompute_day(db, employee, work_date)
    return {"ok": True, "work_date": work_date.isoformat()}


@router.post("/punch/{punch_id}/void")
def void_punch(
    punch_id: int,
    payload: PunchVoidRequest,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Void a punch (a double tap, a test punch) without destroying the record."""
    punch = db.get(Punch, punch_id)
    if punch is None:
        raise HTTPException(status_code=404, detail="Punch not found.")

    punch.is_voided = True
    punch.void_reason = payload.reason
    audit(
        db,
        admin.username,
        "void_punch",
        "punch",
        punch.id,
        f"{punch.employee.name} {punch.work_date}: {payload.reason}",
    )
    db.commit()

    recompute_day(db, punch.employee, punch.work_date)
    return {"ok": True}


@router.get("/review")
def review_queue(
    _: AdminUser = Depends(current_admin), db: Session = Depends(get_db)
) -> dict:
    """Every day the system could not work out on its own."""
    rows = db.scalars(
        select(AttendanceDay)
        .where(AttendanceDay.needs_review == True)  # noqa: E712
        .order_by(AttendanceDay.work_date.desc())
        .limit(200)
    ).all()
    return {
        "count": len(rows),
        "rows": [
            {
                "employee_id": r.employee_id,
                "employee_name": r.employee.name,
                "work_date": r.work_date.isoformat(),
                "reason": r.review_reason,
                "worked": minutes_to_hhmm(r.worked_minutes),
                "resolved": r.manual_status is not None
                or r.manual_worked_minutes is not None,
            }
            for r in rows
        ],
    }


# Photos are personal data, so they are served through the app behind the admin
# session rather than mounted as public static files.
photo_router = APIRouter(prefix="/api/photo", tags=["attendance"])


@photo_router.get("/{path:path}")
def get_photo(
    path: str,
    _: AdminUser = Depends(current_admin),
) -> FileResponse:
    return FileResponse(photos.resolve(path), media_type="image/jpeg")
