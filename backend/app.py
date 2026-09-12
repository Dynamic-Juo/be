"""분석 파이프라인의 FastAPI 백엔드.

분석이 무거우므로 job 기반으로 동작한다. 요청은 즉시 job_id를 받고, 상태를
폴링해서 결과를 가져간다. 실제 실행은 harness의 워커 풀이 맡는다.

에러는 어디서 나든 같은 모양으로 응답한다:

    {"error": {"code": "...", "message": "...", "retryable": bool},
     "request_id": "..."}

request_id는 응답 헤더(`X-Request-ID`)와 로그에 함께 남아서, 사용자가 겪은 실패
하나를 로그에서 그대로 찾을 수 있다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import queue
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deepcheck.config import (
    config,
    default_vlm_model,
    is_valid_deployment_id,
    is_valid_deployment_token,
)
from deepcheck.errors import (
    DeepCheckError,
    JobNotFoundError,
    ServerBusyError,
)
from deepcheck.logging_setup import current_request_id, setup_logging
from deepcheck.url_policy import normalize_youtube_url

from .harness import AdmissionConflictError, AdmissionStateError, Harness
from .schemas import (
    AnalyzeResponse, ErrorResponse, HealthResponse, JOB_EXAMPLES, JobResponse, ReadyResponse,
)

setup_logging()
logger = logging.getLogger(__name__)

harness: Harness | None = None

_DEPLOYMENT_CONTROL_PATHS = frozenset({
    "/internal/deployment/drain",
    "/internal/deployment/resume",
})
_FORWARDED_CLIENT_HEADERS = frozenset({"forwarded", "x-forwarded-for", "x-real-ip"})


@asynccontextmanager
async def lifespan(_: FastAPI):
    global harness
    # Construct the deployment fence before publishing the Harness or yielding
    # startup, so no request can observe an accepting target first.
    running_harness = Harness(
        start_drained=config.start_drained,
        deployment_state_file=config.deployment_state_file,
    )
    harness = running_harness
    logger.info("API 시작")
    try:
        yield
    finally:
        logger.info("API 종료 — 대기열 정리")
        await asyncio.to_thread(running_harness.shutdown)
        harness = None


def _active_harness() -> Harness:
    if harness is None:
        raise ServerBusyError("서버가 준비 중입니다. 잠시 후 다시 시도해주세요.")
    return harness


app = FastAPI(
    title="Conan AI / DeepCheck API", version="0.2.0", lifespan=lifespan,
    description=(
        "YouTube 영상 분석을 접수하고 작업 ID로 누적 결과를 조회하는 비동기 API입니다. "
        "POST /api/analyze의 HTTP 200은 분석 완료가 아니라 접수/재사용 성공입니다. "
        "GET /api/jobs/{job_id}를 2~3초 간격으로 조회하고 최종 job.status에서 폴링을 멈추세요.\n\n"
        "개발계는 Cloudflare Access로 보호됩니다. 같은 API 도메인의 /health에서 먼저 로그인한 뒤 "
        "/docs의 Try it out을 사용할 수 있습니다. 인증되지 않았거나 세션이 만료되면 Access가 "
        "JSON 대신 로그인 리다이렉트/HTML을 반환할 수 있습니다. 아래 API 오류 봉투와 구분하세요. "
        "문서를 열기만 해서는 분석을 실행하지 않으며, POST Execute는 실제 분석을 접수합니다.\n\n"
        "옵션을 생략하면 실행 서버의 환경 설정을 사용합니다. 정적 문서/개발 복사본과 배포 서버의 "
        "자막·모델 설정은 다를 수 있습니다. 예시는 계약 설명용 모의 데이터이며 실측/판정 결과가 아닙니다."
    ),
    openapi_tags=[
        {"name": "분석", "description": "작업 접수와 누적 결과 폴링"},
        {"name": "상태", "description": "프로세스 및 대기열 상태 확인"},
    ],
    servers=[{"url": "/", "description": "이 문서를 제공하는 API와 같은 origin"}],
    swagger_ui_parameters={"withCredentials": True, "tryItOutEnabled": False},
)

_REQUEST_ID_HEADER = {
    "X-Request-ID": {"description": "요청 추적 ID", "schema": {"type": "string"}},
}
_SERVER_ERROR = {"model": ErrorResponse, "description": "서버 내부 오류", "headers": _REQUEST_ID_HEADER}
_BUSY_ERROR = {
    "model": ErrorResponse,
    "description": "server_busy 또는 session_busy. Retry-After 이후 재시도하거나 기존 작업을 조회하세요.",
    "headers": {**_REQUEST_ID_HEADER, "Retry-After": {"description": "재시도 대기 초", "schema": {"type": "string", "example": "10"}}},
    "content": {"application/json": {"example": {
        "error": {"code": "server_busy", "message": "요청이 많아 대기열이 가득 찼습니다.", "retryable": True},
        "request_id": "example-request",
    }}},
}
_EDGE_LOGIN = {
    "description": "개발계 미인증 시 Cloudflare Access에서 반환할 수 있는 로그인 이동. FastAPI JSON 응답이 아닙니다.",
    "headers": {"Location": {"description": "Access 로그인 주소", "schema": {"type": "string"}}},
}

_allowed_origins = [o.strip() for o in config.cors_origins.split(",") if o.strip()]


@app.middleware("http")
async def request_context(request: Request, call_next):
    """요청마다 id를 부여하고, 처리 시간과 결과를 한 줄로 남긴다."""
    internal_control = request.url.path.rstrip("/") in _DEPLOYMENT_CONTROL_PATHS
    # Internal control calls never trust a caller-selected trace value. Besides
    # log injection, it could accidentally contain the deployment credential.
    supplied_id = ("" if internal_control
                   else request.headers.get("X-Request-ID", ""))
    request_id = (supplied_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied_id)
                  else uuid.uuid4().hex[:12])
    token = current_request_id.set(request_id)
    started = time.monotonic()
    try:
        # Apply authorization before routing so unsupported methods, trailing
        # slashes and malformed bodies cannot reveal a configured control route.
        if internal_control and not _deployment_control_authorized(request):
            response = _deployment_control_not_found()
        else:
            response = await call_next(request)
        elapsed_ms = (time.monotonic() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        # 로그는 contextvar를 되돌리기 전에 남긴다. 순서가 바뀌면 접근 로그의
        # request_id가 "-"로 찍혀서 요청 하나를 로그에서 추적할 수 없다.
        logger.info(
            "%s %s → %s (%.0fms)",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
        return response
    finally:
        current_request_id.reset(token)


# Register CORS after the request middleware so it remains the outer layer and
# decorates an internal 404 exactly as it would a normal missing-route response.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # 오리진이 "*"이면 자격증명을 허용할 수 없다(브라우저가 거부한다). 실제로
    # 자격증명이 필요해지면 DEEPCHECK_CORS_ORIGINS에 오리진을 명시해야 한다.
    allow_credentials="*" not in _allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)


def _error_response(status: int, code: str, message: str, retryable: bool = False,
                    extra: dict | None = None, headers: dict | None = None) -> JSONResponse:
    payload = {
        "error": {"code": code, "message": message, "retryable": retryable, **(extra or {})},
        "request_id": current_request_id.get(),
    }
    return JSONResponse(status_code=status, content=payload, headers=headers)


@app.exception_handler(DeepCheckError)
async def handle_domain_error(_: Request, exc: DeepCheckError) -> JSONResponse:
    logger.warning("도메인 오류: %s — %s", exc.code, exc.message)
    # 429에는 언제 다시 오면 되는지 알려준다. 클라이언트가 재시도 간격을 추측하지 않게.
    headers = {"Retry-After": "10"} if exc.http_status == 429 else None
    return _error_response(exc.http_status, exc.code, exc.to_dict()["message"], exc.retryable,
                           {"stage": exc.stage} if exc.stage else None, headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 검증 실패도 같은 봉투로 내보낸다. 클라이언트가 에러 파싱을 두 벌 만들 필요가 없다.
    fields = [
        {"field": ".".join(str(p) for p in err.get("loc", [])), "reason": err.get("msg", "")}
        for err in exc.errors()
    ]
    return _error_response(422, "invalid_request", "요청 값이 올바르지 않습니다.",
                           extra={"fields": fields})


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    # 예상 못 한 예외의 내부 메시지를 그대로 노출하지 않는다. 원인은 로그에 남긴다.
    logger.exception("처리되지 않은 오류: %s", type(exc).__name__)
    return _error_response(500, "internal_error", "서버 내부 오류가 발생했습니다.")


class AnalyzeRequest(BaseModel):
    """분석 요청. 기본값은 deepcheck.pipeline.AnalysisOptions와 같은 출처(config)를 쓴다."""

    url: str = Field(min_length=1, max_length=2048,
                     description="YouTube watch/shorts/youtu.be 영상 주소. 서버가 정규화하며 임의 외부/내부 URL은 거절합니다.",
                     examples=["https://www.youtube.com/watch?v=cYRkZmBuDqI"])
    model_size: str = Field(default_factory=lambda: config.whisper_model_size,
                            pattern="^(tiny|base|small|medium|large-v3)$",
                            description="고급 음성 인식 옵션. 생략하면 서버 설정을 사용합니다.")
    max_frames: int = Field(default_factory=lambda: config.max_frames, ge=0, le=64,
                            description="고급 프레임 수 옵션. 생략하면 서버 설정을 사용합니다.")
    use_classifier: bool = Field(default=True, description="얼굴 분류기 사용 여부")
    vlm_model: str | None = Field(default_factory=default_vlm_model, max_length=128,
                                 description="고급 모델 옵션. 생략하면 서버 설정을 사용합니다.")
    enable_claim_verification: bool = Field(default=True, description="주장 검증 수행 여부")
    caption_policy: str = Field(default_factory=lambda: config.caption_policy,
                                pattern="^(manual|any|off)$",
                                description="manual: 수동 자막 우선, any: 자동 자막도 허용, off: STT. 생략하면 배포 서버 설정을 따릅니다.")
    session_id: str | None = Field(default=None, max_length=64,
                                   description="첫 요청에는 생략하고 응답값을 이후 요청에 재사용합니다. 인증/접근권한 토큰이 아닙니다.")


class DeploymentControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    deployment_id: str = Field(
        min_length=1,
        max_length=128,
    )


def _is_loopback_client(request: Request) -> bool:
    """Trust the ASGI peer only; forwarded client headers are not authority."""
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _deployment_control_authorized(request: Request) -> bool:
    token = config.deployment_token
    header_names = {name.lower() for name in request.headers.keys()}
    # Uvicorn can trust proxy headers from selected peers and replace the ASGI
    # client address. This endpoint is direct-loopback only, so any forwarded
    # client metadata makes the request ineligible even if the rewritten address
    # looks like loopback.
    if not is_valid_deployment_token(token) or header_names & _FORWARDED_CLIENT_HEADERS:
        return False
    assert token is not None
    authorization = request.headers.get("Authorization", "")
    scheme, separator, credential = authorization.partition(" ")
    # Byte comparison also makes malformed/non-ASCII credentials a clean
    # authentication miss instead of an exception with distinguishable output.
    token_matches = secrets.compare_digest(credential.encode(), token.encode())
    return (
        _is_loopback_client(request)
        and bool(separator)
        and scheme.lower() == "bearer"
        and token_matches
    )


def _deployment_control_not_found() -> JSONResponse:
    # Missing configuration, remote callers and bad credentials deliberately
    # receive the same status and body without disclosing which check failed.
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


async def _deployment_control_id(request: Request) -> tuple[str | None, JSONResponse | None]:
    try:
        raw = await request.body()
        if len(raw) > 1024:
            raise ValueError("request too large")
        payload = DeploymentControlRequest.model_validate(json.loads(raw))
        if not is_valid_deployment_id(payload.deployment_id):
            raise ValueError("invalid deployment id")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError, TypeError):
        return None, _error_response(
            422, "invalid_request", "요청 값이 올바르지 않습니다."
        )
    return payload.deployment_id, None


@app.get("/health", tags=["상태"], operation_id="getHealth", summary="프로세스 상태",
         response_model=None,
         responses={200: {"model": HealthResponse, "headers": _REQUEST_ID_HEADER}, 302: _EDGE_LOGIN})
def health() -> dict:
    """프로세스가 살아있는지. 로드밸런서·도커 healthcheck용."""
    return {"status": "ok"}


@app.get("/ready", tags=["상태"], operation_id="getReadiness", summary="대기열 여력",
         response_model=None,
         responses={200: {"model": ReadyResponse, "description": "ready, saturated 또는 draining (모두 HTTP 200)", "headers": _REQUEST_ID_HEADER},
                    429: _BUSY_ERROR, 500: _SERVER_ERROR})
def ready() -> dict:
    """접수 상태와 대기열 여력. drain 중인 기존 작업은 계속 처리한다."""
    active = _active_harness()
    stats = active.stats()
    if stats["admission_state"] == "draining":
        status = "draining"
    else:
        is_ready = stats["accepting_jobs"] and stats["backlog_size"] < active.backlog
        status = "ready" if is_ready else "saturated"
    return {"status": status, "harness": stats}


@app.post("/internal/deployment/drain", include_in_schema=False, response_model=None)
async def begin_deployment_drain(request: Request) -> dict | JSONResponse:
    if not _deployment_control_authorized(request):
        return _deployment_control_not_found()
    deployment_id, error = await _deployment_control_id(request)
    if error is not None:
        return error
    assert deployment_id is not None
    try:
        changed = _active_harness().begin_drain(deployment_id)
    except AdmissionConflictError:
        return _error_response(
            409, "deployment_conflict", "배포 제어 상태가 충돌했습니다."
        )
    except AdmissionStateError:
        return _error_response(
            503, "deployment_state_unavailable", "배포 제어 상태를 저장할 수 없습니다."
        )
    return {"status": "draining", "changed": changed}


@app.post("/internal/deployment/resume", include_in_schema=False, response_model=None)
async def resume_deployment_admission(request: Request) -> dict | JSONResponse:
    if not _deployment_control_authorized(request):
        return _deployment_control_not_found()
    deployment_id, error = await _deployment_control_id(request)
    if error is not None:
        return error
    assert deployment_id is not None
    try:
        changed = _active_harness().resume(deployment_id)
    except AdmissionConflictError:
        return _error_response(
            409, "deployment_conflict", "배포 제어 상태가 충돌했습니다."
        )
    except AdmissionStateError:
        return _error_response(
            503, "deployment_state_unavailable", "배포 제어 상태를 저장할 수 없습니다."
        )
    return {"status": "accepting", "changed": changed}


@app.post("/api/analyze", tags=["분석"], operation_id="submitAnalysis", summary="영상 분석 접수",
          description=("기본 요청은 url만 보내면 됩니다. 같은 정규화 URL의 진행 중 작업은 재사용합니다. "
                       "응답 status는 반환 시점의 상태이며 반드시 queued인 것은 아닙니다. "
                       "한 세션은 다른 영상 두 건을 동시에 접수할 수 없습니다. "
                       "다운로드/모델 오류 등 비동기 실패는 이후 job.error로 조회합니다."),
          response_model=None,
          responses={
              200: {"model": AnalyzeResponse, "description": "접수 또는 진행 중 작업 재사용 성공 (완료 아님)",
                    "headers": _REQUEST_ID_HEADER,
                    "content": {"application/json": {"example": {
                        "job_id": "a1b2c3d4000000000000000000000000", "status": "queued",
                        "session_id": "example-session", "deduplicated": False,
                    }}}},
              302: _EDGE_LOGIN,
              422: {"model": ErrorResponse, "description": "invalid_request 또는 unsupported_url",
                    "headers": _REQUEST_ID_HEADER,
                    "content": {"application/json": {"example": {
                        "error": {"code": "unsupported_url", "message": "YouTube 영상 주소를 입력해주세요.",
                                  "retryable": False, "stage": "input"}, "request_id": "example-request",
                    }}}},
              429: _BUSY_ERROR, 500: _SERVER_ERROR,
          })
def analyze(req: AnalyzeRequest) -> dict:
    url = normalize_youtube_url(req.url)
    session = req.session_id or uuid.uuid4().hex
    params = req.model_dump(exclude={"session_id"})
    params["url"] = url
    try:
        job, reused = _active_harness().submit(url=url, session_id=session, params=params)
    except queue.Full as e:
        # 대기열 포화만 429다. 다른 예외까지 429로 뭉뚱그리면 서버 버그가
        # "서버가 바쁩니다"로 표시돼 원인을 못 찾는다.
        raise ServerBusyError("요청이 많아 대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요.") from e
    return {"job_id": job.id, "status": job.status, "session_id": session,
            "deduplicated": reused}


@app.get("/api/jobs/{job_id}", tags=["분석"], operation_id="getAnalysisJob", summary="작업 상태와 누적 결과 조회",
         description=("매 응답은 변경분이 아닌 현재 전체 스냅샷입니다. result는 null/빈 객체/일부 블록/최종 결과일 수 있습니다. "
                      "completed, completed_with_limitations, failed, timed_out에서 폴링을 종료합니다. "
                      "HTTP 200만으로 분석 성공을 판단하지 말고 job.status와 각 축/주장 status를 확인하세요. "
                      "작업은 메모리에 보관되어 서버 재시작 또는 보관 상한에 따른 정리 후 404가 될 수 있습니다. "
                      "job_id는 공개적으로 나열하지 말고 필요한 사용자에게만 전달하세요."),
         response_model=None,
         responses={
             200: {"model": JobResponse, "headers": _REQUEST_ID_HEADER,
                   "content": {"application/json": {"examples": JOB_EXAMPLES}}},
             302: _EDGE_LOGIN,
             404: {"model": ErrorResponse, "description": "작업이 없거나 만료/서버 재시작으로 사라짐", "headers": _REQUEST_ID_HEADER},
             422: {"model": ErrorResponse, "description": "요청 검증 오류", "headers": _REQUEST_ID_HEADER},
             429: _BUSY_ERROR, 500: _SERVER_ERROR,
         })
def job_status(job_id: str = Path(description="접수 응답의 job_id. display_id(CN-...)가 아닙니다.")) -> dict:
    job = _active_harness().get(job_id)
    if job is None:
        raise JobNotFoundError("해당 job을 찾을 수 없습니다. 만료되었을 수 있습니다.")
    payload = job.to_dict()
    # Deduplicated jobs can have several unrelated requesters. Session grouping
    # metadata is internal and must not disclose their identifiers to each other.
    payload.pop("session_id", None)
    payload.pop("session_ids", None)
    return payload


@app.get("/api/jobs", include_in_schema=False)
def list_jobs() -> dict:
    """세션을 추적하지 않는 클라이언트를 위한 전체 조회(디버그용)."""
    if not config.enable_debug_endpoints:
        raise JobNotFoundError("해당 경로를 사용할 수 없습니다.")
    return {"jobs": [j.to_dict() for j in _active_harness().all_jobs()]}


@app.get("/api/sessions/{session_id}", include_in_schema=False)
def session_jobs(session_id: str) -> dict:
    # Caller-provided session IDs are grouping labels, not authentication.
    if not config.enable_debug_endpoints:
        raise JobNotFoundError("해당 경로를 사용할 수 없습니다.")
    jobs = _active_harness().session_jobs(session_id)
    return {"session_id": session_id, "count": len(jobs), "jobs": [j.to_dict() for j in jobs]}
