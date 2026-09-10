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
from datetime import datetime
from typing import Any

from deepcheck.config import config
from deepcheck.errors import SessionBusyError, as_error_dict
from deepcheck.logging_setup import current_job_id
from deepcheck.pipeline import AnalysisOptions, analyze_url

logger = logging.getLogger(__name__)

# analysis-runtime.md의 작업 상태 표를 그대로 따른다. queued/processing:* 동안은
# 아직 아무 결과도 못 준 상태고, 첫 부분 결과가 오는 순간 partially_completed로
# 넘어가서 그 뒤로는 processing:* 세부 단계를 다시 밟지 않는다.
QUEUED = "queued"
PROCESSING_PREFIX = "processing:"
PARTIALLY_COMPLETED = "partially_completed"
COMPLETED = "completed"
COMPLETED_WITH_LIMITATIONS = "completed_with_limitations"
FAILED = "failed"
TIMED_OUT = "timed_out"

_TERMINAL = (COMPLETED, COMPLETED_WITH_LIMITATIONS, FAILED, TIMED_OUT)

_STOP = object()


def _isoformat(epoch: float) -> str:
    """epoch 초를 로컬 시간 ISO 문자열로 바꾼다(화면 표시용)."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


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
    stage: str | None = None
    progress: float = 0.0
    message: str = "대기 중"
    result: dict[str, Any] | None = None
    # 실패 시 {code, message, retryable, stage?} 구조. 문자열 하나만 남기면
    # 호출자가 실패 종류를 구분할 수 없다.
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None

    @property
    def display_id(self) -> str:
        """사람이 옮겨 적을 수 있는 분석 ID(`CN-A1B2-C3D4`).

        M-06이 "결과 화면에 표시된 분석 ID를 피드백 폼에 첨부"하도록 정했는데,
        32자 hex는 사람이 옮겨 적을 수 없다. job id 앞부분에서 만들어 짧게 줄이되
        서버 로그의 job_id로 되짚을 수 있도록 계산식을 고정한다.

        앞 8자만 쓰므로 이론상 충돌할 수 있다. 사용자 신고를 job과 잇는 용도이지
        조회 키가 아니므로(조회는 job_id로 한다) 이 정도로 충분하다.
        """
        head = self.id[:8].upper()
        return f"CN-{head[:4]}-{head[4:]}"

    def to_dict(self) -> dict:
        data = asdict(self)
        # 화면이 분석 ID와 분석 시각을 결과 요약에 함께 표시한다. epoch 실수는
        # 그대로 쓰기 어려우므로 표시용 문자열을 같이 내려준다.
        data["display_id"] = self.display_id
        data["created_at_iso"] = _isoformat(self.created_at)
        data["updated_at_iso"] = _isoformat(self.updated_at)
        data["elapsed_sec"] = round(
            (time.time() if not self.finished else self.updated_at) - self.created_at, 1
        )
        end = time.time() if not self.finished else self.updated_at
        queue_end = self.started_at if self.started_at is not None else end
        data["queue_wait_sec"] = round(queue_end - self.created_at, 1)
        data["processing_elapsed_sec"] = (
            round(end - self.started_at, 1) if self.started_at is not None else 0.0
        )
        return data

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL


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
    def _active_job_for_session_locked(self, session_id: str) -> Job | None:
        """이 세션이 아직 끝나지 않은 분석을 갖고 있으면 그 job. 락 안에서 부른다."""
        for job in self._jobs.values():
            if not job.finished and session_id in job.session_ids:
                return job
        return None

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

            # 같은 URL 재사용은 위에서 걸렀으니, 여기 걸리는 건 이 세션이 다른
            # 영상을 이미 돌리고 있는 경우다(M-07: 세션별 활성 분석 1건).
            active = self._active_job_for_session_locked(session_id)
            if active is not None:
                logger.info("세션 %s가 이미 분석 중(job %s) — 새 요청 거절",
                            session_id, active.id)
                raise SessionBusyError(
                    "이미 분석 중인 영상이 있습니다. 완료된 뒤에 다시 시도해주세요."
                )

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
        job.status = f"{PROCESSING_PREFIX}collecting"
        job.stage = "collecting"
        job.started_at = time.time()
        job.message = "시작"
        job.result = {}
        job.updated_at = time.time()
        started = time.monotonic()
        try:
            options = AnalysisOptions.from_dict(job.params)
            final = analyze_url(
                url=job.url,
                options=options,
                progress_cb=lambda pct, msg, stage=None: self._update(job, pct, msg, stage),
                on_partial=lambda patch: self._merge_partial(job, patch),
            )
            elapsed = time.monotonic() - started
            with self._lock:
                job.result = final  # 부분 스냅샷을 완성된 결과로 최종 정리
            job.progress = 1.0
            if elapsed > config.max_processing_sec:
                job.status = TIMED_OUT
                job.message = "시간 초과"
            elif final.get("analysis_status") == "complete":
                job.status = COMPLETED
                job.message = "완료"
            else:
                job.status = COMPLETED_WITH_LIMITATIONS
                job.message = "일부 분석만 완료"
            logger.info("job 종료: %s status=%s (%.1fs)", job.id, job.status, elapsed)
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
        job.status = FAILED
        job.error = error
        job.message = error.get("message", "오류")
        job.updated_at = time.time()

    # ---- helpers ----
    def _update(self, job: Job, pct: float, msg: str, stage: str | None = None) -> None:
        job.progress = max(0.0, min(1.0, pct))
        job.message = msg
        if stage and job.status not in _TERMINAL:
            job.stage = stage
        # partially_completed로 넘어간 뒤에는 세부 단계 태그를 더 이상 반영하지
        # 않는다 — "첫 결과가 준비되면 partially_completed로 전환"이 그 뒤로도
        # processing:* 로 되돌아가지 않는다는 뜻이기 때문이다.
        if stage and job.status not in (PARTIALLY_COMPLETED,) and job.status not in _TERMINAL:
            job.status = f"{PROCESSING_PREFIX}{stage}"
        job.updated_at = time.time()

    def _merge_partial(self, job: Job, patch: dict) -> None:
        with self._lock:
            job.result.update(patch)
            has_analysis = any(k in patch for k in (
                "face_manipulation", "whole_video_generation", "claim_verification"))
            if has_analysis and job.status not in (PARTIALLY_COMPLETED,) and job.status not in _TERMINAL:
                job.status = PARTIALLY_COMPLETED
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
            # status는 processing:collecting처럼 세부 단계별로 값이 늘어날 수 있어
            # 고정된 키 집합을 미리 두지 않고, 실제로 관측된 값만 센다.
            counts: dict[str, int] = {}
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
