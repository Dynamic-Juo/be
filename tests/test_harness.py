"""Harness 상태 머신 테스트.

analysis-runtime.md의 작업 상태 표를 그대로 구현했는지 검증한다. `analyze_url`
자체는 가짜로 바꾸고, harness가 progress_cb/on_partial 콜백을 받아 job.status를
올바르게 전이시키는지에 집중한다.
"""

import queue
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

    @pytest.mark.parametrize("reported_status", ["partial", "complete"])
    def test_완료된_분석이_하나도_없으면_부분완료가_아닌_실패다(self, monkeypatch, harness, reported_status):
        final = {
            "analysis_status": reported_status,
            "media": {"title": "metadata is not an analysis"},
            "face_manipulation": {"status": "unavailable", "detail": "얼굴 분석 불가"},
            "whole_video_generation": {"status": "unavailable", "detail": "모델 미선정"},
            "claim_verification": {"status": "unavailable", "claims": []},
        }
        monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: final)
        job, _ = harness.submit("https://youtu.be/aaa", "s1", {})
        _wait_finished(harness, job.id)
        assert job.status == harness_module.FAILED
        assert job.error["code"] == "analysis_unavailable"
        assert job.result["face_manipulation"]["detail"] == "얼굴 분석 불가"
        assert job.result["analysis_status"] == "partial"

    def test_검증할_주장_없음은_다른_축이_불가여도_유효한_완료_결과다(self, monkeypatch, harness):
        final = {
            "analysis_status": "partial",
            "face_manipulation": {"status": "unavailable"},
            "whole_video_generation": {"status": "unavailable"},
            "claim_verification": {"status": "no_claims", "claims": []},
        }
        monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: final)
        job, _ = harness.submit("https://youtu.be/aaa", "s1", {})
        _wait_finished(harness, job.id)
        assert job.status == harness_module.COMPLETED_WITH_LIMITATIONS
        assert job.error is None

    def test_완료된_결과가_없어도_시간초과를_우선한다(self, monkeypatch, harness):
        monkeypatch.setattr(harness_module, "config",
                            replace(harness_module.config, max_processing_sec=0.01))

        def slow_unavailable(**kwargs):
            time.sleep(0.03)
            return {"analysis_status": "partial", "face_manipulation": {"status": "unavailable"}}

        monkeypatch.setattr(harness_module, "analyze_url", slow_unavailable)
        job, _ = harness.submit("https://youtu.be/aaa", "s1", {})
        _wait_finished(harness, job.id)
        assert job.status == harness_module.TIMED_OUT

    def test_실패하면_failed로_끝난다(self, monkeypatch, harness):
        def boom(url, options=None, progress_cb=None, on_partial=None):
            raise RuntimeError("다운로드 실패")

        monkeypatch.setattr(harness_module, "analyze_url", boom)
        job, _ = harness.submit("https://example.com/v", "s1", {})
        _wait_finished(harness, job.id)
        finished = harness.get(job.id)
        assert finished.status == harness_module.FAILED
        assert finished.error["code"] == "internal_error"

    @pytest.mark.parametrize("patch,expected", [
        ({"media": {"title": "metadata only"}}, harness_module.FAILED),
        ({"face_manipulation": {"status": "unavailable"}}, harness_module.FAILED),
        ({"face_manipulation": {"status": "inconclusive"}},
         harness_module.COMPLETED_WITH_LIMITATIONS),
        ({"claim_verification": {"status": "no_claims"}},
         harness_module.COMPLETED_WITH_LIMITATIONS),
        ({"claim_verification": {"claims": [{"status": "done"}, {"status": "verifying"}]}},
         harness_module.COMPLETED_WITH_LIMITATIONS),
    ])
    def test_오류_전에_완료된_분석이_있으면_부분완료로_보존한다(self, monkeypatch, harness, patch, expected):
        def fail_after_partial(**kwargs):
            kwargs["on_partial"](patch)
            raise RuntimeError("private service details")

        monkeypatch.setattr(harness_module, "analyze_url", fail_after_partial)
        job, _ = harness.submit("https://youtu.be/aaa", "s1", {})
        _wait_finished(harness, job.id)
        assert job.status == expected
        assert job.result["analysis_status"] == "partial"
        assert "private service details" not in str(job.error)
        verification = job.result.get("claim_verification") or {}
        assert not any(c["status"] == "verifying" for c in verification.get("claims", []))

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
        "face_manipulation": {"status": "no_clear_signs"},
        "whole_video_generation": {"status": "inconclusive" if status == "complete" else "unavailable"},
        "claim_verification": {"status": "no_claims", "claims": []}, "transcript": {},
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

    def test_다른_세션의_작업_재사용도_활성_분석_제한을_우회하지_못한다(self, monkeypatch, harness):
        release = threading.Event()
        monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: (
            release.wait(timeout=2), _final_payload(kwargs["url"]))[1])
        try:
            harness.submit("https://youtu.be/aaa", "sess-1", {})
            harness.submit("https://youtu.be/bbb", "sess-2", {})
            with pytest.raises(SessionBusyError):
                harness.submit("https://youtu.be/bbb", "sess-1", {})
        finally:
            release.set()


class TestBoundedLifecycle:
    def test_실행_중인_작업_뒤에_숨은_무한_대기열이_없다(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def blocking(**kwargs):
            started.set()
            release.wait(timeout=2)
            return _final_payload(kwargs["url"])

        monkeypatch.setattr(harness_module, "analyze_url", blocking)
        h = Harness(max_workers=1, backlog=2)
        try:
            h.submit("https://youtu.be/one", "1", {})
            assert started.wait(timeout=2)
            h.submit("https://youtu.be/two", "2", {})
            h.submit("https://youtu.be/three", "3", {})
            # Give a dispatcher time to drain the queue if one is accidentally
            # reintroduced; admission must remain bounded while the worker waits.
            time.sleep(0.05)
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/four", "4", {})
            assert h.stats()["backlog_size"] == 2
            assert h.stats()["jobs_total"] == 3
        finally:
            release.set()
            h.shutdown()

    def test_종료는_대기작업을_취소하고_워커를_남기지_않는다(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        visited = []

        def blocking(**kwargs):
            visited.append(kwargs["url"])
            started.set()
            release.wait(timeout=2)
            return _final_payload(kwargs["url"])

        monkeypatch.setattr(harness_module, "analyze_url", blocking)
        h = Harness(max_workers=1, backlog=1)
        try:
            h.submit("https://youtu.be/one", "1", {})
            assert started.wait(timeout=2)
            waiting, _ = h.submit("https://youtu.be/two", "2", {})
            h.shutdown(wait=False)
            assert waiting.status == harness_module.FAILED
            assert waiting.error["code"] == "server_shutdown"
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/three", "3", {})
        finally:
            release.set()
            h.shutdown()
        assert visited == ["https://youtu.be/one"]
        assert not any(worker.is_alive() for worker in h._workers)
        assert h.stats()["inflight_urls"] == 0

    @pytest.mark.parametrize("options", [{"backlog": 0}, {"max_workers": 0},
                                         {"max_retained_jobs": -1}])
    def test_무제한으로_해석되는_잘못된_상한을_거부한다(self, options):
        with pytest.raises(ValueError):
            Harness(**options)

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
