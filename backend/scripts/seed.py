"""First-run setup: create the schema, the locations, and the admin account.

Safe to run more than once -- it only fills in what is missing.

    python backend/scripts/seed.py --admin-password "something long"
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.auth import hash_secret  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.models import AdminUser, Device, Location, LocationKind  # noqa: E402

DEFAULT_LOCATIONS = [
    ("Factory", LocationKind.FACTORY),
    ("Store 1", LocationKind.STORE),
    ("Store 2", LocationKind.STORE),
    ("Store 3", LocationKind.STORE),
    ("Store 4", LocationKind.STORE),
    ("Store 5", LocationKind.STORE),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the attendance database.")
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Password for the admin account. Generated if omitted.",
    )
    parser.add_argument(
        "--skip-locations",
        action="store_true",
        help="Do not create the default factory and 5 stores.",
    )
    args = parser.parse_args()

    create_all()
    print(f"Database ready at {settings.db_path}")

    with SessionLocal() as db:
        created_locations = []
        if not args.skip_locations:
            for name, kind in DEFAULT_LOCATIONS:
                if db.scalar(select(Location).where(Location.name == name)) is None:
                    db.add(Location(name=name, kind=kind))
                    created_locations.append(name)
            db.commit()

        if created_locations:
            print(f"Created locations: {', '.join(created_locations)}")
        else:
            print("Locations already present, left alone.")

        # One kiosk device per location, ready to be paired.
        for location in db.scalars(select(Location)).all():
            existing = db.scalar(
                select(Device).where(Device.location_id == location.id)
            )
            if existing is None:
                db.add(
                    Device(name=f"{location.name} kiosk", location_id=location.id)
                )
        db.commit()

        username = args.admin_username.strip().lower()
        admin = db.scalar(select(AdminUser).where(AdminUser.username == username))
        if admin is not None:
            print(f"Admin '{username}' already exists, password left unchanged.")
        else:
            password = args.admin_password or secrets.token_urlsafe(12)
            db.add(
                AdminUser(
                    username=username,
                    password_hash=hash_secret(password),
                    display_name="Owner",
                )
            )
            db.commit()
            print("\n" + "=" * 58)
            print("  ADMIN ACCOUNT CREATED")
            print(f"  Username: {username}")
            print(f"  Password: {password}")
            print("  Write this down now. It is not stored anywhere in readable")
            print("  form and cannot be shown again.")
            print("=" * 58 + "\n")

    print("Next: open /admin, add your employees, then pair each kiosk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
