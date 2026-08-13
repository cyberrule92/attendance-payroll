"""End-to-end tests through the HTTP surface.

Walks the path the real system takes: the owner signs in, pairs a kiosk, an
employee punches in and out with a photo, and the month is finalised into
payslips. Timestamps are taken relative to "now" and work dates are read back
from the responses, so the suite does not break when it happens to run across
the 05:00 work-day cutover.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.auth import hash_secret
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import AdminUser, Employee, Location, LocationKind

ADMIN_PASSWORD = "owner-password-123"


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    # Photos must land in the temp directory, not the real data folder.
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "photos").mkdir(exist_ok=True)

    db.add(
        AdminUser(
            username="owner",
            password_hash=hash_secret(ADMIN_PASSWORD),
            display_name="Owner",
        )
    )
    db.commit()

    app.dependency_overrides[get_db] = lambda: db
    # Instantiated without a context manager so the startup hook -- which would
    # create the real database -- never runs.
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


def jpeg_bytes(colour=(120, 140, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 1200), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_employee(db, name="Ramesh", code="EMP001", pin="1234", salary="15000"):
    location = db.query(Location).first()
    if location is None:
        location = Location(name="Factory", kind=LocationKind.FACTORY)
        db.add(location)
        db.commit()
    employee = Employee(
        code=code,
        name=name,
        phone=f"90000000{code[-2:]}",
        pin_hash=hash_secret(pin),
        location_id=location.id,
        monthly_salary=Decimal(salary),
        night_threshold_hours=5.0,
        weekly_off_dow=3,
        joined_on=date(2020, 1, 1),
    )
    db.add(employee)
    db.commit()
    return employee


def pair_kiosk(signed_in, db, location_id: int) -> str:
    created = signed_in.post(
        "/api/devices", json={"name": "Factory kiosk", "location_id": location_id}
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["id"]

    coded = signed_in.post(f"/api/devices/{device_id}/pairing-code")
    code = coded.json()["pairing_code"]
    assert code and len(code) == 6

    paired = signed_in.post("/api/kiosk/pair", json={"code": code})
    assert paired.status_code == 200, paired.text
    return paired.json()["device_token"]


# --- authentication ---------------------------------------------------------


def test_login_rejects_a_wrong_password(client):
    response = client.post(
        "/api/login", json={"username": "owner", "password": "wrong"}
    )
    assert response.status_code == 401


def test_admin_endpoints_require_a_session(client):
    assert client.get("/api/employees").status_code == 401
    assert client.get("/api/attendance/today").status_code == 401
    assert client.get("/api/payroll?year=2026&month=8").status_code == 401


def test_health_is_open(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --- kiosk pairing ----------------------------------------------------------


def test_kiosk_needs_a_valid_token(client, db):
    make_employee(db)
    assert client.get("/api/kiosk/employees").status_code == 401
    assert (
        client.get("/api/kiosk/employees", headers={"X-Device-Token": "9.nope"}).status_code
        == 401
    )


def test_pairing_code_is_single_use(signed_in, db):
    employee = make_employee(db)
    created = signed_in.post(
        "/api/devices", json={"name": "Kiosk", "location_id": employee.location_id}
    )
    device_id = created.json()["id"]
    code = signed_in.post(f"/api/devices/{device_id}/pairing-code").json()["pairing_code"]

    assert signed_in.post("/api/kiosk/pair", json={"code": code}).status_code == 200
    # Re-using it must fail -- the code is cleared once redeemed.
    assert signed_in.post("/api/kiosk/pair", json={"code": code}).status_code == 404


def test_unpairing_revokes_the_token(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    headers = {"X-Device-Token": token}
    assert signed_in.get("/api/kiosk/employees", headers=headers).status_code == 200

    device_id = int(token.split(".")[0])
    signed_in.post(f"/api/devices/{device_id}/unpair")
    assert signed_in.get("/api/kiosk/employees", headers=headers).status_code == 401


# --- punching ---------------------------------------------------------------


def punch(client, token, employee_id, pin, uuid, when):
    return client.post(
        "/api/kiosk/punch",
        headers={"X-Device-Token": token},
        data={
            "employee_id": str(employee_id),
            "pin": pin,
            "client_uuid": uuid,
            "captured_at": when.isoformat(),
        },
        files={"photo": ("selfie.jpg", jpeg_bytes(), "image/jpeg")},
    )


def test_full_punch_cycle(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)

    listing = signed_in.get("/api/kiosk/employees", headers={"X-Device-Token": token})
    assert listing.status_code == 200
    assert listing.json()[0]["next_direction"] == "IN"

    now = datetime.now(timezone.utc)
    first = punch(signed_in, token, employee.id, "1234", "uuid-in", now - timedelta(hours=3))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["direction"] == "IN"
    assert body["duplicate"] is False
    assert employee.name in body["message"]

    second = punch(signed_in, token, employee.id, "1234", "uuid-out", now - timedelta(hours=1))
    assert second.json()["direction"] == "OUT"
    assert second.json()["worked_label"] == "2:00"

    listing = signed_in.get("/api/kiosk/employees", headers={"X-Device-Token": token})
    assert listing.json()[0]["next_direction"] == "IN"  # back to IN after an OUT


def test_replayed_punch_is_not_double_counted(signed_in, db):
    """The offline queue retries uploads; a retry must not create a second punch."""
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)

    first = punch(signed_in, token, employee.id, "1234", "same-uuid", now - timedelta(hours=2))
    assert first.json()["duplicate"] is False

    replay = punch(signed_in, token, employee.id, "1234", "same-uuid", now - timedelta(hours=2))
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True

    work_date = first.json()["work_date"]
    board = signed_in.get(f"/api/attendance/today?on={work_date}").json()
    assert len(board["rows"][0]["punches"]) == 1


def test_wrong_pin_is_rejected(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    response = punch(
        signed_in, token, employee.id, "9999", "uuid-bad", datetime.now(timezone.utc)
    )
    assert response.status_code == 401
    assert "Wrong PIN" in response.json()["detail"]


def test_repeated_wrong_pins_lock_the_employee_out(signed_in, db):
    from app.auth import pin_throttle

    pin_throttle._state.clear()
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)

    for index in range(settings.pin_max_attempts):
        punch(signed_in, token, employee.id, "0000", f"bad-{index}", datetime.now(timezone.utc))

    blocked = punch(
        signed_in, token, employee.id, "1234", "good", datetime.now(timezone.utc)
    )
    assert blocked.status_code == 429
    pin_throttle._state.clear()


def test_a_kiosk_cannot_punch_another_locations_employee(signed_in, db):
    employee = make_employee(db)
    other = Location(name="Store 9", kind=LocationKind.STORE)
    db.add(other)
    db.commit()

    token = pair_kiosk(signed_in, db, other.id)
    response = punch(
        signed_in, token, employee.id, "1234", "cross", datetime.now(timezone.utc)
    )
    assert response.status_code == 403


def test_a_device_clock_running_ahead_is_refused(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    response = punch(
        signed_in,
        token,
        employee.id,
        "1234",
        "future",
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert response.status_code == 400
    assert "clock" in response.json()["detail"]


def test_photos_are_stored_and_need_a_session(signed_in, db, client):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)
    result = punch(signed_in, token, employee.id, "1234", "photo-uuid", now - timedelta(hours=1))
    work_date = result.json()["work_date"]

    board = signed_in.get(f"/api/attendance/today?on={work_date}").json()
    photo_url = board["rows"][0]["punches"][0]["photo_url"]
    assert photo_url

    served = signed_in.get(photo_url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    # Downscaled from 900px on upload.
    assert Image.open(io.BytesIO(served.content)).width == settings.photo_max_width

    signed_in.post("/api/logout")
    assert signed_in.get(photo_url).status_code == 401


def test_photo_path_cannot_escape_the_photo_directory(signed_in):
    response = signed_in.get("/api/photo/../../../etc/passwd")
    assert response.status_code in (403, 404)


# --- corrections ------------------------------------------------------------


def test_admin_can_correct_a_forgotten_punch_out(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)

    result = punch(signed_in, token, employee.id, "1234", "only-in", now - timedelta(hours=6))
    work_date = result.json()["work_date"]

    detail = signed_in.get(
        f"/api/attendance/day?employee_id={employee.id}&work_date={work_date}"
    ).json()
    assert detail["needs_review"] is True

    fixed = signed_in.post(
        f"/api/attendance/day/{employee.id}/{work_date}",
        json={"worked_minutes": 660, "note": "Confirmed with supervisor"},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["status"] in ("FULL", "WEEKOFF")


def test_correction_requires_a_note(signed_in, db):
    employee = make_employee(db)
    response = signed_in.post(
        f"/api/attendance/day/{employee.id}/2026-08-12",
        json={"worked_minutes": 600},
    )
    assert response.status_code == 422


def test_voiding_a_punch_updates_the_day(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)

    punch(signed_in, token, employee.id, "1234", "v-in", now - timedelta(hours=4))
    out = punch(signed_in, token, employee.id, "1234", "v-out", now - timedelta(hours=3))
    work_date = out.json()["work_date"]

    board = signed_in.get(f"/api/attendance/today?on={work_date}").json()
    punch_id = board["rows"][0]["punches"][-1]["id"]

    voided = signed_in.post(
        f"/api/attendance/punch/{punch_id}/void", json={"reason": "Tapped twice"}
    )
    assert voided.status_code == 200

    detail = signed_in.get(
        f"/api/attendance/day?employee_id={employee.id}&work_date={work_date}"
    ).json()
    assert detail["needs_review"] is True  # only an IN remains


# --- payroll ----------------------------------------------------------------


def test_payroll_preview_finalize_and_exports(signed_in, db):
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)

    result = punch(signed_in, token, employee.id, "1234", "p-in", now - timedelta(hours=9))
    punch(signed_in, token, employee.id, "1234", "p-out", now - timedelta(minutes=15))
    work_date = date.fromisoformat(result.json()["work_date"])
    year, month = work_date.year, work_date.month

    signed_in.post(
        "/api/advances",
        json={
            "employee_id": employee.id,
            "advance_date": work_date.isoformat(),
            "amount": "2000",
            "note": "Festival advance",
        },
    )

    preview = signed_in.get(f"/api/payroll?year={year}&month={month}")
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["status"] == "NONE"

    row = next(r for r in payload["rows"] if r["employee_id"] == employee.id)
    assert row["advances_deducted"] == "2000.00"
    assert Decimal(row["net_payable"]) == Decimal(row["grand_total"]) - Decimal("2000")
    assert row["breakdown"], "the day-by-day derivation must be present"

    finalized = signed_in.post(f"/api/payroll/finalize?year={year}&month={month}")
    assert finalized.status_code == 200, finalized.text

    # A second finalise must refuse rather than quietly overwrite.
    assert (
        signed_in.post(f"/api/payroll/finalize?year={year}&month={month}").status_code
        == 409
    )

    frozen = signed_in.get(f"/api/payroll?year={year}&month={month}").json()
    assert frozen["status"] == "FINAL"
    frozen_row = next(r for r in frozen["rows"] if r["employee_id"] == employee.id)
    assert frozen_row["net_payable"] == row["net_payable"]

    pdf = signed_in.get(
        f"/api/payroll/payslips.pdf?year={year}&month={month}&employee_id={employee.id}"
    )
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    xlsx = signed_in.get(f"/api/payroll/export.xlsx?year={year}&month={month}")
    assert xlsx.status_code == 200
    assert xlsx.content.startswith(b"PK")  # a zip, which is what xlsx is


def test_finalized_payroll_ignores_later_attendance_edits(signed_in, db):
    """The whole point of freezing: a handed-out payslip must not change."""
    employee = make_employee(db)
    token = pair_kiosk(signed_in, db, employee.location_id)
    now = datetime.now(timezone.utc)

    result = punch(signed_in, token, employee.id, "1234", "f-in", now - timedelta(hours=9))
    punch(signed_in, token, employee.id, "1234", "f-out", now - timedelta(minutes=10))
    work_date = date.fromisoformat(result.json()["work_date"])
    year, month = work_date.year, work_date.month

    signed_in.post(f"/api/payroll/finalize?year={year}&month={month}")
    before = signed_in.get(f"/api/payroll?year={year}&month={month}").json()
    before_net = next(
        r["net_payable"] for r in before["rows"] if r["employee_id"] == employee.id
    )

    # Now retrospectively grant a big chunk of overtime on that day.
    signed_in.post(
        f"/api/attendance/day/{employee.id}/{work_date.isoformat()}",
        json={"worked_minutes": 900, "note": "Late correction after payday"},
    )

    after = signed_in.get(f"/api/payroll?year={year}&month={month}").json()
    after_net = next(
        r["net_payable"] for r in after["rows"] if r["employee_id"] == employee.id
    )
    assert after_net == before_net

    # Reopening puts the month back in draft and picks the correction up.
    signed_in.post(f"/api/payroll/reopen?year={year}&month={month}")
    reopened = signed_in.get(f"/api/payroll?year={year}&month={month}").json()
    assert reopened["status"] == "DRAFT"
    reopened_net = next(
        r["net_payable"] for r in reopened["rows"] if r["employee_id"] == employee.id
    )
    assert Decimal(reopened_net) > Decimal(before_net)  # the extra OT now counts


# --- employees, leaves, advances -------------------------------------------


def test_employee_crud_and_pin_reset(signed_in, db):
    location = db.query(Location).first() or Location(name="Factory")
    if location.id is None:
        db.add(location)
        db.commit()

    created = signed_in.post(
        "/api/employees",
        json={
            "code": "EMP010",
            "name": "Sita",
            "phone": "9111111111",
            "location_id": location.id,
            "monthly_salary": "12000",
            "night_threshold_hours": 4.0,
            "ot_adjustment_per_night_hours": 1.5,
            "weekly_off_dow": 3,
            "pin": "4321",
        },
    )
    assert created.status_code == 201, created.text
    employee_id = created.json()["id"]
    assert created.json()["night_threshold_hours"] == 4.0

    assert (
        signed_in.post(
            "/api/employees",
            json={
                "code": "EMP010",
                "name": "Duplicate",
                "location_id": location.id,
                "monthly_salary": "1000",
                "pin": "1111",
            },
        ).status_code
        == 409
    )

    assert (
        signed_in.post(f"/api/employees/{employee_id}/pin", json={"pin": "12"}).status_code
        == 422
    )
    assert (
        signed_in.post(f"/api/employees/{employee_id}/pin", json={"pin": "8888"}).status_code
        == 200
    )

    signed_in.delete(f"/api/employees/{employee_id}")
    assert all(e["id"] != employee_id for e in signed_in.get("/api/employees").json())


def test_leave_range_creates_one_row_per_day(signed_in, db):
    employee = make_employee(db)
    response = signed_in.post(
        "/api/leaves",
        json={
            "employee_id": employee.id,
            "start_date": "2026-08-10",
            "end_date": "2026-08-12",
            "leave_type": "PAID",
            "reason": "Festival",
        },
    )
    assert response.status_code == 201
    assert response.json()["created"] == 3

    leaves = signed_in.get("/api/leaves?year=2026&month=8").json()
    assert len(leaves) == 3


def test_advance_summary_totals_by_employee(signed_in, db):
    employee = make_employee(db)
    for amount in ("1000", "500"):
        signed_in.post(
            "/api/advances",
            json={
                "employee_id": employee.id,
                "advance_date": "2026-08-05",
                "amount": amount,
            },
        )
    summary = signed_in.get("/api/advances/summary?year=2026&month=8").json()
    assert summary["rows"][0]["total"] == "1500.00"
    assert summary["rows"][0]["count"] == 2
