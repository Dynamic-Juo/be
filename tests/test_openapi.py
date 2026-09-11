"""In-process API documentation checks. No network, models or real jobs."""

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import harness as harness_module
from backend.harness import Job
from backend.schemas import (
    AnalyzeResponse, ErrorResponse, HealthResponse, JOB_EXAMPLES, JobResponse, ReadyResponse,
)
from deepcheck.claims import Claim, Evidence
from deepcheck.report import AnalysisReport, ClaimVerification, ManipulationAxis, TranscriptInfo


@pytest.fixture
def client(monkeypatch):
    def forbidden_analysis(*args, **kwargs):
        pytest.fail("Documentation tests must not start analysis")

    monkeypatch.setattr(harness_module, "analyze_url", forbidden_analysis)
    with TestClient(app_module.app) as session:
        yield session


def _full_result():
    """Actual dataclass serialization, with extra fields to detect filtering."""
    claim = Claim(text="문서 계약 검증용 가상 주장입니다.", status="done")
    claim.evidence = [Evidence(
        title="모의 참고 자료", url="https://example.org/reference", source="naver_news",
        snippet="가상 발췌입니다.", cited=False,
    )]
    claim_payload = claim.to_dict()
    claim_payload["evidence"][0]["future_provenance_field"] = {"retained": True}
    result = AnalysisReport(
        url="https://www.youtube.com/watch?v=cYRkZmBuDqI",
        media={"title": "가상 영상", "duration": 140, "language": "ko", "extra_metadata": 7},
        analysis_status="partial",
        stages={"cleanup": {"status": "ok", "elapsed_sec": None, "detail": "정리 완료", "error": None}},
        face_manipulation=ManipulationAxis(status="inconclusive", status_label="판단 보류"),
        whole_video_generation=ManipulationAxis(status="unavailable", status_label="분석 불가"),
        claim_verification=ClaimVerification(
            status="analyzed", claims=[claim_payload], summary={"total": 1, "done": 1, "unverified": 1}),
        transcript=TranscriptInfo(source="stt", language="ko", word_count=3),
    ).to_dict()
    result["future_result_block"] = {"nested": [1, None, "kept"]}
    return result


def test_openapi_has_explicit_public_contracts_and_error_envelope(client):
    schema = client.get("/openapi.json").json()
    expected = {
        ("/health", "get"): ("getHealth", "HealthResponse"),
        ("/ready", "get"): ("getReadiness", "ReadyResponse"),
        ("/api/analyze", "post"): ("submitAnalysis", "AnalyzeResponse"),
        ("/api/jobs/{job_id}", "get"): ("getAnalysisJob", "JobResponse"),
    }
    assert set(schema["paths"]) == {path for path, _ in expected}
    for (path, method), (operation, model) in expected.items():
        endpoint = schema["paths"][path][method]
        assert endpoint["operationId"] == operation
        assert endpoint["tags"]
        success = endpoint["responses"]["200"]
        assert success["content"]["application/json"]["schema"]["$ref"].endswith(f"/{model}")
        assert "X-Request-ID" in success["headers"]
    for status in ("422", "429", "500"):
        response = schema["paths"]["/api/analyze"]["post"]["responses"][status]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "Retry-After" in schema["paths"]["/api/analyze"]["post"]["responses"]["429"]["headers"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "EvidenceResult" in schema["components"]["schemas"]
    assert "ClaimResult" in schema["components"]["schemas"]
    assert schema["servers"][0]["url"] == "/"


def test_existing_documentation_paths_are_read_only_and_do_not_submit(client):
    assert client.get("/docs").status_code == 200
    assert '"withCredentials": true' in client.get("/docs").text
    assert '"tryItOutEnabled": false' in client.get("/docs").text
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert app_module.harness.stats()["jobs_total"] == 0


@pytest.mark.parametrize("status,result", [
    ("queued", None),
    ("processing:collecting", {}),
    ("processing:transcribing", {"media": {"title": "메타데이터만 도착"}}),
    ("partially_completed", {"face_manipulation": {"status": "unavailable", "detail": "얼굴 없음"}}),
    ("completed_with_limitations", _full_result()),
    ("failed", {"stages": {"cleanup": {"status": "failed", "detail": "정리 실패"}}}),
    ("timed_out", {"claim_verification": {
        "status": "analyzed", "claims": [asdict(Claim(text="가상 주장", status="timed_out"))],
    }}),
])
def test_job_schema_matches_incremental_payload_without_removing_or_filling_fields(client, monkeypatch, status, result):
    monkeypatch.setattr(harness_module.time, "time", lambda: 1020.0)
    job = Job(
        id="a1b2c3d4000000000000000000000000", session_id="private-session", session_ids=["private-session"],
        url="https://www.youtube.com/watch?v=cYRkZmBuDqI", params={"max_frames": 8},
        status=status, stage="collecting" if status != "queued" else None,
        result=result, created_at=1000, updated_at=1020, started_at=None if status == "queued" else 1005,
        error={"code": "internal_error", "message": "실패", "retryable": False} if status == "failed" else None,
    )
    app_module.harness._jobs[job.id] = job
    expected = job.to_dict()
    expected.pop("session_id")
    expected.pop("session_ids")
    response = client.get(f"/api/jobs/{job.id}")
    assert response.status_code == 200
    assert response.json() == expected
    parsed = JobResponse.model_validate(response.json())
    assert parsed.model_dump(exclude_unset=True) == expected


def test_completed_result_contract_also_accepts_complete_axes(client, monkeypatch):
    result = _full_result()
    result["analysis_status"] = "complete"
    result["whole_video_generation"] = {"status": "inconclusive", "status_label": "판단 보류"}
    job = Job(id="contract-id", session_id="internal", url=result["url"], params={},
              status="completed", result=result)
    app_module.harness._jobs[job.id] = job
    response = client.get(f"/api/jobs/{job.id}")
    assert response.status_code == 200
    assert JobResponse.model_validate(response.json()).result.analysis_status == "complete"


def test_examples_are_synthetic_schema_valid_snapshots():
    for example in JOB_EXAMPLES.values():
        assert "모의" in example["summary"]
        JobResponse.model_validate(example["value"])


def test_status_and_submission_payload_contracts(client, monkeypatch):
    HealthResponse.model_validate(client.get("/health").json())
    ReadyResponse.model_validate(client.get("/ready").json())
    job = Job(id="contract-id", session_id="one", url="https://www.youtube.com/watch?v=cYRkZmBuDqI", params={})
    monkeypatch.setattr(app_module.harness, "submit", lambda **kwargs: (job, True))
    response = client.post("/api/analyze", json={"url": job.url, "session_id": "one"})
    assert response.status_code == 200
    payload = AnalyzeResponse.model_validate(response.json())
    assert payload.deduplicated is True


def test_validation_and_missing_job_follow_documented_error_contract(client):
    bad_request = client.post("/api/analyze", json={"url": "https://youtu.be/cYRkZmBuDqI", "max_frames": 999})
    assert bad_request.status_code == 422
    assert ErrorResponse.model_validate(bad_request.json()).error.fields[0].field == "body.max_frames"
    missing = client.get("/api/jobs/not-found")
    assert missing.status_code == 404
    assert ErrorResponse.model_validate(missing.json()).error.code == "job_not_found"


def test_browser_can_read_trace_and_retry_headers_for_allowed_origin(client):
    origins = app_module._allowed_origins
    origin = "https://example.org" if "*" in origins else origins[0]
    response = client.get("/health", headers={"Origin": origin})
    exposed = {value.strip().lower() for value in response.headers["access-control-expose-headers"].split(",")}
    assert {"x-request-id", "retry-after"} <= exposed
