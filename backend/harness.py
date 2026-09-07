"""Hermes — 무거운 분석 작업을 처리하는 제한된 워커 풀.

분석 한 건이 수십 초에서 수 분 걸리므로 이벤트 루프에서 돌릴 수 없고, 요청마다
스레드를 무한히 만들 수도 없다. 작은 워커 풀 + 상한이 있는 대기열로 처리한다.

  submit() ──► [bounded queue] ──► [N workers] ──► job 상태/결과
                 (넘치면 429)        (동시 실행)     job_id·session_id로 조회

같은 URL이 이미 처리 중이면 새 job을 만들지 않고 기존 job을 재사용한다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from deepcheck.config import config
from deepcheck.errors import as_error_dict
from deepcheck.logging_setup import current_job_id
from deepcheck.pipeline import AnalysisOptions, analyze_url

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"

_STOP = object()


@dataclass
class Job:
    id: str
    session_id: str  # 처음 요청한 세션
    url: str
    params: dict[str, Any]
    # 같은 URL을 요청해 이 job을 재사용한 세션들. 중복 제거로 job을 합치더라도
    # 나중에 요청한 세션이 자기 목록에서 이 분석을 찾을 수 있어야 한다.
    session_ids: list[str] = field(default_factory=list)
    status: str = QUEUED
    progress: float = 0.0
    message: str = "대기 중"
    result: dict[str, Any] | None = None
    # 실패 시 {code, message, retryable, stage?} 구조. 문자열 하나만 남기면
    # 호출자가 실패 종류를 구분할 수 없다.
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def finished(self) -> bool:
        return self.status in (DONE, ERROR)


class Harness:
    """상한이 있는 대기열 + 워커 풀 + 세션 그룹핑."""

    def __init__(self, max_workers: int | None = None, backlog: int | None = None,
                 max_retained_jobs: int | None = None):
        self.max_workers = max_workers or config.workers
        self.backlog = backlog or config.backlog
        self.max_retained_jobs = max_retained_jobs or config.max_retained_jobs
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                           thread_name_prefix="hermes")
        self._queue: queue.Queue = queue.Queue(maxsize=self.backlog)
        self._jobs: dict[str, Job] = {}
        self._inflight_url: dict[str, str] = {}
        self._lock = threading.Lock()
        self._dispatcher = threading.Thread(target=self._dispatch, name="hermes-dispatcher",
                                            daemon=True)
        self._dispatcher.start()
        logger.info("Hermes 시작: 워커 %d, 대기열 상한 %d", self.max_workers, self.backlog)

    # ---- submission ----
    def submit(self, url: str, session_id: str, params: dict[str, Any]) -> tuple[Job, bool]:
        """작업을 큐에 넣는다. (job, 재사용여부)를 반환한다.

        같은 URL이 이미 대기/실행 중이면 중복 분석 대신 그 job을 돌려준다.
        대기열이 가득 차면 queue.Full을 올리고, 호출자가 429로 변환한다.
        """
        with self._lock:
            existing_id = self._inflight_url.get(url)
            if existing_id and existing_id in self._jobs:
                existing = self._jobs[existing_id]
                if session_id not in existing.session_ids:
                    existing.session_ids.append(session_id)
                logger.info("중복 URL 요청 — 기존 job 재사용: %s", existing_id)
                return existing, True

            job = Job(id=uuid.uuid4().hex, session_id=session_id, url=url, params=params,
                      session_ids=[session_id])
            self._jobs[job.id] = job
            self._inflight_url[url] = job.id
            self._evict_old_jobs_locked()

        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._jobs.pop(job.id, None)
                if self._inflight_url.get(url) == job.id:
                    self._inflight_url.pop(url, None)
            logger.warning("대기열 포화(%d) — 요청 거절", self.backlog)
            raise

        logger.info("job 등록: %s (session %s) %s", job.id, session_id, url)
        return job, False

    # ---- worker flow ----
    def _dispatch(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                logger.info("디스패처 종료")
                break
            try:
                self.executor.submit(self._run, item)
            except Exception:
                # 여기서 예외가 새면 디스패처 스레드가 죽고, 이후 모든 job이 영원히
                # queued에 머문다. 반드시 삼키고 로그만 남긴다.
                logger.exception("job 디스패치 실패: %s", getattr(item, "id", "?"))
                if isinstance(item, Job):
                    self._fail(item, {
                        "code": "dispatch_failed",
                        "message": "작업을 워커에 전달하지 못했습니다.",
                        "retryable": True,
                    })

    def _run(self, job: Job) -> None:
        token = current_job_id.set(job.id)
        job.status = RUNNING
        job.message = "시작"
        job.updated_at = time.time()
        started = time.monotonic()
        try:
            options = AnalysisOptions.from_dict(job.params)
            job.result = analyze_url(
                url=job.url,
                options=options,
                progress_cb=lambda pct, msg: self._update(job, pct, msg),
            )
            job.progress = 1.0
            job.status = DONE
            job.message = "완료"
            logger.info("job 완료: %s (%.1fs)", job.id, time.monotonic() - started)
        except Exception as e:
            # 응답에는 구조화된 에러를, 로그에는 traceback을 남긴다. 둘 중 하나만
            # 있으면 원인 추적이 안 된다.
            error = as_error_dict(e)
            logger.error("job 실패: %s (%s)", job.id, error["code"], exc_info=True)
            self._fail(job, error)
        finally:
            job.updated_at = time.time()
            with self._lock:
                self._inflight_url.pop(job.url, None)
            current_job_id.reset(token)

    def _fail(self, job: Job, error: dict) -> None:
        job.status = ERROR
        job.error = error
        job.message = error.get("message", "오류")
        job.updated_at = time.time()

    # ---- helpers ----
    def _update(self, job: Job, pct: float, msg: str) -> None:
        job.progress = max(0.0, min(1.0, pct))
        job.message = msg
        job.updated_at = time.time()

    def _evict_old_jobs_locked(self) -> None:
        """완료된 오래된 job부터 정리한다.

        job은 결과 리포트 전체를 들고 있어서, 정리하지 않으면 장기 구동 시 메모리가
        계속 늘어난다. 진행 중인 job은 건드리지 않는다.
        """
        if len(self._jobs) <= self.max_retained_jobs:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.finished), key=lambda j: j.updated_at
        )
        removable = len(self._jobs) - self.max_retained_jobs
        for job in finished[:removable]:
            self._jobs.pop(job.id, None)
        logger.info("완료된 job %d건 정리 (보관 상한 %d)",
                    min(removable, len(finished)), self.max_retained_jobs)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def session_jobs(self, session_id: str) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values()
                    if session_id == j.session_id or session_id in j.session_ids]
        return sorted(jobs, key=lambda j: j.created_at)

    def all_jobs(self) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at)

    def stats(self) -> dict:
        with self._lock:
            counts = {QUEUED: 0, RUNNING: 0, DONE: 0, ERROR: 0}
            for j in self._jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            return {
                "jobs_total": len(self._jobs),
                "inflight_urls": len(self._inflight_url),
                "backlog_size": self._queue.qsize(),
                "max_workers": self.max_workers,
                "job_counts": counts,
            }

    def shutdown(self) -> None:
        self._queue.put(_STOP)
