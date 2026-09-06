"""파이프라인 전역 설정.

임계값·가중치·모델 ID·타임아웃을 여기 한 곳에 모은다. 값이 코드 곳곳에 흩어져
있으면 튜닝할 때 어디를 고쳐야 하는지 알기 어렵고, 같은 값이 두 곳에 복제되면
한쪽만 고쳐져서 조용히 어긋난다(개편 전 등급 경계가 텍스트 출력과 HTML 출력에서
서로 달랐던 것이 그 사례다).

모든 값은 `DEEPCHECK_` 접두사 환경변수로 덮어쓸 수 있다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    return os.environ.get(f"DEEPCHECK_{name}", default)


def _env_opt_str(name: str) -> str | None:
    value = os.environ.get(f"DEEPCHECK_{name}", "").strip()
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"DEEPCHECK_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"DEEPCHECK_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # --- 모델 ---
    classifier_model: str = "dima806/deepfake_vs_real_image_detection"
    whisper_model_size: str = "small"
    # VLM은 교체 가능하다(deepcheck/vlm.py). 지금 구현된 제공자는 ollama뿐이다.
    vlm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"

    # --- 점수 집계 ---
    # 단순 평균은 강한 단일 프레임 신호를 희석시킨다(실측: 8프레임 중 1장이 98.4%
    # fake인데 평균이 13/100까지 떨어졌다). 평균과 최댓값을 섞어 완화한다.
    # 이 가중치는 실측으로 튜닝해야 할 판단값이지 검증된 최적값이 아니다.
    frame_mean_weight: float = 0.6
    frame_peak_weight: float = 0.4
    fake_frame_threshold: float = 0.5
    # 분류기 없이 휴리스틱만으로 점수를 낼 때의 상한 계수.
    heuristic_only_scale: float = 0.55
    # 자가표기("이 영상은 AI로 만들었습니다")는 우리 분류기의 픽셀 추론보다 신뢰도가
    # 높으므로, 임계 이상이면 다른 신호에 희석되지 않게 하한선으로 작동시킨다.
    self_disclosure_gate: float = 50.0
    self_disclosure_floor_ratio: float = 0.9
    # 합성 음성 의심은 미디어 조작 축의 보조 신호로만 반영한다.
    tts_signal_weight: float = 0.35

    # --- 등급 경계 (미디어 조작 위험도 0..100) ---
    level_high: float = 70.0
    level_moderate: float = 45.0
    level_caution: float = 25.0

    # --- 실행 ---
    max_frames: int = 8
    max_video_height: int = 720
    audio_convert_timeout_sec: int = 600
    probe_timeout_sec: int = 120
    frame_extract_timeout_sec: int = 120
    vlm_timeout_sec: int = 180

    # --- 백엔드 ---
    workers: int = 3
    backlog: int = 64
    # job은 결과 리포트 전체를 들고 있어서 무한히 쌓으면 메모리를 계속 먹는다.
    max_retained_jobs: int = 200

    log_level: str = "INFO"
    # json으로 두면 로그가 한 줄짜리 JSON으로 나가서 나중에 수집·검색이 쉽다.
    log_format: str = "text"
    # 허용할 CORS 오리진. 쉼표로 구분한다. "*"는 자격증명 요청과 함께 쓸 수 없다.
    cors_origins: str = "*"


def load_config() -> Config:
    return Config(
        classifier_model=_env_str("CLASSIFIER_MODEL", Config.classifier_model),
        whisper_model_size=_env_str("WHISPER_MODEL_SIZE", Config.whisper_model_size),
        vlm_provider=_env_str("VLM_PROVIDER", Config.vlm_provider),
        ollama_url=_env_str("OLLAMA_URL", Config.ollama_url).rstrip("/"),
        frame_mean_weight=_env_float("FRAME_MEAN_WEIGHT", Config.frame_mean_weight),
        frame_peak_weight=_env_float("FRAME_PEAK_WEIGHT", Config.frame_peak_weight),
        fake_frame_threshold=_env_float("FAKE_FRAME_THRESHOLD", Config.fake_frame_threshold),
        heuristic_only_scale=_env_float("HEURISTIC_ONLY_SCALE", Config.heuristic_only_scale),
        self_disclosure_gate=_env_float("SELF_DISCLOSURE_GATE", Config.self_disclosure_gate),
        self_disclosure_floor_ratio=_env_float(
            "SELF_DISCLOSURE_FLOOR_RATIO", Config.self_disclosure_floor_ratio
        ),
        tts_signal_weight=_env_float("TTS_SIGNAL_WEIGHT", Config.tts_signal_weight),
        level_high=_env_float("LEVEL_HIGH", Config.level_high),
        level_moderate=_env_float("LEVEL_MODERATE", Config.level_moderate),
        level_caution=_env_float("LEVEL_CAUTION", Config.level_caution),
        max_frames=_env_int("MAX_FRAMES", Config.max_frames),
        max_video_height=_env_int("MAX_VIDEO_HEIGHT", Config.max_video_height),
        audio_convert_timeout_sec=_env_int(
            "AUDIO_CONVERT_TIMEOUT_SEC", Config.audio_convert_timeout_sec
        ),
        probe_timeout_sec=_env_int("PROBE_TIMEOUT_SEC", Config.probe_timeout_sec),
        frame_extract_timeout_sec=_env_int(
            "FRAME_EXTRACT_TIMEOUT_SEC", Config.frame_extract_timeout_sec
        ),
        vlm_timeout_sec=_env_int("VLM_TIMEOUT_SEC", Config.vlm_timeout_sec),
        workers=_env_int("WORKERS", Config.workers),
        backlog=_env_int("BACKLOG", Config.backlog),
        max_retained_jobs=_env_int("MAX_RETAINED_JOBS", Config.max_retained_jobs),
        log_level=_env_str("LOG_LEVEL", Config.log_level).upper(),
        log_format=_env_str("LOG_FORMAT", Config.log_format).lower(),
        cors_origins=_env_str("CORS_ORIGINS", Config.cors_origins),
    )


config = load_config()


def default_vlm_model() -> str | None:
    """VLM은 명시적으로 지정했을 때만 실행한다(기본 비활성)."""
    return _env_opt_str("VLM_MODEL")
