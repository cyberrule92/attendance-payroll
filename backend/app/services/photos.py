"""Attendance photo storage.

Photos are downscaled and re-encoded server-side before they touch the disk.
That does three useful things: it caps the size a kiosk can push at us, it
strips EXIF (including any location the phone camera stamped in), and it keeps
a year of attendance at a few hundred megabytes instead of tens of gigabytes.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

from ..config import settings


def _safe_name(client_uuid: str) -> str:
    """Filenames come from client input, so keep them to a known-safe alphabet."""
    cleaned = "".join(c for c in client_uuid if c.isalnum() or c in "-_")
    return cleaned[:64] or "punch"


def save_punch_photo(raw: bytes, work_date: date, client_uuid: str) -> str:
    """Store one punch photo and return its path relative to the photo root."""
    if not raw:
        raise HTTPException(status_code=400, detail="No photo was received.")
    if len(raw) > settings.max_photo_bytes:
        raise HTTPException(
            status_code=413,
            detail="Photo is too large. The kiosk should be shrinking it first.",
        )

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="That file is not an image.")

    # Selfies arrive rotated on some Android devices; honour the EXIF tag, then
    # drop the metadata by rebuilding the image on save.
    try:
        from PIL import ImageOps

        image = ImageOps.exif_transpose(image)
    except Exception:  # pragma: no cover - defensive, EXIF is often absent
        pass

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if image.width > settings.photo_max_width:
        ratio = settings.photo_max_width / image.width
        image = image.resize(
            (settings.photo_max_width, max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )

    folder = settings.photo_dir / work_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    relative = Path(work_date.isoformat()) / f"{_safe_name(client_uuid)}.jpg"

    image.save(
        settings.photo_dir / relative,
        format="JPEG",
        quality=settings.photo_jpeg_quality,
        optimize=True,
    )
    return str(relative).replace("\\", "/")


def resolve(relative: str) -> Path:
    """Turn a stored relative path back into an absolute one, safely.

    Rejects anything that escapes the photo directory, so a tampered database
    value cannot be used to read arbitrary files off the laptop.
    """
    root = settings.photo_dir.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Photo not found.")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    return candidate


def delete_photo(relative: str | None) -> None:
    if not relative:
        return
    try:
        resolve(relative).unlink(missing_ok=True)
    except HTTPException:
        pass
