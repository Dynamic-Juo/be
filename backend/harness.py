"""Hermes — a bounded worker-pool harness for running heavyweight DeepCheck jobs.

Motivation: each analysis (download → STT → classifier) is heavy and blocking, so
we must NOT run them on the event loop or spawn an unbounded thread per request.
Hermes keeps a small pool of workers + a bounded backlog queue:

  submit() ──► [bounded queue] ──► [N workers] ──► job status/result
                 (backpressure   (concurrency)      queried by session/job id
                  -> 429 busy)

Multi-session / multi-thread friendly:
  - every job carries a session_id so a front-end can group its own requests
  - dedup: the same in-flight URL is served as one job instead of re-analyzed
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any

from deepcheck.cli import analyze_url

# Statuses
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"

_STOP = object()  # internal sentinel to stop the dispatcher


class Status(str, Enum):
    queued = QUEUED
    running = RUNNING
    done = DONE
    error = ERROR


@dataclass
class Job:
    id: str
    session_id: str
    url: str
    params: dict[str, Any]
    status: str = QUEUED
    progress: float = 0.0
    message: str = "대기 중"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class Harness:
    """Bounded worker pool with a backlog queue and session grouping."""

    def __init__(self, max_workers: int = 3, backlog: int = 64):
        self.max_workers = max_executors = max_workers
        self.backlog = backlog
        self.executor = ThreadPoolExecutor(max_workers=max_executors, thread_name_prefix="hermes")
        self._queue: queue.Queue = queue.Queue(maxsize=backlog)
        self._jobs: dict[str, Job] = {}
        self._inflight_url: dict[str, str] = {}  # url -> job_id (queued|running)
        self._lock = threading.Lock()
        self._dispatcher = threading.Thread(target=self._dispatch, name="hermes-dispatcher", daemon=True)
        self._dispatcher.start()

    # ---- submission ----
    def submit(self, url: str, session_id: str, params: dict[str, Any]) -> tuple[Job, bool]:
        """Enqueue a job. Returns (job, reused).

        reused=True means the same URL was already queued/running, so this job is
        deduplicated (multi-thread friendly). Raises queue.Full if the backlog is
        saturated (caller maps to HTTP 429).
        """
        with self._lock:
            # dedup: same URL already queued/running → reuse it (multi-thread friendly)
            existing_id = self._inflight_url.get(url)
            if existing_id:
                return self._jobs[existing_id], True

            job = Job(
                id=uuid.uuid4().hex,
                session_id=session_id,
                url=url,
                params=params,
            )
            self._jobs[job.id] = job
            self._inflight_url[url] = job.id

        # enqueue outside the lock; queue.Full becomes backpressure signal
        try:
            self._queue.put_nowait((job, params))
        except queue.Full:
            with self._lock:
                # roll back
                self._jobs.pop(job.id, None)
                if self._inflight_url.get(url) == job.id:
                    self._inflight_url.pop(url, None)
            raise

        return job, False

    # ---- worker flow ----
    def _dispatch(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            job, params = item
            self.executor.submit(self._run, job, params)

    def _run(self, job: Job, params: dict[str, Any]) -> None:
        job.status = RUNNING
        job.message = "시작"
        try:
            result = analyze_url(
                url=job.url,
                model_size=params.get("model_size", "small"),
                max_frames=params.get("max_frames", 8),
                use_classifier=params.get("use_classifier", True),
                vlm_model=params.get("vlm_model"),
                progress_cb=lambda pct, msg: self._update(job, pct, msg),
            )
            job.result = result
            job.progress = 1.0
            job.status = DONE
            job.message = "완료"
        except Exception as e:  # pragma: no cover
            job.status = ERROR
            job.error = str(e)
            job.message = "오류"
        finally:
            self._reset(parallel=job.url)

    # ---- helpers ----
    def _update(self, job: Job, pct: float, msg: str) -> None:
        job.progress = max(0.0, min(1.0, pct))
        job.message = msg
        job.updated_at = time.time()

    def _reset(self, parallel: str) -> None:
        with self._lock:
            self._inflight_url.pop(parallel, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def session_jobs(self, session_id: str) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.session_id == session_id]
        return sorted(jobs, key=lambda j: j.created_at)

    def all_jobs(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at)

    def stats(self) -> dict:
        with self._lock:
            counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
            for j in self._jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            return {
                "jobs_total": len(self._jobs),
                "inflight_urls": len(self._inflight_url),
                "backlog_size": self._queue.qsize(),
                "max_workers": self.max_workers,
                "status": counts,
            }

    def shutdown(self) -> None:
        self._queue.put(_STOP)


def default_workers() -> int:
    try:
        return int(os.environ.get("DEEPCHECK_WORKERS", "3"))
    except ValueError:
        return 3
