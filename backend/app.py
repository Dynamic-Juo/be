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

import logging
import queue
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from deepcheck.config import config, default_vlm_model
from deepcheck.errors import (
    DeepCheckError,
    JobNotFoundError,
    ServerBusyError,
    UnsupportedURLError,
)
from deepcheck.logging_setup import current_request_id, setup_logging

from .harness import Harness

setup_logging()
logger = logging.getLogger(__name__)

harness = Harness()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("API 시작")
    yield
    logger.info("API 종료 — 대기열 정리")
    harness.shutdown()


app = FastAPI(title="DeepCheck API", version="0.2.0", lifespan=lifespan)

_allowed_origins = [o.strip() for o in config.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # 오리진이 "*"이면 자격증명을 허용할 수 없다(브라우저가 거부한다). 실제로
    # 자격증명이 필요해지면 DEEPCHECK_CORS_ORIGINS에 오리진을 명시해야 한다.
    allow_credentials="*" not in _allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """요청마다 id를 부여하고, 처리 시간과 결과를 한 줄로 남긴다."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = current_request_id.set(request_id)
    started = time.monotonic()
    try:
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
    return _error_response(exc.http_status, exc.code, exc.message, exc.retryable,
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

    url: str = Field(min_length=1, max_length=2048)
    model_size: str = Field(default_factory=lambda: config.whisper_model_size,
                            pattern="^(tiny|base|small|medium|large-v3)$")
    max_frames: int = Field(default_factory=lambda: config.max_frames, ge=0, le=64)
    use_classifier: bool = True
    vlm_model: str | None = Field(default_factory=default_vlm_model, max_length=128)
    enable_claim_verification: bool = True
    caption_policy: str = Field(default_factory=lambda: config.caption_policy,
                                pattern="^(manual|any|off)$")
    session_id: str | None = Field(default=None, max_length=64)


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@app.get("/health")
def health() -> dict:
    """프로세스가 살아있는지. 로드밸런서·도커 healthcheck용."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    """요청을 받을 여력이 있는지. 대기열이 가득 차면 준비되지 않은 것으로 본다."""
    stats = harness.stats()
    is_ready = stats["backlog_size"] < harness.backlog
    return {"status": "ready" if is_ready else "saturated", "harness": stats}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    if not _is_http_url(req.url):
        raise UnsupportedURLError("http(s)로 시작하는 영상 URL을 입력해주세요.", stage="input")
    session = req.session_id or uuid.uuid4().hex
    params = req.model_dump(exclude={"session_id"})
    try:
        job, reused = harness.submit(url=req.url, session_id=session, params=params)
    except queue.Full as e:
        # 대기열 포화만 429다. 다른 예외까지 429로 뭉뚱그리면 서버 버그가
        # "서버가 바쁩니다"로 표시돼 원인을 못 찾는다.
        raise ServerBusyError("요청이 많아 대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요.") from e
    return {"job_id": job.id, "status": job.status, "session_id": session,
            "deduplicated": reused}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = harness.get(job_id)
    if job is None:
        raise JobNotFoundError("해당 job을 찾을 수 없습니다. 만료되었을 수 있습니다.")
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> dict:
    """세션을 추적하지 않는 클라이언트를 위한 전체 조회(디버그용)."""
    return {"jobs": [j.to_dict() for j in harness.all_jobs()]}


@app.get("/api/sessions/{session_id}")
def session_jobs(session_id: str) -> dict:
    jobs = harness.session_jobs(session_id)
    return {"session_id": session_id, "count": len(jobs), "jobs": [j.to_dict() for j in jobs]}
