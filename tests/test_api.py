"""API 계약 테스트.

실제 분석은 돌리지 않고(무거움), 요청 검증·에러 봉투·상태 엔드포인트만 확인한다.
"""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import harness as harness_module
from backend.app import app
from deepcheck.errors import DownloadError, as_error_dict


DEPLOYMENT_TOKEN = "deployment-control-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def no_real_analysis(monkeypatch):
    # Even valid API requests must never download media or load models in tests.
    monkeypatch.setattr(harness_module, "analyze_url", lambda **kwargs: {
        "analysis_status": "partial", "url": kwargs["url"],
        "claim_verification": {"status": "no_claims", "claims": []},
    })


@pytest.fixture
def client(monkeypatch, tmp_path):
    tmp_path = tmp_path.resolve()
    monkeypatch.setattr(app_module, "config", replace(
        app_module.config,
        start_drained=None,
        deployment_state_file=str(tmp_path / "admission-private" / "state.json"),
    ))
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
        assert body["harness"]["admission_state"] == "accepting"
        assert body["harness"]["admission_protocol"] == "durable-api-drain-v1"


class TestDeploymentControl:
    def test_start_drained는_빈값만_accepting이고_잘못된_ID는_startup을_막는다(
        self, monkeypatch, tmp_path,
    ):
        from deepcheck.config import load_config

        monkeypatch.setenv(
            "DEEPCHECK_DEPLOYMENT_STATE_FILE",
            str(tmp_path / "admission-private" / "state.json"),
        )
        monkeypatch.delenv("DEEPCHECK_START_DRAINED", raising=False)
        assert load_config().start_drained is None
        monkeypatch.setenv("DEEPCHECK_START_DRAINED", "")
        assert load_config().start_drained is None

        deployment_id = "startup-deployment-sensitive-id"
        monkeypatch.setenv("DEEPCHECK_START_DRAINED", deployment_id)
        loaded = load_config()
        assert loaded.start_drained == deployment_id
        assert deployment_id not in repr(loaded)

        for invalid in (" leading-space", "bad/id", "bad\nline", "x" * 129):
            monkeypatch.setenv("DEEPCHECK_START_DRAINED", invalid)
            with pytest.raises(ValueError) as raised:
                load_config()
            assert invalid not in str(raised.value)

    def test_배포토큰_설정은_길이와_header_safe_문자를_검증한다(
        self, monkeypatch, tmp_path,
    ):
        from deepcheck.config import load_config

        monkeypatch.setenv(
            "DEEPCHECK_DEPLOYMENT_STATE_FILE",
            str(tmp_path / "admission-private" / "state.json"),
        )
        for invalid in ("short", "x" * 513, "한" * 32, "x" * 16 + " " + "y" * 16):
            monkeypatch.setenv("DEEPCHECK_DEPLOYMENT_TOKEN", invalid)
            assert load_config().deployment_token is None
        monkeypatch.setenv("DEEPCHECK_DEPLOYMENT_TOKEN", DEPLOYMENT_TOKEN)
        loaded = load_config()
        assert loaded.deployment_token == DEPLOYMENT_TOKEN
        assert DEPLOYMENT_TOKEN not in repr(loaded)

    def test_배포제어_설정은_안전한_절대_state_path를_필수로_한다(
        self, monkeypatch,
    ):
        from deepcheck.config import load_config

        monkeypatch.delenv("DEEPCHECK_DEPLOYMENT_STATE_FILE", raising=False)
        monkeypatch.setenv("DEEPCHECK_DEPLOYMENT_TOKEN", DEPLOYMENT_TOKEN)
        with pytest.raises(ValueError):
            load_config()

        monkeypatch.delenv("DEEPCHECK_DEPLOYMENT_TOKEN", raising=False)
        monkeypatch.setenv("DEEPCHECK_START_DRAINED", "startup-deploy-a")
        with pytest.raises(ValueError):
            load_config()

        monkeypatch.delenv("DEEPCHECK_START_DRAINED", raising=False)
        for invalid in (
            "relative/state.json",
            "/tmp/../unsafe.json",
            "/tmp//unsafe.json",
            "/tmp/trailing/",
        ):
            monkeypatch.setenv("DEEPCHECK_DEPLOYMENT_STATE_FILE", invalid)
            with pytest.raises(ValueError) as raised:
                load_config()
            assert invalid not in str(raised.value)

    def test_start_drained_app은_첫_요청부터_닫히고_소유_ID로만_resume한다(
        self, monkeypatch, tmp_path,
    ):
        tmp_path = tmp_path.resolve()
        deployment_id = "startup-deploy-a"
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config,
            deployment_token=DEPLOYMENT_TOKEN,
            start_drained=deployment_id,
            deployment_state_file=str(
                tmp_path / "admission-private" / "state.json"
            ),
        ))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        headers = {"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"}

        with TestClient(app) as startup_client:
            readiness = startup_client.get("/ready")
            assert readiness.status_code == 200
            assert readiness.json()["status"] == "draining"
            assert readiness.json()["harness"]["accepting_jobs"] is False
            assert readiness.json()["harness"]["admission_state"] == "draining"
            assert deployment_id not in readiness.text

            rejected = startup_client.post(
                "/api/analyze", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
            )
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "server_busy"

            wrong_owner = startup_client.post(
                "/internal/deployment/resume",
                json={"deployment_id": "startup-deploy-b"},
                headers=headers,
            )
            assert wrong_owner.status_code == 409
            assert deployment_id not in wrong_owner.text

            resumed = startup_client.post(
                "/internal/deployment/resume",
                json={"deployment_id": deployment_id},
                headers=headers,
            )
            assert resumed.status_code == 200
            assert resumed.json() == {"status": "accepting", "changed": True}
            assert deployment_id not in resumed.text
            assert startup_client.get("/ready").json()["status"] == "ready"

        # Docker keeps START_DRAINED in the container environment across a
        # process restart. The durable accepting tombstone must therefore win
        # over the same bootstrap ID and prevent an accidental re-drain.
        with TestClient(app) as restarted_client:
            assert restarted_client.get("/ready").json()["status"] == "ready"
            retry = restarted_client.post(
                "/internal/deployment/resume",
                json={"deployment_id": deployment_id},
                headers=headers,
            )
            assert retry.status_code == 200
            assert retry.json() == {"status": "accepting", "changed": False}

    def test_잘못된_start_drained는_lifespan이_열리기전에_실패한다(
        self, monkeypatch, tmp_path,
    ):
        tmp_path = tmp_path.resolve()
        invalid = "sensitive/bad-id"
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config,
            start_drained=invalid,
            deployment_state_file=str(
                tmp_path / "admission-private" / "state.json"
            ),
        ))
        with pytest.raises(ValueError) as raised:
            with TestClient(app):
                pytest.fail("invalid startup configuration entered lifespan")
        assert invalid not in str(raised.value)

    @pytest.mark.parametrize("host,expected", [
        ("127.0.0.1", True),
        ("127.12.34.56", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("10.0.0.1", False),
        ("testclient", False),
        ("not-an-ip", False),
    ])
    def test_ASGI_peer의_loopback만_신뢰한다(self, host, expected):
        request = SimpleNamespace(client=SimpleNamespace(host=host))
        assert app_module._is_loopback_client(request) is expected

    @pytest.mark.parametrize("path", [
        "/internal/deployment/drain",
        "/internal/deployment/resume",
    ])
    def test_토큰_미설정_오류토큰_비loopback은_동일한_404다(
        self, client, monkeypatch, path,
    ):
        payload = {"deployment_id": "deploy-a"}

        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=None))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        unconfigured = client.post(
            path, json=payload, headers={"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"}
        )
        unconfigured_method_probe = client.get(
            f"{path}/", headers={"X-Request-ID": "missing-route-probe"}
        )
        assert unconfigured_method_probe.headers["X-Request-ID"] != "missing-route-probe"

        weak_token = "too-short"
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=weak_token))
        weak_configuration = client.post(
            path, json=payload, headers={"Authorization": f"Bearer {weak_token}"}
        )

        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=DEPLOYMENT_TOKEN))
        wrong_token = client.post(
            path, content=b"not-json", headers={"Authorization": "Bearer wrong-token"}
        )

        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: False)
        remote = client.post(
            path,
            json=payload,
            headers={"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"},
        )
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        forwarded_loopback = client.post(
            path,
            json=payload,
            headers={
                "Authorization": f"Bearer {DEPLOYMENT_TOKEN}",
                "X-Forwarded-For": "127.0.0.1",
            },
        )

        for response in (
            unconfigured, unconfigured_method_probe, weak_configuration,
            wrong_token, remote, forwarded_loopback,
        ):
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}

    def test_인증뒤에도_deployment_id_계약은_fail_closed다(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=DEPLOYMENT_TOKEN))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        headers = {"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"}

        for payload in (
            {"deployment_id": "deploy-a\n"},
            {"deployment_id": "deploy-a", "unexpected": True},
            {"deployment_id": "x" * 129},
            {"deployment_id": 123},
        ):
            response = client.post(
                "/internal/deployment/drain", json=payload, headers=headers
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
        assert client.get("/ready").json()["harness"]["admission_state"] == "accepting"

    def test_인증실패_404는_일반_없는경로와_CORS_표면도_같다(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=None))
        origin = "https://example.org"
        headers = {"Origin": origin, "X-Request-ID": "route-probe"}
        hidden = client.post(
            "/internal/deployment/drain", json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        missing = client.post("/definitely-not-a-route", json={}, headers=headers)

        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == {"detail": "Not Found"}
        assert hidden.headers["X-Request-ID"] != "route-probe"
        assert missing.headers["X-Request-ID"] == "route-probe"
        assert hidden.headers.get("access-control-allow-origin") == missing.headers.get(
            "access-control-allow-origin"
        )

    def test_drain_resume과_ready가_접수상태를_일관되게_보인다(
        self, client, monkeypatch,
    ):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=DEPLOYMENT_TOKEN))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        headers = {"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"}

        first = client.post(
            "/internal/deployment/drain",
            json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        retry = client.post(
            "/internal/deployment/drain",
            json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        assert first.status_code == 200 and first.json() == {
            "status": "draining", "changed": True,
        }
        assert retry.status_code == 200 and retry.json() == {
            "status": "draining", "changed": False,
        }
        readiness = client.get("/ready").json()
        assert readiness["status"] == "draining"
        assert readiness["harness"]["accepting_jobs"] is False
        assert readiness["harness"]["admission_state"] == "draining"
        assert "deployment_id" not in str(readiness)

        rejected = client.post(
            "/api/analyze", json={"url": "https://youtu.be/cYRkZmBuDqI"}
        )
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "server_busy"

        conflict = client.post(
            "/internal/deployment/resume",
            json={"deployment_id": "deploy-b"},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "deployment_conflict"
        assert "deploy-a" not in conflict.text and "deploy-b" not in conflict.text

        resumed = client.post(
            "/internal/deployment/resume",
            json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        resumed_retry = client.post(
            "/internal/deployment/resume",
            json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        assert resumed.status_code == 200 and resumed.json() == {
            "status": "accepting", "changed": True,
        }
        assert resumed_retry.status_code == 200 and resumed_retry.json() == {
            "status": "accepting", "changed": False,
        }
        replay = client.post(
            "/internal/deployment/drain",
            json={"deployment_id": "deploy-a"},
            headers=headers,
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "deployment_conflict"
        readiness = client.get("/ready").json()
        assert readiness["status"] == "ready"
        assert readiness["harness"]["accepting_jobs"] is True

    def test_durable_state_commit실패는_503이고_접수는_fail_closed다(
        self, client, monkeypatch,
    ):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=DEPLOYMENT_TOKEN))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)
        active = app_module._active_harness()
        assert active._admission_store is not None
        monkeypatch.setattr(
            active._admission_store,
            "replace",
            lambda expected, record: (_ for _ in ()).throw(
                harness_module.AdmissionStateError("sensitive/path/deploy-a")
            ),
        )

        response = client.post(
            "/internal/deployment/drain",
            json={"deployment_id": "deploy-a"},
            headers={"Authorization": f"Bearer {DEPLOYMENT_TOKEN}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "deployment_state_unavailable"
        assert "deploy-a" not in response.text
        readiness = client.get("/ready").json()
        assert readiness["status"] == "draining"
        assert readiness["harness"]["accepting_jobs"] is False

    def test_배포토큰은_응답_요청id_설정repr_로그에_남지_않는다(
        self, client, monkeypatch, caplog,
    ):
        monkeypatch.setattr(app_module, "config", replace(
            app_module.config, deployment_token=DEPLOYMENT_TOKEN))
        monkeypatch.setattr(app_module, "_is_loopback_client", lambda _: True)

        with caplog.at_level("INFO", logger="backend.app"):
            response = client.post(
                "/internal/deployment/drain",
                # Even if an operator accidentally repeats the secret in another
                # accepted field, the control response/access log must not echo it.
                json={"deployment_id": DEPLOYMENT_TOKEN},
                headers={
                    "Authorization": f"Bearer {DEPLOYMENT_TOKEN}",
                    "X-Request-ID": DEPLOYMENT_TOKEN,
                },
            )

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] != DEPLOYMENT_TOKEN
        assert DEPLOYMENT_TOKEN not in response.text
        assert DEPLOYMENT_TOKEN not in repr(app_module.config)
        assert any(
            "POST /internal/deployment/drain" in record.getMessage()
            for record in caplog.records
        )
        assert DEPLOYMENT_TOKEN not in caplog.text


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
