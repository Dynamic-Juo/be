"""API 계약 테스트.

실제 분석은 돌리지 않고(무거움), 요청 검증·에러 봉투·상태 엔드포인트만 확인한다.
"""

import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import harness as harness_module
from backend.app import app
from deepcheck.errors import DownloadError, as_error_dict


@pytest.fixture(autouse=True)
def no_real_analysis(monkeypatch):
    # Even valid API requests must never download media or load models in tests.
    monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: {
        "analysis_status": "partial", "url": kwargs["url"],
        "claim_verification": {"status": "no_claims", "claims": []},
    })


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health는_단순히_살아있음만_알린다(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_ready는_대기열_여력을_알린다(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert "backlog_size" in body["harness"]


class TestErrorEnvelope:
    def test_없는_job은_404와_에러코드를_준다(self, client):
        r = client.get("/api/jobs/존재하지않는id")
        assert r.status_code == 404
        body = r.json()
        assert body["error"]["code"] == "job_not_found"
        assert body["error"]["retryable"] is False
        assert body["request_id"]

    def test_http가_아닌_URL은_422(self, client):
        r = client.post("/api/analyze", json={"url": "ftp://example.com/a.mp4"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "unsupported_url"

    def test_검증_실패도_같은_봉투로_나온다(self, client):
        r = client.post("/api/analyze", json={"url": "https://e.com/v", "max_frames": 999})
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "invalid_request"
        assert any(f["field"].endswith("max_frames") for f in body["error"]["fields"])

    def test_요청id가_헤더로_돌아온다(self, client):
        r = client.get("/health", headers={"X-Request-ID": "my-trace-1"})
        assert r.headers["X-Request-ID"] == "my-trace-1"

    def test_접근_로그에_요청id가_남는다(self, client, caplog):
        # contextvar를 로그보다 먼저 되돌리면 여기가 "-"로 찍힌다.
        with caplog.at_level("INFO", logger="backend.app"):
            client.get("/health", headers={"X-Request-ID": "trace-abc"})
        access_logs = [r for r in caplog.records if "GET /health" in r.getMessage()]
        assert access_logs, "접근 로그가 남지 않았다"
        assert access_logs[-1].request_id == "trace-abc"


class TestErrorMapping:
    def test_도메인_예외는_코드와_재시도여부를_들고_다닌다(self):
        err = DownloadError("영상을 받지 못했습니다", stage="download")
        payload = err.to_dict()
        assert payload["code"] == "download_failed"
        assert payload["retryable"] is True
        assert payload["stage"] == "download"

    def test_우리가_정의하지_않은_예외도_같은_모양으로_감싼다(self):
        payload = as_error_dict(ValueError("이상한 값"), stage="frames")
        assert payload["code"] == "internal_error"
        assert payload["retryable"] is False
        assert payload["stage"] == "frames"
        assert "ValueError" not in payload["message"]
        assert "이상한 값" not in payload["message"]

    def test_원인_예외는_공개_응답에_포함하지_않는다(self):
        cause = TimeoutError("시간 초과")
        payload = DownloadError("실패", cause=cause).to_dict()
        assert "cause" not in payload

    def test_도메인_오류에_포함된_비밀값도_공개하지_않는다(self):
        secret = "https://provider.invalid?api_key=not-for-clients"
        payload = DownloadError(secret, cause=RuntimeError(secret)).to_dict()
        assert "not-for-clients" not in str(payload)


class TestBackpressure:
    def test_대기열이_차면_429와_Retry_After를_준다(self, monkeypatch, client):
        import queue as queue_mod

        def saturated(*args, **kwargs):
            raise queue_mod.Full()

        monkeypatch.setattr(app_module.harness, "submit", saturated)
        r = client.post("/api/analyze", json={"url": "https://youtu.be/cYRkZmBuDqI"})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "server_busy"
        # 재시도 간격을 클라이언트가 추측하지 않게 알려준다.
        assert r.headers["Retry-After"] == "10"
        assert r.json()["error"]["retryable"] is True


class TestSessionGrouping:
    def test_중복_제거된_요청도_요청한_세션에서_보인다(self, monkeypatch):
        from backend.harness import Harness

        release = threading.Event()
        monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: (
            release.wait(timeout=2), {
                "analysis_status": "partial", "claim_verification": {"status": "no_claims"},
            })[1])
        h = Harness(max_workers=1, backlog=8)
        try:
            first, reused_a = h.submit("https://example.com/v", "SES_A", {})
            second, reused_b = h.submit("https://example.com/v", "SES_B", {})

            assert reused_a is False and reused_b is True
            assert first.id == second.id
            # 두 번째 요청자가 자기 목록에서 이 분석을 못 찾으면,
            # 프론트에서는 "요청했는데 목록에 없다"가 된다.
            assert [j.id for j in h.session_jobs("SES_A")] == [first.id]
            assert [j.id for j in h.session_jobs("SES_B")] == [first.id]
        finally:
            release.set()
            h.shutdown()


class TestPublicBoundary:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/health", "http://169.254.169.254/latest/meta-data/",
        "http://app/", "http://[::1]/", "https://youtube.com.evil.invalid/watch?v=cYRkZmBuDqI",
        "https://www.youtube.com/redirect?q=http://127.0.0.1",
    ])
    def test_지원하지_않는_대상은_접수하지_않는다(self, client, url):
        response = client.post("/api/analyze", json={"url": url})
        assert response.status_code == 422
        assert app_module.harness.stats()["jobs_total"] == 0

    def test_지원_URL은_정규화해_접수한다(self, client):
        response = client.post("/api/analyze", json={
            "url": "https://youtu.be/cYRkZmBuDqI?si=tracking",
        })
        assert response.status_code == 200
        job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
        assert job["url"] == "https://www.youtube.com/watch?v=cYRkZmBuDqI"
        assert "session_id" not in job and "session_ids" not in job

    @pytest.mark.parametrize("path", ["/api/jobs", "/api/sessions/arbitrary"])
    def test_전체목록과_세션목록은_기본적으로_노출하지_않는다(self, client, path):
        assert client.get(path).status_code == 404

    def test_디버그_목록은_명시적으로_활성화할_수_있다(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, enable_debug_endpoints=True))
        assert client.get("/api/jobs").json() == {"jobs": []}

    def test_요청id의_개행과_과도한_길이를_반사하지_않는다(self, client):
        for request_id in ["injected\nlog", "a" * 1000]:
            response = client.get("/health", headers={"X-Request-ID": request_id})
            assert response.headers["X-Request-ID"] != request_id
            assert len(response.headers["X-Request-ID"]) == 12

    def test_도메인_예외_응답도_내부_메시지를_노출하지_않는다(self, client, monkeypatch):
        def fail(**kwargs):
            raise DownloadError("api_key=hidden-value")
        monkeypatch.setattr(app_module.harness, "submit", fail)
        response = client.post("/api/analyze", json={"url": "https://youtu.be/cYRkZmBuDqI"})
        assert response.status_code == 502
        assert "hidden-value" not in response.text


def test_API_수명주기마다_워커를_새로_만들고_종료한다():
    with TestClient(app):
        first = app_module.harness
    assert app_module.harness is None
    assert not any(worker.is_alive() for worker in first._workers)
    with TestClient(app):
        assert app_module.harness is not first
