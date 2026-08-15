"""Application settings.

Everything here can be overridden with environment variables or a .env file
sitting next to the project root, so the Windows laptop deployment never needs
code edits.
"""

from __future__ import annotations

from datetime import tzinfo
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

# /opt/attendance
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_prefix="ATTENDANCE_",
        extra="ignore",
    )

    # --- storage -------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    backup_dir: Path = PROJECT_ROOT / "backups"

    # --- branding ------------------------------------------------------
    # Shown on the kiosk screen and printed at the top of every payslip.
    business_name: str = "Dry Cleaning Franchise"

    # --- locale --------------------------------------------------------
    # Punches are stored in UTC; every "what day is this" decision uses this zone.
    timezone: str = "Asia/Kolkata"
    currency_symbol: str = "₹"

    # --- payroll defaults ----------------------------------------------
    # Overridable per employee in the database; these are the fallbacks used
    # when creating a new employee.
    shift_hours: float = 8.5
    default_night_threshold_hours: float = 5.0
    default_ot_adjustment_per_night_hours: float = 0.0
    # Monday=0 ... Sunday=6.  Thursday=3.
    default_weekly_off_dow: int = 3

    # A work day runs from this local hour to the same hour next day, so a
    # night shift that starts at 20:00 and ends at 03:00 stays on ONE work
    # date instead of being split across midnight into two broken days.
    # Punches before this hour are credited to the previous work date.
    day_cutover_hour: int = 5

    # --- security ------------------------------------------------------
    # Used to sign the admin session cookie. Generated on first run and
    # written to .env by scripts/start-windows.bat if left at the default.
    secret_key: str = "change-me-in-dot-env"
    session_max_age_seconds: int = 60 * 60 * 12
    # Staff sign in from their own phones, so a shorter life than the owner's
    # console session on a laptop they control.
    staff_session_max_age_seconds: int = 60 * 60 * 8
    # Wrong portal logins before that employee is locked out for
    # pin_lockout_seconds. Lower than the kiosk's, because this login is
    # reachable from anywhere and a locked portal costs nobody a shift.
    portal_max_attempts: int = 5
    # A kiosk that fails this many PIN attempts for one employee is locked out
    # for pin_lockout_seconds.
    pin_max_attempts: int = 5
    pin_lockout_seconds: int = 300

    # --- photos --------------------------------------------------------
    max_photo_bytes: int = 2 * 1024 * 1024
    photo_max_width: int = 640
    photo_jpeg_quality: int = 72

    # --- face verification (anti-proxy) --------------------------------
    # Master switch. Off means punches are recorded exactly as before, with
    # the photo kept as evidence only.
    face_enabled: bool = True
    # Refuse the punch outright once the retries are used up, instead of
    # recording it flagged. Off by default: a genuine employee in bad light
    # should never be left unable to clock in.
    face_strict: bool = False
    # How many times the kiosk may re-capture before the punch is recorded
    # and flagged for review.
    face_max_attempts: int = 3

    # Cosine similarity above which two faces are the same person. 0.363 is
    # OpenCV's published operating point for SFace; raise it to make the
    # check stricter, lower it if genuine staff are being asked to retry.
    face_match_threshold: float = 0.363
    # YuNet's own confidence that a region is a face at all.
    face_detect_threshold: float = 0.85

    # Liveness. A punch passes if either the face moved independently of the
    # background, or it changed shape after alignment. Both are near zero for
    # a photo held up to the camera.
    face_motion_threshold: float = 0.08
    face_parallax_threshold: float = 0.10
    # Trained anti-spoof model, when installed: probability the crop is live.
    face_antispoof_threshold: float = 0.55
    # Frames the kiosk captures per punch. Below two, liveness cannot be
    # judged at all.
    face_burst_frames: int = 5

    # Enrolment is held to a higher bar than a punch -- see services/face.py.
    face_min_enroll_pixels: int = 80
    face_enroll_agreement_threshold: float = 0.30
    face_max_enrollments: int = 5

    @property
    def face_models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "attendance.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def photo_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.photo_dir.mkdir(parents=True, exist_ok=True)
    settings.face_models_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
