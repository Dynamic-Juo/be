"""Harness 상태 머신 테스트.

analysis-runtime.md의 작업 상태 표를 그대로 구현했는지 검증한다. `analyze_url`
자체는 가짜로 바꾸고, harness가 progress_cb/on_partial 콜백을 받아 job.status를
올바르게 전이시키는지에 집중한다.
"""

import os
import queue
import stat
import threading
import time
from dataclasses import replace

import pytest

from backend import harness as harness_module
from backend.harness import Harness
from deepcheck.errors import SessionBusyError


@pytest.fixture
def admission_state_file(tmp_path):
    return str(tmp_path.resolve() / "admission-private" / "state.json")


@pytest.fixture
def harness(admission_state_file):
    h = Harness(
        max_workers=1,
        backlog=4,
        deployment_state_file=admission_state_file,
    )
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
        assert h.stats()["admission_state"] == harness_module.CLOSED

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


class TestDeploymentAdmission:
    @pytest.mark.parametrize("deployment_id", [
        " leading-space", "bad/id", "bad\nline", "x" * 129,
    ])
    def test_잘못된_start_drained_ID는_worker_시작전에_거부한다(
        self, monkeypatch, deployment_id,
    ):
        monkeypatch.setattr(
            harness_module.threading,
            "Thread",
            lambda *args, **kwargs: pytest.fail("invalid config started a worker"),
        )
        with pytest.raises(ValueError) as raised:
            Harness(max_workers=1, backlog=1, start_drained=deployment_id)
        assert str(raised.value) == "start_drained must be a canonical deployment ID"

    def test_빈_start_drained는_기존_accepting으로_시작한다(self):
        h = Harness(max_workers=1, backlog=1, start_drained="")
        try:
            assert h.stats()["admission_state"] == harness_module.ACCEPTING
            assert h.stats()["accepting_jobs"] is True
            assert h.stats()["admission_protocol"] == "unavailable"
        finally:
            h.shutdown()

    def test_start_drained는_생성직후부터_소유_ID로_fence된다(
        self, monkeypatch, admission_state_file,
    ):
        monkeypatch.setattr(
            harness_module,
            "analyze_url",
            lambda **kwargs: _final_payload(kwargs["url"]),
        )
        deployment_id = "startup-deploy-a"
        h = Harness(
            max_workers=1,
            backlog=2,
            start_drained=deployment_id,
            deployment_state_file=admission_state_file,
        )
        try:
            stats = h.stats()
            assert stats["admission_state"] == harness_module.DRAINING
            assert stats["accepting_jobs"] is False
            assert stats["jobs_total"] == 0
            assert deployment_id not in str(stats)
            assert h.begin_drain(deployment_id) is False
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/before-resume", "one", {})

            assert h.resume(deployment_id) is True
            job, _ = h.submit("https://youtu.be/after-resume", "one", {})
            _wait_finished(h, job.id)
            assert job.error is None
        finally:
            h.shutdown()

    def test_startup_fence뒤_동시_submit은_하나도_접수되지_않는다(
        self, admission_state_file,
    ):
        h = Harness(
            max_workers=1,
            backlog=16,
            start_drained="startup-deploy-a",
            deployment_state_file=admission_state_file,
        )
        barrier = threading.Barrier(9)
        outcomes = []

        def submit(index):
            barrier.wait(timeout=2)
            try:
                h.submit(f"https://youtu.be/startup-{index}", str(index), {})
            except queue.Full:
                outcomes.append("rejected")
            else:
                outcomes.append("accepted")

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
        try:
            for thread in threads:
                thread.start()
            barrier.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)
            assert not any(thread.is_alive() for thread in threads)
            assert outcomes == ["rejected"] * 8
            assert h.stats()["jobs_total"] == 0
            assert h.stats()["admission_state"] == harness_module.DRAINING
        finally:
            for thread in threads:
                thread.join(timeout=2)
            h.shutdown()

    def test_drain과_accepting_tombstone은_process_restart에_복원된다(
        self, admission_state_file,
    ):
        deployment_id = "restart-deploy-a"
        first = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        assert first.begin_drain(deployment_id) is True
        first.shutdown()

        draining = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        assert draining.stats()["admission_state"] == harness_module.DRAINING
        assert draining.begin_drain(deployment_id) is False
        assert draining.resume(deployment_id) is True
        draining.shutdown()

        accepting = Harness(
            max_workers=1,
            backlog=1,
            # The bootstrap env remains attached to a restarted container.
            start_drained=deployment_id,
            deployment_state_file=admission_state_file,
        )
        try:
            assert accepting.stats()["admission_state"] == harness_module.ACCEPTING
            assert accepting.resume(deployment_id) is False
        finally:
            accepting.shutdown()

    def test_persisted_owner와_다른_start_drained는_worker전에_충돌한다(
        self, monkeypatch, admission_state_file,
    ):
        first = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        first.begin_drain("persisted-owner-a")
        first.shutdown()

        monkeypatch.setattr(
            harness_module.threading,
            "Thread",
            lambda *args, **kwargs: pytest.fail("conflict started a worker"),
        )
        with pytest.raises(harness_module.AdmissionStateError) as raised:
            Harness(
                max_workers=1,
                backlog=1,
                start_drained="different-owner-b",
                deployment_state_file=admission_state_file,
            )
        assert "persisted-owner-a" not in str(raised.value)
        assert "different-owner-b" not in str(raised.value)

    def test_state_directory와_file은_owner_only_권한을_강제한다(self, tmp_path):
        tmp_path = tmp_path.resolve()
        unsafe_directory = tmp_path / "group-readable"
        unsafe_directory.mkdir(mode=0o755)
        os.chmod(unsafe_directory, 0o755)
        with pytest.raises(harness_module.AdmissionStateError):
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(unsafe_directory / "state.json"),
            )

        state_path = tmp_path / "private" / "state.json"
        valid = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=str(state_path),
        )
        valid.begin_drain("permissions-deploy-a")
        valid.shutdown()
        lock_path = state_path.parent / f".{state_path.name}.lock"
        assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

        os.chmod(state_path, 0o644)
        with pytest.raises(harness_module.AdmissionStateError):
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(state_path),
            )

    def test_같은_state_path의_두번째_process_writer는_startup을_막는다(
        self, admission_state_file,
    ):
        first = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        try:
            with pytest.raises(harness_module.AdmissionStateError):
                Harness(
                    max_workers=1,
                    backlog=1,
                    deployment_state_file=admission_state_file,
                )
        finally:
            first.shutdown()

    def test_symlink_hardlink와_corrupt_marker는_startup을_막는다(self, tmp_path):
        tmp_path = tmp_path.resolve()
        real_directory = tmp_path / "real-private"
        real_directory.mkdir(mode=0o700)
        os.chmod(real_directory, 0o700)
        linked_directory = tmp_path / "linked-private"
        linked_directory.symlink_to(real_directory, target_is_directory=True)
        with pytest.raises(harness_module.AdmissionStateError):
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(linked_directory / "state.json"),
            )

        private = tmp_path / "marker-private"
        private.mkdir(mode=0o700)
        os.chmod(private, 0o700)
        target = private / "target.json"
        target.write_text("not state", encoding="utf-8")
        os.chmod(target, 0o600)
        symlink_marker = private / "symlink.json"
        symlink_marker.symlink_to(target)
        with pytest.raises(harness_module.AdmissionStateError):
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(symlink_marker),
            )

        corrupt = private / "corrupt.json"
        corrupt.write_bytes(b'{"deployment_id":"secret/bad","state":"draining","version":1}\n')
        os.chmod(corrupt, 0o600)
        with pytest.raises(harness_module.AdmissionStateError) as raised:
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(corrupt),
            )
        assert "secret/bad" not in str(raised.value)

        hardlink = private / "hardlink.json"
        os.link(corrupt, hardlink)
        with pytest.raises(harness_module.AdmissionStateError):
            Harness(
                max_workers=1,
                backlog=1,
                deployment_state_file=str(hardlink),
            )

    def test_persistence_failure는_success없이_현재_process도_fail_closed한다(
        self, monkeypatch, admission_state_file,
    ):
        h = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        store = h._admission_store
        assert store is not None
        monkeypatch.setattr(
            store,
            "replace",
            lambda expected, record: (_ for _ in ()).throw(
                harness_module.AdmissionStateError("injected persistence failure")
            ),
        )
        try:
            with pytest.raises(harness_module.AdmissionStateError):
                h.begin_drain("crash-deploy-a")
            assert h.stats()["admission_state"] == harness_module.DRAINING
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/blocked", "one", {})
            with pytest.raises(harness_module.AdmissionStateError):
                h.resume("crash-deploy-a")
            assert h.stats()["admission_state"] == harness_module.DRAINING
        finally:
            h.shutdown()

    @pytest.mark.parametrize(
        "fault,expected_restart_state",
        [
            ("file_fsync", harness_module.ACCEPTING),
            ("replace", harness_module.ACCEPTING),
            ("directory_fsync", harness_module.DRAINING),
        ],
    )
    def test_begin_commit_crash_point는_partial_success를_반환하지_않는다(
        self, monkeypatch, tmp_path, fault, expected_restart_state,
    ):
        case_root = tmp_path.resolve() / fault
        case_root.mkdir(mode=0o700)
        state_path = str(case_root / "admission-private" / "state.json")
        h = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=state_path,
        )
        original_fsync = harness_module.os.fsync
        original_replace = harness_module.os.replace
        calls = 0

        def injected_fsync(descriptor):
            nonlocal calls
            calls += 1
            fail_at = 1 if fault == "file_fsync" else 2
            if fault != "replace" and calls == fail_at:
                raise OSError("injected fsync failure")
            return original_fsync(descriptor)

        def injected_replace(*args, **kwargs):
            if fault == "replace":
                raise OSError("injected replace failure")
            return original_replace(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(harness_module.os, "fsync", injected_fsync)
            scoped.setattr(harness_module.os, "replace", injected_replace)
            with pytest.raises(harness_module.AdmissionStateError):
                h.begin_drain("crash-point-a")
        assert h.stats()["admission_state"] == harness_module.DRAINING
        h.shutdown()

        restarted = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=state_path,
        )
        try:
            assert restarted.stats()["admission_state"] == expected_restart_state
        finally:
            restarted.shutdown()

    @pytest.mark.parametrize(
        "fault,expected_restart_state",
        [
            ("file_fsync", harness_module.DRAINING),
            ("replace", harness_module.DRAINING),
            ("directory_fsync", harness_module.ACCEPTING),
        ],
    )
    def test_resume_commit_crash_point는_memory를_먼저_열지_않는다(
        self, monkeypatch, tmp_path, fault, expected_restart_state,
    ):
        case_root = tmp_path.resolve() / fault
        case_root.mkdir(mode=0o700)
        state_path = str(case_root / "admission-private" / "state.json")
        h = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=state_path,
        )
        h.begin_drain("crash-point-a")
        original_fsync = harness_module.os.fsync
        original_replace = harness_module.os.replace
        calls = 0

        def injected_fsync(descriptor):
            nonlocal calls
            calls += 1
            fail_at = 1 if fault == "file_fsync" else 2
            if fault != "replace" and calls == fail_at:
                raise OSError("injected fsync failure")
            return original_fsync(descriptor)

        def injected_replace(*args, **kwargs):
            if fault == "replace":
                raise OSError("injected replace failure")
            return original_replace(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(harness_module.os, "fsync", injected_fsync)
            scoped.setattr(harness_module.os, "replace", injected_replace)
            with pytest.raises(harness_module.AdmissionStateError):
                h.resume("crash-point-a")
        assert h.stats()["admission_state"] == harness_module.DRAINING
        h.shutdown()

        restarted = Harness(
            max_workers=1,
            backlog=1,
            start_drained="crash-point-a",
            deployment_state_file=state_path,
        )
        try:
            assert restarted.stats()["admission_state"] == expected_restart_state
        finally:
            restarted.shutdown()

    def test_commit뒤_crash는_restart에서_durable_state로_수렴한다(
        self, monkeypatch, admission_state_file,
    ):
        first = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        first_store = first._admission_store
        assert first_store is not None
        original_begin_replace = first_store.replace

        def commit_then_crash(expected, record):
            original_begin_replace(expected, record)
            raise harness_module.AdmissionStateError("injected post-commit crash")

        monkeypatch.setattr(first_store, "replace", commit_then_crash)
        with pytest.raises(harness_module.AdmissionStateError):
            first.begin_drain("crash-deploy-a")
        assert first.stats()["admission_state"] == harness_module.DRAINING
        first.shutdown()

        draining = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        assert draining.stats()["admission_state"] == harness_module.DRAINING
        draining_store = draining._admission_store
        assert draining_store is not None
        original_resume_replace = draining_store.replace

        def resume_commit_then_crash(expected, record):
            original_resume_replace(expected, record)
            raise harness_module.AdmissionStateError("injected post-commit crash")

        monkeypatch.setattr(draining_store, "replace", resume_commit_then_crash)
        with pytest.raises(harness_module.AdmissionStateError):
            draining.resume("crash-deploy-a")
        assert draining.stats()["admission_state"] == harness_module.DRAINING
        draining.shutdown()

        recovered = Harness(
            max_workers=1,
            backlog=1,
            start_drained="crash-deploy-a",
            deployment_state_file=admission_state_file,
        )
        try:
            assert recovered.stats()["admission_state"] == harness_module.ACCEPTING
            assert recovered.resume("crash-deploy-a") is False
        finally:
            recovered.shutdown()

    def test_drain과_resume은_소유_deployment에_대해_idempotent하다(self, harness):
        assert harness.stats()["admission_state"] == harness_module.ACCEPTING
        assert harness.stats()["admission_protocol"] == "durable-api-drain-v1"

        assert harness.begin_drain("deploy-a") is True
        assert harness.begin_drain("deploy-a") is False
        assert harness.stats()["admission_state"] == harness_module.DRAINING
        assert harness.stats()["accepting_jobs"] is False
        assert "deployment_id" not in harness.stats()

        with pytest.raises(harness_module.AdmissionConflictError):
            harness.begin_drain("deploy-b")
        with pytest.raises(harness_module.AdmissionConflictError):
            harness.resume("deploy-b")

        assert harness.resume("deploy-a") is True
        assert harness.resume("deploy-a") is False
        # A delayed drain from the completed deployment is replay, not an
        # idempotent success claiming the service is draining.
        with pytest.raises(harness_module.AdmissionConflictError):
            harness.begin_drain("deploy-a")
        assert harness.stats()["admission_state"] == harness_module.ACCEPTING
        assert harness.begin_drain("deploy-b") is True
        with pytest.raises(harness_module.AdmissionConflictError):
            harness.begin_drain("deploy-a")
        assert harness.resume("deploy-b") is True
        with pytest.raises(harness_module.AdmissionConflictError):
            harness.begin_drain("deploy-a")

    def test_drain은_기존_실행과_대기작업을_취소하지_않고_신규접수만_막는다(
        self, monkeypatch, admission_state_file,
    ):
        started = threading.Event()
        release = threading.Event()

        def blocking(**kwargs):
            started.set()
            release.wait(timeout=2)
            return _final_payload(kwargs["url"])

        monkeypatch.setattr(harness_module, "analyze_url", blocking)
        h = Harness(
            max_workers=1,
            backlog=2,
            deployment_state_file=admission_state_file,
        )
        try:
            running, _ = h.submit("https://youtu.be/running", "one", {})
            assert started.wait(timeout=2)
            waiting, _ = h.submit("https://youtu.be/waiting", "two", {})

            assert h.begin_drain("deploy-a") is True
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/new", "three", {})
            assert not running.finished
            assert waiting.status == harness_module.QUEUED
            assert waiting.error is None
            assert h.stats()["inflight_urls"] == 2

            release.set()
            _wait_finished(h, running.id)
            _wait_finished(h, waiting.id)
            assert running.error is None and waiting.error is None
        finally:
            release.set()
            h.shutdown()

    def test_queue_등록과_drain_전이는_동일_lock으로_직렬화된다(
        self, monkeypatch, admission_state_file,
    ):
        put_entered = threading.Event()
        allow_put = threading.Event()
        drain_returned = threading.Event()
        analysis_release = threading.Event()
        errors = []

        monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: (
            analysis_release.wait(timeout=2), _final_payload(kwargs["url"]))[1])
        h = Harness(
            max_workers=1,
            backlog=1,
            deployment_state_file=admission_state_file,
        )
        original_put = h._queue.put_nowait

        def controlled_put(job):
            put_entered.set()
            assert allow_put.wait(timeout=2)
            original_put(job)

        monkeypatch.setattr(h._queue, "put_nowait", controlled_put)

        def submit():
            try:
                h.submit("https://youtu.be/before-drain", "one", {})
            except BaseException as exc:  # surfaced in the test thread below
                errors.append(exc)

        def drain():
            try:
                h.begin_drain("deploy-a")
            except BaseException as exc:  # surfaced in the test thread below
                errors.append(exc)
            finally:
                drain_returned.set()

        submit_thread = threading.Thread(target=submit)
        drain_thread = threading.Thread(target=drain)
        try:
            submit_thread.start()
            assert put_entered.wait(timeout=2)
            drain_thread.start()
            # submit owns the admission lock while its queue registration is
            # paused, so drain cannot claim completion halfway through it.
            assert not drain_returned.wait(timeout=0.05)
            allow_put.set()
            submit_thread.join(timeout=2)
            drain_thread.join(timeout=2)
            assert not submit_thread.is_alive() and not drain_thread.is_alive()
            assert not errors
            assert h.stats()["admission_state"] == harness_module.DRAINING
            assert h.stats()["inflight_urls"] == 1
            with pytest.raises(queue.Full):
                h.submit("https://youtu.be/after-drain", "two", {})
        finally:
            allow_put.set()
            analysis_release.set()
            submit_thread.join(timeout=2)
            if drain_thread.ident is not None:
                drain_thread.join(timeout=2)
            h.shutdown()
