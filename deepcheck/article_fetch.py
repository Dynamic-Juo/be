"""근거 URL의 실제 원문을 안전하게 가져와 본문을 추출한다.

검색 제공자(네이버·위키백과·FactCheck)는 제목과 발췌문만 준다. evidence-policy.md
(Accepted)는 일치·불일치 판정에 `content_scope=original` + `provenance_verified=true`인
근거를 요구하는데, 지금까지는 이걸 채울 방법이 없어서 모든 판정이 근거 부족으로
강등됐다(2026-09-16 실사용 — 백일섭 오보 영상에서 핵심 주장에 근거 6건을 확보하고도
근거 부족으로 끝난 것으로 확인). 이 모듈이 그 공백을 채운다.

신뢰는 두 겹으로 건다.
1. SSRF 방어(이 모듈) — 사설망·과도한 리다이렉트·비정상 응답을 차단한다. "안전하게
   가져왔다"는 뜻이지 "믿을 만한 자료"라는 뜻은 아니다.
2. 신뢰 도메인 확인(`is_trusted_domain`, 호출부인 claims.py가 사용) — 실제 도착한
   도메인이 주요 언론사여야만 provenance_verified=true를 준다. 출처 불명 사이트는
   본문을 확보했어도 원문으로 인정하지 않는다.

둘 다 통과 못 하면 조용히 None을 돌려준다. 호출부는 기존처럼 search_excerpt로
남겨서 근거 부족 처리하므로, 이 모듈이 무엇을 하지 못해도 기존 동작보다 나빠지지
않는다 — "분석 못 함"과 "확인했다"를 구분하는 이 저장소의 원칙과 같다.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import config

logger = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

_USER_AGENT = "ConanAI/0.2 (hackathon fact-check; https://github.com/Dynamic-Juo)"
# 이보다 짧으면 봇 차단·로그인 요구·페이월 안내 페이지일 가능성이 높다. 실제 기사
# 본문이면 200자는 보통 한두 문장을 넘는다.
_MIN_TEXT_CHARS = 200
_MAX_REDIRECTS = 3


@dataclass
class FetchedArticle:
    text: str
    final_url: str
    domain: str


def _is_public_host(hostname: str) -> bool:
    """이 호스트명이 가리키는 모든 IP가 공인망인지 확인한다.

    사설·루프백·링크로컬·예약 대역으로 가면 SSRF다. DNS가 여러 IP를 주면 전부
    확인한다 — 하나만 공인이고 나머지가 사설이면 라운드로빈으로 사설망에 걸릴 수
    있다.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _normalize_domain(hostname: str) -> str:
    return hostname[4:] if hostname.startswith("www.") else hostname


def is_trusted_domain(hostname: str | None) -> bool:
    """실제 도착한 도메인이 신뢰 언론사 목록에 있는지 본다.

    `DEEPCHECK_TRUSTED_NEWS_DOMAINS`(쉼표 구분)를 그대로 쓴다. 서브도메인도
    포함(news.kbs.co.kr는 kbs.co.kr 하위로 인정)하지만, 아무 문자열 포함이 아니라
    정확한 도메인 경계로 비교한다.
    """
    if not hostname:
        return False
    domain = _normalize_domain(hostname).lower()
    for raw in config.trusted_news_domains.split(","):
        trusted = raw.strip().lower()
        if trusted and (domain == trusted or domain.endswith(f".{trusted}")):
            return True
    return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """리다이렉트마다 목적지를 다시 검증한다.

    기본 처리기는 첫 요청의 호스트만 우리가 확인하고 나면 리다이렉트 자체는
    그대로 따라간다. 신뢰한 URL이 리다이렉트 한 번으로 사설망을 가리킬 수 있어서,
    매 홉마다 scheme·공인망 여부를 다시 검사한다.
    """

    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme.lower() != "https":
            raise urllib.error.URLError(f"안전하지 않은 리다이렉트 스킴: {parsed.scheme}")
        if not parsed.hostname or not _is_public_host(parsed.hostname):
            raise urllib.error.URLError("리다이렉트 대상이 공인망이 아니다")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_article_text(url: str) -> FetchedArticle | None:
    """근거 URL을 열어 기사 본문을 확보한다. 실패하면 조용히 None을 돌려준다.

    반환된 `domain`이 신뢰 목록에 있는지는 이 함수가 정하지 않는다 — SSRF 방어와
    신뢰 판단은 분리해서, "안전하게 열 수 있었다"와 "믿을 만하다"를 섞지 않는다.
    """
    if trafilatura is None or not config.evidence_fetch_original:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if not _is_public_host(parsed.hostname):
        logger.info("원문 요청 차단(공인망 아님): %s", parsed.hostname)
        return None

    opener = urllib.request.build_opener(_SafeRedirectHandler)
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with opener.open(request, timeout=config.evidence_fetch_timeout_sec) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return None
            body = resp.read(config.evidence_fetch_max_bytes + 1)
            if len(body) > config.evidence_fetch_max_bytes:
                logger.info("원문 응답이 허용 크기를 초과했다: %s", url)
                return None
            final_url = resp.geturl()
            charset = resp.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.info("원문 확보 실패(%s): %s", url, type(e).__name__)
        return None

    final_host = urllib.parse.urlsplit(final_url).hostname
    if not final_host or not _is_public_host(final_host):
        return None

    try:
        html = body.decode(charset, errors="replace")
        text = trafilatura.extract(
            html, url=final_url, include_comments=False, include_tables=False,
            favor_precision=True,
        )
    except Exception as e:
        logger.warning("원문 본문 추출 실패(%s): %s", url, type(e).__name__)
        return None

    if not text or len(text.strip()) < _MIN_TEXT_CHARS:
        return None

    return FetchedArticle(
        text=text.strip(), final_url=final_url, domain=_normalize_domain(final_host)
    )
