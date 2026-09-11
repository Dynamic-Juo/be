"""주장 사실성 검증.

설계 문서(`design/ai-pipeline.md`)의 흐름을 따른다.

    발언 텍스트 → 검증 가능한 주장 추출 → 주장별 외부 근거 검색 → 근거와 일치·불일치·근거 부족

판정에 대한 태도가 이 모듈의 핵심이다. PRD와 설계 문서가 못 박아 둔 두 가지를
그대로 구현한다.

1. 검색 결과가 없다는 이유만으로 주장을 거짓으로 판정하지 않는다.
2. 근거가 부족하거나 서로 충돌하면 판단을 유보한다.

우리가 내는 판정은 주장의 진위가 아니라 주장과 근거의 관계다. `근거와 일치`는
"이 주장은 참"이 아니라 "우리가 찾은 근거와 일치한다"는 뜻이다.

전문 기관의 공개 판정도 검색 메타데이터만으로 그대로 옮기지 않는다. LLM에게 근거를
주고 관계를 판정하게 하되, 원문·출처 검증과 인용 검증을 통과해야 일치·불일치를
인정한다. 조건을 만족하지 못하면 근거 부족으로 강등한다.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .config import config
from .prompts import EXTRACT_SYSTEM, VERDICT_SYSTEM, PROMPT_VERSION

logger = logging.getLogger(__name__)

SUPPORTED = "supported"
REFUTED = "refuted"
UNVERIFIED = "unverified"

# 화면 표기는 docs U-03이 정한 문구를 그대로 쓴다. "지지/반박"은 주장 자체의
# 진위를 단정하는 것처럼 읽혀서 바꿨다 — 우리가 말하는 건 주장과 "우리가 찾은
# 근거" 사이의 관계다.
VERDICT_LABELS = {
    SUPPORTED: "근거와 일치",
    REFUTED: "근거와 불일치",
    UNVERIFIED: "근거 부족",
}

# 근거 부족 사유(U-03, evidence-policy.md). 규칙만으로 판별 가능한 것과 LLM 판정이
# 있어야 하는 것이 섞여 있다 — 앞의 셋은 지금 코드가 스스로 구분한다.
NO_SOURCE = "no_source"            # 검색했지만 관련 자료를 찾지 못함
NOT_DIRECT = "not_direct"          # 자료는 있으나 주장을 직접 확인하지 못함
TIMEOUT = "timeout"                # 시간 내 확인하지 못함
TIME_MISMATCH = "time_mismatch"    # 주장과 자료의 기준 시점이 다름
SOURCE_CONFLICT = "source_conflict"  # 신뢰할 수 있는 출처들이 서로 충돌
WEAK_SOURCE = "weak_source"        # 출처의 원문·신뢰성·내용이 충분하지 않음
PARTIAL = "partial"                # 복합 주장의 일부만 확인됨

INSUFFICIENT_LABELS = {
    NO_SOURCE: "관련 자료를 찾지 못함",
    NOT_DIRECT: "자료는 있으나 주장을 직접 확인하지 못함",
    TIMEOUT: "시간 내 확인하지 못함",
    TIME_MISMATCH: "주장과 자료의 시점이 다름",
    SOURCE_CONFLICT: "출처들이 서로 충돌함",
    WEAK_SOURCE: "출처의 내용이 판정에 충분하지 않음",
    PARTIAL: "주장의 일부만 확인됨",
}

# 카드 처리 상태. verdict(지지/반박/유보)와는 별개 축이다 — result-ui.md가 이
# 둘을 분리해서 표시하라고 정했다: "처리 상태와 검증 판정을 분리한다."
# 발언 위치의 정확도.
TIME_EXACT = "exact"
TIME_APPROX = "approx"

PENDING = "pending"
VERIFYING = "verifying"
DONE = "done"
FAILED = "failed"
TIMED_OUT = "timed_out"

# 근거 본문 범위. 검색 제공자가 돌려준 제목·요약은 원문이 아니다. 양성 판정은
# ORIGINAL이면서 아래 provenance 검증 필드가 명시된 자료만 사용할 수 있다.
SEARCH_EXCERPT = "search_excerpt"
ORIGINAL = "original"

# 검증할 만한 주장인지 가르는 신호들. 숫자·연도·비율은 사실 확인이 가능하고,
# 단정적 표현은 확인해볼 가치가 있는 문장을 고르는 데 쓴다.
_NUMERIC_RE = re.compile(r"\d")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*년?")
_PERCENT_RE = re.compile(r"\d+(\.\d+)?\s*(%|퍼센트|프로)")
# 큰 수 단위(조·경)가 빠져 있어서 "42조원 규모" 같은 문장이 후보에서 탈락했다.
# 뉴스에서 가장 흔한 수치 표현이라 놓치면 안 된다.
_QUANTITY_RE = re.compile(
    r"\d+(\.\d+)?\s*(경|조|억|만|천|명|원|달러|엔|위안|유로|배|위|건|개|대|톤|kg|km|킬로|미터|시간|분|초|세|살|%)"
)
# 사실을 서술하는 문장인지 본다. 한국어 서술문은 "-다"/"-니다"로 끝나는 격식체
# 뿐 아니라, 방송·인터뷰에서 흔한 구어체 종결어미(-는데요, -죠, -네요 등)로도
# 끝난다. 격식체만 보고 있었을 때는 "...최대 50%까지 할인 받을 수 있는데요."처럼
# 수치가 명확한 문장을 실제로 놓쳤다(2026-09-08 실측, 발언 위치 00:44 부근).
# 문장 끝을 보므로 잘린 자막 조각("...외식 물가가")도 자연히 걸러진다.
_ASSERTION_RE = re.compile(
    r"(?:니다|다|는데요|거든요|잖아요|네요|던데요|죠)\s*[.!?]?\s*$"
    r"|(?:\bis\b|\bare\b|\bwas\b|\bwere\b|said|announced|reported|rose|fell|reached)"
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
# 자막에는 마침표가 없는 경우가 많다(줄 단위로 끊겨 들어온다). 문장 부호만으로
# 나누면 여러 문장이 통째로 붙거나 조각이 남으므로, 한국어 종결형 뒤도 경계로 본다.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+|(?<=니다)\s+|(?<=습니다)\s+")


# 출처 유형(U-02). evidence-policy.md가 "1차 출처를 우선하고 없으면 서로 독립적인
# 신뢰 가능한 자료 두 개 이상"을 요구해서, 어느 것이 1차인지 구분할 수 있어야 한다.
STATISTICS = "statistics"      # 통계 원문 — 1차
OFFICIAL = "official"          # 정부·기관 공식 발표 — 1차
FACTCHECK = "factcheck"        # 전문 기관 팩트체크 판정
NEWS = "news"                  # 언론 보도
ENCYCLOPEDIA = "encyclopedia"  # 백과사전
UNKNOWN_SOURCE = "unknown"

SOURCE_TYPE_LABELS = {
    STATISTICS: "통계 원문",
    OFFICIAL: "공식 발표",
    FACTCHECK: "팩트체크 판정",
    NEWS: "언론 보도",
    ENCYCLOPEDIA: "백과사전",
    UNKNOWN_SOURCE: "기타",
}

# 1차 출처로 취급하는 유형. 판정 우선순위와 근거 정렬에 쓴다.
PRIMARY_SOURCE_TYPES = (STATISTICS, OFFICIAL)


class ClaimExtractionError(Exception):
    """설정한 주장 추출기가 유효한 결과를 만들지 못했다.

    명시적인 ``{"claims": []}``만 정상적인 "주장 없음"이다. 제공자 실패나
    잘못된 스키마를 빈 결과로 바꾸면 UI가 시스템 실패를 정상 결과로 표시하므로,
    호출부가 claim_verification 축을 unavailable로 만들 수 있게 예외를 올린다.
    """


class EvidenceSearchError(Exception):
    """근거 제공자 호출 또는 응답 처리에 실패했다."""


_MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_HTTP_ERROR_BYTES = 4096


def _is_safe_source_url(url: str | None) -> bool:
    """사용자에게 출처 링크로 내보낼 수 있는 HTTP(S) URL인지 확인한다."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        # user@host 형태는 화면에 신뢰 도메인을 가장하기 쉬워 출처 링크로 인정하지 않는다.
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def _normalize_published_at(raw: str | None) -> str | None:
    """근거의 발행일을 YYYY-MM-DD로 맞춘다.

    검색 수단마다 형식이 다르다. 네이버는 RFC 2822("Wed, 02 Sep 2026 07:00:00 +0900"),
    위키백과는 ISO("2026-08-12T08:58:57Z")를 준다. 형식이 섞이면 화면 표시도
    지저분하지만, 더 큰 문제는 판정이다 — evidence-policy.md가 주장과 자료의 시점을
    비교하라고 요구하는데, 프롬프트에 형식이 뒤섞인 날짜가 들어가면 모델이 비교를
    제대로 하기 어렵다.

    어느 형식으로도 못 읽으면 버리지 않고 원문을 그대로 둔다. 표시가 어색한 것이
    발행일 정보가 사라지는 것보다 낫다.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return parsedate_to_datetime(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        pass
    logger.debug("읽지 못한 발행일 형식: %r", raw)
    return text


def _publisher_from_url(url: str | None) -> str | None:
    """원문 링크의 도메인을 발행처로 쓴다.

    네이버 검색 응답에는 언론사명 필드가 없다. 도메인은 정확한 회사명은 아니지만
    사용자가 어디서 온 자료인지 알아보기에는 충분하고, 언론사명 매핑 표를 코드에
    박아두는 것보다 낫다. 예쁜 이름이 필요해지면 그건 코드가 아니라 데이터다.
    """
    if not url:
        return None
    host = urllib.parse.urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host or None


@dataclass
class Evidence:
    """주장과 대조할 외부 자료 한 건."""

    title: str
    url: str
    source: str          # 어디서 찾았는지 (wikipedia / naver_news / factcheck ...)
    published_at: str | None = None
    snippet: str | None = None
    rating: str | None = None  # 전문 기관이 내린 판정이 있으면 그 원문 표기
    # 모델에 실제로 전달할 자료 본문과 범위. 검색 제공자는 snippet만 채우며 기본 범위는
    # search_excerpt다. 원문을 별도 절차로 확보한 신뢰 경로만 content_scope=original과
    # content를 함께 설정한다.
    content: str | None = None
    content_scope: str = SEARCH_EXCERPT
    # 이 값은 LLM 응답이나 검색 제공자의 source_type만 보고 자동으로 켜지 않는다.
    # 신뢰 경계 안의 원문 검증기가 URL·발행처·본문 대응을 확인한 경우에만 True다.
    provenance_verified: bool = False
    # 1차 출처가 없을 때 같은 보도자료 재전송을 독립 출처로 중복 계산하지 않기 위한
    # 검증된 원문 계보 식별자다. 두 독립 출처 판정은 서로 다른 값 두 개가 필요하다.
    independence_group: str | None = None
    # 출처 유형. evidence-policy.md가 "1차 출처를 우선하고 없으면 독립 출처 두 개
    # 이상"을 요구하는데, 그 규칙을 적용하려면 유형을 알아야 한다.
    source_type: str = UNKNOWN_SOURCE
    source_type_label: str = ""
    # 1차 출처인지. 화면에서 근거의 무게를 구분해 보여줄 때 쓴다.
    is_primary: bool = False
    # 발행처. 네이버 검색 응답에는 언론사명이 없어서 원문 링크의 도메인을 쓴다.
    publisher: str | None = None
    # 이 자료가 실제로 판정에 쓰였는지. 근거 부족일 때 붙는 자료는 "참고 자료"이지
    # 판정 근거가 아니다. 화면에서 이 둘을 섞으면 판정하지 않은 것을 판정한 것처럼
    # 보여주게 된다.
    cited: bool = False
    # 이 자료가 주장을 어떻게 뒷받침·반박하는지. 판정에 쓰인 자료에만 채워진다.
    cite_reason: str | None = None
    # 위 이유의 근거가 된 원문 발췌. 실제로 이 자료 안에 있는지 대조한 것만 남는다.
    quote: str | None = None

    def __post_init__(self):
        self.source_type_label = SOURCE_TYPE_LABELS.get(self.source_type, "기타")
        self.is_primary = self.source_type in PRIMARY_SOURCE_TYPES
        if not self.publisher:
            self.publisher = _publisher_from_url(self.url)
        self.published_at = _normalize_published_at(self.published_at)
        if not isinstance(self.content_scope, str):
            self.content_scope = SEARCH_EXCERPT
        else:
            self.content_scope = self.content_scope.strip().lower()
        if not isinstance(self.independence_group, str) or not self.independence_group.strip():
            self.independence_group = None
        else:
            self.independence_group = self.independence_group.strip()


@dataclass
class Claim:
    """영상에서 뽑은 검증 대상 주장 하나."""

    text: str
    start: float | None = None
    end: float | None = None
    # 카드 처리 상태(pending/verifying/done/failed/timed_out). 폴링으로 이 값이
    # 바뀌는 걸 보고 프론트가 카드를 갱신한다.
    status: str = PENDING
    verdict: str = UNVERIFIED
    verdict_label: str = ""
    reason: str = ""
    # 근거 부족일 때의 사유 코드(U-03). verdict가 부족이 아니면 None.
    insufficient_reason: str | None = None
    insufficient_label: str | None = None
    # 판정 근거로 실제 인용한 문장. 인용 검증을 통과한 것만 들어간다.
    quote: str | None = None
    # 발언 위치의 정확도. exact=자막·세그먼트와 글자가 맞음, approx=핵심어 겹침으로
    # 추정, None=못 찾음. 화면에서 "00:12 ~ 00:19"와 "약 00:28"을 가르는 값이다.
    # 음성 인식이 준 구간은 몇 초 어긋날 수 있어서 단정해 보이면 안 된다(U-02).
    time_precision: str | None = None
    # 이 주장이 영상에서 언급된 모든 위치(M-04: 같은 의미는 병합하고 위치 보관).
    mentions: list[dict] = field(default_factory=list)
    # 전문에서 대조를 통과한 문맥만 보관한다. 영상 정보는 판정의 시점 해석용이다.
    context: str = ""
    video_title: str | None = None
    video_published_at: str | None = None
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self):
        if not self.verdict_label:
            self.verdict_label = VERDICT_LABELS.get(self.verdict, "")

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[Evidence]:
        """주장과 관련된 자료를 찾는다. 실패하면 예외를 발생시킨다."""


# 호스트별 요청 간격. 주장을 3건 병렬로 검증하면서 같은 호스트를 동시에 때리자
# 위키백과가 429를 돌려주기 시작했고, 근거가 통째로 비었다. 병렬은 유지하되
# 같은 호스트로 나가는 요청만 줄 세운다 — 서로 다른 제공자는 여전히 동시에 돈다.
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LAST_CALL: dict[str, float] = {}
_HOST_REGISTRY_LOCK = threading.Lock()
_TLS_CONTEXT = ssl.create_default_context()


def _host_gate(host: str) -> threading.Lock:
    with _HOST_REGISTRY_LOCK:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


def _http_get_json(url: str, timeout: int, headers: dict | None = None) -> dict:
    endpoint = url.split("?")[0]
    host = urllib.parse.urlparse(url).netloc
    merged = {
        # 위키미디어는 연락처가 없는 요청을 더 강하게 제한한다.
        "User-Agent": "ConanAI/0.2 (hackathon research; https://github.com/Dynamic-Juo)",
        "Accept": "application/json",
    }
    if headers:
        merged.update(headers)

    attempts = config.evidence_retry + 1
    for attempt in range(attempts):
        with _host_gate(host):
            gap = config.evidence_min_interval_sec - (
                time.monotonic() - _HOST_LAST_CALL.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            _HOST_LAST_CALL[host] = time.monotonic()
            try:
                request = urllib.request.Request(url, headers=merged)
                kwargs = {"timeout": timeout}
                if urllib.parse.urlsplit(url).scheme.lower() == "https":
                    # 명시적인 기본 컨텍스트로 CA 검증과 호스트명 검사를 고정한다.
                    kwargs["context"] = _TLS_CONTEXT
                with urllib.request.urlopen(request, **kwargs) as resp:
                    body = resp.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                    if len(body) > _MAX_HTTP_RESPONSE_BYTES:
                        raise EvidenceSearchError("근거 검색 응답이 허용 크기를 초과했다")
                    parsed = json.loads(body.decode())
                    if not isinstance(parsed, dict):
                        raise EvidenceSearchError("근거 검색 응답이 JSON 객체가 아니다")
                    return parsed
            except urllib.error.HTTPError as e:
                e.read(_MAX_HTTP_ERROR_BYTES)
                if e.code == 429 and attempt < attempts - 1:
                    backoff = config.evidence_min_interval_sec * (attempt + 2)
                    logger.info("속도 제한(%s) — %.1f초 후 재시도 %d/%d",
                                endpoint, backoff, attempt + 1, attempts - 1)
                    time.sleep(backoff)
                    continue
                if e.code == 429:
                    logger.warning("근거 검색이 속도 제한에 걸렸습니다(%s) — 이번 주장은 건너뜁니다",
                                   endpoint)
                else:
                    logger.warning("근거 검색 요청 실패(%s): HTTP %s", endpoint, e.code)
                raise EvidenceSearchError(f"근거 검색 HTTP 오류: {e.code}") from e
            except urllib.error.URLError as e:
                logger.warning(
                    "근거 검색 요청 실패(%s): %s",
                    endpoint,
                    type(e.reason).__name__,
                )
                raise EvidenceSearchError("근거 검색 연결 오류") from e
            except EvidenceSearchError:
                raise
            except Exception as e:
                logger.warning("근거 검색 응답 처리 실패(%s): %s", endpoint, type(e).__name__)
                raise EvidenceSearchError("근거 검색 응답 처리 오류") from e
    raise EvidenceSearchError("근거 검색 요청 실패")


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
                source_type=ENCYCLOPEDIA,
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
                source_type=NEWS,
            )
            for item in data.get("articles", [])[:limit]
            if _is_safe_source_url(item.get("url"))
        ]


class FactCheckProvider:
    """Google Fact Check Tools API.

    전문 기관이 이미 검증해 공개한 판정을 검색한다. API 응답은 원문이 아니라
    ClaimReview 메타데이터이므로 사용자 판정에 바로 옮기지 않는다. API 키가 필요하다.
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
        for item in data.get("claims", []):
            reviewed_claim = item.get("text")
            for review in item.get("claimReview", []):
                review_url = review.get("url", "")
                if not _is_safe_source_url(review_url):
                    continue
                publisher = review.get("publisher")
                publisher_name = publisher.get("name") if isinstance(publisher, dict) else None
                results.append(Evidence(
                    title=review.get("title") or reviewed_claim or "",
                    url=review_url,
                    source=self.name,
                    published_at=review.get("reviewDate"),
                    snippet=reviewed_claim,
                    rating=review.get("textualRating"),
                    source_type=FACTCHECK,
                    publisher=publisher_name,
                ))
                if len(results) >= limit:
                    return results
        return results


class NaverSearchProvider:
    """NAVER API HUB 검색 API.

    위키백과가 못 덮는 국내 시사 영역을 메우는 게 목적이다. 우리가 뽑는 주장은
    대부분 한국 뉴스의 수치·정책 발언인데, 그건 백과사전에 없고 기사에 있다.

    블로그·카페·지식iN은 쓰지 않는다. 개인 게시물을 판정 근거로 붙이면 결과가
    오염된다(evidence-policy.md의 출처 선택 기준).

    2026년 7월 31일에 기존 개발자센터 검색 API가 종료되고 네이버 클라우드의
    NAVER API HUB로 이관됐다. 도메인·경로·인증 헤더가 전부 바뀌어서 예전 코드는
    도메인만 갈아끼워도 동작하지 않는다.
      옛것: openapi.naver.com/v1/search/news.json + X-Naver-Client-Id
      지금: naverapihub.apigw.ntruss.com/search/v1/news + X-NCP-APIGW-API-KEY-ID
    """

    # 카테고리별로 무엇을 근거로 삼을지. 검색 결과의 성격이 달라서 출처 유형도 다르다.
    CATEGORIES = {
        "news": ("naver_news", NEWS),
        "encyc": ("naver_encyc", ENCYCLOPEDIA),
        "webkr": ("naver_web", UNKNOWN_SOURCE),
    }

    def __init__(self, category: str, client_id: str, client_secret: str,
                 timeout: int | None = None, base_url: str | None = None):
        self.category = category
        self.name, self.source_type = self.CATEGORIES[category]
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout or config.evidence_timeout_sec
        self.base_url = (base_url or config.naver_base_url).rstrip("/")

    def search(self, query: str, limit: int) -> list[Evidence]:
        params = urllib.parse.urlencode({
            "query": query, "display": limit, "sort": "sim",
        })
        url = f"{self.base_url}/search/v1/{self.category}?{params}"
        data = _http_get_json(url, self.timeout, headers={
            "X-NCP-APIGW-API-KEY-ID": self.client_id,
            "X-NCP-APIGW-API-KEY": self.client_secret,
        })
        if not data:
            return []
        results = []
        for item in data.get("items", [])[:limit]:
            # 검색어 강조를 <b> 태그로 넣어 준다. 그대로 두면 LLM 판정의 인용 대조가
            # 어긋나므로 제거한다.
            title = _strip_tags(item.get("title", ""))
            desc = _strip_tags(item.get("description", ""))
            link = item.get("originallink") or item.get("link", "")
            if not _is_safe_source_url(link):
                continue
            results.append(Evidence(
                title=title,
                url=link,
                source=self.name,
                published_at=item.get("pubDate"),
                snippet=desc or None,
                source_type=self.source_type,
            ))
        return results


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY = {"&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">", "&apos;": "'",
           "&nbsp;": " "}


def _strip_tags(raw: str) -> str:
    text = _TAG_RE.sub("", raw or "")
    for entity, ch in _ENTITY.items():
        text = text.replace(entity, ch)
    return text.strip()


# 전문 기관 판정끼리 충돌하는지 확인할 때만 쓰는 보수적인 정규화다. 이 결과만으로
# 사용자 판정을 확정하지 않는다. "no evidence", "misleading", "mostly true"처럼
# 부족·부분 판정일 수 있는 표현을 참/거짓으로 강제 변환하지 않는다.
_REFUTED_RATINGS = {"false", "거짓", "사실 아님", "허위", "pants on fire", "incorrect"}
_SUPPORTED_RATINGS = {"true", "사실", "correct", "accurate"}


def _verdict_from_rating(rating: str | None) -> tuple[str, str] | None:
    if not rating:
        return None
    lowered = re.sub(r"\s+", " ", rating).strip().lower()
    if lowered in _REFUTED_RATINGS:
        return REFUTED, f'전문 기관 판정: "{rating}"'
    if lowered in _SUPPORTED_RATINGS:
        return SUPPORTED, f'전문 기관 판정: "{rating}"'
    return None


# 자막 앞머리에 붙는 화자 표기. 주장 문장에 남으면 검색어와 화면 표시가 지저분해진다.
_SPEAKER_PREFIX_RE = re.compile(r"^[-–—\s]*\(?\s*(기자|앵커|리포터|인터뷰|현장음|내레이션)\s*\)?\s*[:：]?\s*")


def _clean_sentence(raw: str) -> str:
    """문장에서 화자 표기와 중복 공백을 걷어낸다."""
    sentence = _SPEAKER_PREFIX_RE.sub("", raw.strip())
    return re.sub(r"\s+", " ", sentence).strip()


def _candidate_score(sentence: str) -> float:
    """이 문장이 사실 확인 대상으로 얼마나 적합한지. 2점 미만이면 후보에서 뺀다."""
    if not (15 <= len(sentence) <= 300):
        return 0.0
    if _OPINION_RE.search(sentence.lower()):
        return 0.0
    if _SPECULATION_RE.search(sentence):
        return 0.0
    if not _ASSERTION_RE.search(sentence):
        return 0.0

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
    return score if score >= 2 else 0.0


def _is_duplicate(sentence: str, chosen: list[str]) -> bool:
    """이미 고른 주장과 사실상 같은 말인지 본다.

    뉴스는 앵커가 말한 것을 기자가 다시 말한다. 같은 사실이 두 슬롯을 차지하면
    영상의 다른 내용을 검증할 기회가 줄어든다.
    """
    tokens = set(_key_tokens(sentence)[:8])
    if not tokens:
        return False
    for other in chosen:
        other_tokens = set(_key_tokens(other)[:8])
        if not other_tokens:
            continue
        overlap = len(tokens & other_tokens) / min(len(tokens), len(other_tokens))
        if overlap >= 0.7:
            return True
    return False


def extract_claims(text: str, segments: list[dict] | None = None,
                   max_claims: int | None = None) -> list[Claim]:
    """발언 텍스트에서 검증 가능한 주장을 고른다.

    한 영상에 여러 내용이 나오므로 두 가지를 함께 처리한다.

    - **분산**: 점수 상위만 뽑으면 숫자가 몰린 한 대목이 슬롯을 다 차지하고 영상
      뒷부분이 통째로 누락된다. 전사를 구간으로 나눠 각 구간에서 먼저 한 건씩 뽑고,
      남는 자리를 점수순으로 채운다.
    - **중복 제거**: 같은 사실을 앵커와 기자가 반복하면 슬롯만 낭비한다.

    규칙 기반이라 LLM 없이 동작하고 왜 뽑혔는지 설명할 수 있다. 대신 대명사로
    지칭한 대상처럼 문맥을 넘는 주장은 여전히 놓친다. 개선하려면 이 함수를 교체한다.
    """
    limit = max_claims if max_claims is not None else config.max_claims
    if not text:
        return []
    # limit<=0은 무제한이다(M-04: 검증 가능한 주장을 모두 검증). 개수가 아니라
    # 시간으로 자르므로, 여기서는 후보를 다 돌려주고 파이프라인이 예산 안에서 처리한다.
    unlimited = limit is None or limit <= 0

    sentences = [_clean_sentence(s) for s in _SENTENCE_SPLIT_RE.split(text)]
    sentences = [s for s in sentences if s]
    candidates: list[tuple[int, float, str]] = []
    for position, sentence in enumerate(sentences):
        score = _candidate_score(sentence)
        if score:
            candidates.append((position, score, sentence))

    if not candidates:
        logger.info("검증 대상 주장 없음 (문장 %d개 검토)", len(sentences))
        return []

    chosen: list[tuple[int, str]] = []
    chosen_texts: list[str] = []

    if unlimited:
        # 무제한이면 구간 분산이 필요 없다. 중복만 걸러 등장 순서대로 돌려준다.
        for position, _score, sentence in candidates:
            if _is_duplicate(sentence, chosen_texts):
                continue
            chosen.append((position, sentence))
            chosen_texts.append(sentence)
        claims = [Claim(text=sentence) for _, sentence in chosen]
        _attach_timestamps(claims, segments or [])
        logger.info("검증 대상 주장 %d건 추출 (상한 없음, 후보 %d건, 문장 %d개)",
                    len(claims), len(candidates), len(sentences))
        return claims

    # 1단계: 영상을 limit개 구간으로 나눠 각 구간의 최고 점수를 하나씩 가져온다.
    span = max(len(sentences) / limit, 1)
    for bucket in range(limit):
        low, high = bucket * span, (bucket + 1) * span
        in_bucket = [c for c in candidates if low <= c[0] < high and c[2] not in chosen_texts]
        if not in_bucket:
            continue
        best = max(in_bucket, key=lambda c: c[1])
        if _is_duplicate(best[2], chosen_texts):
            continue
        chosen.append((best[0], best[2]))
        chosen_texts.append(best[2])

    # 2단계: 남은 자리는 점수가 높은 순으로 채운다.
    for position, _score, sentence in sorted(candidates, key=lambda c: c[1], reverse=True):
        if len(chosen) >= limit:
            break
        if sentence in chosen_texts or _is_duplicate(sentence, chosen_texts):
            continue
        chosen.append((position, sentence))
        chosen_texts.append(sentence)

    chosen.sort(key=lambda pair: pair[0])  # 영상에 나온 순서대로 돌려준다
    claims = [Claim(text=sentence) for _, sentence in chosen]
    _attach_timestamps(claims, segments or [])
    logger.info("검증 대상 주장 %d건 추출 (후보 %d건, 문장 %d개)",
                len(claims), len(candidates), len(sentences))
    return claims


_EXTRACT_SYSTEM = EXTRACT_SYSTEM


def extract_claims_llm(text: str, segments: list[dict] | None = None,
                       max_claims: int | None = None,
                       llm_provider=None) -> list[Claim]:
    """LLM으로 주장을 추출한다. 정상 빈 배열과 추출 실패를 구분한다.

    규칙 기반이 못 하던 세 가지를 여기서 처리한다 — 종결어미 목록에 없는 문체,
    대명사로 앞을 가리키는 주장(M-04의 문맥 보존), 같은 의미 주장의 병합.

    타임스탬프는 LLM에게 맡기지 않는다. 원문 그대로 받은 문장을 기존 매칭 로직에
    넘겨서 붙인다 — 지어낼 여지를 하나라도 줄이는 편이 낫다.
    """
    from . import llm

    if llm_provider is None or not text:
        return extract_claims(text, segments, max_claims)

    limit = max_claims if max_claims is not None else config.max_claims
    try:
        data = llm.complete_json(
            llm_provider,
            _EXTRACT_SYSTEM,
            json.dumps({"max_claims": limit, "transcript": text}, ensure_ascii=False),
            max_tokens=4096,
        )
    except llm.LLMUnavailable as e:
        raise ClaimExtractionError("LLM 주장 추출 응답을 사용할 수 없다") from e

    raw_items = data.get("claims")
    if not isinstance(raw_items, list):
        raise ClaimExtractionError("LLM 주장 추출 응답에 claims 배열이 없다")

    # 정상적인 빈 배열은 "주장 없음"이다. 오류 폴백과 구분한다.
    if not raw_items:
        return []

    # 원문에 실제로 있는 문장만 남긴다. 판정의 인용 검증과 같은 이유로,
    # 발언하지 않은 문장을 검증 대상에 올리면 그 자체가 허위 정보가 된다.
    haystack = _normalize_for_quote(text)
    claims: list[Claim] = []
    for index, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            raise ClaimExtractionError(f"LLM 주장 {index}번이 객체가 아니다")
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            raise ClaimExtractionError(f"LLM 주장 {index}번의 text가 문자열이 아니다")
        sentence = _clean_sentence(raw_text)
        if not sentence:
            raise ClaimExtractionError(f"LLM 주장 {index}번의 text가 비어 있다")
        if _normalize_for_quote(sentence) not in haystack:
            raise ClaimExtractionError(f"LLM 주장 {index}번이 발언 전문에 없다")
        if any(_is_duplicate(sentence, [c.text]) for c in claims):
            continue
        claim = Claim(text=sentence)
        repeat = item.get("repeat", 1)
        if type(repeat) is not int or repeat < 1:
            raise ClaimExtractionError(f"LLM 주장 {index}번의 repeat가 양의 정수가 아니다")
        raw_context = item.get("context", "")
        if not isinstance(raw_context, str):
            raise ClaimExtractionError(f"LLM 주장 {index}번의 context가 문자열이 아니다")
        context = raw_context.strip()
        if context and _normalize_for_quote(context) in haystack:
            claim.context = context
            claim.mentions.append({"context": context})
        elif context:
            raise ClaimExtractionError(f"LLM 주장 {index}번의 context가 발언 전문에 없다")
        if repeat > 1:
            claim.mentions.append({"repeat": repeat})
        claims.append(claim)
        if limit and limit > 0 and len(claims) >= limit:
            break

    if not claims:
        # 비어 있지 않은 배열이 전부 중복이었다면 응답 계약 위반이다. 정상적인 주장
        # 없음은 위의 명시적인 빈 배열 경로에서만 반환한다.
        raise ClaimExtractionError("LLM의 비어 있지 않은 claims 배열에 고유한 주장이 없다")

    _attach_timestamps(claims, segments or [])
    logger.info("LLM 주장 추출 %d건 (prompt=%s)", len(claims), PROMPT_VERSION)
    return claims


def select_extractor(llm_provider=None):
    """설정에 맞는 주장 추출 함수를 돌려준다.

    호출부가 분기하지 않도록 여기서 한 번만 고른다. llm으로 설정했는데 제공자가
    없거나 알 수 없는 모드면 실패를 명시한다. LLM 실패를 규칙의 빈 결과로 바꾸면
    시스템 오류가 정상적인 "주장 없음"으로 표시될 수 있기 때문이다.
    """
    mode = (config.claim_extractor or "rule").strip().lower()
    if mode == "llm":
        if llm_provider is None:
            raise ClaimExtractionError("주장 추출을 llm으로 설정했지만 제공자를 사용할 수 없다")
        return lambda text, segments, max_claims: extract_claims_llm(
            text, segments, max_claims, llm_provider)
    if mode == "rule":
        return extract_claims
    raise ClaimExtractionError(f"알 수 없는 주장 추출 방식: {mode}")


def _attach_timestamps(claims: list[Claim], segments: list[dict]) -> None:
    """주장이 영상 어디쯤에서 나온 말인지 붙인다. 사용자가 원문을 확인할 수 있게.

    주장 문장은 화자 표기를 걷어내고 공백을 정리한 상태라 자막 원문과 글자가
    그대로 맞지 않는다. 양쪽을 같은 방식으로 정규화한 뒤 비교하고, 그래도 안 맞으면
    핵심어가 가장 많이 겹치는 자막 줄을 고른다.
    """
    if not segments:
        return

    normalized = [(_clean_sentence(s.get("text", "")), s) for s in segments]

    for claim in claims:
        head = claim.text[:12]
        matched = next((s for text, s in normalized if head and head in text), None)
        precision = TIME_EXACT if matched is not None else None

        if matched is None:
            # 자막은 한 문장이 여러 줄에 걸쳐 끊기므로 글자 매칭이 실패할 수 있다.
            tokens = set(_key_tokens(claim.text)[:6])
            best, best_overlap = None, 0
            for text, segment in normalized:
                overlap = len(tokens & set(_key_tokens(text)[:6]))
                if overlap > best_overlap:
                    best, best_overlap = segment, overlap
            matched = best if best_overlap >= 2 else None
            if matched is not None:
                # 글자가 아니라 핵심어 겹침으로 고른 줄이라 몇 초 어긋날 수 있다.
                precision = TIME_APPROX

        if matched is not None:
            claim.start = matched.get("start")
            claim.end = matched.get("end")
            claim.time_precision = precision


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
    # 숫자 사이의 마침표는 남긴다. 사실 확인의 핵심이 수치인데 "6.0%"가 "6"과 "0%"로
    # 쪼개지면 검색어가 망가진다.
    cleaned = re.sub(r"(?<!\d)[.](?!\d)", " ", claim_text)
    cleaned = re.sub(r"[^\w가-힣%.\s]", " ", cleaned)
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


_FACT_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*(?:%p|%|퍼센트|프로))?")


def _factcheck_review_matches(claim_text: str, evidence: Evidence) -> bool:
    """ClaimReview가 바로 이 주장을 다루는지 보수적으로 확인한다.

    검색 API가 돌려준 rating은 검색 질의에 대한 답이 아니라 ``item.text``에 대한
    판정이다. 두 문장이 다르면 rating이 아무리 단정적이어도 현재 주장에 옮기지 않는다.
    의미 유사도 임계값을 새로 만들지 않고, 수치가 같고 한쪽의 핵심 표현이 다른 쪽에
    모두 포함되는 경우만 같은 대상으로 본다.
    """
    reviewed_claim = evidence.snippet
    if not isinstance(reviewed_claim, str) or not reviewed_claim.strip():
        return False

    claim_numbers = set(_FACT_NUMBER_RE.findall(claim_text))
    review_numbers = set(_FACT_NUMBER_RE.findall(reviewed_claim))
    if claim_numbers != review_numbers:
        return False

    claim_normalized = _normalize_for_quote(claim_text)
    review_normalized = _normalize_for_quote(reviewed_claim)
    if (len(claim_normalized) >= 8 and len(review_normalized) >= 8
            and (claim_normalized in review_normalized or review_normalized in claim_normalized)):
        return True

    claim_tokens = {token.lower() for token in _key_tokens(claim_text)[:8]}
    review_tokens = {token.lower() for token in _key_tokens(reviewed_claim)[:8]}
    if min(len(claim_tokens), len(review_tokens)) < 2:
        return False
    return claim_tokens <= review_tokens or review_tokens <= claim_tokens


def _is_relevant(claim_text: str, evidence: Evidence, context: str = "") -> bool:
    """찾아온 자료가 이 주장과 실제로 관련이 있는지 최소한으로 확인한다.

    검색 엔진은 질의어가 애매하면 아무 문서나 돌려준다. 실측에서 "물가 상승률"
    주장에 "주기율표"가 딸려 왔다. 무관한 자료를 근거라고 보여주는 것은 사실상
    거짓 근거이므로, 주장의 핵심어가 제목이나 인용문에 하나도 없으면 버린다.
    """
    body = evidence.content if evidence.content_scope == ORIGINAL else evidence.snippet
    haystack = f"{evidence.title} {body or ''}".lower()
    if not haystack.strip():
        return False
    # 전문 기관 판정도 검색 결과다. 실제로 판정한 문장이 현재 주장과 맞아야 한다.
    if evidence.rating:
        return _factcheck_review_matches(claim_text, evidence)
    for token in _key_tokens(f"{claim_text} {context}")[:8]:
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
    if name.startswith("naver_"):
        category = name[len("naver_"):]
        if category not in NaverSearchProvider.CATEGORIES:
            logger.warning("지원하지 않는 네이버 검색 카테고리: %s", category)
            return None
        if cfg.naver_client_id and cfg.naver_client_secret:
            return NaverSearchProvider(category, cfg.naver_client_id,
                                       cfg.naver_client_secret,
                                       cfg.evidence_timeout_sec, cfg.naver_base_url)
        logger.info("NAVER API HUB 자격 정보가 없어 %s 검색은 건너뜁니다", category)
        return None
    logger.warning("알 수 없는 근거 검색 제공자: %s", name)
    return None


def default_providers(cfg=None) -> list[EvidenceProvider]:
    """설정(`DEEPCHECK_EVIDENCE_PROVIDERS`)에 적힌 순서대로 검색 수단을 만든다.

    전문 기관 판정(factcheck)을 앞에 두어 관련 참고 자료를 우선 확보한다. 검색 API의
    rating 자체는 원문·시점 검증을 대신하지 않으므로 사용자 판정으로 바로 옮기지 않는다.

    설정을 인자로 받는 이유는 테스트에서 다른 조합을 넣어보기 위해서다. Config는
    frozen dataclass라 속성을 덮어쓸 수 없다.
    """
    cfg = cfg or config
    names = [n.strip() for n in cfg.evidence_providers.split(",") if n.strip()]
    providers = [p for p in (_build_provider(name, cfg) for name in names) if p is not None]
    if not providers:
        logger.warning("사용 가능한 근거 검색 제공자가 없다 — 모든 주장이 근거 부족으로 남는다")
    return providers


# --- LLM 판정 ---------------------------------------------------------------
#
# 검색 결과와 ClaimReview rating은 참고 자료다. LLM이 관계를 제안하더라도 서버가
# 원문 provenance·출처 충분성·인용을 검증한 경우에만 양성 판정을 허용한다.

_VERDICT_SYSTEM = VERDICT_SYSTEM

_LLM_VERDICT_MAP = {"일치": SUPPORTED, "불일치": REFUTED, "부족": UNVERIFIED}
_LLM_INSUFFICIENT = {NO_SOURCE, NOT_DIRECT, TIME_MISMATCH, SOURCE_CONFLICT,
                     WEAK_SOURCE, PARTIAL}

# 인용 대조는 공백·따옴표 차이를 무시한다. 모델이 줄바꿈이나 인용부호를 바꿔서
# 옮기는 일이 흔한데, 그건 지어낸 것과 다르다.
_NORMALIZE_RE = re.compile(r"[\s\"\'“”‘’·,]+")


def _normalize_for_quote(text: str) -> str:
    return _NORMALIZE_RE.sub("", text or "").lower()


def _quote_found_in(quote: str, item: Evidence) -> bool:
    """인용한 문장이 바로 그 근거 안에 있는지 대조한다.

    이게 환각을 막는 장치다. LLM이 근거에 없는 말을 지어내면 여기서 걸러낸다.

    "아무 근거에나 있으면 통과"가 아니라 **지목한 근거 안에** 있어야 한다.
    느슨하게 보면 A 자료의 문장을 B 자료의 근거인 것처럼 붙여도 통과해버리는데,
    화면은 근거 카드마다 이유를 따로 보여주므로 그건 틀린 정보가 된다.
    """
    needle = _normalize_for_quote(quote)
    # 너무 짧은 인용은 우연히 일치할 수 있어 근거로 인정하지 않는다.
    if len(needle) < 8:
        return False
    haystack = _normalize_for_quote(_evidence_content(item))
    return needle in haystack


def _evidence_content(item: Evidence) -> str:
    """모델에 보낸 범위와 서버 인용 검증 범위를 일치시킨다."""
    if item.content_scope == ORIGINAL and isinstance(item.content, str):
        return item.content
    # 검색 결과의 제목과 요약만 보낸다는 사실은 content_scope로 함께 전달한다.
    return f"{item.title}\n{(item.snippet or '')[:800]}"


def _decision_evidence_is_sufficient(cited: list[Evidence]) -> bool:
    """Accepted 근거 정책의 양성 판정 최소 조건을 서버에서 강제한다.

    모든 인용은 신뢰 경계 안에서 원문·출처 대응이 확인돼야 한다. 그중 검증된 1차
    출처가 하나라도 있거나, 1차 출처가 없으면 서로 다른 원문 계보 두 개 이상이어야
    한다. source_type·publisher 문자열이나 LLM 답만으로 검증 상태를 만들지 않는다.
    """
    if not cited:
        return False
    if any(
        item.content_scope != ORIGINAL
        or item.provenance_verified is not True
        or not isinstance(item.content, str)
        or not item.content.strip()
        or not _is_safe_source_url(item.url)
        for item in cited
    ):
        return False
    if any(item.is_primary is True for item in cited):
        return True
    independent_groups = {
        item.independence_group for item in cited if item.independence_group is not None
    }
    return len(independent_groups) >= 2


def _clear_citations(evidence: list[Evidence]) -> None:
    for item in evidence:
        item.cited = False
        item.quote = None
        item.cite_reason = None


def _apply_citations(raw: object, evidence: list[Evidence]) -> list[Evidence]:
    """LLM 인용을 전부 검증한다. 하나라도 잘못되면 전체를 무효화한다."""
    if not isinstance(raw, list) or not raw:
        return []
    cited: list[Evidence] = []
    seen_indexes: set[int] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            _clear_citations(evidence)
            return []
        index = entry.get("index")
        if type(index) is not int or not 1 <= index <= len(evidence):
            logger.warning("LLM이 없는 근거 번호를 지목: %r", index)
            _clear_citations(evidence)
            return []
        if index in seen_indexes:
            logger.warning("LLM이 같은 근거 번호를 중복 인용: %d", index)
            _clear_citations(evidence)
            return []
        seen_indexes.add(index)
        item = evidence[index - 1]
        quote = entry.get("quote")
        reason = entry.get("reason")
        if not isinstance(quote, str) or not isinstance(reason, str) or not reason.strip():
            _clear_citations(evidence)
            return []
        quote = quote.strip()
        if not quote or not _quote_found_in(quote, item):
            logger.warning("인용 검증 실패 — 근거 %d에 없는 문장", index)
            _clear_citations(evidence)
            return []
        item.cited = True
        item.quote = quote or None
        item.cite_reason = reason.strip()
        cited.append(item)
    return cited


def _llm_verdict(claim: Claim, evidence: list[Evidence], provider) -> dict | None:
    """LLM에게 판정을 묻는다. 못 쓰거나 신뢰할 수 없으면 None을 돌려 폴백시킨다."""
    from . import llm  # 순환 import 방지를 위해 호출 시점에 가져온다.

    _clear_citations(evidence)
    user = json.dumps({
        "claim": claim.text,
        "context": claim.context,
        "video_title": claim.video_title,
        "video_published_at": claim.video_published_at,
        "evidence": [{
            "index": i, "title": item.title, "url": item.url,
            "publisher": item.publisher, "source_type": item.source_type,
            "published_at": item.published_at,
            "content_scope": item.content_scope,
            "provenance_verified": item.provenance_verified is True,
            "independence_group": item.independence_group,
            "rating": item.rating,
            "content": _evidence_content(item),
        } for i, item in enumerate(evidence, 1)],
    }, ensure_ascii=False)

    try:
        data = llm.complete_json(provider, _VERDICT_SYSTEM, user, max_tokens=2048)
    except llm.LLMUnavailable as e:
        logger.warning("LLM 판정 실패 — 규칙 기반으로 폴백: %s", e)
        return None

    verdict = _LLM_VERDICT_MAP.get(str(data.get("verdict", "")).strip())
    if not verdict:
        logger.warning("LLM이 허용 목록 밖의 판정을 반환")
        return None

    raw_reason = data.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    raw_cited = data.get("cited")
    # 부족 판정에서 모델이 인용을 잘못 붙여도 참고 자료만으로 남긴다.
    cited = (_apply_citations(raw_cited, evidence)
             if verdict in (SUPPORTED, REFUTED) else [])

    # 일치·불일치는 항상 인용을 검증한다. 디버그 설정으로 이 안전장치를 끌 수 없다.
    # 검색 발췌나 출처 검증이 안 된 자료, 1차/독립 출처 조건을 만족하지 못한 자료도
    # 양성 판정 근거로 인정하지 않는다.
    if verdict in (SUPPORTED, REFUTED) and (
        not reason or not cited or not _decision_evidence_is_sufficient(cited)
    ):
        logger.warning("검증된 원문 근거가 부족해 판정 %s를 근거 부족으로 강등: %s",
                       verdict, claim.text[:40])
        _clear_citations(evidence)
        return {
            "verdict": UNVERIFIED,
            "reason": "원문과 출처가 확인된 충분한 근거로 판정을 뒷받침하지 못해 판단을 유보한다.",
            "quote": None,
            "insufficient_reason": WEAK_SOURCE,
        }

    insufficient = None
    if verdict == UNVERIFIED:
        raw_value = data.get("insufficient_reason")
        raw = raw_value.strip() if isinstance(raw_value, str) else ""
        insufficient = raw if raw in _LLM_INSUFFICIENT else NOT_DIRECT

    return {
        "verdict": verdict,
        "reason": reason or VERDICT_LABELS[verdict],
        # 카드 요약에 쓸 대표 인용. 자세한 것은 근거별 quote에 있다.
        "quote": cited[0].quote if cited else None,
        "insufficient_reason": insufficient,
    }


def _set_verdict(claim: Claim, verdict: str, reason: str,
                 insufficient: str | None = None, quote: str | None = None) -> None:
    """판정과 표시 문구를 한 곳에서 채운다. 라벨이 판정과 어긋나는 걸 막는다."""
    claim.verdict = verdict
    claim.verdict_label = VERDICT_LABELS.get(verdict, "")
    claim.reason = reason
    claim.quote = quote
    claim.insufficient_reason = insufficient if verdict == UNVERIFIED else None
    claim.insufficient_label = (
        INSUFFICIENT_LABELS.get(claim.insufficient_reason)
        if claim.insufficient_reason else None
    )


def verify_one_claim(claim: Claim, providers: list[EvidenceProvider],
                     evidence_per_claim: int | None = None,
                     llm_provider=None) -> Claim:
    """주장 하나에 근거를 모으고 판정한다. 카드를 하나씩 갱신해야 하는 파이프라인이
    직접 부르는 단위. `claim.status`를 VERIFYING → DONE으로 바꾸고 반환한다.

    전문 기관의 rating도 원문 확인 없이 그대로 옮기지 않는다. LLM 판정은 검증된
    원문 인용과 Accepted 근거 충분성 조건을 모두 통과해야 인정한다. 판정 수단이나
    충분한 근거가 없으면 관련 자료만 참고로 붙이고 판단을 유보한다.
    """
    per_claim = evidence_per_claim or config.evidence_per_claim
    claim.status = VERIFYING

    query = _search_query(f"{claim.text} {claim.context}")
    collected: list[Evidence] = []
    provider_failures: list[str] = []
    for provider in providers:
        provider_name = getattr(provider, "name", "?")
        try:
            found = provider.search(query, per_claim)
            if not isinstance(found, list) or any(
                    not isinstance(item, Evidence) for item in found):
                raise EvidenceSearchError("근거 제공자가 잘못된 결과 형식을 반환했다")
            collected.extend(found)
        except Exception as e:
            # 근거 검색 실패가 분석 전체를 멈추게 하지 않는다.
            provider_failures.append(provider_name)
            logger.warning("근거 검색 실패(%s): %s", provider_name, type(e).__name__)

    if not providers:
        provider_failures.append("configured-provider")

    def note_partial_search() -> None:
        if provider_failures:
            suffix = "일부 근거 검색 제공자 조회에 실패해 확인 범위가 제한됐다."
            claim.reason = f"{claim.reason} {suffix}".strip()

    # 검색 결과 중 이 주장과 실제로 관련 있는 것만 근거로 삼는다.
    relevant = [e for e in collected if _is_relevant(claim.text, e, claim.context)]
    dropped = len(collected) - len(relevant)
    if dropped:
        logger.info("무관한 검색 결과 %d건 제외 (남은 근거 %d건)", dropped, len(relevant))
    # 판정에 보낸 자료를 응답에서 자르면 인용이나 충돌 출처가 사라진다.
    claim.evidence = relevant

    # 검색 API의 구조화된 ClaimReview도 원문 자체가 아니며 첫 rating을 그대로 옮기지
    # 않는다. 다만 같은 주장에 대한 명시적 판정들이 서로 충돌하면 그 사실만은 서버가
    # 보수적으로 확정할 수 있으므로 즉시 근거 부족으로 처리한다.
    rated_verdicts = {
        decided[0]
        for evidence in relevant
        if (decided := _verdict_from_rating(evidence.rating)) is not None
    }
    if len(rated_verdicts) > 1:
        _set_verdict(
            claim,
            UNVERIFIED,
            "같은 주장에 대한 전문 기관 판정들이 서로 충돌해 판단을 유보한다.",
            insufficient=SOURCE_CONFLICT,
        )
        note_partial_search()
        claim.status = DONE
        logger.info("주장 판정(전문기관 충돌): %s", claim.text[:40])
        return claim

    # 근거가 아예 없으면 LLM을 부를 이유가 없다. 못 찾았다고 거짓도 아니다.
    if not relevant:
        if provider_failures:
            _set_verdict(
                claim,
                UNVERIFIED,
                "근거 검색 제공자 조회가 실패해 관련 자료 유무를 확인할 수 없다.",
                insufficient=WEAK_SOURCE,
            )
            claim.status = FAILED
            logger.warning("주장 판정(근거 검색 실패): %s", claim.text[:40])
            return claim
        _set_verdict(claim, UNVERIFIED,
                     "관련 근거를 찾지 못해 판단을 유보한다. 거짓이라는 뜻은 아니다.",
                     insufficient=NO_SOURCE)
        claim.status = DONE
        logger.info("주장 판정(근거 없음): %s", claim.text[:40])
        return claim

    # 2순위 — LLM에게 근거를 주고 관계를 묻는다.
    if llm_provider is not None and config.llm_verdict:
        result = _llm_verdict(claim, relevant, llm_provider)
        if result:
            _set_verdict(claim, result["verdict"], result["reason"],
                         insufficient=result.get("insufficient_reason"),
                         quote=result.get("quote"))
            note_partial_search()
            claim.status = DONE
            logger.info("주장 판정(LLM): %s — %s (근거 %d건)",
                        claim.verdict, claim.text[:40], len(claim.evidence))
            return claim

    # 3순위 — 근거는 모았지만 판정할 수단이 없다. 있는 척하지 않는다.
    _set_verdict(claim, UNVERIFIED,
                 "관련 자료는 찾았지만 이 주장을 직접 검증한 판정이 없어 판단을 유보한다.",
                 insufficient=NOT_DIRECT)
    note_partial_search()
    claim.status = DONE
    logger.info("주장 판정(판정 수단 없음): %s (근거 %d건)",
                claim.text[:40], len(claim.evidence))
    return claim


def verify_claims(claims: list[Claim], providers: list[EvidenceProvider] | None = None,
                  evidence_per_claim: int | None = None,
                  time_budget_sec: float | None = None) -> list[Claim]:
    """`verify_one_claim`을 순서대로 돌리는 일괄 처리 도우미.

    CLI처럼 중간 갱신이 필요 없는 호출자를 위한 것이다. 폴링으로 카드를 하나씩
    갱신해야 하는 API 경로는 `verify_one_claim`을 직접 부른다(`pipeline.py` 참고).

    외부 검색은 느려질 수 있으므로(제공자 하나가 타임아웃까지 버티면 주장마다 수십
    초가 든다) 전체 시간 예산을 두고, 예산을 넘기면 남은 주장은 검색 없이 유보한다.
    """
    if not claims:
        return []
    active = providers if providers is not None else default_providers()
    budget = time_budget_sec if time_budget_sec is not None else config.evidence_budget_sec
    started = time.monotonic()

    for claim in claims:
        if budget and time.monotonic() - started > budget:
            _set_verdict(
                claim,
                UNVERIFIED,
                "근거 검색 시간이 예산을 넘어 이 주장은 확인하지 못했다.",
                insufficient=TIMEOUT,
            )
            claim.status = TIMED_OUT
            logger.warning("근거 검색 시간 예산(%.0fs) 초과 — 남은 주장은 검색을 건너뛴다", budget)
            continue
        verify_one_claim(claim, active, evidence_per_claim)

    return claims
