"""Database glue for face verification.

``services/face.py`` is deliberately free of the database so the matching and
liveness logic can be tested directly. This module is the other half: it loads
an employee's enrolled references, records what a check decided, and manages
enrolment rows. Same split as ``payroll.py`` and ``payroll_run.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Employee, FaceCheck, FaceEnrollment, Punch
from . import face, photos

logger = logging.getLogger(__name__)

# How a failed check maps onto the punch record.
_OUTCOME_TO_STATUS = {
    face.OUTCOME_VERIFIED: FaceCheck.VERIFIED,
    face.OUTCOME_MISMATCH: FaceCheck.MISMATCH,
    face.OUTCOME_NO_FACE: FaceCheck.NO_FACE,
    face.OUTCOME_NOT_LIVE: FaceCheck.NOT_LIVE,
    face.OUTCOME_NOT_ENROLLED: FaceCheck.NOT_ENROLLED,
    face.OUTCOME_UNAVAILABLE: FaceCheck.UNAVAILABLE,
}


def status_for(result: face.VerificationResult) -> FaceCheck:
    return _OUTCOME_TO_STATUS.get(result.outcome, FaceCheck.UNCHECKED)


# --- references --------------------------------------------------------------


def enrollments_for(db: Session, employee_id: int) -> list[FaceEnrollment]:
    return list(
        db.scalars(
            select(FaceEnrollment)
            .where(FaceEnrollment.employee_id == employee_id)
            .order_by(FaceEnrollment.id)
        )
    )


def references_for(db: Session, employee_id: int) -> list[np.ndarray]:
    """The embeddings to match a punch against.

    A row whose blob will not unpack is skipped rather than fatal: one corrupt
    reference must not stop somebody clocking in for the rest of the month.
    """
    references: list[np.ndarray] = []
    for row in enrollments_for(db, employee_id):
        try:
            references.append(face.unpack_embedding(row.embedding))
        except ValueError:
            logger.warning(
                "Skipping unreadable face enrolment %s for employee %s",
                row.id,
                employee_id,
            )
    return references


def is_enrolled(db: Session, employee_id: int) -> bool:
    return bool(enrollments_for(db, employee_id))


# --- running a check ---------------------------------------------------------


def verify_frames(
    db: Session, employee_id: int, frames: list[bytes]
) -> face.VerificationResult:
    """Check captured frames against one employee's enrolled faces."""
    if not settings.face_enabled:
        return face.VerificationResult(
            outcome=face.OUTCOME_UNAVAILABLE, message="Face checking is switched off."
        )
    return face.verify(frames, references_for(db, employee_id))


def apply_result(punch: Punch, result: face.VerificationResult, attempts: int) -> None:
    """Write the outcome of a check onto the punch row."""
    punch.face_status = status_for(result)
    punch.face_score = round(result.score, 4) if result.score else None
    punch.face_attempts = attempts
    if result.liveness is not None:
        # The trained model's opinion when there is one, otherwise the motion
        # evidence -- whichever actually drove the decision.
        punch.liveness_score = round(
            result.liveness.antispoof_score
            if result.liveness.antispoof_score is not None
            else max(result.liveness.motion_score, result.liveness.parallax_score),
            4,
        )


def review_note(result: face.VerificationResult) -> str | None:
    """A short line for the review queue explaining what the camera saw."""
    if result.outcome == face.OUTCOME_MISMATCH:
        return f"Face did not match the employee on file (score {result.score:.2f})"
    if result.outcome == face.OUTCOME_NOT_LIVE:
        return "Capture looked like a photo or a screen, not a live person"
    if result.outcome == face.OUTCOME_NO_FACE:
        return "No face was visible in the capture"
    if result.outcome == face.OUTCOME_NOT_ENROLLED:
        return "No reference face on file for this employee"
    if result.outcome == face.OUTCOME_UNAVAILABLE:
        return "Face checking was unavailable when this punch was recorded"
    return None


# --- enrolment ---------------------------------------------------------------


def enroll(
    db: Session, employee: Employee, raw: bytes, actor: str
) -> tuple[bool, str, FaceEnrollment | None]:
    """Add one reference face for an employee.

    Returns ``(ok, message, row)``. Failures are returned rather than raised so
    the caller can show the admin exactly which photo was rejected and why.
    """
    existing_rows = enrollments_for(db, employee.id)
    if len(existing_rows) >= settings.face_max_enrollments:
        return (
            False,
            f"{employee.name} already has {len(existing_rows)} photos on file "
            f"(the maximum is {settings.face_max_enrollments}). Remove one first.",
            None,
        )

    existing = references_for(db, employee.id)
    result = face.enroll_from_image(raw, existing)
    if not result.ok or result.embedding is None:
        return False, result.message, None

    photo_path = photos.save_face_photo(
        raw, employee.id, f"ref-{datetime.now(timezone.utc).timestamp():.0f}"
    )

    row = FaceEnrollment(
        employee_id=employee.id,
        embedding=face.pack_embedding(result.embedding),
        photo_path=photo_path,
        added_by=actor,
    )
    db.add(row)
    db.flush()
    return True, f"Face added for {employee.name}.", row


def remove(db: Session, enrollment_id: int) -> FaceEnrollment | None:
    row = db.get(FaceEnrollment, enrollment_id)
    if row is None:
        return None
    photos.delete_photo(row.photo_path)
    db.delete(row)
    return row
