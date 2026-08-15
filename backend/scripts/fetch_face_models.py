"""Download the face-verification models.

Run once after installing, and again only if the models are ever replaced:

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\fetch_face_models.py

The two required models come from OpenCV's own model zoo, which is where the
matching code in ``services/face.py`` gets its published operating points from.
They are kept out of the repository because they are 39 MB of binary that never
changes, and downloading them is a one-time step.

Every file is checked against a pinned SHA-256 before it is put in place. This
is the control that decides whether somebody gets paid for a shift they did not
work, so it does not load a file it cannot identify. A checksum mismatch aborts
and leaves any previously working model untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import face  # noqa: E402

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"


class Model:
    def __init__(self, filename: str, url: str, sha256: str, label: str, required: bool):
        self.filename = filename
        self.url = url
        self.sha256 = sha256
        self.label = label
        self.required = required


MODELS = [
    Model(
        filename=face.DETECTOR_FILE,
        url=f"{ZOO}/face_detection_yunet/{face.DETECTOR_FILE}",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        label="YuNet face detector",
        required=True,
    ),
    Model(
        filename=face.RECOGNIZER_FILE,
        url=f"{ZOO}/face_recognition_sface/{face.RECOGNIZER_FILE}",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        label="SFace face recogniser",
        required=True,
    ),
]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(model: Model, destination: Path, *, trust: bool) -> bool:
    """Fetch one model, verify it, and move it into place. True if installed."""
    target = destination / model.filename
    if target.is_file():
        actual = sha256_of(target)
        if trust or actual == model.sha256:
            print(f"  {model.label}: already present")
            return True
        print(f"  {model.label}: on disk but checksum differs -- re-downloading")

    print(f"  {model.label}: downloading...")
    try:
        with urllib.request.urlopen(model.url, timeout=120) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  {model.label}: FAILED to download ({exc})")
        return False

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(payload)
        staged = Path(handle.name)

    actual = sha256_of(staged)
    if not trust and actual != model.sha256:
        staged.unlink(missing_ok=True)
        print(f"  {model.label}: CHECKSUM MISMATCH -- refusing to install")
        print(f"      expected {model.sha256}")
        print(f"      got      {actual}")
        return False

    destination.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), target)
    size_mb = target.stat().st_size / 1e6
    print(f"  {model.label}: installed ({size_mb:.1f} MB, sha256 {actual[:16]}...)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trust-downloads",
        action="store_true",
        help=(
            "Skip checksum verification and print the hashes instead. Only for "
            "pinning a NEW model version -- never for a routine install."
        ),
    )
    args = parser.parse_args()

    destination = settings.face_models_dir
    print(f"\nFace models -> {destination}\n")

    installed = all(
        download(model, destination, trust=args.trust_downloads)
        for model in MODELS
        if model.required
    )

    if args.trust_downloads:
        print("\nPin these into MODELS:")
        for model in MODELS:
            path = destination / model.filename
            if path.is_file():
                print(f'  {model.filename}\n    sha256="{sha256_of(path)}",')

    print()
    if not installed:
        print("Some required models are missing. Face checking will stay off and")
        print("punches will be recorded unverified until this succeeds.\n")
        return 1

    if not face.antispoof_installed():
        print("Optional: the trained anti-spoof model is not installed.")
        print("The motion and parallax checks are active without it; see RUNBOOK.md.")

    print("Done. Restart the server to load them.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
