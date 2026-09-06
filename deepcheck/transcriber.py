"""Speech-to-text with faster-whisper (CTranslate2), auto device/compute detection."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .config import config

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover
    WhisperModel = None


@dataclass
class Transcript:
    text: str
    language: str | None
    language_probability: float | None
    segments: list[dict] = field(default_factory=list)
    duration: float | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _detect_device() -> tuple[str, str]:
    """Return (device, compute_type) for faster-whisper (CTranslate2).

    CTranslate2 supports only CUDA and CPU — NOT Apple MPS — so Macs fall back to
    an efficient INT8 CPU path.
    """
    try:
        import ctranslate2
    except ImportError:
        return "cpu", "int8"

    try:
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception as e:
        logger.debug("CUDA 장치 확인 실패, CPU로 진행: %s", e)

    return "cpu", "int8"


_model_cache: dict[tuple, "WhisperModel"] = {}
_model_lock = threading.Lock()


def _get_model(model_size: str, device: str, compute_type: str):
    """Module-level singleton cache: loading a Whisper model costs real time and
    memory, so we load each (model_size, device, compute_type) combo once per
    process and reuse it across every request/worker thread, instead of
    reloading from scratch on every single analyze_url() call. Matters most on
    resource-constrained deploy targets (e.g. a home-server Mac mini).
    """
    key = (model_size, device, compute_type)
    if key in _model_cache:
        return _model_cache[key]
    with _model_lock:
        if key in _model_cache:
            return _model_cache[key]
        logger.info("STT 모델 로드: %s (%s/%s)", model_size, device, compute_type)
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _model_cache[key] = model
        return model


def transcribe(audio_or_video: str, model_size: str | None = None,
               language: str | None = None) -> Transcript:
    """Transcribe an audio/video file. Falls back gracefully if model files are missing."""
    if WhisperModel is None:
        raise RuntimeError("faster-whisper is not installed. Run: uv pip install -r requirements.txt")

    model_size = model_size or config.whisper_model_size
    device, compute_type = _detect_device()
    model = _get_model(model_size, device, compute_type)
    started = time.monotonic()
    segments_iter, info = model.transcribe(
        audio_or_video,
        language=language,
        beam_size=5,
        vad_filter=True,
        word_timestamps=False,
    )

    segments = []
    for seg in segments_iter:
        segments.append(
            {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            }
        )

    text = " ".join(s["text"] for s in segments).strip()
    logger.info(
        "STT 완료: %d세그먼트, %d단어, 언어 %s(%.2f), 처리 구간 %.1fs, 소요 %.1fs",
        len(segments), len(text.split()), info.language, info.language_probability or 0.0,
        float(info.duration or 0.0), time.monotonic() - started,
    )
    return Transcript(
        text=text,
        language=info.language,
        language_probability=info.language_probability,
        segments=segments,
        duration=float(info.duration) if info.duration else None,
    )
