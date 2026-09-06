"""Face detection/cropping preprocessing for the deepfake classifier.

Most face-swap deepfake classifiers (including the one wired in `deepfake.py`)
are trained on cropped face images, not full video frames. Feeding them a whole
frame (person + background) measurably hurts accuracy -- this was one of the
diagnosed causes in PLAN.md's "탐지 갭 분석" for the AI-labeled parody video that
slipped through undetected.

Uses MediaPipe's Face Detector (Tasks API). This needs a small (~1-3MB) model
file that MediaPipe does NOT bundle in the pip package anymore -- as of
mediapipe>=0.10 the old `mp.solutions.face_detection` bundled API was removed
in favor of the Tasks API, which requires a locally supplied model asset. See
README.md for the one-time download step. If the model file isn't present (or
mediapipe/PIL aren't installed), this degrades gracefully: `crop_face()`
returns None and callers fall back to the full frame -- same pattern as every
other optional stage in this project (classifier/VLM missing -> heuristics-only).
"""

from __future__ import annotations

import os
import threading

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
_MODEL_PATH = os.path.join(_MODEL_DIR, "blaze_face_short_range.tflite")
_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)

_detector = None
_detector_load_attempted = False
_lock = threading.Lock()


def _get_detector():
    """Module-level singleton (loaded once per process, reused across every
    request/worker thread -- same caching pattern used for the STT/ViT models)."""
    global _detector, _detector_load_attempted
    if _detector is not None or _detector_load_attempted:
        return _detector
    with _lock:
        if _detector is not None or _detector_load_attempted:
            return _detector
        _detector_load_attempted = True
        if not os.path.exists(_MODEL_PATH):
            return None
        try:
            from mediapipe.tasks.python import vision as mp_vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
            base_options = BaseOptions(model_asset_path=_MODEL_PATH)
            options = mp_vision.FaceDetectorOptions(base_options=base_options)
            _detector = mp_vision.FaceDetector.create_from_options(options)
        except Exception:
            _detector = None
    return _detector


def available() -> bool:
    return os.path.exists(_MODEL_PATH)


def crop_face(frame_path: str, padding: float = 0.35) -> str | None:
    """Detect the most prominent face in frame_path and save a padded crop
    beside it. Returns the crop's path, or None (no model / no face / error) --
    callers should fall back to the original frame in that case.
    """
    detector = _get_detector()
    if detector is None or Image is None:
        return None
    try:
        import mediapipe as mp
        mp_image = mp.Image.create_from_file(frame_path)
        result = detector.detect(mp_image)
        if not result.detections:
            return None
        best = max(result.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height)
        bbox = best.bounding_box

        img = Image.open(frame_path).convert("RGB")
        w, h = img.size
        pad_x = bbox.width * padding
        pad_y = bbox.height * padding
        x0 = max(0, int(bbox.origin_x - pad_x))
        y0 = max(0, int(bbox.origin_y - pad_y))
        x1 = min(w, int(bbox.origin_x + bbox.width + pad_x))
        y1 = min(h, int(bbox.origin_y + bbox.height + pad_y))
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None

        crop = img.crop((x0, y0, x1, y1))
        out = f"{os.path.splitext(frame_path)[0]}_face.jpg"
        crop.save(out, "JPEG", quality=90)
        return out
    except Exception:
        return None


def setup_instructions() -> str:
    return (
        "얼굴 crop 모델이 아직 없습니다. 아래 명령으로 한 번만 받아두세요 (약 1-3MB):\n"
        f"  mkdir -p {_MODEL_DIR}\n"
        f"  curl -L -o {_MODEL_PATH} \\\n"
        f"    {_MODEL_URL}"
    )
