"""주장 사실성 검증.

설계 문서(`design/ai-pipeline.md`)의 흐름을 따른다.

    발언 텍스트 → 검증 가능한 주장 추출 → 주장별 외부 근거 검색 → 지지·반박·판단 유보

판정에 대한 태도가 이 모듈의 핵심이다. PRD와 설계 문서가 못 박아 둔 두 가지를
그대로 구현한다.

1. 검색 결과가 없다는 이유만으로 주장을 거짓으로 판정하지 않는다.
2. 근거가 부족하거나 서로 충돌하면 판단을 유보한다.

그래서 기본 동작은 보수적이다. 우리가 직접 "지지/반박"을 선언하는 경우는 이미
전문 기관이 검증해 공개한 판정을 찾았을 때뿐이고, 나머지는 관련 근거를 모아
보여주되 판정은 유보한다. 키워드가 겹친다는 이유로 참·거짓을 단정하면 그럴듯한
오답을 만들어낼 뿐이다.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .config import config

logger = logging.getLogger(__name__)

SUPPORTED = "supported"
REFUTED = "refuted"
UNVERIFIED = "unverified"

# 검증할 만한 주장인지 가르는 신호들. 숫자·연도·비율은 사실 확인이 가능하고,
# 단정적 표현은 확인해볼 가치가 있는 문장을 고르는 데 쓴다.
_NUMERIC_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*년?")
_PERCENT_RE = re.compile(r"\d+(\.\d+)?\s*(%|퍼센트|프로)")
_QUANTITY_RE = re.compile(r"\d+(\.\d+)?\s*(명|원|달러|억|만|천|배|위|건|개|톤|km|킬로)")
_ASSERTION_RE = re.compile(
    r"(이다|입니다|였다|했다|한다|된다|밝혀졌|확인됐|발표했|증가했|감소했|"
    r"is |are |was |were |said|announced|reported|found)"
)
# 의견·감상·인사말처럼 사실 확인 대상이 아닌 문장을 걸러낸다.
_OPINION_RE = re.compile(
    r"(생각합니다|생각해|같아요|같습니다|느낌|좋아요|싫어|재밌|구독|좋아요 눌러|"
    r"안녕하세요|감사합니다|i think|i feel|please subscribe|welcome back)"
)
# 아직 일어나지 않은 일은 검증할 수 없다. "조만간 7%도 넘어설 것으로 보입니다" 같은
# 전망을 주장으로 뽑으면, 어떤 근거를 가져와도 판정할 수 없는 항목만 늘어난다.
_SPECULATION_RE = re.compile(
    r"(것으로 보입니다|것으로 예상|전망입니다|전망이다|예상됩니다|예상된다|"
    r"할 것입니다|할 전망|우려됩니다|가능성이 (있|높|커)|"
    r"is expected to|is likely to|will likely|forecast)"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")


@dataclass
class Evidence:
    """주장과 대조할 외부 자료 한 건."""

    title: str
    url: str
    source: str          # 어디서 찾았는지 (wikipedia / gdelt / factcheck)
    published_at: str | None = None
    snippet: str | None = None
    rating: str | None = None  # 전문 기관이 내린 판정이 있으면 그 원문 표기


@dataclass
class Claim:
    """영상에서 뽑은 검증 대상 주장 하나."""

    text: str
    start: float | None = None
    end: float | None = None
    verdict: str = UNVERIFIED
    reason: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[Evidence]:
        """주장과 관련된 자료를 찾는다. 실패하면 빈 목록을 반환한다."""


def _http_get_json(url: str, timeout: int) -> dict | None:
    endpoint = url.split("?")[0]
    request = urllib.request.Request(url, headers={"User-Agent": "ConanAI/0.2 (research)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("근거 검색이 속도 제한에 걸렸습니다(%s) — 이번 주장은 건너뜁니다", endpoint)
        else:
            logger.warning("근거 검색 요청 실패(%s): HTTP %s", endpoint, e.code)
    except urllib.error.URLError as e:
        logger.warning("근거 검색 요청 실패(%s): %s", endpoint, e.reason)
    except Exception as e:
        logger.warning("근거 검색 응답 처리 실패(%s): %s", endpoint, e)
    return None


class WikipediaProvider:
    """위키백과 검색. API 키가 필요 없고 응답이 빠르다."""

    def __init__(self, language: str = "ko", timeout: int | None = None):
        self.language = language
        # 언어별 인스턴스를 구분할 수 있게 이름에 언어를 담는다(근거의 source는 wikipedia로 통일).
        self.name = "wikipedia" if language == "ko" else f"wikipedia_{language}"
        self.timeout = timeout or config.evidence_timeout_sec

    def search(self, query: str, limit: int) -> list[Evidence]:
        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": limit, "format": "json", "utf8": 1,
        })
        data = _http_get_json(f"https://{self.language}.wikipedia.org/w/api.php?{params}",
                              self.timeout)
        if not data:
            return []
        results = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
            results.append(Evidence(
                title=title,
                url=f"https://{self.language}.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                source="wikipedia",
                published_at=item.get("timestamp"),
                snippet=snippet or None,
            ))
        return results


class GDELTProvider:
    """GDELT 뉴스 아카이브 검색. API 키가 필요 없다.

    기본 제공자에서는 빠져 있다. 실측에서 응답에 16초 이상 걸렸고 공유 IP 기준으로
    429(속도 제한)를 자주 돌려줘서, 주장마다 붙이면 분석 시간이 감당이 안 됐다.
    `DEEPCHECK_EVIDENCE_PROVIDERS`에 `gdelt`를 넣으면 다시 켤 수 있다.
    """

    name = "gdelt"

    def __init__(self, timeout: int | None = None):
        self.timeout = timeout or config.evidence_timeout_sec

    def search(self, query: str, limit: int) -> list[Evidence]:
        params = urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json",
            "maxrecords": limit, "sort": "hybridrel",
        })
        data = _http_get_json(f"https://api.gdeltproject.org/api/v2/doc/doc?{params}",
                              self.timeout)
        if not data:
            return []
        return [
            Evidence(
                title=item.get("title", ""),
                url=item.get("url", ""),
                source=self.name,
                published_at=item.get("seendate"),
                snippet=item.get("domain"),
            )
            for item in data.get("articles", [])[:limit]
            if item.get("url")
        ]


class FactCheckProvider:
    """Google Fact Check Tools API.

    전문 기관이 이미 검증해 공개한 판정을 찾는다. 우리가 유일하게 지지/반박을
    선언하는 근거이므로 다른 제공자와 구분해서 다룬다. API 키가 필요하다.
    """

    name = "factcheck"

    def __init__(self, api_key: str, timeout: int | None = None):
        self.api_key = api_key
        self.timeout = timeout or config.evidence_timeout_sec

    def search(self, query: str, limit: int) -> list[Evidence]:
        params = urllib.parse.urlencode({
            "query": query, "key": self.api_key, "languageCode": "ko", "pageSize": limit,
        })
        url = f"https://factchecktools.googleapis.com/v1alpha1/claims:search?{params}"
        data = _http_get_json(url, self.timeout)
        if not data:
            return []
        results = []
        for item in data.get("claims", [])[:limit]:
            for review in item.get("claimReview", [])[:1]:
                results.append(Evidence(
                    title=review.get("title") or item.get("text", ""),
                    url=review.get("url", ""),
                    source=self.name,
                    published_at=review.get("reviewDate"),
                    snippet=item.get("text"),
                    rating=review.get("textualRating"),
                ))
        return results


# 전문 기관의 판정 표기를 우리 판정으로 옮긴다. 애매한 표기는 옮기지 않고 유보한다.
_REFUTED_RATINGS = ("false", "거짓", "사실 아님", "허위", "잘못", "misleading", "오해",
                    "pants on fire", "incorrect", "no evidence")
_SUPPORTED_RATINGS = ("true", "사실", "correct", "accurate")


def _verdict_from_rating(rating: str | None) -> tuple[str, str] | None:
    if not rating:
        return None
    lowered = rating.lower()
    if any(word in lowered for word in _REFUTED_RATINGS):
        return REFUTED, f'전문 기관 판정: "{rating}"'
    # "mostly true"처럼 부분 사실은 지지로 넘기지 않는다. 정확히 사실로 읽히는 것만.
    if any(lowered.startswith(word) or lowered == word for word in _SUPPORTED_RATINGS):
        return SUPPORTED, f'전문 기관 판정: "{rating}"'
    return None


def extract_claims(text: str, segments: list[dict] | None = None,
                   max_claims: int | None = None) -> list[Claim]:
    """발언 텍스트에서 검증 가능한 주장을 고른다.

    지금은 규칙 기반이다. 숫자·연도·수량이 들어 있고 단정적으로 서술한 문장을
    검증 대상으로 본다. 의견이나 인사말은 제외한다. 이 방식은 LLM 없이 동작하고
    결과를 설명할 수 있다는 장점이 있지만, 문맥을 넘는 주장(대명사로 지칭한 대상
    등)은 놓친다. 개선하려면 이 함수만 교체하면 된다.
    """
    limit = max_claims if max_claims is not None else config.max_claims
    if not text or limit <= 0:
        return []

    scored: list[tuple[float, str]] = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw.strip()
        if not (15 <= len(sentence) <= 300):
            continue
        if _OPINION_RE.search(sentence.lower()):
            continue
        if _SPECULATION_RE.search(sentence):
            continue
        if not _ASSERTION_RE.search(sentence):
            continue

        score = 0.0
        if _PERCENT_RE.search(sentence):
            score += 3
        if _QUANTITY_RE.search(sentence):
            score += 2
        if _YEAR_RE.search(sentence):
            score += 2
        if _NUMERIC_RE.search(sentence):
            score += 1
        # 고유명사(대문자로 시작하는 낱말, 따옴표로 인용한 대상)가 있으면 검색이 쉬워진다.
        if re.search(r"[A-Z][a-z]{2,}|[「\"'][^」\"']{2,}[」\"']", sentence):
            score += 1
        if score < 2:
            continue
        scored.append((score, sentence))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    claims = [Claim(text=sentence) for _, sentence in scored[:limit]]
    _attach_timestamps(claims, segments or [])
    logger.info("검증 대상 주장 %d건 추출 (후보 %d건)", len(claims), len(scored))
    return claims


def _attach_timestamps(claims: list[Claim], segments: list[dict]) -> None:
    """주장이 영상 어디쯤에서 나온 말인지 붙인다. 사용자가 원문을 확인할 수 있게."""
    for claim in claims:
        head = claim.text[:20]
        for segment in segments:
            if head and head in segment.get("text", ""):
                claim.start = segment.get("start")
                claim.end = segment.get("end")
                break


# 검색에 도움이 안 되는 흔한 낱말과 서술어. 한국어는 조사가 붙어 오므로 접미 제거도 함께 한다.
_QUERY_STOPWORDS = {
    "그리고", "하지만", "그런데", "이런", "저런", "그것", "이것", "우리", "여러분", "정말",
    "매우", "아주", "너무", "가장", "모든", "때문에", "위해", "대해", "통해", "따르면",
    "있다", "없다", "이다", "한다", "된다", "합니다", "입니다", "했다", "됐다", "같다",
    "the", "and", "that", "this", "with", "from", "have", "has", "was", "were", "are",
    "for", "but", "not", "you", "they", "what", "when", "which", "there", "their",
}
_PARTICLE_RE = re.compile(r"(이|가|은|는|을|를|의|에|에서|으로|로|와|과|도|만|보다|처럼|까지)$")


def _key_tokens(claim_text: str) -> list[str]:
    """주장에서 식별력이 높은 낱말만 뽑는다. 검색어와 관련성 판정에 함께 쓴다."""
    cleaned = re.sub(r"[^\w가-힣%\s]", " ", claim_text)
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for raw in cleaned.split():
        word = _PARTICLE_RE.sub("", raw) if re.search(r"[가-힣]$", raw) else raw
        lowered = word.lower()
        if len(word) < 2 or lowered in _QUERY_STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)

        score = float(len(word))
        if _NUMERIC_RE.search(word):
            score += 6  # 연도·수치는 사실 확인의 핵심이다
        if re.match(r"[A-Z]", word):
            score += 4  # 고유명사
        if len(word) >= 3 and re.search(r"[가-힣]", word):
            score += 2  # 긴 한국어 낱말은 대체로 명사다
        scored.append((score, word))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [word for _, word in scored]


def _is_relevant(claim_text: str, evidence: Evidence) -> bool:
    """찾아온 자료가 이 주장과 실제로 관련이 있는지 최소한으로 확인한다.

    검색 엔진은 질의어가 애매하면 아무 문서나 돌려준다. 실측에서 "물가 상승률"
    주장에 "주기율표"가 딸려 왔다. 무관한 자료를 근거라고 보여주는 것은 사실상
    거짓 근거이므로, 주장의 핵심어가 제목이나 인용문에 하나도 없으면 버린다.
    """
    haystack = f"{evidence.title} {evidence.snippet or ''}".lower()
    if not haystack.strip():
        return False
    # 전문 기관 판정은 그 자체가 이 주장을 검증한 결과이므로 통과시킨다.
    if evidence.rating:
        return True
    for token in _key_tokens(claim_text)[:8]:
        if len(token) >= 2 and token.lower() in haystack:
            return True
    return False


def _search_query(claim_text: str) -> str:
    """주장 문장을 검색어로 줄인다.

    문장을 통째로 넣으면 검색 엔진이 매칭할 것을 찾지 못한다. 고유명사와 수치처럼
    식별력이 높은 낱말만 남겨 짧은 질의를 만든다.
    """
    return " ".join(_key_tokens(claim_text)[:6])


def _build_provider(name: str, cfg) -> EvidenceProvider | None:
    if name == "wikipedia":
        return WikipediaProvider("ko", cfg.evidence_timeout_sec)
    if name == "wikipedia_en":
        return WikipediaProvider("en", cfg.evidence_timeout_sec)
    if name == "gdelt":
        return GDELTProvider(cfg.evidence_timeout_sec)
    if name == "factcheck":
        if cfg.google_factcheck_api_key:
            return FactCheckProvider(cfg.google_factcheck_api_key, cfg.evidence_timeout_sec)
        logger.info("Google Fact Check API 키가 없어 전문 기관 판정 검색은 건너뛴다")
        return None
    logger.warning("알 수 없는 근거 검색 제공자: %s", name)
    return None


def default_providers(cfg=None) -> list[EvidenceProvider]:
    """설정(`DEEPCHECK_EVIDENCE_PROVIDERS`)에 적힌 순서대로 검색 수단을 만든다.

    전문 기관 판정(factcheck)을 앞에 두는 이유는, 우리가 지지·반박을 선언할 수 있는
    유일한 근거라서 먼저 확인하는 편이 낫기 때문이다.

    설정을 인자로 받는 이유는 테스트에서 다른 조합을 넣어보기 위해서다. Config는
    frozen dataclass라 속성을 덮어쓸 수 없다.
    """
    cfg = cfg or config
    names = [n.strip() for n in cfg.evidence_providers.split(",") if n.strip()]
    providers = [p for p in (_build_provider(name, cfg) for name in names) if p is not None]
    if not providers:
        logger.warning("사용 가능한 근거 검색 제공자가 없다 — 모든 주장이 판단 유보로 남는다")
    return providers


def verify_claims(claims: list[Claim], providers: list[EvidenceProvider] | None = None,
                  evidence_per_claim: int | None = None,
                  time_budget_sec: float | None = None) -> list[Claim]:
    """주장마다 근거를 모으고 판정한다.

    우리가 지지/반박을 선언하는 경우는 전문 기관의 공개 판정을 찾았을 때뿐이다.
    나머지는 관련 자료를 붙여 두되 판단은 유보한다.

    외부 검색은 느려질 수 있으므로(제공자 하나가 타임아웃까지 버티면 주장마다 수십
    초가 든다) 전체 시간 예산을 두고, 예산을 넘기면 남은 주장은 검색 없이 유보한다.
    응답이 영원히 안 오는 것보다 "일부는 확인하지 못했다"고 말하는 편이 낫다.
    """
    if not claims:
        return []
    active = providers if providers is not None else default_providers()
    per_claim = evidence_per_claim or config.evidence_per_claim
    budget = time_budget_sec if time_budget_sec is not None else config.evidence_budget_sec
    started = time.monotonic()

    for claim in claims:
        if budget and time.monotonic() - started > budget:
            claim.verdict = UNVERIFIED
            claim.reason = "근거 검색 시간이 예산을 넘어 이 주장은 확인하지 못했다."
            logger.warning("근거 검색 시간 예산(%.0fs) 초과 — 남은 주장은 검색을 건너뛴다", budget)
            continue

        query = _search_query(claim.text)
        collected: list[Evidence] = []
        for provider in active:
            try:
                collected.extend(provider.search(query, per_claim))
            except Exception as e:
                # 근거 검색 실패가 분석 전체를 멈추게 하지 않는다.
                logger.warning("근거 검색 실패(%s): %s", getattr(provider, "name", "?"), e)

        # 검색 결과 중 이 주장과 실제로 관련 있는 것만 근거로 삼는다.
        relevant = [e for e in collected if _is_relevant(claim.text, e)]
        dropped = len(collected) - len(relevant)
        if dropped:
            logger.info("무관한 검색 결과 %d건 제외 (남은 근거 %d건)", dropped, len(relevant))
        claim.evidence = relevant[: per_claim * 2]

        decided = None
        for evidence in relevant:
            decided = _verdict_from_rating(evidence.rating)
            if decided:
                break

        if decided:
            claim.verdict, claim.reason = decided
        elif relevant:
            claim.verdict = UNVERIFIED
            claim.reason = ("관련 자료는 찾았지만 이 주장을 직접 검증한 판정이 없어 "
                            "판단을 유보한다.")
        else:
            # 못 찾았다고 거짓이 아니다.
            claim.verdict = UNVERIFIED
            claim.reason = "관련 근거를 찾지 못해 판단을 유보한다. 거짓이라는 뜻은 아니다."

        logger.info("주장 판정: %s — %s (근거 %d건)",
                    claim.verdict, claim.text[:40], len(claim.evidence))

    return claims
