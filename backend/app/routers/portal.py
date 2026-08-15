"""The staff portal: what an employee can see about their own work and pay.

Read-only, and scoped to one person. Every query in this module is filtered by
``current_employee``, never by an id from the request -- there is no endpoint
here that takes an employee id at all, so there is nothing to tamper with.

The pay figures follow the same rule the payslips do. A finalised month is read
back from its frozen ``payroll_lines``, so what an employee sees is exactly the
paper they were handed. An unfinalised month is computed live and clearly
labelled an estimate, because it moves every time somebody punches.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    STAFF_COOKIE,
    authenticate_employee,
    current_employee,
    hash_secret,
    issue_staff_session,
    verify_secret,
)
from ..config import settings
from ..db import get_db
from ..models import Advance, AttendanceDay, Employee, LeaveRecord, PayrollStatus, PayrollRun
from ..schemas import (
    PortalLoginRequest,
    PortalMe,
    PortalPasswordRequest,
)
from ..services import exports
from ..services.attendance import to_local, work_date_for
from ..services.payroll import minutes_to_hhmm, month_dates
from ..services.payroll_run import (
    compute_for_employee,
    frozen_month,
    month_bounds,
    result_payload,
)

router = APIRouter(prefix="/api/portal", tags=["portal"])

YEAR = Query(ge=2000, le=2100)
MONTH = Query(ge=1, le=12)

STATUS_LABELS = {
    "FULL": "Present",
    "HALF": "Half day",
    "LEAVE": "Leave",
    "PAID_LEAVE": "Paid leave",
    "WEEKOFF": "Week off",
    "HOLIDAY": "Holiday",
}


# --- session ----------------------------------------------------------------


@router.post("/login", response_model=PortalMe)
def login(
    payload: PortalLoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
) -> PortalMe:
    employee = authenticate_employee(db, payload.phone, payload.secret)
    if employee is None:
        # One message for every kind of failure. Saying "no such number" would
        # let anyone check which phone numbers belong to staff here.
        raise HTTPException(
            status_code=401,
            detail="Wrong phone number or PIN. Ask the office if you are stuck.",
        )

    employee.portal_last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    response.set_cookie(
        STAFF_COOKIE,
        issue_staff_session(employee),
        max_age=settings.staff_session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return _me(employee)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(STAFF_COOKIE, path="/")
    return {"ok": True}


def _me(employee: Employee) -> PortalMe:
    return PortalMe(
        name=employee.name,
        code=employee.code,
        location=employee.location.name if employee.location else "",
        # So the portal can nudge people off the PIN the office also knows.
        uses_own_password=bool(employee.portal_password_hash),
        currency=settings.currency_symbol,
    )


@router.get("/me", response_model=PortalMe)
def me(employee: Employee = Depends(current_employee)) -> PortalMe:
    return _me(employee)


@router.post("/password")
def set_password(
    payload: PortalPasswordRequest,
    employee: Employee = Depends(current_employee),
    db: Session = Depends(get_db),
) -> dict:
    """Replace the office-issued PIN with a password of the employee's own.

    Proving the current secret is required, so someone who finds an unlocked
    phone cannot lock the real owner out of their own record.
    """
    current = employee.portal_password_hash or employee.portal_pin_hash
    if not verify_secret(payload.current_secret, current):
        raise HTTPException(status_code=400, detail="Your current PIN or password is wrong.")

    employee.portal_password_hash = hash_secret(payload.new_password)
    db.commit()
    return {
        "ok": True,
        "message": "Password saved. Use it instead of your PIN from now on.",
    }


# --- the month --------------------------------------------------------------


def _day_rows(db: Session, employee: Employee, year: int, month: int) -> list[dict]:
    """Day-by-day attendance, as the employee would count it themselves."""
    dates = month_dates(year, month)
    today = work_date_for(datetime.now(timezone.utc))

    rows = {
        row.work_date: row
        for row in db.scalars(
            select(AttendanceDay).where(
                AttendanceDay.employee_id == employee.id,
                AttendanceDay.work_date >= dates[0],
                AttendanceDay.work_date <= dates[-1],
            )
        )
    }

    out: list[dict] = []
    for work_date in dates:
        # Days they were not employed, and days that have not happened, are left
        # out rather than shown as absences.
        if not employee.employed_on(work_date) or work_date > today:
            continue
        row = rows.get(work_date)
        worked = 0
        if row is not None:
            worked = (
                row.manual_worked_minutes
                if row.manual_worked_minutes is not None
                else row.worked_minutes
            )
        status = row.status.value if row else "LEAVE"
        out.append(
            {
                "date": work_date.isoformat(),
                "day": work_date.strftime("%a"),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "first_in": (
                    to_local(row.first_in).strftime("%I:%M %p").lstrip("0")
                    if row and row.first_in
                    else None
                ),
                "last_out": (
                    to_local(row.last_out).strftime("%I:%M %p").lstrip("0")
                    if row and row.last_out
                    else None
                ),
                "worked": minutes_to_hhmm(worked),
                "ot": minutes_to_hhmm(row.ot_minutes) if row else "0:00",
                "is_night": bool(row and row.is_night),
                # Shown so a corrected day is visibly a correction, not a
                # number that quietly changed behind their back.
                "corrected": bool(
                    row and (row.manual_status is not None or row.manual_worked_minutes is not None)
                ),
            }
        )
    return out


def _earnings(db: Session, employee: Employee, year: int, month: int) -> tuple[dict, bool]:
    """This employee's pay for the month, and whether it is final.

    A finalised month comes from the frozen line, not a fresh calculation, so
    it cannot drift from the payslip already handed over.
    """
    saved = frozen_month(db, year, month)
    if saved is not None:
        for row in saved["rows"]:
            if row["employee_id"] == employee.id:
                return row, True
        # Finalised, but this employee has no line -- they were not on the
        # payroll that month. Nothing to show rather than a live figure that
        # would contradict a closed month.
        return {}, True

    return result_payload(employee, compute_for_employee(db, employee, year, month)), False


@router.get("/month")
def month_view(
    year: int = YEAR,
    month: int = MONTH,
    employee: Employee = Depends(current_employee),
    db: Session = Depends(get_db),
) -> dict:
    """Everything an employee sees for one month, in a single request."""
    start, end = month_bounds(year, month)
    earnings, is_final = _earnings(db, employee, year, month)

    leaves = [
        {
            "date": leave.leave_date.isoformat(),
            "type": leave.leave_type.value,
            "paid": leave.leave_type.value == "PAID",
            "reason": leave.reason,
        }
        for leave in db.scalars(
            select(LeaveRecord)
            .where(
                LeaveRecord.employee_id == employee.id,
                LeaveRecord.leave_date >= start,
                LeaveRecord.leave_date <= end,
            )
            .order_by(LeaveRecord.leave_date)
        )
    ]

    # Advances are shown because net pay makes no sense without them: someone
    # who took an advance would otherwise see a smaller number than the total
    # says, with nothing to explain the gap.
    advances = [
        {
            "date": advance.advance_date.isoformat(),
            "amount": str(advance.amount),
            "note": advance.note,
        }
        for advance in db.scalars(
            select(Advance)
            .where(
                Advance.employee_id == employee.id,
                Advance.advance_date >= start,
                Advance.advance_date <= end,
            )
            .order_by(Advance.advance_date)
        )
    ]

    # The owner-facing flags ("salary not prorated", "check the kiosk") are
    # deliberately not passed through -- they are notes for whoever runs
    # payroll, and would only alarm the person the row is about.
    if earnings:
        earnings = {k: v for k, v in earnings.items() if k not in ("flags", "notes", "breakdown")}

    return {
        "year": year,
        "month": month,
        "month_label": exports.period_label(year, month),
        "is_final": is_final,
        # Anything not finalised is still moving. The portal says so plainly
        # rather than showing a figure that looks settled.
        "is_estimate": not is_final,
        "has_earnings": bool(earnings),
        "days": _day_rows(db, employee, year, month),
        "earnings": earnings,
        "leaves": leaves,
        "advances": advances,
    }


@router.get("/months")
def available_months(
    employee: Employee = Depends(current_employee),
    db: Session = Depends(get_db),
) -> dict:
    """Which months the picker should offer: finalised ones, plus this one."""
    today = work_date_for(datetime.now(timezone.utc))
    runs = db.scalars(
        select(PayrollRun)
        .where(PayrollRun.status == PayrollStatus.FINAL)
        .order_by(PayrollRun.year.desc(), PayrollRun.month.desc())
    ).all()

    months = [
        {"year": run.year, "month": run.month, "label": exports.period_label(run.year, run.month), "final": True}
        for run in runs
    ]
    if not any(m["year"] == today.year and m["month"] == today.month for m in months):
        months.insert(
            0,
            {
                "year": today.year,
                "month": today.month,
                "label": exports.period_label(today.year, today.month),
                "final": False,
            },
        )
    return {"current": {"year": today.year, "month": today.month}, "months": months}


@router.get("/payslip.pdf")
def payslip(
    year: int = YEAR,
    month: int = MONTH,
    employee: Employee = Depends(current_employee),
    db: Session = Depends(get_db),
) -> Response:
    """Their own payslip, for finalised months only.

    A PDF of an estimate would circulate as though it were a payslip, so an
    unfinalised month is refused rather than rendered.
    """
    earnings, is_final = _earnings(db, employee, year, month)
    if not is_final or not earnings:
        raise HTTPException(
            status_code=404,
            detail="That month is not finalised yet, so there is no payslip for it.",
        )

    pdf = exports.payslip_pdf([earnings], year, month)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="payslip-{employee.code}-{year}-{month:02d}.pdf"'
            )
        },
    )
