"""Harness 상태 머신 테스트.

analysis-runtime.md의 작업 상태 표를 그대로 구현했는지 검증한다. `analyze_url`
자체는 가짜로 바꾸고, harness가 progress_cb/on_partial 콜백을 받아 job.status를
올바르게 전이시키는지에 집중한다.
"""

import threading
import time
from dataclasses import replace

import pytest

from backend import harness as harness_module
from backend.harness import Harness
from deepcheck.errors import SessionBusyError


@pytest.fixture
def harness():
    h = Harness(max_workers=1, backlog=4)
    yield h
    h.shutdown()


class TestStateMachine:
    def test_dispatch_직후_processing_collecting로_시작한다(self, monkeypatch, harness):
        started = threading.Event()
        release = threading.Event()

        def fake_analyze_url(url, options=None, progress_cb=None, on_partial=None):
            started.set()
            release.wait(timeout=2)
            return _final_payload(url)

        monkeypatch.setattr(harness_module, "analyze_url", fake_analyze_url)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        assert started.wait(timeout=2)
        assert harness.get(job.id).status == f"{harness_module.PROCESSING_PREFIX}collecting"
        release.set()

    def test_stage_태그로_processing_상태가_바뀐다(self, monkeypatch, harness):
        reached = threading.Event()
        release = threading.Event()

        def fake_analyze_url(url, options=None, progress_cb=None, on_partial=None):
            progress_cb(0.5, "발언 텍스트 확보 중...", "transcribing")
            reached.set()
            release.wait(timeout=2)
            return _final_payload(url)

        monkeypatch.setattr(harness_module, "analyze_url", fake_analyze_url)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        assert reached.wait(timeout=2)
        assert harness.get(job.id).status == f"{harness_module.PROCESSING_PREFIX}transcribing"
        release.set()

    def test_첫_부분결과가_오면_partially_completed로_전환한다(self, monkeypatch, harness):
        reached = threading.Event()
        release = threading.Event()

        def fake_analyze_url(url, options=None, progress_cb=None, on_partial=None):
            on_partial({"face_manipulation": {"status": "no_clear_signs"}})
            reached.set()
            release.wait(timeout=2)
            return _final_payload(url)

        monkeypatch.setattr(harness_module, "analyze_url", fake_analyze_url)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        assert reached.wait(timeout=2)
        current = harness.get(job.id)
        assert current.status == harness_module.PARTIALLY_COMPLETED
        assert current.result.get("face_manipulation") == {"status": "no_clear_signs"}
        release.set()

    def test_partially_completed_이후엔_stage_태그를_무시한다(self, monkeypatch, harness):
        reached = threading.Event()
        release = threading.Event()

        def fake_analyze_url(url, options=None, progress_cb=None, on_partial=None):
            on_partial({"face_manipulation": {}})
            progress_cb(0.9, "정리 중...", "verifying")  # partially_completed 이후의 stage 태그
            reached.set()
            release.wait(timeout=2)
            return _final_payload(url)

        monkeypatch.setattr(harness_module, "analyze_url", fake_analyze_url)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        assert reached.wait(timeout=2)
        # processing:verifying으로 되돌아가지 않고 partially_completed를 유지해야 한다.
        assert harness.get(job.id).status == harness_module.PARTIALLY_COMPLETED
        release.set()

    def test_완료되면_completed로_끝난다(self, monkeypatch, harness):
        monkeypatch.setattr(harness_module, "analyze_url",
                            lambda url, options=None, progress_cb=None, on_partial=None:
                            _final_payload(url, status="complete"))
        job, _ = harness.submit("https://example.com/v", "s1", {})
        _wait_finished(harness, job.id)
        assert harness.get(job.id).status == harness_module.COMPLETED

    def test_일부만_완료되면_completed_with_limitations로_끝난다(self, monkeypatch, harness):
        monkeypatch.setattr(harness_module, "analyze_url",
                            lambda url, options=None, progress_cb=None, on_partial=None:
                            _final_payload(url, status="partial"))
        job, _ = harness.submit("https://example.com/v", "s1", {})
        _wait_finished(harness, job.id)
        assert harness.get(job.id).status == harness_module.COMPLETED_WITH_LIMITATIONS

    def test_실패하면_failed로_끝난다(self, monkeypatch, harness):
        def boom(url, options=None, progress_cb=None, on_partial=None):
            raise RuntimeError("다운로드 실패")

        monkeypatch.setattr(harness_module, "analyze_url", boom)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        _wait_finished(harness, job.id)
        finished = harness.get(job.id)
        assert finished.status == harness_module.FAILED
        assert finished.error["code"] == "internal_error"

    def test_전체_처리시간을_넘기면_timed_out으로_끝난다(self, monkeypatch, harness):
        monkeypatch.setattr(harness_module, "config",
                            replace(harness_module.config, max_processing_sec=0.05))

        def slow_analyze_url(url, options=None, progress_cb=None, on_partial=None):
            time.sleep(0.15)
            return _final_payload(url, status="complete")

        monkeypatch.setattr(harness_module, "analyze_url", slow_analyze_url)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        _wait_finished(harness, job.id)
        assert harness.get(job.id).status == harness_module.TIMED_OUT


def _final_payload(url: str, status: str = "complete") -> dict:
    return {
        "url": url, "analysis_status": status, "media": {}, "stages": {},
        "face_manipulation": {}, "whole_video_generation": {},
        "claim_verification": {}, "transcript": {},
    }


def _wait_finished(harness: Harness, job_id: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if harness.get(job_id).finished:
            return
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id}이 제한 시간 안에 끝나지 않았다")


class TestSessionLimit:
    """M-07: 브라우저 세션별 활성 분석도 1건으로 제한한다."""

    def test_같은_세션이_다른_영상을_동시에_요청하면_거절한다(self, monkeypatch, harness):
        # 워커가 job을 집어가 끝내버리면 "활성" 상태가 아니게 되므로 붙잡아 둔다.
        release = threading.Event()
        monkeypatch.setattr(
            harness_module, "analyze_url",
            lambda url, options=None, progress_cb=None, on_partial=None: (
                release.wait(timeout=2), _final_payload(url))[1],
        )
        harness.submit("https://youtu.be/aaa", "sess-1", {})

        with pytest.raises(SessionBusyError):
            harness.submit("https://youtu.be/bbb", "sess-1", {})
        release.set()

    def test_다른_세션은_영향을_받지_않는다(self, monkeypatch, harness):
        release = threading.Event()
        monkeypatch.setattr(
            harness_module, "analyze_url",
            lambda url, options=None, progress_cb=None, on_partial=None: (
                release.wait(timeout=2), _final_payload(url))[1],
        )
        harness.submit("https://youtu.be/aaa", "sess-1", {})

        job, reused = harness.submit("https://youtu.be/bbb", "sess-2", {})

        assert job and not reused
        release.set()

    def test_같은_URL_재요청은_거절하지_않고_기존_job을_재사용한다(self, monkeypatch, harness):
        """세션 제한보다 중복 URL 재사용이 먼저다. 사용자가 새로고침했다고
        거절당하면 안 된다."""
        release = threading.Event()
        monkeypatch.setattr(
            harness_module, "analyze_url",
            lambda url, options=None, progress_cb=None, on_partial=None: (
                release.wait(timeout=2), _final_payload(url))[1],
        )
        first, _ = harness.submit("https://youtu.be/aaa", "sess-1", {})

        second, reused = harness.submit("https://youtu.be/aaa", "sess-1", {})

        assert reused is True and second.id == first.id
        release.set()

    def test_이전_분석이_끝나면_다시_요청할_수_있다(self, monkeypatch, harness):
        monkeypatch.setattr(
            harness_module, "analyze_url",
            lambda url, options=None, progress_cb=None, on_partial=None: _final_payload(url),
        )
        first, _ = harness.submit("https://youtu.be/aaa", "sess-1", {})
        _wait_finished(harness, first.id)

        job, reused = harness.submit("https://youtu.be/bbb", "sess-1", {})

        assert job and not reused
