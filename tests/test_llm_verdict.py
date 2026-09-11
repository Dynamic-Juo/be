"""LLM 판정·추출 경로 테스트.

네트워크를 타지 않는다. LLM 제공자를 가짜로 넣어서 "우리 코드가 LLM 응답을
어떻게 다루는지"만 본다 — 특히 지어낸 응답을 걸러내는지가 핵심이다.
"""

import json
import io
import logging
import ssl
import urllib.error
from dataclasses import replace

import pytest

from deepcheck import claims, llm
from deepcheck.config import config


class FakeLLM:
    """정해진 JSON을 그대로 돌려주는 제공자."""

    name = "fake"

    def __init__(self, payload, raise_error=False):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = []

    def complete(self, system, user, max_tokens):
        self.calls.append(user)
        if self.raise_error:
            raise llm.LLMUnavailable("테스트용 실패")
        return json.dumps(self.payload, ensure_ascii=False)


class RawLLM:
    name = "raw"

    def __init__(self, response):
        self.response = response

    def complete(self, system, user, max_tokens):
        return self.response


def _evidence(snippet="소비자물가 상승률은 6.0%로 집계됐다."):
    return claims.Evidence(
        title="통계청 소비자물가동향",
        url="https://kostat.go.kr/x",
        source="naver_news",
        published_at="2022-08-02",
        snippet=snippet,
        source_type=claims.NEWS,
    )


def _verified_primary(content="소비자물가 상승률은 6.0%로 집계됐다.",
                      url="https://kostat.go.kr/x"):
    return claims.Evidence(
        title="통계청 소비자물가동향",
        url=url,
        source="verified_fixture",
        published_at="2022-08-02",
        content=content,
        content_scope=claims.ORIGINAL,
        provenance_verified=True,
        source_type=claims.STATISTICS,
    )


class StubProvider:
    name = "stub"

    def __init__(self, items):
        self.items = items

    def search(self, query, limit):
        return list(self.items)


# --- 판정 사다리 ---------------------------------------------------------

def test_전문기관_검색_rating을_바로_판정으로_옮기지_않는다():
    claim_text = "소비자물가 상승률이 6.0%를 넘었습니다"
    rated = claims.Evidence(title="팩트체크", url="https://x", source="factcheck",
                            snippet=claim_text, rating="거짓",
                            source_type=claims.FACTCHECK)
    fake = FakeLLM({"verdict": "부족", "reason": "원문 미확인", "cited": [],
                    "insufficient_reason": "weak_source"})
    claim = claims.Claim(text=claim_text)

    claims.verify_one_claim(claim, [StubProvider([rated])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.WEAK_SOURCE
    assert len(fake.calls) == 1


def test_서로_충돌하는_전문기관_rating은_첫_결과를_고르지_않는다():
    claim_text = "소비자물가 상승률이 6.0%를 넘었습니다"
    items = [
        claims.Evidence(title="검증 A", url="https://a.example/x", source="factcheck",
                        snippet=claim_text, rating="거짓", source_type=claims.FACTCHECK),
        claims.Evidence(title="검증 B", url="https://b.example/x", source="factcheck",
                        snippet=claim_text, rating="사실", source_type=claims.FACTCHECK),
    ]
    fake = FakeLLM({"verdict": "일치", "reason": "...", "cited": []})
    claim = claims.Claim(text=claim_text)

    claims.verify_one_claim(claim, [StubProvider(items)], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.SOURCE_CONFLICT
    assert fake.calls == []


def test_no_evidence_rating은_거짓으로_매핑하지_않는다():
    assert claims._verdict_from_rating("No evidence") is None


def test_근거가_없으면_LLM을_부르지_않고_사유를_남긴다():
    fake = FakeLLM({"verdict": "일치", "reason": "...", "cited": []})
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.NO_SOURCE
    assert claim.insufficient_label == "관련 자료를 찾지 못함"
    assert fake.calls == []


def test_LLM이_인용까지_맞으면_판정이_그대로_반영된다():
    fake = FakeLLM({
        "verdict": "일치",
        "reason": "통계청 자료가 같은 수치를 제시한다.",
        "cited": [{"index": 1, "quote": "소비자물가 상승률은 6.0%로 집계됐다.",
                   "reason": "같은 기간 수치를 그대로 제시한다."}],
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.SUPPORTED
    assert claim.verdict_label == "근거와 일치"
    assert claim.quote == "소비자물가 상승률은 6.0%로 집계됐다."
    assert claim.insufficient_reason is None
    assert len(fake.calls) == 1

    # 화면은 근거 카드마다 이유를 보여준다 — 근거 쪽에도 붙어 있어야 한다.
    used = claim.evidence[0]
    assert used.cited is True
    assert used.cite_reason == "같은 기간 수치를 그대로 제시한다."
    assert used.quote == "소비자물가 상승률은 6.0%로 집계됐다."


def test_검색_요약에_인용이_있어도_양성_판정은_근거부족으로_강등한다():
    fake = FakeLLM({
        "verdict": "일치",
        "reason": "검색 요약에 같은 수치가 있다.",
        "cited": [{"index": 1, "quote": "소비자물가 상승률은 6.0%로 집계됐다.",
                   "reason": "같은 수치다."}],
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_evidence()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.WEAK_SOURCE
    assert not claim.evidence[0].cited


def test_원문이어도_provenance가_검증되지_않으면_양성_판정을_막는다():
    item = claims.Evidence(
        title="통계 원문", url="https://kostat.go.kr/x", source="fixture",
        content="소비자물가 상승률은 6.0%로 집계됐다.",
        content_scope=claims.ORIGINAL, source_type=claims.STATISTICS,
    )
    fake = FakeLLM({
        "verdict": "일치", "reason": "같은 수치다.",
        "cited": [{"index": 1, "quote": item.content, "reason": "같은 수치다."}],
    })

    result = claims._llm_verdict(claims.Claim(text="소비자물가 상승률"), [item], fake)

    assert result["verdict"] == claims.UNVERIFIED
    assert not item.cited


def test_1차_출처가_없으면_검증된_독립_원문_두_개가_필요하다():
    text = "소비자물가 상승률은 6.0%로 집계됐다."
    one = claims.Evidence(
        title="기사 A", url="https://a.example/news", source="fixture", content=text,
        content_scope=claims.ORIGINAL, provenance_verified=True,
        independence_group="wire-a", source_type=claims.NEWS,
    )
    two = claims.Evidence(
        title="기사 B", url="https://b.example/news", source="fixture", content=text,
        content_scope=claims.ORIGINAL, provenance_verified=True,
        independence_group="wire-b", source_type=claims.NEWS,
    )
    payload = {
        "verdict": "일치", "reason": "독립 원문 두 건이 같은 수치를 제시한다.",
        "cited": [
            {"index": 1, "quote": text, "reason": "같은 수치다."},
            {"index": 2, "quote": text, "reason": "같은 수치다."},
        ],
    }

    insufficient = claims._llm_verdict(
        claims.Claim(text="소비자물가 상승률"), [one],
        FakeLLM({**payload, "cited": payload["cited"][:1]}),
    )
    sufficient = claims._llm_verdict(
        claims.Claim(text="소비자물가 상승률"), [one, two], FakeLLM(payload),
    )

    assert insufficient["verdict"] == claims.UNVERIFIED
    assert sufficient["verdict"] == claims.SUPPORTED


def test_안전하지_않은_출처_URL은_검증된_원문이어도_인용할_수_없다():
    item = _verified_primary(url="javascript:alert(1)")
    fake = FakeLLM({
        "verdict": "일치", "reason": "같은 수치다.",
        "cited": [{"index": 1, "quote": item.content, "reason": "같은 수치다."}],
    })

    result = claims._llm_verdict(claims.Claim(text="소비자물가 상승률"), [item], fake)

    assert result["verdict"] == claims.UNVERIFIED
    assert not item.cited


def test_인용은_지목한_근거_안에_있어야_한다():
    """A 자료의 문장을 B 자료의 근거인 것처럼 붙이면 화면에 틀린 정보가 나간다."""
    a = _verified_primary("소비자물가 상승률은 6.0%로 집계됐다.")
    b = _verified_primary("추석 성수품 공급을 늘린다.", "https://kostat.go.kr/y")
    fake = FakeLLM({
        "verdict": "일치",
        "reason": "...",
        # 2번 근거를 지목하면서 1번 근거의 문장을 인용했다.
        "cited": [{"index": 2, "quote": "소비자물가 상승률은 6.0%로 집계됐다.",
                   "reason": "..."}],
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([a, b])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.WEAK_SOURCE
    assert not any(e.cited for e in claim.evidence)


def test_없는_근거_번호를_지목하면_무시한다():
    fake = FakeLLM({
        "verdict": "일치", "reason": "...",
        "cited": [{"index": 99, "quote": "소비자물가 상승률은 6.0%로 집계됐다.",
                   "reason": "..."}],
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED


@pytest.mark.parametrize("bad_cited", [None, {}, "1", [
    {"index": 1, "quote": "소비자물가 상승률은 6.0%로 집계됐다.", "reason": "같다."},
    {"index": 1, "quote": "소비자물가 상승률은 6.0%로 집계됐다.", "reason": "같다."},
]])
def test_인용_배열이_잘못됐거나_번호가_중복되면_안전하게_강등한다(bad_cited):
    item = _verified_primary()
    result = claims._llm_verdict(
        claims.Claim(text="소비자물가 상승률"), [item],
        FakeLLM({"verdict": "일치", "reason": "같다.", "cited": bad_cited}),
    )
    assert result["verdict"] == claims.UNVERIFIED
    assert not item.cited


def test_근거부족이면_붙은_자료는_참고_자료로_남는다():
    """판정 근거와 참고 자료를 섞으면, 판정하지 않은 것을 판정한 것처럼 보여주게 된다."""
    fake = FakeLLM({
        "verdict": "부족", "reason": "직접 확인하지 못했다.",
        "cited": [], "insufficient_reason": "not_direct",
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.evidence  # 참고 자료로는 남는다
    assert not any(e.cited for e in claim.evidence)


def test_인용이_근거에_없으면_판정을_근거부족으로_강등한다():
    """환각 방지의 핵심 — 지어낸 인용은 판정을 통째로 무효화한다."""
    fake = FakeLLM({
        "verdict": "불일치",
        "reason": "자료와 다르다.",
        "cited": [{"index": 1, "reason": "다르다.",
                   "quote": "실제 상승률은 12.7%였다고 한국은행이 발표했다."}],  # 근거에 없는 문장
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.WEAK_SOURCE
    assert claim.quote is None


def test_인용_검증은_공백과_따옴표_차이를_허용한다():
    fake = FakeLLM({
        "verdict": "일치",
        "reason": "같은 수치다.",
        "cited": [{"index": 1, "reason": "같은 수치다.",
                   "quote": '"소비자물가  상승률은 6.0%로 집계됐다"'}],
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.SUPPORTED


def test_근거부족_판정에는_인용_검증을_요구하지_않는다():
    fake = FakeLLM({
        "verdict": "부족",
        "reason": "자료의 기준 시점이 다르다.",
        "cited": [],
        "insufficient_reason": "time_mismatch",
    })
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.TIME_MISMATCH
    assert claim.insufficient_label == "주장과 자료의 시점이 다름"


def test_LLM이_죽으면_판정_수단_없음으로_떨어진다():
    fake = FakeLLM({}, raise_error=True)
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_evidence()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.NOT_DIRECT
    assert claim.status == claims.DONE  # 실패가 아니라 정상 종료


def test_LLM이_알_수_없는_판정을_주면_무시한다():
    fake = FakeLLM({"verdict": "아마도 맞음", "reason": "..."})
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_evidence()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED


# --- 판정 이름(U-03) -----------------------------------------------------

def test_판정_이름은_docs_U03이_정한_문구를_쓴다():
    assert claims.VERDICT_LABELS[claims.SUPPORTED] == "근거와 일치"
    assert claims.VERDICT_LABELS[claims.REFUTED] == "근거와 불일치"
    assert claims.VERDICT_LABELS[claims.UNVERIFIED] == "근거 부족"


def test_report와_claims의_판정_이름이_같은_정의를_쓴다():
    from deepcheck import report
    assert report.VERDICT_LABELS is claims.VERDICT_LABELS


# --- LLM 주장 추출 -------------------------------------------------------

_TRANSCRIPT = (
    "소비자물가 상승률이 6.0%를 넘었습니다. "
    "정부는 성수품 공급을 1.4배로 늘립니다. "
    "조만간 7%도 넘어설 것으로 보입니다."
)


def test_LLM이_원문에_없는_문장을_지어내면_추출_실패다():
    fake = FakeLLM({"claims": [
        {"text": "소비자물가 상승률이 6.0%를 넘었습니다.", "repeat": 1},
        {"text": "실업률이 3.2%로 떨어졌습니다.", "repeat": 1},  # 원문에 없음
    ]})

    with pytest.raises(claims.ClaimExtractionError):
        claims.extract_claims_llm(_TRANSCRIPT, [], 0, fake)


def test_LLM_추출_호출이_실패하면_명시적_추출_실패다():
    fake = FakeLLM({}, raise_error=True)

    with pytest.raises(claims.ClaimExtractionError):
        claims.extract_claims_llm(_TRANSCRIPT, [], 0, fake)


def test_LLM이_유효한_주장을_하나도_못_내면_명시적_추출_실패다():
    fake = FakeLLM({"claims": [{"text": "원문에 전혀 없는 문장입니다."}]})

    with pytest.raises(claims.ClaimExtractionError):
        claims.extract_claims_llm(_TRANSCRIPT, [], 0, fake)


def test_LLM_추출기_선택은_제공자가_없으면_명시적으로_실패한다(monkeypatch):
    # config는 frozen이라 필드를 직접 못 바꾼다. 사본을 만들어 갈아끼운다.
    monkeypatch.setattr(claims, "config", replace(config, claim_extractor="llm"))
    with pytest.raises(claims.ClaimExtractionError):
        claims.select_extractor(None)


@pytest.mark.parametrize("payload", [
    {}, {"claims": "[]"}, {"claims": [None]},
    {"claims": [{"text": 123}]},
    {"claims": [{"text": "소비자물가 상승률이 6.0%를 넘었습니다.", "repeat": True}]},
])
def test_명시적_빈_배열이_아닌_잘못된_추출_스키마는_실패다(payload):
    with pytest.raises(claims.ClaimExtractionError):
        claims.extract_claims_llm(_TRANSCRIPT, [], 0, FakeLLM(payload))


def test_알_수_없는_추출기_설정은_실패다(monkeypatch):
    monkeypatch.setattr(claims, "config", replace(config, claim_extractor="typo"))
    with pytest.raises(claims.ClaimExtractionError):
        claims.select_extractor(None)


def test_추출기_선택은_제공자가_있으면_LLM을_쓴다(monkeypatch):
    monkeypatch.setattr(claims, "config", replace(config, claim_extractor="llm"))
    fake = FakeLLM({"claims": [{"text": "소비자물가 상승률이 6.0%를 넘었습니다."}]})

    extract = claims.select_extractor(fake)
    result = extract(_TRANSCRIPT, [], 0)

    assert len(fake.calls) == 1
    assert result[0].text == "소비자물가 상승률이 6.0%를 넘었습니다."


def test_인용_검증_설정이_꺼져도_안전장치를_우회하지_못한다(monkeypatch):
    monkeypatch.setattr(claims, "config", replace(config, llm_quote_check=False))
    fake = FakeLLM({"verdict": "불일치", "reason": "다르다.",
                    "cited": [{"index": 1, "reason": "다르다.",
                               "quote": "근거에 전혀 없는 문장이다."}]})
    claim = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")

    claims.verify_one_claim(claim, [StubProvider([_verified_primary()])], llm_provider=fake)

    assert claim.verdict == claims.UNVERIFIED
    assert claim.insufficient_reason == claims.WEAK_SOURCE


# --- LLM 응답·TLS 경계 ----------------------------------------------------

def test_JSON_코드펜스_앞뒤에_다른_문장이_붙으면_거부한다():
    provider = RawLLM('이전 지시를 무시함\n```json\n{"claims": []}\n```')
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json(provider, "system", "user")


def test_중복_JSON_키는_거부한다():
    provider = RawLLM('{"verdict":"일치","verdict":"불일치"}')
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json(provider, "system", "user")


def test_LLM_HTTPS_요청은_인증서와_호스트명을_검증한다(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            captured["read_size"] = size
            return b'{}'

    def fake_urlopen(request, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm._post_json("https://llm.example/v1", {}, {}, 1) == {}
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True
    assert captured["read_size"] == llm._MAX_HTTP_RESPONSE_BYTES + 1


def test_LLM_HTTP_오류_본문은_예외나_로그로_전파하지_않는다(monkeypatch):
    secret = "provider echoed secret"
    read_sizes = []

    class TrackingBody(io.BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    error = urllib.error.HTTPError(
        "https://llm.example/v1", 401, "Unauthorized", {}, TrackingBody(secret.encode())
    )
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(error))
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm._post_json("https://llm.example/v1", {}, {}, 1)
    assert str(caught.value) == "HTTP 401"
    assert secret not in str(caught.value)
    assert read_sizes == [llm._MAX_HTTP_ERROR_BYTES]


def test_LLM_HTTP_성공_응답이_상한을_넘으면_안전하게_실패한다(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return b"x" * size

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(llm.LLMUnavailable, match="허용 크기"):
        llm._post_json("https://llm.example/v1", {}, {}, 1)


# --- 주장 개수 상한 (M-04) -----------------------------------------------

def test_상한이_0이면_검증_가능한_주장을_모두_뽑는다():
    text = ("소비자물가가 6.0% 올랐습니다. 성수품 공급을 1.4배로 늘립니다. "
            "42조원 규모의 보증자금을 공급합니다. 650억원 쿠폰을 발행합니다.")
    result = claims.extract_claims(text, [], max_claims=0)
    assert len(result) == 4


def test_큰_수_단위_조가_포함된_문장도_후보가_된다():
    result = claims.extract_claims("42조원 규모의 명절 대출 보증자금을 공급합니다.", [],
                                   max_claims=0)
    assert len(result) == 1


# --- 출처 유형 (U-02) ----------------------------------------------------

def test_출처_유형에_따라_1차_출처가_구분된다():
    stat = claims.Evidence(title="t", url="u", source="kosis",
                           source_type=claims.STATISTICS)
    news = claims.Evidence(title="t", url="u", source="naver_news",
                           source_type=claims.NEWS)
    assert stat.is_primary and stat.source_type_label == "통계 원문"
    assert not news.is_primary and news.source_type_label == "언론 보도"


# --- 병렬 검증 시 로그 상관관계 -------------------------------------------

def test_병렬_검증에도_job_id가_로그에_남는다(monkeypatch, caplog):
    """워커가 여러 개 도는 서버에서 job_id 없는 로그는 추적이 불가능하다."""
    from deepcheck import pipeline
    from deepcheck.logging_setup import current_job_id

    import threading
    seen = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(3)

    def fake_verify(claim, providers, evidence_per_claim=None, llm_provider=None):
        # 실제로 겹쳐 돌게 만든다. 순차로 끝나버리면 동시 진입 버그를 못 잡는다
        # (처음 작성한 테스트가 정확히 그래서 통과했고, 컨테이너에서만 터졌다).
        barrier.wait(timeout=5)
        with seen_lock:
            seen.append(current_job_id.get())
        claim.status = claims.DONE
        return claim

    monkeypatch.setattr(claims, "verify_one_claim", fake_verify)
    monkeypatch.setattr(claims, "default_providers", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "config", replace(config, claim_workers=3))

    token = current_job_id.set("job-abc")
    try:
        tracker = pipeline.StageTracker()
        pipeline._verify_claims(
            "소비자물가가 6.0% 올랐습니다. 성수품 공급을 1.4배로 늘립니다. "
            "42조원 규모의 보증자금을 공급합니다.",
            [], pipeline.AnalysisOptions(), tracker,
            deadline=float("inf"), progress=lambda *a: None, partial=lambda *a: None,
        )
    finally:
        current_job_id.reset(token)

    assert seen, "검증이 한 건도 실행되지 않았다"
    assert all(job == "job-abc" for job in seen), f"job_id가 유실됨: {seen}"


# --- NAVER API HUB 규격 ----------------------------------------------------

def test_네이버_검색은_API_HUB_규격으로_호출한다(monkeypatch):
    """2026-07-31에 도메인·경로·인증 헤더가 모두 바뀌었다. 예전 규격으로
    돌아가면 조용히 401이 나고 근거가 비므로 고정해둔다."""
    captured = {}

    def fake_get(url, timeout, headers=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return {"items": [{"title": "물가 <b>6.3%</b> 상승", "description": "본문",
                           "originallink": "https://news.example/1",
                           "pubDate": "Mon, 02 Sep 2026 09:00:00 +0900"}]}

    monkeypatch.setattr(claims, "_http_get_json", fake_get)
    provider = claims.NaverSearchProvider("news", "cid", "csecret")
    results = provider.search("소비자물가", 3)

    assert captured["url"].startswith(
        "https://naverapihub.apigw.ntruss.com/search/v1/news?")
    assert captured["headers"]["X-NCP-APIGW-API-KEY-ID"] == "cid"
    assert captured["headers"]["X-NCP-APIGW-API-KEY"] == "csecret"
    # 예전 헤더가 남아 있으면 안 된다
    assert "X-Naver-Client-Id" not in captured["headers"]

    assert results[0].title == "물가 6.3% 상승"  # <b> 태그 제거
    assert results[0].source_type == claims.NEWS
    assert results[0].source_type_label == "언론 보도"


def test_네이버_카테고리별로_출처_유형이_달라진다():
    news = claims.NaverSearchProvider("news", "a", "b")
    encyc = claims.NaverSearchProvider("encyc", "a", "b")
    assert news.source_type == claims.NEWS
    assert encyc.source_type == claims.ENCYCLOPEDIA


def test_팩트체크_검색은_첫_review만_고르지_않는다(monkeypatch):
    monkeypatch.setattr(claims, "_http_get_json", lambda *a, **k: {"claims": [{
        "text": "가상시 인구는 10만명이다",
        "claimReview": [
            {"title": "검증 A", "url": "https://a.example/review", "textualRating": "사실"},
            {"title": "검증 B", "url": "https://b.example/review", "textualRating": "거짓"},
        ],
    }]})
    result = claims.FactCheckProvider("key").search("가상시 인구", 3)
    assert [item.rating for item in result] == ["사실", "거짓"]


def test_근거_HTTPS_요청은_인증서와_호스트명을_검증한다(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            captured["read_size"] = size
            return b'{}'

    def fake_urlopen(request, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(claims.urllib.request, "urlopen", fake_urlopen)
    claims._HOST_LAST_CALL.pop("evidence-tls.example", None)
    assert claims._http_get_json("https://evidence-tls.example/data", 1) == {}
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True
    assert captured["read_size"] == claims._MAX_HTTP_RESPONSE_BYTES + 1


def test_근거_HTTP_성공_응답이_상한을_넘으면_검색_실패다(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return b"x" * size

    monkeypatch.setattr(claims.urllib.request, "urlopen", lambda *a, **k: Response())
    claims._HOST_LAST_CALL.pop("large-evidence.example", None)
    with pytest.raises(claims.EvidenceSearchError, match="허용 크기"):
        claims._http_get_json("https://large-evidence.example/data", 1)


def test_근거_HTTP_오류_본문도_고정_상한만_읽는다(monkeypatch):
    read_sizes = []

    class TrackingBody(io.BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    error = urllib.error.HTTPError(
        "https://error-evidence.example/data", 500, "error", {}, TrackingBody(b"detail")
    )
    monkeypatch.setattr(
        claims.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(error)
    )
    claims._HOST_LAST_CALL.pop("error-evidence.example", None)
    with pytest.raises(claims.EvidenceSearchError, match="HTTP 오류"):
        claims._http_get_json("https://error-evidence.example/data", 1)
    assert read_sizes == [claims._MAX_HTTP_ERROR_BYTES]


def test_근거_연결_오류_로그에_reason_원문을_남기지_않는다(monkeypatch, caplog):
    secret = "connection detail with credential"
    error = urllib.error.URLError(RuntimeError(secret))
    monkeypatch.setattr(
        claims.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(error)
    )
    claims._HOST_LAST_CALL.pop("connection-error.example", None)
    with caplog.at_level(logging.WARNING), pytest.raises(
            claims.EvidenceSearchError, match="연결 오류"):
        claims._http_get_json("https://connection-error.example/data", 1)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_자격_정보가_없으면_네이버_제공자는_조용히_빠진다(monkeypatch):
    cfg = replace(config, naver_client_id="", naver_client_secret="")
    assert claims._build_provider("naver_news", cfg) is None


def test_지원하지_않는_네이버_카테고리는_거른다():
    cfg = replace(config, naver_client_id="a", naver_client_secret="b")
    # 개인 게시물은 판정 근거로 쓰지 않기로 했다(evidence-policy.md).
    assert claims._build_provider("naver_blog", cfg) is None
    assert claims._build_provider("naver_cafe", cfg) is None


# --- 응답 스키마 (FE 와이어프레임 계약) ---------------------------------

def test_media_블록은_목록에_있는_항목을_빠짐없이_담는다():
    """키를 하나씩 골라 담다가 나중에 추가한 필드가 조용히 버려진 적이 있다."""
    from deepcheck import report

    meta = {key: f"v-{key}" for key in report._MEDIA_KEYS}
    meta["url"] = "https://example.com"
    built = report.build(meta, {"frames_analyzed": 0, "method": "none"},
                         {"summary": "", "keywords": [], "tone": ""}, {})

    for key in report._MEDIA_KEYS:
        assert built.media.get(key) == f"v-{key}", f"{key}가 응답에서 빠졌다"


def test_발행일은_검색_수단이_달라도_같은_형식으로_나온다():
    """네이버는 RFC 2822, 위키백과는 ISO를 준다. 형식이 섞이면 화면도 지저분하고
    LLM이 주장과 자료의 시점을 비교하기도 어려워진다."""
    naver = claims.Evidence(title="t", url="https://a.co.kr/1", source="naver_news",
                            published_at="Wed, 02 Sep 2026 07:00:00 +0900")
    wiki = claims.Evidence(title="t", url="https://ko.wikipedia.org/wiki/X",
                           source="wikipedia", published_at="2026-08-12T08:58:57Z")

    assert naver.published_at == "2026-09-02"
    assert wiki.published_at == "2026-08-12"


def test_읽지_못한_발행일은_버리지_않는다():
    e = claims.Evidence(title="t", url="https://a.co.kr/1", source="s",
                        published_at="언젠가")
    assert e.published_at == "언젠가"


def test_발행처는_원문_링크의_도메인을_쓴다():
    e = claims.Evidence(title="t", url="https://www.yna.co.kr/view/AKR1", source="naver_news")
    assert e.publisher == "yna.co.kr"


def test_발언_위치의_정확도를_구분한다():
    segments = [{"text": "소비자물가 상승률이 6.0%를 넘었습니다", "start": 12.4, "end": 18.9},
                {"text": "정부는 성수품 공급을 늘린다고 밝혔습니다", "start": 20.0, "end": 26.0}]
    exact = claims.Claim(text="소비자물가 상승률이 6.0%를 넘었습니다")
    approx = claims.Claim(text="성수품 공급을 정부가 늘린다")
    missing = claims.Claim(text="전혀 관계없는 다른 문장입니다")

    claims._attach_timestamps([exact, approx, missing], segments)

    assert exact.time_precision == claims.TIME_EXACT
    assert approx.time_precision == claims.TIME_APPROX
    assert missing.time_precision is None and missing.start is None


def test_분석_ID는_사람이_옮겨_적을_수_있는_길이다():
    from backend.harness import Job

    job = Job(id="ac6a500f7fa94cf696cb19527b9c06ca", session_id="s", url="u", params={})
    data = job.to_dict()

    assert data["display_id"] == "CN-AC6A-500F"
    assert data["created_at_iso"]
    assert isinstance(data["elapsed_sec"], float)


# --- 검증할 주장이 없을 때 (U-03) ---------------------------------------

def test_검증할_주장이_없으면_분석_불가와_구분한다(monkeypatch):
    """카드가 0개인 것과 분석을 못 한 것은 사용자에게 완전히 다른 이야기다."""
    from deepcheck import pipeline, report

    tracker = pipeline.StageTracker()
    result = pipeline._verify_claims(
        "안녕하세요. 오늘 날씨가 참 좋네요. 다들 좋은 하루 보내세요.",
        [], pipeline.AnalysisOptions(), tracker,
        deadline=float("inf"), progress=lambda *a: None, partial=lambda *a: None,
    )

    assert result.status == report.AxisStatus.NO_CLAIMS.value
    assert result.status != report.AxisStatus.UNAVAILABLE.value
    assert result.summary["total"] == 0
    assert result.detail  # 왜 없는지 안내가 있다


def test_발언_텍스트가_없으면_분석_불가다():
    """주장이 없는 것과 발언 텍스트를 못 얻은 것은 다르다."""
    from deepcheck import pipeline, report

    tracker = pipeline.StageTracker()
    result = pipeline._verify_claims(
        "", [], pipeline.AnalysisOptions(), tracker,
        deadline=float("inf"), progress=lambda *a: None, partial=lambda *a: None,
    )

    assert result.status == report.AxisStatus.UNAVAILABLE.value
