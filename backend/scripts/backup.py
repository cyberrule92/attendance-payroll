"""Back up the database and the attendance photos into one dated zip.

The database is copied with SQLite's own backup API rather than by copying the
file, so a backup taken while a kiosk is mid-punch is still consistent.

    python backend/scripts/backup.py
    python backend/scripts/backup.py --to "D:/Backups/Attendance"
    python backend/scripts/backup.py --keep 30 --no-photos
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def consistent_db_copy(source: Path, destination: Path) -> None:
    """Copy the database using SQLite's backup API (safe while in use)."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def prune(folder: Path, keep: int) -> int:
    backups = sorted(folder.glob("attendance-backup-*.zip"))
    removed = 0
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up attendance data.")
    parser.add_argument(
        "--to",
        default=None,
        help="Where to write the zip. Defaults to the project's backups folder.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=30,
        help="How many backups to keep. 0 keeps everything. Default 30.",
    )
    parser.add_argument(
        "--no-photos",
        action="store_true",
        help="Back up only the database, not the punch photos.",
    )
    args = parser.parse_args()

    if not settings.db_path.exists():
        print(f"No database at {settings.db_path}. Nothing to back up.")
        return 1

    target = Path(args.to) if args.to else settings.backup_dir
    target.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    archive = target / f"attendance-backup-{stamp}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "attendance.db"
        consistent_db_copy(settings.db_path, snapshot)

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, "attendance.db")

            photo_count = 0
            if not args.no_photos and settings.photo_dir.is_dir():
                for photo in settings.photo_dir.rglob("*.jpg"):
                    zf.write(photo, Path("photos") / photo.relative_to(settings.photo_dir))
                    photo_count += 1

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Backup written to {archive}  ({size_mb:.1f} MB, {photo_count} photos)")

    removed = prune(target, args.keep)
    if removed:
        print(f"Removed {removed} backup(s) older than the last {args.keep}.")

    print("\nKeep a copy somewhere other than this laptop -- a cloud drive folder")
    print("or an external disk. A backup that only exists on the laptop does not")
    print("help if the laptop is what fails.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
