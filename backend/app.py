"""분석 파이프라인의 FastAPI 백엔드.

분석이 무거우므로 job 기반으로 동작한다. 요청은 즉시 job_id를 받고, 상태를
폴링해서 결과를 가져간다. 실제 실행은 harness의 워커 풀이 맡는다.
"""

from __future__ import annotations

import queue
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from deepcheck.config import config, default_vlm_model
from deepcheck.logging_setup import setup_logging

from .harness import Harness

setup_logging()

harness = Harness()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    harness.shutdown()


app = FastAPI(title="DeepCheck API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    """분석 요청.

    기본값은 deepcheck.pipeline.AnalysisOptions와 같은 출처(config)를 쓴다.
    """

    url: str
    model_size: str = Field(default_factory=lambda: config.whisper_model_size)
    max_frames: int = Field(default_factory=lambda: config.max_frames)
    use_classifier: bool = True
    vlm_model: str | None = Field(default_factory=default_vlm_model)
    enable_claim_verification: bool = False
    session_id: str | None = None


def _is_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except ValueError:
        return False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "harness": harness.stats()}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    if not _is_http_url(req.url):
        raise HTTPException(422, "유효한 http(s) URL이 아닙니다.")
    session = req.session_id or uuid.uuid4().hex
    params = req.model_dump(exclude={"session_id"})
    try:
        job, reused = harness.submit(url=req.url, session_id=session, params=params)
    except queue.Full:
        # 대기열 포화만 429다. 다른 예외까지 429로 뭉뚱그리면 서버 버그가
        # "서버가 바쁩니다"로 표시돼 원인을 못 찾는다.
        raise HTTPException(429, "서버가 바쁩니다. 잠시 후 다시 시도하세요.")
    return {"job_id": job.id, "status": job.status, "session_id": session,
            "deduplicated": reused}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = harness.get(job_id)
    if job is None:
        raise HTTPException(404, "job_id를 찾을 수 없습니다.")
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> dict:
    """세션을 추적하지 않는 클라이언트를 위한 전체 조회(디버그용)."""
    return {"jobs": [j.to_dict() for j in harness.all_jobs()]}


@app.get("/api/sessions/{session_id}")
def session_jobs(session_id: str) -> dict:
    jobs = harness.session_jobs(session_id)
    return {"session_id": session_id, "count": len(jobs), "jobs": [j.to_dict() for j in jobs]}
