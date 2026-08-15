"""Database engine and session handling.

Synchronous SQLAlchemy on SQLite. With well under 50 employees the write volume
is tiny, SQLite serialises writes anyway, and the photo file I/O in the punch
path is blocking regardless -- so FastAPI's threadpool for sync endpoints is a
better fit here than async plumbing.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    # SQLite + FastAPI threadpool: connections hop threads between requests.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """WAL keeps the admin console readable while a kiosk is writing a punch."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    from . import models  # noqa: F401  (registers the mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


# Columns added after the first release. ``create_all`` creates missing tables
# but never alters existing ones, so a database created before a feature landed
# would keep working until the first query touched a new column. Alembic is a
# dependency but no migration has ever been written against this schema, and a
# laptop deployment that upgrades by copying a folder cannot be relied on to run
# one. SQLite's ADD COLUMN is cheap and safe, so the additions are applied here
# on every start. Existing rows take the default, which is what UNCHECKED means:
# "this punch predates face checking", not "this punch failed it".
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "punches": {
        "face_status": "VARCHAR(16) DEFAULT 'UNCHECKED'",
        "face_score": "FLOAT",
        "liveness_score": "FLOAT",
        "face_attempts": "INTEGER DEFAULT 0",
    },
    "employees": {
        "portal_pin_hash": "VARCHAR(120)",
        "portal_password_hash": "VARCHAR(120)",
        "portal_last_login_at": "DATETIME",
    },
}


def _add_missing_columns() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name in present:
                    continue
                connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                )
