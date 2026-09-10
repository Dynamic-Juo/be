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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"DEEPCHECK_{name}")
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    # 프레임 점수를 영상 점수로 합치는 방식. blend | trimmed_mean
    # 두 방식이 정반대 위험을 감수한다(deepcheck/deepfake.py 참고). 정답을 아는
    # 영상 세트가 나오기 전에는 확정할 수 없어 설정으로 열어둔다.
    frame_aggregation: str = "blend"
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

    # --- 발언 텍스트 확보 ---
    # 자막이 있으면 STT를 건너뛴다. manual=사람이 단 자막만, any=자동 자막까지,
    # off=항상 STT. 자동 자막은 결국 다른 STT의 출력이라 기본값은 manual이다.
    caption_policy: str = "manual"

    # --- LLM (주장 추출·판정) ---
    # off면 전부 규칙 기반으로 동작한다. deepseek/openai_compatible/ollama 지원.
    # 외부 API가 죽어도 서비스가 멈추지 않도록 호출부는 항상 규칙으로 폴백한다.
    llm_provider: str = "off"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_timeout_sec: int = 30
    # 판정은 창작이 아니다. 같은 입력에 같은 답이 나오는 편이 디버깅에도 낫다.
    llm_temperature: float = 0.0
    # 주장 추출 방식: rule=정규식 점수, llm=LLM. llm인데 제공자가 없으면 rule로 떨어진다.
    claim_extractor: str = "rule"
    # 전문 기관 판정이 없을 때 LLM으로 판정할지. 끄면 예전처럼 전부 판단 유보.
    llm_verdict: bool = True
    # LLM이 인용한 문장이 실제 근거 원문에 있는지 대조한다. 없으면 판정을 버린다.
    # 환각을 코드로 막는 장치라 끄지 않는 것을 권한다(디버깅용 스위치).
    llm_quote_check: bool = True

    # --- 주장 사실성 검증 ---
    # 영상 하나에서 검증할 주장 수. 0이면 무제한이다.
    # M-04가 "검증 가능한 주장을 모두 검증"으로 확정했고, 우선순위는 처리 순서에만
    # 쓴다고 정했다. 그래서 개수가 아니라 시간(evidence_budget_sec,
    # max_processing_sec)으로 자르고, 못 끝낸 주장은 시간 초과로 표시한다.
    max_claims: int = 0
    # 한 영상 안에서 동시에 검증할 주장 수(M-07: 최대 3건 병렬).
    claim_workers: int = 3
    evidence_per_claim: int = 3
    evidence_timeout_sec: int = 8
    # 같은 호스트로 나가는 근거 검색 요청의 최소 간격(초)과 429 재시도 횟수.
    # 주장을 병렬로 검증하면 같은 API를 동시에 때리게 되고, 위키백과는 그걸
    # 429로 막는다. 병렬은 유지하되 호스트 단위로만 줄을 세운다.
    evidence_min_interval_sec: float = 0.35
    evidence_retry: int = 2
    # 주장 전체에 쓸 수 있는 근거 검색 시간 총량. 넘기면 남은 주장은 검색 없이 유보한다.
    # 응답이 하염없이 늦어지는 것보다 "일부는 확인하지 못했다"고 말하는 편이 낫다.
    evidence_budget_sec: int = 30
    # 사용할 근거 검색 수단(쉼표 구분, 앞에서부터 순서대로 조회).
    # gdelt는 실측에서 16초 이상 걸리고 429가 잦아 기본에서 제외했다.
    # 조회 순서대로 적는다. naver_* 는 NAVER API HUB의 검색 카테고리다
    # (news / encyc / webkr). 자격 정보가 없는 항목은 자동으로 빠진다.
    evidence_providers: str = "factcheck,naver_news,naver_encyc,wikipedia,wikipedia_en"
    # NAVER API HUB. 2026-07-31에 기존 개발자센터 검색 API가 종료되고 이관됐다.
    # 콘솔에서 발급한 Client ID/Secret을 그대로 넣는다.
    naver_client_id: str = ""
    naver_client_secret: str = ""
    naver_base_url: str = "https://naverapihub.apigw.ntruss.com"
    # 전문 기관의 공개 판정 검색(Google Fact Check Tools). 키가 있을 때만 사용한다.
    google_factcheck_api_key: str = ""

    # --- 실행 ---
    # M-02: YouTube가 Shorts로 분류하는 최대 길이가 3분이다. 0이면 검사하지 않는다.
    max_video_sec: int = 180
    max_frames: int = 8
    max_video_height: int = 720
    audio_convert_timeout_sec: int = 600
    probe_timeout_sec: int = 120
    frame_extract_timeout_sec: int = 120
    vlm_timeout_sec: int = 180

    # --- 백엔드 ---
    # 분석 시작부터 이 시간을 넘기면 남은 주장은 검색을 포기하고 시간 초과로
    # 채운다(소프트 타임아웃 — 이미 도는 단계를 강제로 끊진 않는다).
    # docs analysis-runtime.md의 최대 처리 시간(10분)을 따른다.
    max_processing_sec: int = 600
    # M-07: 분석 Worker는 동시에 1건. STT와 딥페이크 모델이 CPU를 크게 쓰기 때문에
    # 영상 여러 편을 동시에 돌리면 서로 느려진다. 대기는 큐가 흡수한다.
    workers: int = 1
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
        frame_aggregation=_env_str("FRAME_AGGREGATION", Config.frame_aggregation).lower(),
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
        caption_policy=_env_str("CAPTION_POLICY", Config.caption_policy).lower(),
        llm_provider=_env_str("LLM_PROVIDER", Config.llm_provider).lower(),
        llm_model=_env_str("LLM_MODEL", Config.llm_model),
        llm_base_url=_env_str("LLM_BASE_URL", Config.llm_base_url),
        llm_api_key=_env_str("LLM_API_KEY", Config.llm_api_key),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", Config.llm_timeout_sec),
        llm_temperature=_env_float("LLM_TEMPERATURE", Config.llm_temperature),
        claim_extractor=_env_str("CLAIM_EXTRACTOR", Config.claim_extractor).lower(),
        llm_verdict=_env_bool("LLM_VERDICT", Config.llm_verdict),
        llm_quote_check=_env_bool("LLM_QUOTE_CHECK", Config.llm_quote_check),
        max_claims=_env_int("MAX_CLAIMS", Config.max_claims),
        claim_workers=_env_int("CLAIM_WORKERS", Config.claim_workers),
        evidence_per_claim=_env_int("EVIDENCE_PER_CLAIM", Config.evidence_per_claim),
        evidence_timeout_sec=_env_int("EVIDENCE_TIMEOUT_SEC", Config.evidence_timeout_sec),
        evidence_min_interval_sec=_env_float(
            "EVIDENCE_MIN_INTERVAL_SEC", Config.evidence_min_interval_sec),
        evidence_retry=_env_int("EVIDENCE_RETRY", Config.evidence_retry),
        evidence_providers=_env_str("EVIDENCE_PROVIDERS", Config.evidence_providers),
        naver_client_id=_env_str("NAVER_CLIENT_ID", Config.naver_client_id),
        naver_client_secret=_env_str("NAVER_CLIENT_SECRET", Config.naver_client_secret),
        naver_base_url=_env_str("NAVER_BASE_URL", Config.naver_base_url),
        evidence_budget_sec=_env_int("EVIDENCE_BUDGET_SEC", Config.evidence_budget_sec),
        google_factcheck_api_key=_env_str(
            "GOOGLE_FACTCHECK_API_KEY", Config.google_factcheck_api_key
        ),
        max_video_sec=_env_int("MAX_VIDEO_SEC", Config.max_video_sec),
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
        max_processing_sec=_env_int("MAX_PROCESSING_SEC", Config.max_processing_sec),
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
