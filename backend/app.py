"""FastAPI backend for the video-AI risk analyzer (Hermes harness wired in).

Job-based, session-aware API. Heavy compute is offloaded to a bounded worker pool
(see harness.py) so many client sessions can fire analyses concurrently without
spawning an unbounded thread per request or blocking the event loop.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .harness import Harness, default_workers

app = FastAPI(title="DeepCheck API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

harness = Harness(max_workers=default_workers())


class AnalyzeRequest(BaseModel):
    url: str
    model_size: str = "small"
    max_frames: int = 8
    use_classifier: bool = True
    vlm_model: str | None = None
    session_id: str | None = None


def _is_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **harness.stats()}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    if not _is_http_url(req.url):
        raise HTTPException(422, "유효한 http(s) URL이 아닙니다.")
    session = req.session_id or _new_session()
    try:
        job, reused = harness.submit(url=req.url, session_id=session, params=req.model_dump())
    except Exception:
        # backlog saturated → backpressure: tell client to retry (429 busy)
        raise HTTPException(429, "서버가 바쁩니다. 잠시 후 다시 시도하세요.")
    return {"job_id": job.id, "status": job.status, "session_id": session,
            "deduplicated": reused}


def _new_session() -> str:
    return uuid.uuid4().hex


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = harness.get(job_id)
    if job is None:
        raise HTTPException(404, "job_id를 찾을 수 없습니다.")
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> dict:
    """All jobs for cases where the client does not track a session (debug)."""
    return {"jobs": [j.to_dict() for j in harness.all_jobs()]}


@app.get("/api/sessions/{session_id}")
def session_jobs(session_id: str) -> dict:
    jobs = harness.session_jobs(session_id)
    return {"session_id": session_id, "count": len(jobs), "jobs": [j.to_dict() for j in jobs]}


@app.on_event("shutdown")
def _shutdown() -> None:  # pragma: no cover
    harness.shutdown()
