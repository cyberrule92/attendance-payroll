"""Face verification for kiosk punches -- the anti-proxy control.

Until now the punch photo was *evidence*: PIN plus photo made buddy-punching
traceable, not impossible. This module makes the face itself part of the
authentication, so someone holding a colleague's PIN cannot punch for them.

Three things have to be true before a punch is treated as verified:

1. **A face is there.** YuNet finds it and gives five landmarks.
2. **It is a live face, not a picture of one.** A held-up phone or a printed
   photo is the obvious attack once the face matters, so it is checked for
   before identity is even considered. See ``assess_liveness``.
3. **It is the right face.** SFace turns the aligned crop into a 128-number
   embedding, compared by cosine similarity against the employee's enrolled
   references.

Everything runs **server side**. The kiosk is a shared tablet in a shop and is
not trusted with this decision -- a tampered device could otherwise simply post
``verified: true``. The kiosk's only job is to capture frames and upload them.

Model choice: OpenCV ships YuNet and SFace in the ``opencv-python`` wheel, so
there is no C++ toolchain and no build step in the deployment -- the same
constraint that shaped the rest of this project. The weights come from the
official ``opencv/opencv_zoo`` repository and are checksummed on download by
``scripts/fetch_face_models.py``.

Nothing here touches the database or HTTP. That keeps it directly testable, the
same reason ``services/payroll.py`` is written the way it is.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import settings

logger = logging.getLogger(__name__)

# --- model files -------------------------------------------------------------

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
# Optional third stage. Absent by default; see scripts/fetch_face_models.py.
ANTISPOOF_FILE = "face_antispoof_minifasnet.onnx"

# SFace produces a 128-dimension embedding.
EMBEDDING_DIMS = 128

# The face crop SFace expects, and the size MiniFASNet was trained on.
ANTISPOOF_INPUT = 80
# MiniFASNet is trained on a crop wider than the face box itself -- it needs the
# surrounding context (a screen bezel, the edge of a print) to spot an attack.
ANTISPOOF_CROP_SCALE = 2.7


class FaceUnavailable(RuntimeError):
    """Raised when the models are not installed, so callers can degrade."""


# --- lazily loaded singletons ------------------------------------------------
#
# The models are ~39 MB and take a moment to initialise, so they are loaded on
# first use and kept. OpenCV's detector and recognizer objects carry internal
# state and are *not* safe to call from several threads at once; FastAPI runs
# sync endpoints in a threadpool, so every inference call holds ``_lock``.
# At this scale (under 50 staff across 6 shops) serialising is free.

_lock = threading.Lock()
_detector = None
_recognizer = None
_antispoof = None
_antispoof_checked = False


def models_dir() -> Path:
    return settings.face_models_dir


def _model_path(name: str) -> Path:
    return models_dir() / name


def models_installed() -> bool:
    """True when the two required models are on disk."""
    return _model_path(DETECTOR_FILE).is_file() and _model_path(RECOGNIZER_FILE).is_file()


def antispoof_installed() -> bool:
    return _model_path(ANTISPOOF_FILE).is_file()


def _load() -> tuple:
    """Build the detector and recognizer once."""
    global _detector, _recognizer
    if _detector is not None and _recognizer is not None:
        return _detector, _recognizer

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise FaceUnavailable("opencv-python is not installed.") from exc

    if not models_installed():
        raise FaceUnavailable(
            f"Face models are missing from {models_dir()}. "
            "Run: python scripts/fetch_face_models.py"
        )

    _detector = cv2.FaceDetectorYN.create(
        model=str(_model_path(DETECTOR_FILE)),
        config="",
        input_size=(320, 320),
        score_threshold=settings.face_detect_threshold,
        nms_threshold=0.3,
        top_k=5000,
    )
    _recognizer = cv2.FaceRecognizerSF.create(
        model=str(_model_path(RECOGNIZER_FILE)), config=""
    )
    logger.info("Face models loaded from %s", models_dir())
    return _detector, _recognizer


def _load_antispoof():
    """Load the anti-spoof model if it was installed. Returns None if not.

    This stage is optional on purpose: the motion check in ``assess_liveness``
    works with no extra model at all, and a missing file should degrade the
    system rather than stop every punch in six shops.
    """
    global _antispoof, _antispoof_checked
    if _antispoof_checked:
        return _antispoof
    _antispoof_checked = True

    if not antispoof_installed():
        logger.warning(
            "Anti-spoof model not installed -- relying on the motion check alone. "
            "See RUNBOOK.md to add it."
        )
        return None

    import cv2

    _antispoof = cv2.dnn.readNetFromONNX(str(_model_path(ANTISPOOF_FILE)))
    logger.info("Anti-spoof model loaded.")
    return _antispoof


def reset_cache() -> None:
    """Drop the loaded models. Used by tests and after installing new files."""
    global _detector, _recognizer, _antispoof, _antispoof_checked
    _detector = _recognizer = _antispoof = None
    _antispoof_checked = False


# --- detection ---------------------------------------------------------------


@dataclass(frozen=True)
class Face:
    """One detected face.

    ``row`` is YuNet's raw 15-number output (box, five landmarks, score).
    SFace's ``alignCrop`` needs exactly that, so it is carried around rather
    than unpacked and rebuilt.
    """

    row: np.ndarray
    score: float

    @property
    def box(self) -> tuple[int, int, int, int]:
        x, y, w, h = self.row[:4]
        return int(x), int(y), int(w), int(h)

    @property
    def landmarks(self) -> np.ndarray:
        """Five points: right eye, left eye, nose, right mouth, left mouth."""
        return self.row[4:14].reshape(5, 2)

    @property
    def area(self) -> int:
        _, _, w, h = self.box
        return max(0, w) * max(0, h)


def decode_image(raw: bytes) -> np.ndarray:
    """JPEG/PNG bytes -> BGR array. Raises ValueError on anything unreadable."""
    import cv2

    buffer = np.frombuffer(raw, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("That file is not a readable image.")
    return image


def detect_faces(image: np.ndarray) -> list[Face]:
    """Every face in the frame, largest first."""
    import cv2  # noqa: F401  (ensures the import error surfaces consistently)

    detector, _ = _load()
    height, width = image.shape[:2]
    with _lock:
        detector.setInputSize((width, height))
        _, raw = detector.detect(image)

    if raw is None or len(raw) == 0:
        return []
    faces = [Face(row=np.asarray(row, dtype=np.float32), score=float(row[14])) for row in raw]
    return sorted(faces, key=lambda f: f.area, reverse=True)


def primary_face(image: np.ndarray) -> Face | None:
    """The face the punch is about: the largest one, i.e. closest to the camera.

    If two people are in frame the nearer one is taken to be the person at the
    kiosk. A colleague standing behind cannot displace them, and if the wrong
    person leans in it becomes a mismatch rather than a silent pass.
    """
    faces = detect_faces(image)
    return faces[0] if faces else None


# --- embeddings --------------------------------------------------------------


def embed(image: np.ndarray, face: Face) -> np.ndarray:
    """Aligned crop -> L2-normalised 128-number embedding.

    Normalising here means similarity is a plain dot product later, and stored
    embeddings are directly comparable regardless of which frame produced them.
    """
    _, recognizer = _load()
    with _lock:
        aligned = recognizer.alignCrop(image, face.row)
        vector = recognizer.feature(aligned)

    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        raise ValueError("Face produced an empty embedding.")
    return vector / norm


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two normalised embeddings, in [-1, 1].

    OpenCV's SFace guidance puts same-person pairs above ~0.363; the working
    value is ``settings.face_match_threshold`` so it can be tuned per site
    without a code change.
    """
    return float(np.dot(a.astype(np.float32), b.astype(np.float32)))


def pack_embedding(vector: np.ndarray) -> bytes:
    """Store as raw float32 -- 512 bytes, no precision lost to text encoding."""
    return np.asarray(vector, dtype=np.float32).reshape(-1).tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    vector = np.frombuffer(blob, dtype=np.float32)
    if vector.size != EMBEDDING_DIMS:
        raise ValueError(
            f"Stored embedding has {vector.size} values, expected {EMBEDDING_DIMS}."
        )
    return vector


def best_match(probe: np.ndarray, references: list[np.ndarray]) -> float:
    """Highest similarity against any enrolled reference.

    Best-of rather than an average: people are enrolled from a few angles, and
    a good match on one of them is a match. Averaging would let a poor
    reference drag a genuine employee below the line.
    """
    if not references:
        return -1.0
    return max(similarity(probe, reference) for reference in references)


# --- liveness: is this a real face or a picture of one? ----------------------


@dataclass
class LivenessReport:
    passed: bool
    motion_score: float = 0.0
    parallax_score: float = 0.0
    antispoof_score: float | None = None  # None when the model is not installed
    reason: str = ""


def _aligned_gray(image: np.ndarray, face: Face) -> np.ndarray:
    """Face crop, aligned and greyscaled, for frame-to-frame comparison."""
    import cv2

    _, recognizer = _load()
    with _lock:
        aligned = recognizer.alignCrop(image, face.row)
    grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    # Equalising removes the flicker of a tablet's auto-exposure, which would
    # otherwise read as "movement" on a completely static photo.
    return cv2.equalizeHist(grey)


def motion_score(frames: list[np.ndarray], faces: list[Face]) -> float:
    """How much the face changes across the burst, after alignment.

    Alignment is the point. Waving a photo around produces a lot of raw pixel
    movement, but once every frame is aligned on the eyes and nose the crops
    are near-identical -- a print has no independent eyelids or mouth. A real
    face keeps changing after alignment: blinks, micro-expressions, the way the
    nose and cheek shift as the head turns.

    Returned as the mean absolute difference between consecutive aligned crops,
    normalised to 0..1.
    """
    if len(frames) < 2:
        return 0.0

    crops = [_aligned_gray(frame, face) for frame, face in zip(frames, faces)]
    diffs = []
    for earlier, later in zip(crops, crops[1:]):
        if earlier.shape != later.shape:
            continue
        diffs.append(float(np.mean(np.abs(earlier.astype(np.int16) - later.astype(np.int16)))))
    if not diffs:
        return 0.0
    return float(np.clip(np.mean(diffs) / 255.0 * 10.0, 0.0, 1.0))


def parallax_score(frames: list[np.ndarray], faces: list[Face]) -> float:
    """Does the face move independently of the background?

    A phone or a printed photo held up to the camera is one rigid object: when
    the hand holding it moves, the face and everything around it move together.
    A real person's head moves against a background that stays put.

    Returned as the face's motion minus the background's, clipped to 0..1. Near
    zero means "the whole scene moved as one", which is what a held-up image
    looks like.
    """
    import cv2

    if len(frames) < 2:
        return 0.0

    face_deltas: list[float] = []
    background_deltas: list[float] = []

    for index in range(len(frames) - 1):
        first = cv2.cvtColor(frames[index], cv2.COLOR_BGR2GRAY)
        second = cv2.cvtColor(frames[index + 1], cv2.COLOR_BGR2GRAY)
        if first.shape != second.shape:
            continue
        delta = np.abs(first.astype(np.int16) - second.astype(np.int16)).astype(np.float32)

        mask = np.zeros(delta.shape, dtype=bool)
        x, y, w, h = faces[index].box
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(delta.shape[1], x + w), min(delta.shape[0], y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        mask[y0:y1, x0:x1] = True

        if mask.all() or not mask.any():
            continue
        face_deltas.append(float(delta[mask].mean()))
        background_deltas.append(float(delta[~mask].mean()))

    if not face_deltas:
        return 0.0

    face_motion = float(np.mean(face_deltas))
    background_motion = float(np.mean(background_deltas))
    # Relative, so a dim shop and a bright one score alike.
    denominator = max(face_motion, background_motion, 1.0)
    return float(np.clip((face_motion - background_motion) / denominator, 0.0, 1.0))


def antispoof_score(image: np.ndarray, face: Face) -> float | None:
    """Probability the crop is a live face, per the trained model.

    Returns None when the model is not installed, which callers must treat as
    "unknown", never as "passed".
    """
    import cv2

    net = _load_antispoof()
    if net is None:
        return None

    height, width = image.shape[:2]
    x, y, w, h = face.box
    # Widen to the context window MiniFASNet expects.
    centre_x, centre_y = x + w / 2, y + h / 2
    side = max(w, h) * ANTISPOOF_CROP_SCALE
    x0 = int(max(0, centre_x - side / 2))
    y0 = int(max(0, centre_y - side / 2))
    x1 = int(min(width, centre_x + side / 2))
    y1 = int(min(height, centre_y + side / 2))
    if x1 <= x0 or y1 <= y0:
        return None

    crop = image[y0:y1, x0:x1]
    blob = cv2.dnn.blobFromImage(
        crop, scalefactor=1.0, size=(ANTISPOOF_INPUT, ANTISPOOF_INPUT), swapRB=False
    )
    with _lock:
        net.setInput(blob)
        output = net.forward()

    scores = np.asarray(output, dtype=np.float64).reshape(-1)
    shifted = scores - scores.max()
    probabilities = np.exp(shifted) / np.exp(shifted).sum()
    # MiniFASNet's three classes are [print attack, live, replay attack].
    return float(probabilities[1]) if probabilities.size >= 2 else None


def assess_liveness(frames: list[np.ndarray], faces: list[Face]) -> LivenessReport:
    """Combine the checks into one live/not-live decision.

    The trained model, when installed, is decisive on its own -- it is the only
    stage that catches a *video* replay, which by definition moves. The motion
    and parallax checks are what stand between the system and a held-up still
    when that model is absent, and they stay switched on either way as a second
    opinion.
    """
    if len(frames) < 2:
        return LivenessReport(
            passed=False,
            reason="Not enough frames were captured to tell a live face from a photo.",
        )

    motion = motion_score(frames, faces)
    parallax = parallax_score(frames, faces)

    # Score the sharpest frame -- a blurred one makes any classifier guess.
    trained = antispoof_score(frames[len(frames) // 2], faces[len(faces) // 2])

    if trained is not None and trained < settings.face_antispoof_threshold:
        return LivenessReport(
            passed=False,
            motion_score=motion,
            parallax_score=parallax,
            antispoof_score=trained,
            reason="This looks like a photo or a screen, not a live person.",
        )

    if motion < settings.face_motion_threshold and parallax < settings.face_parallax_threshold:
        return LivenessReport(
            passed=False,
            motion_score=motion,
            parallax_score=parallax,
            antispoof_score=trained,
            reason="The face did not move like a live face. Hold the camera steady and look at it.",
        )

    return LivenessReport(
        passed=True,
        motion_score=motion,
        parallax_score=parallax,
        antispoof_score=trained,
    )


# --- the whole check ---------------------------------------------------------


@dataclass
class VerificationResult:
    """The outcome of checking one burst of frames against one employee."""

    outcome: str  # one of the OUTCOME_* values below
    score: float = 0.0
    liveness: LivenessReport | None = None
    message: str = ""
    # Index of the frame worth keeping as the punch photo.
    best_frame: int = 0
    embedding: np.ndarray | None = field(default=None, repr=False)

    @property
    def verified(self) -> bool:
        return self.outcome == OUTCOME_VERIFIED

    @property
    def retryable(self) -> bool:
        """Worth another attempt at the kiosk, rather than a settled answer."""
        return self.outcome in (OUTCOME_NO_FACE, OUTCOME_NOT_LIVE, OUTCOME_MISMATCH)


OUTCOME_VERIFIED = "VERIFIED"
OUTCOME_MISMATCH = "MISMATCH"
OUTCOME_NO_FACE = "NO_FACE"
OUTCOME_NOT_LIVE = "NOT_LIVE"
OUTCOME_NOT_ENROLLED = "NOT_ENROLLED"
OUTCOME_UNAVAILABLE = "UNAVAILABLE"


def verify(frames_raw: list[bytes], references: list[np.ndarray]) -> VerificationResult:
    """Check a burst of captured frames against an employee's enrolled faces.

    ``references`` empty means the employee has not been enrolled yet. That is
    reported as its own outcome rather than a failure: switching this on with
    nobody enrolled would otherwise lock every employee out of the kiosk on the
    first morning. The caller records the punch and flags it.
    """
    if not models_installed():
        return VerificationResult(
            outcome=OUTCOME_UNAVAILABLE,
            message="Face checking is not set up on the server.",
        )

    images: list[np.ndarray] = []
    for raw in frames_raw:
        try:
            images.append(decode_image(raw))
        except ValueError:
            continue

    if not images:
        return VerificationResult(
            outcome=OUTCOME_NO_FACE, message="No usable photo was received."
        )

    paired = [(image, primary_face(image)) for image in images]
    usable = [(image, face) for image, face in paired if face is not None]

    if not usable:
        return VerificationResult(
            outcome=OUTCOME_NO_FACE,
            message="No face was visible. Hold the tablet at eye level and try again.",
        )

    frames = [image for image, _ in usable]
    faces = [face for _, face in usable]

    # The sharpest, largest face is the one to embed and to keep on record.
    best_index = max(range(len(faces)), key=lambda i: faces[i].area * faces[i].score)

    liveness = assess_liveness(frames, faces)
    if not liveness.passed:
        return VerificationResult(
            outcome=OUTCOME_NOT_LIVE,
            liveness=liveness,
            message=liveness.reason,
            best_frame=best_index,
        )

    if not references:
        return VerificationResult(
            outcome=OUTCOME_NOT_ENROLLED,
            liveness=liveness,
            best_frame=best_index,
            message="This employee has no face on file yet.",
        )

    probe = embed(frames[best_index], faces[best_index])
    score = best_match(probe, references)

    if score < settings.face_match_threshold:
        return VerificationResult(
            outcome=OUTCOME_MISMATCH,
            score=score,
            liveness=liveness,
            best_frame=best_index,
            embedding=probe,
            message="That does not look like the right person. Please try again.",
        )

    return VerificationResult(
        outcome=OUTCOME_VERIFIED,
        score=score,
        liveness=liveness,
        best_frame=best_index,
        embedding=probe,
    )


# --- enrolment ---------------------------------------------------------------


@dataclass
class EnrollmentResult:
    ok: bool
    embedding: np.ndarray | None = None
    message: str = ""
    face: Face | None = None


def enroll_from_image(raw: bytes, existing: list[np.ndarray]) -> EnrollmentResult:
    """Turn one admin-supplied photo into a reference embedding.

    Enrolment is gated harder than a punch is. A blurred or half-turned
    reference face quietly raises the false-reject rate for that person every
    day afterwards, and the person it inconveniences is never the one who
    enrolled them.
    """
    if not models_installed():
        return EnrollmentResult(ok=False, message="Face models are not installed on the server.")

    try:
        image = decode_image(raw)
    except ValueError as exc:
        return EnrollmentResult(ok=False, message=str(exc))

    faces = detect_faces(image)
    if not faces:
        return EnrollmentResult(
            ok=False, message="No face found in that photo. Use a clear, front-on picture."
        )
    if len(faces) > 1 and faces[1].area > faces[0].area * 0.5:
        return EnrollmentResult(
            ok=False,
            message="More than one face in that photo. Use a picture of just this person.",
        )

    face = faces[0]
    _, _, width, height = face.box
    if min(width, height) < settings.face_min_enroll_pixels:
        return EnrollmentResult(
            ok=False,
            message=(
                f"The face is too small in that photo ({min(width, height)}px). "
                "Take it closer, or use a higher-resolution picture."
            ),
        )

    embedding = embed(image, face)

    # A reference that does not resemble the ones already on file usually means
    # the wrong person's photo was attached to the wrong employee.
    if existing:
        agreement = best_match(embedding, existing)
        if agreement < settings.face_enroll_agreement_threshold:
            return EnrollmentResult(
                ok=False,
                message=(
                    "This photo does not look like the same person as the ones "
                    "already on file. Check you picked the right employee."
                ),
            )

    return EnrollmentResult(ok=True, embedding=embedding, face=face, message="Face added.")
