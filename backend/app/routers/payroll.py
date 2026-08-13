"""Monthly payroll: preview, finalise, payslips, export."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import current_admin
from ..db import get_db
from ..models import AdminUser
from ..services import exports
from ..services.payroll_run import (
    finalize_month,
    frozen_month,
    preview_month,
    reopen_month,
)
from .admin import audit

router = APIRouter(prefix="/api/payroll", tags=["payroll"])

YEAR = Query(ge=2000, le=2100)
MONTH = Query(ge=1, le=12)


def _load(db: Session, year: int, month: int) -> dict:
    """A finalised month is read back from its frozen lines; anything else is
    computed live from current attendance."""
    saved = frozen_month(db, year, month)
    if saved is not None:
        return saved
    return preview_month(db, year, month)


@router.get("")
def get_payroll(
    year: int = YEAR,
    month: int = MONTH,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    return _load(db, year, month)


@router.post("/finalize")
def finalize(
    year: int = YEAR,
    month: int = MONTH,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    try:
        run = finalize_month(db, year, month, admin.username)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    audit(db, admin.username, "finalize", "payroll_run", run.id, f"{month:02d}/{year}")
    db.commit()
    return {
        "ok": True,
        "status": run.status.value,
        "message": (
            f"Payroll for {exports.period_label(year, month)} is finalised. "
            "These numbers will not change if attendance is edited later."
        ),
    }


@router.post("/reopen")
def reopen(
    year: int = YEAR,
    month: int = MONTH,
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Unfreeze a month so it can be corrected and finalised again."""
    try:
        run = reopen_month(db, year, month)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    audit(db, admin.username, "reopen", "payroll_run", run.id, f"{month:02d}/{year}")
    db.commit()
    return {
        "ok": True,
        "status": run.status.value,
        "message": (
            f"{exports.period_label(year, month)} is open again. Any payslips "
            "already handed out will no longer match until you finalise it."
        ),
    }


@router.get("/payslips.pdf")
def payslips_pdf(
    year: int = YEAR,
    month: int = MONTH,
    employee_id: int | None = None,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    payload = _load(db, year, month)
    rows = payload["rows"]
    if employee_id is not None:
        rows = [r for r in rows if r["employee_id"] == employee_id]
        if not rows:
            raise HTTPException(
                status_code=404, detail="No payroll line for that employee."
            )

    pdf = exports.payslip_pdf(rows, year, month)
    name = (
        f"payslip-{rows[0]['employee_code']}-{year}-{month:02d}.pdf"
        if employee_id is not None
        else f"payslips-{year}-{month:02d}.pdf"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@router.get("/export.xlsx")
def export_xlsx(
    year: int = YEAR,
    month: int = MONTH,
    _: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    payload = _load(db, year, month)
    return Response(
        content=exports.payroll_xlsx(payload),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="salary-{year}-{month:02d}.xlsx"'
            )
        },
    )
