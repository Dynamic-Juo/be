"""OpenAPI response contracts for the existing incremental JSON API.

These models describe responses and validate offline fixtures; routes continue
returning their original dictionaries. In particular, generating documentation
must not drop extra evidence fields or fill missing partial-result blocks with
null/default values. Optional result fields can be absent until their stage ends.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Annotated[str, Field(
    pattern=r"^(queued|processing:.+|partially_completed|completed|completed_with_limitations|failed|timed_out)$",
    description=("작업 생명주기. completed, completed_with_limitations, failed, timed_out은 "
                 "최종 상태이므로 폴링을 종료한다. processing:*의 세부 단계는 확장될 수 있다."),
    examples=["queued", "processing:collecting", "partially_completed", "completed_with_limitations"],
)]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ValidationField(APIModel):
    field: str = Field(description="잘못된 요청 필드 경로", examples=["body.max_frames"])
    reason: str


class APIError(APIModel):
    code: str = Field(description="분기용 오류 코드. 내부 예외나 비밀값은 포함하지 않는다.")
    message: str = Field(description="사용자에게 안내할 실패 이유")
    retryable: bool
    stage: str | None = None
    fields: list[ValidationField] | None = Field(default=None, description="요청 검증 오류에만 제공")


class ErrorResponse(APIModel):
    error: APIError
    request_id: str = Field(description="해당 HTTP 요청의 X-Request-ID와 대응하는 추적 ID")


class HealthResponse(APIModel):
    status: Literal["ok"]


class HarnessStats(APIModel):
    jobs_total: int
    inflight_urls: int
    backlog_size: int
    max_workers: int
    accepting_jobs: bool
    admission_state: Literal["accepting", "draining", "closed"]
    admission_protocol: Literal["durable-api-drain-v1", "unavailable"]
    job_counts: dict[str, int] = Field(description="현재 보관한 작업의 생명주기별 개수")


class ReadyResponse(APIModel):
    status: Literal["ready", "saturated", "draining"] = Field(
        description="draining은 배포를 위해 신규 접수만 닫힌 상태이며, 모든 상태가 HTTP 200이다."
    )
    harness: HarnessStats


class AnalyzeResponse(APIModel):
    job_id: str = Field(description="결과 조회에 사용할 불투명한 작업 ID", examples=["a1b2c3d4000000000000000000000000"])
    status: JobStatus
    session_id: str = Field(description="다음 분석 요청에 재사용할 세션 묶음 ID. 인증 토큰이 아니다.")
    deduplicated: bool = Field(description="같은 정규화 URL의 진행 중 작업을 재사용했는지")


class MediaResult(APIModel):
    title: str | None = None
    uploader: str | None = None
    duration: float | None = Field(default=None, description="영상 길이, 초")
    video_id: str | None = None
    thumbnail: str | None = None
    upload_date: str | None = Field(default=None, description="알려진 경우 YYYY-MM-DD; 알려지지 않으면 null")
    language: str | None = None
    transcript_source: str | None = Field(default=None, description="stt, caption 또는 none")
    stt_coverage_pct: float | None = Field(default=None, description="품질·정확도 점수가 아님. basis/detail을 함께 확인한다.")
    transcript_coverage_basis: str | None = None
    transcript_segment_coverage_pct: float | None = None
    transcript_coverage_detail: str | None = None


class StageResult(APIModel):
    status: Literal["ok", "failed", "skipped"]
    detail: str | None = None
    elapsed_sec: float | None = None
    error: APIError | None = None


class ManipulationResult(APIModel):
    status: Literal["suspected", "no_clear_signs", "inconclusive", "unavailable"]
    status_label: str | None = None
    detail: str | None = None
    evidence: list[str] | None = None
    signals: dict[str, Any] | None = Field(default=None, description="내부 참고 신호. 사용자용 진위 점수로 표시하지 않는다.")


class EvidenceResult(APIModel):
    title: str
    url: str = Field(description="원문/참고 자료 주소. 화면에서 텍스트 및 링크를 안전하게 렌더링한다.")
    source: str = Field(description="검색 제공자 식별자; 예: naver_news, wikipedia")
    published_at: str | None = None
    snippet: str | None = None
    rating: str | None = Field(default=None, description="기존 팩트체크의 표기이며 서비스의 최종 판정이 아니다.")
    content: str | None = None
    content_scope: str | None = Field(default=None, description="search_excerpt는 검색 발췌, original은 별도 확보한 원문")
    provenance_verified: bool | None = None
    independence_group: str | None = None
    source_type: str | None = None
    source_type_label: str | None = None
    is_primary: bool | None = None
    publisher: str | None = None
    cited: bool | None = Field(default=None, description="true: 실제 판정 근거로 사용. false: 참고 자료로 구분한다.")
    cite_reason: str | None = None
    quote: str | None = Field(default=None, description="서버 인용 검증을 통과한 발췌")


class ClaimResult(APIModel):
    text: str
    start: float | None = Field(default=None, description="영상 내 발언 시작, 초. 위치를 모르면 null")
    end: float | None = None
    status: Literal["pending", "verifying", "done", "failed", "timed_out"]
    verdict: Literal["supported", "refuted", "unverified"] | None = Field(
        default=None, description="처리 상태와 별개. pending/verifying의 기본 unverified를 최종 판정으로 표시하지 않는다.")
    verdict_label: str | None = None
    reason: str | None = None
    insufficient_reason: str | None = None
    insufficient_label: str | None = None
    quote: str | None = None
    time_precision: Literal["exact", "approx"] | None = Field(default=None, description="exact도 전사 구간과의 정합성이며 음성 인식 정확도 보장이 아님")
    mentions: list[dict[str, Any]] | None = Field(default=None, description="반복 언급 위치·문맥 정보. 없는 위치를 임의로 채우지 않는다.")
    context: str | None = None
    video_title: str | None = None
    video_published_at: str | None = None
    evidence: list[EvidenceResult] | None = None
    error: APIError | None = Field(default=None, description="전체 작업 실패로 미완료 카드를 종료한 경우 제공될 수 있음")


class ClaimSummary(APIModel):
    total: int | None = None
    pending: int | None = None
    verifying: int | None = None
    done: int | None = None
    failed: int | None = None
    timed_out: int | None = None
    supported: int | None = None
    refuted: int | None = None
    unverified: int | None = None


class ClaimVerificationResult(APIModel):
    status: Literal["analyzed", "unavailable", "no_claims"] = Field(
        description="analyzed는 카드 묶음이 만들어졌다는 뜻이며 pending/verifying 카드가 남아 있을 수 있다.")
    claims: list[ClaimResult] | None = None
    summary: ClaimSummary | None = None
    detail: str | None = None


class TranscriptResult(APIModel):
    summary: str | None = None
    keywords: list[str] | None = None
    tone: str | None = None
    language: str | None = None
    word_count: int | None = None
    coverage_pct: float | None = Field(default=None, description="전사 정확도나 내용 완성도 점수가 아님")
    coverage_basis: str | None = None
    segment_coverage_pct: float | None = None
    coverage_detail: str | None = None
    source: str | None = None
    signals: dict[str, Any] | None = None


class AnalysisResult(APIModel):
    """누적 스냅샷. 처리 중에는 {}, media만, 일부 축만 있을 수 있다."""
    url: str | None = None
    analysis_status: Literal["complete", "partial"] | None = Field(
        default=None, description="최종 결과의 완성도. 작업 종료 여부는 바깥 job.status로 판단한다.")
    media: MediaResult | None = None
    stages: dict[str, StageResult] | None = Field(default=None, description="download/frames/transcript/cleanup 등 단계별 진단")
    face_manipulation: ManipulationResult | None = None
    whole_video_generation: ManipulationResult | None = None
    claim_verification: ClaimVerificationResult | None = None
    transcript: TranscriptResult | None = None


class JobResponse(APIModel):
    id: str = Field(description="접수 응답의 job_id와 같은 값")
    url: str = Field(description="서버에서 정규화한 YouTube 영상 주소")
    params: dict[str, Any] = Field(description="접수한 분석 옵션. provider API 키는 포함하지 않는다.")
    status: JobStatus
    stage: str | None = Field(description="현재 세부 단계. partially_completed 중에도 갱신된다.")
    progress: float = Field(description="0~1 실행 진행률. 정확도·완성도 점수가 아니다.")
    message: str
    result: AnalysisResult | None = Field(description="대기 중 null, 실행 초기 {}, 이후 누적되는 전체 스냅샷")
    error: APIError | None = Field(description="HTTP 200 조회 응답 안에도 분석 실패가 있을 수 있다.")
    created_at: float = Field(description="접수 시각, Unix epoch 초")
    updated_at: float
    started_at: float | None
    display_id: str = Field(description="신고·피드백용 분석 ID. 조회 키가 아니다.", examples=["CN-A1B2-C3D4"])
    created_at_iso: str = Field(description="서버 로컬 시각 ISO 문자열. 현재 시간대 offset은 포함하지 않는다.")
    updated_at_iso: str
    elapsed_sec: float = Field(description="접수 후 총 경과, 대기 포함")
    queue_wait_sec: float = Field(description="워커 시작 전 대기 시간")
    processing_elapsed_sec: float = Field(description="워커 시작 후 경과. 지연 안내에는 이 값을 사용한다.")


# Synthetic examples, not measured results or facts about a real video.
_JOB = {
    "id": "a1b2c3d4000000000000000000000000",
    "url": "https://www.youtube.com/watch?v=cYRkZmBuDqI", "params": {},
    "status": "queued", "stage": None, "progress": 0, "message": "대기 중",
    "result": None, "error": None,
    "created_at": 1000, "updated_at": 1000, "started_at": None,
    "display_id": "CN-A1B2-C3D4", "created_at_iso": "1970-01-01T00:16:40",
    "updated_at_iso": "1970-01-01T00:16:40", "elapsed_sec": 0,
    "queue_wait_sec": 0, "processing_elapsed_sec": 0,
}
JOB_EXAMPLES = {
    "queued": {"summary": "대기 중: result는 null (모의 예시)", "value": _JOB},
    "partial": {
        "summary": "미디어 결과가 먼저 도착한 누적 스냅샷 (모의 예시)",
        "value": {**_JOB, "status": "partially_completed", "stage": "transcribing",
                  "started_at": 1000, "progress": 0.45, "message": "발언 텍스트 확보 중...",
                  "result": {"media": {"title": "문서용 가상 영상", "duration": 140},
                             "face_manipulation": {"status": "inconclusive", "status_label": "판단 보류"}}},
    },
    "limited": {
        "summary": "최종 부분 완료: 지원하지 못한 축을 명시 (모의 예시)",
        "value": {**_JOB, "status": "completed_with_limitations", "stage": "verifying",
                  "started_at": 1000, "progress": 1, "message": "일부 분석만 완료",
                  "result": {"analysis_status": "partial", "media": {"title": "문서용 가상 영상"},
                             "face_manipulation": {"status": "inconclusive", "status_label": "판단 보류"},
                             "whole_video_generation": {"status": "unavailable", "detail": "전체 영상 생성 탐지 모델 미선정"},
                             "claim_verification": {"status": "no_claims", "claims": [], "summary": {}},
                             "stages": {}, "transcript": {"source": "stt"}}},
    },
    "failed": {
        "summary": "분석 실패도 조회는 HTTP 200 (모의 예시)",
        "value": {**_JOB, "status": "failed", "stage": "collecting", "started_at": 1000,
                  "result": {}, "message": "영상을 받지 못했습니다.",
                  "error": {"code": "download_failed", "message": "영상을 받지 못했습니다.",
                            "retryable": True, "stage": "download"}},
    },
}
