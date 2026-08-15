"""Tests for the anti-proxy face check.

Three layers, because they fail in different ways:

* The maths -- similarity, storage, and the liveness scores. Run against the
  real OpenCV models with synthetic frames, so a change in how a photo is told
  apart from a person is caught here.
* The decision -- which outcome ``verify`` returns for each situation. The
  model calls are stubbed, so these assert the security logic itself rather
  than the accuracy of a neural network.
* The flow -- what the punch endpoint actually does with each outcome: when it
  offers a retry, when it records and flags, and when it must not do either.

The question the whole feature exists to answer is
``test_a_colleague_cannot_punch_for_someone_else``.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.auth import hash_secret
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import (
    AdminUser,
    Employee,
    FaceCheck,
    FaceEnrollment,
    Location,
    LocationKind,
    Punch,
)
from app.services import face, face_store

ADMIN_PASSWORD = "owner-password-123"


# --- helpers -----------------------------------------------------------------


def unit(*values: float) -> np.ndarray:
    """A normalised 128-dimension embedding, padded from the values given."""
    vector = np.zeros(face.EMBEDDING_DIMS, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


def synthetic_face(x=200, y=150, size=120) -> face.Face:
    """A Face with plausible geometry, for exercising the crop and score maths."""
    row = np.array(
        [
            x, y, size, size,
            x + 35, y + 40,  # right eye
            x + 85, y + 40,  # left eye
            x + 60, y + 70,  # nose
            x + 40, y + 100,  # right mouth
            x + 80, y + 100,  # left mouth
            0.99,
        ],
        dtype=np.float32,
    )
    return face.Face(row=row, score=0.99)


def noise_image(seed: int, shape=(480, 640, 3)) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, shape, dtype=np.uint8)


def jpeg_bytes(colour=(120, 140, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 1200), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


needs_models = pytest.mark.skipif(
    not face.models_installed(),
    reason="face models not installed -- run scripts/fetch_face_models.py",
)


# --- the maths ---------------------------------------------------------------


def test_identical_embeddings_are_a_perfect_match():
    vector = unit(1, 2, 3)
    assert face.similarity(vector, vector) == pytest.approx(1.0)


def test_unrelated_embeddings_score_near_zero():
    assert face.similarity(unit(1, 0), unit(0, 1)) == pytest.approx(0.0)


def test_embedding_survives_a_storage_round_trip():
    vector = unit(0.3, -0.7, 0.1, 0.9)
    restored = face.unpack_embedding(face.pack_embedding(vector))
    assert np.array_equal(vector, restored)


def test_a_truncated_embedding_is_rejected_rather_than_used():
    with pytest.raises(ValueError):
        face.unpack_embedding(b"\x00\x01\x02\x03")


def test_best_match_takes_the_closest_reference_not_the_average():
    probe = unit(1, 0)
    # One good reference among poor ones must still let the employee through.
    references = [unit(0, 1), unit(0, 0, 1), unit(1, 0)]
    assert face.best_match(probe, references) == pytest.approx(1.0)


def test_no_references_cannot_accidentally_match():
    assert face.best_match(unit(1, 0), []) == -1.0


# --- liveness maths, against the real models --------------------------------


@needs_models
def test_a_still_photo_produces_no_motion():
    """Two identical frames are what a printed photo looks like."""
    frame = noise_image(0)
    detected = synthetic_face()
    score = face.motion_score([frame, frame.copy()], [detected, detected])
    assert score == pytest.approx(0.0, abs=1e-6)
    assert score < settings.face_motion_threshold


@needs_models
def test_a_changing_face_produces_motion():
    detected = synthetic_face()
    score = face.motion_score([noise_image(1), noise_image(2)], [detected, detected])
    assert score > settings.face_motion_threshold


def test_a_phone_held_up_moves_with_its_background():
    """A rigid object gives the face and the background the same motion.

    This is the signal that separates a real head from a picture of one being
    waved at the camera: a person's face moves, the shop behind them does not.
    """
    base = noise_image(3)
    shifted = np.roll(base, 6, axis=1)  # whole scene translates together
    detected = synthetic_face()
    assert face.parallax_score([base, shifted], [detected, detected]) < settings.face_parallax_threshold


def test_a_real_head_moves_against_a_still_background():
    base = noise_image(4)
    moved = base.copy()
    detected = synthetic_face()
    x, y, w, h = detected.box
    moved[y : y + h, x : x + w] = noise_image(5)[y : y + h, x : x + w]
    assert face.parallax_score([base, moved], [detected, detected]) > settings.face_parallax_threshold


def test_liveness_needs_more_than_one_frame():
    report = face.assess_liveness([noise_image(6)], [synthetic_face()])
    assert not report.passed
    assert "frames" in report.reason


# --- the decision ------------------------------------------------------------


@pytest.fixture()
def stub_engine(monkeypatch):
    """Drive ``verify`` without depending on a real camera or real faces.

    Returns a setter so each test states exactly what the camera "saw": which
    embedding came back, whether a face was found, and whether it looked live.
    """

    state = {"embedding": unit(1, 0), "found": True, "live": True}

    monkeypatch.setattr(face, "models_installed", lambda: True)
    monkeypatch.setattr(
        face, "primary_face", lambda image: synthetic_face() if state["found"] else None
    )
    monkeypatch.setattr(face, "embed", lambda image, detected: state["embedding"])
    monkeypatch.setattr(
        face,
        "assess_liveness",
        lambda frames, faces: face.LivenessReport(
            passed=state["live"],
            motion_score=0.5 if state["live"] else 0.0,
            reason="" if state["live"] else "This looks like a photo or a screen.",
        ),
    )
    monkeypatch.setattr(face, "decode_image", lambda raw: noise_image(7))
    return state


FRAMES = [b"frame-a", b"frame-b", b"frame-c"]


def test_the_right_person_is_verified(stub_engine):
    result = face.verify(FRAMES, [unit(1, 0)])
    assert result.outcome == face.OUTCOME_VERIFIED
    assert result.verified
    assert result.score == pytest.approx(1.0)


def test_a_colleague_cannot_punch_for_someone_else(stub_engine):
    """The whole point of the feature.

    The PIN was right -- that is checked before this ever runs -- but the face
    in front of the camera belongs to somebody else, so the punch is not
    confirmed.
    """
    stub_engine["embedding"] = unit(0, 1)  # a different person
    result = face.verify(FRAMES, [unit(1, 0)])
    assert result.outcome == face.OUTCOME_MISMATCH
    assert not result.verified
    assert result.score < settings.face_match_threshold


def test_a_face_just_over_the_line_is_accepted(stub_engine):
    """Guards the threshold itself, so tuning it cannot silently invert."""
    angle = settings.face_match_threshold + 0.05
    stub_engine["embedding"] = unit(angle, (1 - angle**2) ** 0.5)
    assert face.verify(FRAMES, [unit(1, 0)]).outcome == face.OUTCOME_VERIFIED


def test_a_face_just_under_the_line_is_refused(stub_engine):
    angle = settings.face_match_threshold - 0.05
    stub_engine["embedding"] = unit(angle, (1 - angle**2) ** 0.5)
    assert face.verify(FRAMES, [unit(1, 0)]).outcome == face.OUTCOME_MISMATCH


def test_a_photo_of_the_right_person_is_still_refused(stub_engine):
    """Liveness is judged before identity, so a perfect likeness cannot pass."""
    stub_engine["live"] = False
    result = face.verify(FRAMES, [unit(1, 0)])  # embedding would match exactly
    assert result.outcome == face.OUTCOME_NOT_LIVE
    assert not result.verified


def test_nothing_in_front_of_the_camera(stub_engine):
    stub_engine["found"] = False
    assert face.verify(FRAMES, [unit(1, 0)]).outcome == face.OUTCOME_NO_FACE


def test_an_unenrolled_employee_is_reported_not_refused(stub_engine):
    """Switching this on must not lock out staff nobody has enrolled yet."""
    result = face.verify(FRAMES, [])
    assert result.outcome == face.OUTCOME_NOT_ENROLLED
    assert not result.verified


def test_missing_models_report_unavailable(monkeypatch):
    monkeypatch.setattr(face, "models_installed", lambda: False)
    assert face.verify(FRAMES, [unit(1, 0)]).outcome == face.OUTCOME_UNAVAILABLE


def test_only_a_real_failure_counts_as_suspicious():
    """NOT_ENROLLED and UNAVAILABLE are admin gaps, not proxy signals."""
    suspicious = {FaceCheck.MISMATCH, FaceCheck.NOT_LIVE, FaceCheck.NO_FACE}
    for status in FaceCheck:
        punch = Punch(face_status=status)
        assert punch.face_suspect is (status in suspicious), status


# --- the flow through the punch endpoint ------------------------------------


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    # Photos must land in the temp directory, not the real data folder. That
    # moves the models directory too, since both hang off data_dir -- so the
    # real one is pinned back, or every test here would silently skip the
    # face check it is supposed to be exercising.
    real_models = settings.face_models_dir
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(face, "models_dir", lambda: real_models)
    (tmp_path / "photos").mkdir(exist_ok=True)

    db.add(
        AdminUser(
            username="owner", password_hash=hash_secret(ADMIN_PASSWORD), display_name="Owner"
        )
    )
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def signed_in(client):
    response = client.post(
        "/api/login", json={"username": "owner", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def kiosk(signed_in, db):
    """A paired kiosk and an employee with a face on file."""
    location = Location(name="Factory", kind=LocationKind.FACTORY)
    db.add(location)
    db.commit()

    employee = Employee(
        code="EMP001",
        name="Ramesh",
        phone="9000000001",
        pin_hash=hash_secret("1234"),
        location_id=location.id,
        monthly_salary=Decimal("15000"),
        night_threshold_hours=5.0,
        weekly_off_dow=3,
        joined_on=date(2020, 1, 1),
    )
    db.add(employee)
    db.commit()

    db.add(
        FaceEnrollment(
            employee_id=employee.id, embedding=face.pack_embedding(unit(1, 0))
        )
    )
    db.commit()

    created = signed_in.post(
        "/api/devices", json={"name": "Factory kiosk", "location_id": location.id}
    )
    device_id = created.json()["id"]
    code = signed_in.post(f"/api/devices/{device_id}/pairing-code").json()["pairing_code"]
    token = signed_in.post("/api/kiosk/pair", json={"code": code}).json()["device_token"]

    return {"client": signed_in, "token": token, "employee": employee, "db": db}


def force_outcome(monkeypatch, outcome: str, score: float = 0.0):
    """Make every check return one fixed verdict."""
    monkeypatch.setattr(
        face_store,
        "verify_frames",
        lambda db, employee_id, frames: face.VerificationResult(
            outcome=outcome, score=score, message="stubbed"
        ),
    )


def punch(kiosk, *, uuid="punch-1", attempt=1, deferred=False, frames=3):
    """Punch the way a current kiosk does: a burst, not a single photo."""
    return kiosk["client"].post(
        "/api/kiosk/punch",
        headers={"X-Device-Token": kiosk["token"]},
        data={
            "employee_id": kiosk["employee"].id,
            "pin": "1234",
            "client_uuid": uuid,
            "attempt": attempt,
            "deferred": str(deferred).lower(),
        },
        files=[
            ("frames", (f"f{index}.jpg", jpeg_bytes((120 + index * 8, 140, 160)), "image/jpeg"))
            for index in range(frames)
        ],
    )


def test_a_verified_punch_is_recorded_and_marked(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_VERIFIED, score=0.72)
    body = punch(kiosk).json()

    assert body["accepted"] is True
    assert body["face_verified"] is True
    assert body["face_status"] == "VERIFIED"
    assert body["warning"] is None

    stored = kiosk["db"].query(Punch).one()
    assert stored.face_status is FaceCheck.VERIFIED
    assert stored.face_score == pytest.approx(0.72)


def test_a_mismatch_asks_for_another_try_without_recording_anything(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH)
    body = punch(kiosk, attempt=1).json()

    assert body["accepted"] is False
    assert body["retry"] is True
    assert body["attempts_left"] == settings.face_max_attempts - 1
    # Nothing may be written while a retry is still on offer, or one person
    # standing at the kiosk would generate three punches.
    assert kiosk["db"].query(Punch).count() == 0


def test_a_proxy_punch_is_recorded_and_flagged_once_the_retries_run_out(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH, score=0.11)
    body = punch(kiosk, attempt=settings.face_max_attempts).json()

    # Recorded, so the employee is never left unable to clock in...
    assert body["accepted"] is True
    assert body["face_verified"] is False
    # ...but plainly marked, on the ticket and in the record.
    assert body["warning"]
    assert body["face_status"] == "MISMATCH"

    stored = kiosk["db"].query(Punch).one()
    assert stored.face_status is FaceCheck.MISMATCH
    assert stored.face_suspect is True
    assert stored.face_attempts == settings.face_max_attempts
    assert "did not match" in (stored.note or "")


def test_a_flagged_punch_puts_the_day_in_the_review_queue(kiosk, monkeypatch):
    """A complete day -- in and out -- so no timing problem masks the reason.

    A missed punch out is reported in preference to the face concern, because
    that is the one that changes the hours; this asserts the face reason is
    what surfaces when the timings are sound.
    """
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH)
    assert punch(kiosk, uuid="in-1", attempt=settings.face_max_attempts).status_code == 200
    assert punch(kiosk, uuid="out-1", attempt=settings.face_max_attempts).status_code == 200

    queue = kiosk["client"].get("/api/attendance/review").json()
    assert queue["count"] == 1
    assert "Face not confirmed" in queue["rows"][0]["reason"]
    assert "mismatch" in queue["rows"][0]["reason"]


def test_a_missed_punch_out_is_reported_ahead_of_the_face_concern(kiosk, monkeypatch):
    """The timing problem is the one that changes the hours, so it wins."""
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH)
    punch(kiosk, uuid="in-only", attempt=settings.face_max_attempts)

    queue = kiosk["client"].get("/api/attendance/review").json()
    assert "never punched out" in queue["rows"][0]["reason"]


def test_a_queued_punch_is_never_asked_to_retry(kiosk, monkeypatch):
    """Uploaded from the offline queue: nobody is there to face the camera."""
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH)
    body = punch(kiosk, attempt=1, deferred=True).json()

    assert body["retry"] is False
    assert body["accepted"] is True
    assert kiosk["db"].query(Punch).one().face_status is FaceCheck.MISMATCH


def test_an_old_kiosk_sending_one_photo_is_never_asked_to_retry(kiosk, monkeypatch):
    """A tablet still running the pre-liveness page.

    It can only ever send a single frame, so liveness can never pass and a
    retry would loop forever. Worse, the old page treats any 200 as success --
    so a retry would show the employee a ticket for a punch that was never
    written. The punch is recorded and flagged instead.
    """
    force_outcome(monkeypatch, face.OUTCOME_NOT_LIVE)
    response = kiosk["client"].post(
        "/api/kiosk/punch",
        headers={"X-Device-Token": kiosk["token"]},
        data={
            "employee_id": kiosk["employee"].id,
            "pin": "1234",
            "client_uuid": "legacy-1",
            "attempt": 1,
        },
        # The old field name, one image, no burst.
        files=[("photo", ("punch.jpg", jpeg_bytes(), "image/jpeg"))],
    )
    body = response.json()

    assert body["retry"] is False
    assert body["accepted"] is True
    stored = kiosk["db"].query(Punch).one()
    assert stored.face_status is FaceCheck.NOT_LIVE
    assert stored.photo_path, "the photo must still be kept as evidence"


def test_strict_mode_refuses_instead_of_flagging(kiosk, monkeypatch):
    monkeypatch.setattr(settings, "face_strict", True)
    force_outcome(monkeypatch, face.OUTCOME_MISMATCH)

    response = punch(kiosk, attempt=settings.face_max_attempts)
    assert response.status_code == 403
    assert kiosk["db"].query(Punch).count() == 0


def test_an_unenrolled_employee_can_still_clock_in(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_NOT_ENROLLED)
    body = punch(kiosk).json()

    assert body["accepted"] is True
    stored = kiosk["db"].query(Punch).one()
    assert stored.face_status is FaceCheck.NOT_ENROLLED
    # Not an accusation -- nobody has enrolled them yet.
    assert stored.face_suspect is False


def test_a_replayed_punch_id_does_not_re_run_the_check(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_VERIFIED, score=0.8)
    first = punch(kiosk, uuid="same-id").json()
    second = punch(kiosk, uuid="same-id").json()

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert kiosk["db"].query(Punch).count() == 1


def test_the_pin_is_still_required_before_the_face_is_looked_at(kiosk, monkeypatch):
    force_outcome(monkeypatch, face.OUTCOME_VERIFIED, score=0.9)
    response = kiosk["client"].post(
        "/api/kiosk/punch",
        headers={"X-Device-Token": kiosk["token"]},
        data={
            "employee_id": kiosk["employee"].id,
            "pin": "9999",
            "client_uuid": "wrong-pin",
            "attempt": 1,
        },
        files=[("frames", ("f0.jpg", jpeg_bytes(), "image/jpeg"))],
    )
    assert response.status_code == 401
    assert kiosk["db"].query(Punch).count() == 0


# --- enrolment ---------------------------------------------------------------


def test_enrolling_a_photo_with_no_face_is_refused(kiosk):
    """A flat colour has no face in it, and the real detector says so."""
    if not face.models_installed():
        pytest.skip("face models not installed")

    response = kiosk["client"].post(
        f"/api/employees/{kiosk['employee'].id}/faces",
        files=[("photos", ("blank.jpg", jpeg_bytes(), "image/jpeg"))],
    )
    assert response.status_code == 400
    assert "no face" in response.text.lower()


def test_face_status_reports_what_is_on_file(kiosk):
    body = kiosk["client"].get(f"/api/employees/{kiosk['employee'].id}/faces").json()
    assert body["enrolled"] is True
    assert body["count"] == 1
    assert body["max_allowed"] == settings.face_max_enrollments


def test_removing_a_face_leaves_the_employee_unenrolled(kiosk):
    employee_id = kiosk["employee"].id
    listed = kiosk["client"].get(f"/api/employees/{employee_id}/faces").json()
    enrollment_id = listed["enrollments"][0]["id"]

    body = kiosk["client"].delete(
        f"/api/employees/{employee_id}/faces/{enrollment_id}"
    ).json()
    assert body["enrolled"] is False
    assert body["count"] == 0


def test_a_face_cannot_be_removed_through_another_employees_id(kiosk, db):
    """Ownership is checked before anything is deleted."""
    other = Employee(
        code="EMP002",
        name="Sita",
        pin_hash=hash_secret("4321"),
        location_id=kiosk["employee"].location_id,
        monthly_salary=Decimal("12000"),
        joined_on=date(2020, 1, 1),
    )
    db.add(other)
    db.commit()

    listed = kiosk["client"].get(f"/api/employees/{kiosk['employee'].id}/faces").json()
    enrollment_id = listed["enrollments"][0]["id"]

    response = kiosk["client"].delete(f"/api/employees/{other.id}/faces/{enrollment_id}")
    assert response.status_code == 404
    assert db.query(FaceEnrollment).count() == 1


def test_the_employee_list_shows_who_is_not_set_up(kiosk, db):
    db.add(
        Employee(
            code="EMP003",
            name="Shama",
            pin_hash=hash_secret("1111"),
            location_id=kiosk["employee"].location_id,
            monthly_salary=Decimal("12000"),
            joined_on=date(2020, 1, 1),
        )
    )
    db.commit()

    rows = kiosk["client"].get("/api/employees").json()
    by_name = {row["name"]: row["face_enrolled"] for row in rows}
    assert by_name["Ramesh"] is True
    assert by_name["Shama"] is False
