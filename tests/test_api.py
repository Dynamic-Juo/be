"""API 계약 테스트.

실제 분석은 돌리지 않고(무거움), 요청 검증·에러 봉투·상태 엔드포인트만 확인한다.
"""

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from deepcheck.errors import DownloadError, as_error_dict


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
        assert "ValueError" in payload["message"]

    def test_원인_예외의_타입을_보존한다(self):
        cause = TimeoutError("시간 초과")
        payload = DownloadError("실패", cause=cause).to_dict()
        assert "TimeoutError" in payload["cause"]


class TestBackpressure:
    def test_대기열이_차면_429와_Retry_After를_준다(self, monkeypatch):
        import queue as queue_mod

        from backend import app as app_module

        def saturated(*args, **kwargs):
            raise queue_mod.Full()

        monkeypatch.setattr(app_module.harness, "submit", saturated)
        with TestClient(app) as client:
            r = client.post("/api/analyze", json={"url": "https://example.com/v"})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "server_busy"
        # 재시도 간격을 클라이언트가 추측하지 않게 알려준다.
        assert r.headers["Retry-After"] == "10"
        assert r.json()["error"]["retryable"] is True


class TestSessionGrouping:
    def test_중복_제거된_요청도_요청한_세션에서_보인다(self):
        from backend.harness import Harness

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
            h.shutdown()
