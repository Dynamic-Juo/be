"""모델 응답 오류와 입력 손실에 대한 회귀 검사. 모델의 판단 정확도 검사는 별도다."""
import json

import pytest

from deepcheck import claims, pipeline, llm
from backend.harness import Job, Harness


class FakeLLM:
    name = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.input = None

    def complete(self, system, user, max_tokens):
        self.input = json.loads(user)
        return json.dumps(self.payload, ensure_ascii=False)


def evidence(text="가상시의 올해 지원금은 10만원으로 발표됐다."):
    return claims.Evidence(title="가상시 지원금 발표", url="https://example.com/news",
                           source="fixture", snippet=text)


def citation(index=1, quote="가상시의 올해 지원금은 10만원으로 발표됐다."):
    return {"index": index, "quote": quote, "reason": "지원금 수치를 제시한다."}


def test_empty_extraction_is_a_successful_no_claims_result():
    assert claims.extract_claims_llm("물가가 6.0% 올랐으면 좋겠습니다.", [], 0,
                                    FakeLLM({"claims": []})) == []


@pytest.mark.parametrize("context, expected", [
    ("가상시가 지원금을 발표했다.", "가상시가 지원금을 발표했다."),
    ("다른도시의 통계청이다.", ""),
])
def test_context_must_be_found_in_transcript(context, expected):
    text = "가상시가 지원금을 발표했다. 올해는 10만원을 줍니다."
    result = claims.extract_claims_llm(text, [], 0, FakeLLM({"claims": [{
        "text": "올해는 10만원을 줍니다.", "context": context}]}))
    assert result[0].context == expected


def test_extraction_does_not_silently_drop_the_end_of_transcript():
    tail = "가상시 지원금은 10만원입니다."
    text = "진행 안내입니다. " * 1600 + tail
    fake = FakeLLM({"claims": [{"text": tail}]})
    assert claims.extract_claims_llm(text, [], 0, fake)[0].text == tail
    assert fake.input["transcript"] == text


def test_verdict_receives_context_date_and_source_scope():
    claim = claims.Claim(text="올해는 10만원입니다.", context="가상시가 발표했다.",
                         video_title="가상시 뉴스", video_published_at="2022-08-01")
    fake = FakeLLM({"verdict": "부족", "reason": "시점 불명", "cited": []})
    claims._llm_verdict(claim, [evidence()], fake)
    assert fake.input["context"] == claim.context
    assert fake.input["video_published_at"] == "2022-08-01"
    assert fake.input["evidence"][0]["content_scope"] == "search_excerpt"
    assert fake.input["evidence"][0]["url"]


def test_unverified_never_leaves_cited_sources():
    item = evidence()
    result = claims._llm_verdict(claims.Claim(text="가상시 지원금"), [item], FakeLLM({
        "verdict": "부족", "reason": "조건 부족", "cited": [citation()],
        "insufficient_reason": "partial"}))
    assert result["quote"] is None
    assert not item.cited and item.quote is None and item.cite_reason is None


@pytest.mark.parametrize("bad", [citation(99), citation(True), citation(2, "없는 문장이다."),
                                 {"index": 2, "quote": "가상시 지원금", "reason": None}])
def test_one_valid_citation_does_not_rescue_an_invalid_one(bad):
    items = [evidence(), evidence()]
    result = claims._llm_verdict(claims.Claim(text="가상시 지원금"), items, FakeLLM({
        "verdict": "일치", "reason": "지원금", "cited": [citation(), bad]}))
    assert result["verdict"] == claims.UNVERIFIED
    assert not any(e.cited for e in items)


def test_quote_outside_the_content_sent_to_model_is_rejected():
    item = evidence("가" * 810 + "실제로 확인하지 않은 뒤쪽 문장이다.")
    result = claims._llm_verdict(claims.Claim(text="가상시 지원금"), [item], FakeLLM({
        "verdict": "일치", "reason": "지원금", "cited": [citation(1, "실제로 확인하지 않은 뒤쪽 문장이다.")]}))
    assert result["verdict"] == claims.UNVERIFIED


def test_cited_source_beyond_old_display_limit_is_preserved():
    class Provider:
        def search(self, query, limit):
            return [evidence() for _ in range(3)]
    claim = claims.Claim(text="가상시 지원금은 10만원입니다.")
    claims.verify_one_claim(claim, [Provider()], evidence_per_claim=1, llm_provider=FakeLLM({
        "verdict": "일치", "reason": "지원금", "cited": [citation(3)]}))
    assert len(claim.evidence) == 3 and claim.evidence[2].cited


def test_context_is_used_in_search():
    seen = []
    class Provider:
        def search(self, query, limit):
            seen.append(query)
            return [evidence("가상시의 청년지원금 안내입니다.")]
    claim = claims.Claim(text="이것은 지원됩니다.", context="가상시의 청년지원금")
    claims.verify_one_claim(claim, [Provider()])
    assert "청년지원금" in seen[0]
    assert claim.evidence, "문맥으로 찾은 근거를 관련성 필터에서 다시 버리면 안 된다"


def test_metadata_does_not_switch_job_to_partially_completed():
    h = Harness(max_workers=1)
    try:
        job = Job(id="j", session_id="s", url="u", params={}, result={},
                  status="processing:collecting")
        h._merge_partial(job, {"media": {"title": "제목"}})
        assert job.status == "processing:collecting"
        h._merge_partial(job, {"face_manipulation": {"status": "unavailable"}})
        h._update(job, .7, "주장 추출", "extracting_claims")
        assert job.status == "partially_completed" and job.stage == "extracting_claims"
    finally:
        h.shutdown()


def test_queue_time_is_separate_from_processing_time():
    job = Job(id="j", session_id="s", url="u", params={}, status="completed",
              created_at=100, started_at=130, updated_at=150)
    payload = job.to_dict()
    assert payload["queue_wait_sec"] == 30
    assert payload["processing_elapsed_sec"] == 20
    assert payload["elapsed_sec"] == 50


def test_partial_final_counts_and_verifying_start(monkeypatch):
    claim = claims.Claim(text="가상시 지원금은 10만원이다.")
    monkeypatch.setattr(llm, "get_provider", lambda: None)
    monkeypatch.setattr(claims, "select_extractor", lambda _: lambda *a: [claim])
    monkeypatch.setattr(claims, "default_providers", lambda: [])
    snapshots = []
    result = pipeline._verify_claims(claim.text, [], pipeline.AnalysisOptions(),
        pipeline.StageTracker(), float("inf"), lambda *a: None, snapshots.append,
        video_title="뉴스", video_published_at="2022-08-01")
    assert [s["claim_verification"]["claims"][0]["status"] for s in snapshots][:3] == [
        "pending", "verifying", "done"]
    assert result.summary == snapshots[-1]["claim_verification"]["summary"]
    assert result.summary["done"] == 1
    assert result.claims[0]["video_published_at"] == "2022-08-01"
