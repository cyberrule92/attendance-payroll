"""Tests for the staff portal.

Employees sign in from their own phones, over the internet, to read their pay.
That makes the interesting tests the ones about what they *cannot* do:

* read another employee's hours or pay -- ``test_an_employee_only_ever_sees_their_own_figures``
* get in with the kiosk PIN that everyone at the counter has seen them type
* keep a session after the office revokes their access
* cross the line between the staff session and the admin one

The rest covers the promise made on the screen: a finalised month matches the
payslip exactly, and anything unfinalised is labelled an estimate.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_secret, portal_throttle
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import (
    AdminUser,
    AttendanceDay,
    DayStatus,
    Employee,
    Location,
    LocationKind,
    Punch,
    PunchDirection,
)

ADMIN_PASSWORD = "owner-password-123"
KIOSK_PIN = "1234"
PORTAL_PIN = "654321"


@pytest.fixture(autouse=True)
def clear_throttle():
    """The lockout is process-wide, so one test must not leak into the next."""
    portal_throttle._state.clear()
    yield
    portal_throttle._state.clear()


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
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


def make_staff(db, name, code, phone, salary="15000", portal_pin=PORTAL_PIN):
    location = db.query(Location).first()
    if location is None:
        location = Location(name="Factory", kind=LocationKind.FACTORY)
        db.add(location)
        db.commit()

    employee = Employee(
        code=code,
        name=name,
        phone=phone,
        pin_hash=hash_secret(KIOSK_PIN),
        location_id=location.id,
        monthly_salary=Decimal(salary),
        night_threshold_hours=5.0,
        weekly_off_dow=3,
        joined_on=date(2020, 1, 1),
        portal_pin_hash=hash_secret(portal_pin) if portal_pin else None,
    )
    db.add(employee)
    db.commit()
    return employee


@pytest.fixture()
def staff(client, db):
    """Two employees, so isolation can actually be tested."""
    ramesh = make_staff(db, "Ramesh", "EMP001", "9000000001", salary="15000")
    sita = make_staff(db, "Sita", "EMP002", "9000000002", salary="21000")
    return {"client": client, "db": db, "ramesh": ramesh, "sita": sita}


def sign_in(client, phone="9000000001", secret=PORTAL_PIN):
    return client.post("/api/portal/login", json={"phone": phone, "secret": secret})


def worked_day(db, employee, when: date, minutes: int, status=DayStatus.FULL, ot=0):
    """A finished day, written the way a recompute would leave it."""
    start = datetime(when.year, when.month, when.day, 9, 0)
    db.add(
        AttendanceDay(
            employee_id=employee.id,
            work_date=when,
            first_in=start,
            last_out=start + timedelta(minutes=minutes),
            worked_minutes=minutes,
            punch_count=2,
            status=status,
            ot_minutes=ot,
        )
    )
    db.commit()


# --- signing in --------------------------------------------------------------


def test_an_employee_can_sign_in_with_their_portal_pin(staff):
    response = sign_in(staff["client"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Ramesh"
    assert body["code"] == "EMP001"
    assert body["uses_own_password"] is False


def test_the_kiosk_pin_does_not_open_the_portal(staff):
    """The whole reason the portal PIN is separate.

    The kiosk PIN is typed in the open on a shared tablet dozens of times a
    day. If it also opened the portal, watching a colleague clock in would be
    enough to read their salary.
    """
    response = sign_in(staff["client"], secret=KIOSK_PIN)
    assert response.status_code == 401


def test_an_unknown_phone_number_is_refused_the_same_way_as_a_wrong_pin(staff):
    """Otherwise anyone could test which numbers belong to staff here."""
    unknown = sign_in(staff["client"], phone="9999999999", secret=PORTAL_PIN)
    wrong = sign_in(staff["client"], secret="000000")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_an_employee_without_portal_access_cannot_sign_in(client, db):
    make_staff(db, "Shama", "EMP009", "9000000009", portal_pin=None)
    assert sign_in(client, phone="9000000009").status_code == 401


def test_an_employee_who_has_left_cannot_sign_in(staff, db):
    staff["ramesh"].is_active = False
    db.commit()
    assert sign_in(staff["client"]).status_code == 401


def test_repeated_wrong_pins_lock_the_account(staff):
    for _ in range(settings.portal_max_attempts):
        sign_in(staff["client"], secret="000000")

    # Even the correct PIN is refused while the lockout stands.
    assert sign_in(staff["client"]).status_code == 429


def test_a_portal_lockout_does_not_stop_them_clocking_in(staff, db):
    """The kiosk throttle is separate on purpose.

    Someone guessing at the web login must not be able to lock a colleague out
    of the tablet and cost them a shift.
    """
    from app.auth import pin_throttle

    for _ in range(settings.portal_max_attempts):
        sign_in(staff["client"], secret="000000")

    pin_throttle.check(staff["ramesh"].id)  # must not raise


def test_signing_out_ends_the_session(staff):
    sign_in(staff["client"])
    assert staff["client"].get("/api/portal/me").status_code == 200
    staff["client"].post("/api/portal/logout")
    assert staff["client"].get("/api/portal/me").status_code == 401


# --- isolation ---------------------------------------------------------------


def test_an_employee_only_ever_sees_their_own_figures(staff, db):
    """The question the portal has to get right.

    There is no employee id anywhere in the portal API -- everything is scoped
    to the signed-in session -- so this asserts the two employees' data really
    are kept apart.
    """
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600, ot=90)
    worked_day(db, staff["sita"], today.replace(day=1), 480)

    sign_in(staff["client"], phone="9000000001")
    mine = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert mine["earnings"]["employee_name"] == "Ramesh"
    assert Decimal(mine["earnings"]["monthly_salary"]) == Decimal("15000")
    # Sita earns 21000; none of her figures may appear anywhere in this payload.
    assert "21000" not in str(mine)
    assert "Sita" not in str(mine)


def test_the_portal_api_exposes_no_employee_id_parameter():
    """A structural guard: nothing to tamper with beats validating it."""
    portal_routes = [
        route for route in app.routes
        if getattr(route, "path", "").startswith("/api/portal")
    ]
    assert portal_routes
    for route in portal_routes:
        assert "employee_id" not in route.path
        assert "{" not in route.path or route.path.endswith(".pdf")


def test_an_admin_session_is_not_a_staff_session(staff):
    """Both cookies are signed with the same secret but different salts."""
    staff["client"].post(
        "/api/login", json={"username": "owner", "password": ADMIN_PASSWORD}
    )
    assert staff["client"].get("/api/portal/me").status_code == 401


def test_a_staff_session_is_not_an_admin_session(staff):
    sign_in(staff["client"])
    assert staff["client"].get("/api/employees").status_code == 401
    assert staff["client"].get("/api/attendance/today").status_code == 401


def test_revoked_access_takes_effect_immediately(staff, db):
    """Not at the next login -- while they are still holding a valid cookie."""
    sign_in(staff["client"])
    assert staff["client"].get("/api/portal/me").status_code == 200

    staff["ramesh"].portal_pin_hash = None
    staff["ramesh"].portal_password_hash = None
    db.commit()

    assert staff["client"].get("/api/portal/me").status_code == 401


# --- what they see -----------------------------------------------------------


def test_the_current_month_is_labelled_an_estimate(staff, db):
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600, ot=90)

    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert body["is_final"] is False
    assert body["is_estimate"] is True
    assert body["has_earnings"] is True


def test_days_are_listed_with_hours_and_overtime(staff, db):
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600, ot=90)

    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    first = next(day for day in body["days"] if day["date"].endswith("-01"))
    assert first["worked"] == "10:00"
    assert first["ot"] == "1:30"
    assert first["status_label"] == "Present"


def test_days_that_have_not_happened_yet_are_not_shown(staff, db):
    """Otherwise the rest of the month reads as a wall of absences."""
    today = date.today()
    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert all(day["date"] <= today.isoformat() for day in body["days"])


def test_owner_only_notes_are_not_shown_to_staff(staff, db):
    """Flags like 'salary not prorated' are notes for whoever runs payroll."""
    today = date.today()
    staff["ramesh"].joined_on = today.replace(day=1)
    db.commit()

    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert "flags" not in body["earnings"]
    assert "notes" not in body["earnings"]
    assert "prorated" not in str(body)


def test_leaves_and_advances_appear(staff, db):
    today = date.today()
    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    admin.post(
        "/api/leaves",
        json={
            "employee_id": staff["ramesh"].id,
            "start_date": today.replace(day=2).isoformat(),
            "end_date": today.replace(day=2).isoformat(),
            "leave_type": "PAID",
            "reason": "Family function",
        },
    )
    admin.post(
        "/api/advances",
        json={
            "employee_id": staff["ramesh"].id,
            "advance_date": today.replace(day=3).isoformat(),
            "amount": "2000",
            "note": "Festival",
        },
    )
    admin.post("/api/logout")

    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert body["leaves"][0]["paid"] is True
    assert body["leaves"][0]["reason"] == "Family function"
    # Advances are shown because net pay is otherwise unexplainable.
    assert body["advances"][0]["amount"] == "2000.00"


def test_a_finalised_month_matches_the_frozen_payslip(staff, db):
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600, ot=90)

    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    finalised = admin.post(f"/api/payroll/finalize?year={today.year}&month={today.month}")
    assert finalised.status_code == 200, finalised.text
    official = admin.get(f"/api/payroll?year={today.year}&month={today.month}").json()
    admin.post("/api/logout")

    theirs = next(r for r in official["rows"] if r["employee_name"] == "Ramesh")

    sign_in(staff["client"])
    body = staff["client"].get(
        f"/api/portal/month?year={today.year}&month={today.month}"
    ).json()

    assert body["is_final"] is True
    assert body["is_estimate"] is False
    # The figure on the screen must be the figure on the paper they were given.
    assert body["earnings"]["net_payable"] == theirs["net_payable"]
    assert body["earnings"]["grand_total"] == theirs["grand_total"]


def test_no_payslip_pdf_until_the_month_is_finalised(staff, db):
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600)

    sign_in(staff["client"])
    response = staff["client"].get(
        f"/api/portal/payslip.pdf?year={today.year}&month={today.month}"
    )
    assert response.status_code == 404
    assert "not finalised" in response.json()["detail"]


def test_a_finalised_month_produces_their_payslip(staff, db):
    today = date.today()
    worked_day(db, staff["ramesh"], today.replace(day=1), 600)

    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    admin.post(f"/api/payroll/finalize?year={today.year}&month={today.month}")
    admin.post("/api/logout")

    sign_in(staff["client"])
    response = staff["client"].get(
        f"/api/portal/payslip.pdf?year={today.year}&month={today.month}"
    )
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"


# --- setting a password ------------------------------------------------------


def test_an_employee_can_replace_their_pin_with_a_password(staff):
    sign_in(staff["client"])
    response = staff["client"].post(
        "/api/portal/password",
        json={"current_secret": PORTAL_PIN, "new_password": "monsoon-2026"},
    )
    assert response.status_code == 200
    staff["client"].post("/api/portal/logout")

    # The password works...
    assert sign_in(staff["client"], secret="monsoon-2026").status_code == 200
    staff["client"].post("/api/portal/logout")
    # ...and the PIN the office knows no longer does.
    assert sign_in(staff["client"], secret=PORTAL_PIN).status_code == 401


def test_changing_the_password_needs_the_current_one(staff):
    sign_in(staff["client"])
    response = staff["client"].post(
        "/api/portal/password",
        json={"current_secret": "000000", "new_password": "something-else"},
    )
    assert response.status_code == 400


def test_the_office_can_reset_a_forgotten_password(staff):
    """Setting a new PIN has to clear the employee's own password.

    Otherwise a forgotten password would leave them permanently locked out.
    """
    sign_in(staff["client"])
    staff["client"].post(
        "/api/portal/password",
        json={"current_secret": PORTAL_PIN, "new_password": "forgotten-one"},
    )
    staff["client"].post("/api/portal/logout")

    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    reset = admin.post(
        f"/api/employees/{staff['ramesh'].id}/portal-pin", json={"pin": "112233"}
    )
    assert reset.status_code == 200
    admin.post("/api/logout")

    assert sign_in(staff["client"], secret="112233").status_code == 200
    staff["client"].post("/api/portal/logout")
    assert sign_in(staff["client"], secret="forgotten-one").status_code == 401


def test_a_portal_pin_must_be_six_digits(staff):
    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    response = admin.post(
        f"/api/employees/{staff['ramesh'].id}/portal-pin", json={"pin": "1234"}
    )
    assert response.status_code == 422


def test_portal_access_needs_a_phone_number(client, db):
    employee = make_staff(db, "Noor", "EMP010", None, portal_pin=None)
    client.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    response = client.post(
        f"/api/employees/{employee.id}/portal-pin", json={"pin": "445566"}
    )
    assert response.status_code == 400
    assert "phone number" in response.json()["detail"]


def test_the_employee_list_shows_who_has_portal_access(staff):
    admin = staff["client"]
    admin.post("/api/login", json={"username": "owner", "password": ADMIN_PASSWORD})
    rows = admin.get("/api/employees").json()
    assert all(row["portal_ready"] for row in rows)
