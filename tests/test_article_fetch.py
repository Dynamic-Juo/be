"""근거 원문 확보(article_fetch) 테스트.

실제 네트워크는 타지 않는다(conftest의 block_network가 어차피 막는다). DNS 조회와
HTTP 응답을 가짜로 주입해서 SSRF 방어와 신뢰 도메인 판단, 본문 추출 배선만 본다.
"""

import socket
import urllib.error
from dataclasses import replace
from email.message import Message

import pytest

from deepcheck import article_fetch
from deepcheck.config import config

# conftest.py의 autouse 픽스처가 article_fetch.fetch_article_text를 기본적으로
# "못 찾음"으로 막아 둔다(다른 테스트가 실수로 네트워크를 타지 않도록). 이 파일은
# 그 함수 자체를 검증하는 파일이라 진짜 구현을 다시 꽂아 넣어야 한다. 모듈이
# import되는 시점(어떤 테스트의 monkeypatch보다 먼저)에 원본을 붙잡아 둔다.
_REAL_FETCH_ARTICLE_TEXT = article_fetch.fetch_article_text


@pytest.fixture(autouse=True)
def use_real_fetch_article_text(monkeypatch):
    monkeypatch.setattr(article_fetch, "fetch_article_text", _REAL_FETCH_ARTICLE_TEXT)


def _addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


class TestIsPublicHost:
    def test_공인_ip는_통과한다(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: _addrinfo("8.8.8.8"))
        assert article_fetch._is_public_host("example.com") is True

    @pytest.mark.parametrize("ip", [
        "127.0.0.1",       # loopback
        "10.0.0.5",        # private
        "192.168.1.1",     # private
        "169.254.1.1",     # link-local
        "::1",             # IPv6 loopback
        "fc00::1",         # IPv6 unique-local
    ])
    def test_사설망은_차단한다(self, monkeypatch, ip):
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: _addrinfo(ip))
        assert article_fetch._is_public_host("internal.example") is False

    def test_여러_ip_중_하나라도_사설망이면_차단한다(self, monkeypatch):
        # DNS 라운드로빈으로 공인·사설 IP가 섞여 나오면 사설망 쪽으로 갈 수 있다.
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda host, port: _addrinfo("8.8.8.8", "10.0.0.1"))
        assert article_fetch._is_public_host("mixed.example") is False

    def test_dns_실패는_차단한다(self, monkeypatch):
        def raise_gaierror(host, port):
            raise socket.gaierror("no such host")
        monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
        assert article_fetch._is_public_host("nonexistent.invalid") is False


class TestTrustedDomain:
    def test_정확히_일치하면_신뢰한다(self):
        assert article_fetch.is_trusted_domain("yna.co.kr") is True

    def test_하위_도메인도_신뢰한다(self):
        assert article_fetch.is_trusted_domain("news.kbs.co.kr") is True

    def test_www는_제거하고_비교한다(self):
        assert article_fetch.is_trusted_domain("www.yna.co.kr") is True

    def test_무관한_도메인은_신뢰하지_않는다(self):
        assert article_fetch.is_trusted_domain("정체불명-블로그.example") is False

    def test_신뢰_도메인의_접미사만_같은_다른_도메인은_거른다(self):
        # "notyna.co.kr"이 "yna.co.kr"으로 끝나지 않는지 — 문자열 포함이 아니라
        # 도메인 경계(.)로 비교해야 한다.
        assert article_fetch.is_trusted_domain("notyna.co.kr") is False

    def test_none은_신뢰하지_않는다(self):
        assert article_fetch.is_trusted_domain(None) is False


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str, final_url: str):
        self._body = body
        self._headers = Message()
        self._headers["Content-Type"] = content_type
        self.final_url = final_url

    @property
    def headers(self):
        return self._headers

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def geturl(self):
        return self.final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


ARTICLE_HTML = (
    "<html><body><article><p>" + ("백일섭 배우는 최근 방송에 출연해 근황을 전했다. " * 15)
    + "</p></article></body></html>"
)


class TestFetchArticleText:
    def _wire_ok(self, monkeypatch, url="https://yna.co.kr/article/1",
                final_url=None, body=None, content_type="text/html; charset=utf-8"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: _addrinfo("8.8.8.8"))
        opener = _FakeOpener(_FakeResponse(
            (body or ARTICLE_HTML).encode("utf-8"), content_type, final_url or url,
        ))
        monkeypatch.setattr(article_fetch.urllib.request, "build_opener", lambda *h: opener)
        return opener

    def test_기능이_꺼져있으면_시도하지_않는다(self, monkeypatch):
        monkeypatch.setattr(article_fetch, "config", replace(config, evidence_fetch_original=False))

        def forbidden(host, port):
            pytest.fail("evidence_fetch_original=False면 DNS도 조회하면 안 된다")
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None

    def test_trafilatura가_없으면_시도하지_않는다(self, monkeypatch):
        monkeypatch.setattr(article_fetch, "trafilatura", None)

        def forbidden(host, port):
            pytest.fail("trafilatura 미설치면 DNS도 조회하면 안 된다")
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None

    def test_https가_아니면_거절한다(self, monkeypatch):
        def forbidden(host, port):
            pytest.fail("스킴 검사가 DNS 조회보다 먼저 실행돼야 한다")
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        assert article_fetch.fetch_article_text("http://yna.co.kr/x") is None

    def test_사설망_호스트는_연결하지_않는다(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: _addrinfo("127.0.0.1"))
        opener = _FakeOpener()
        monkeypatch.setattr(article_fetch.urllib.request, "build_opener", lambda *h: opener)
        assert article_fetch.fetch_article_text("https://internal.example/x") is None
        assert opener.requests == []

    def test_정상_기사는_본문을_추출해_돌려준다(self, monkeypatch):
        self._wire_ok(monkeypatch)
        result = article_fetch.fetch_article_text("https://yna.co.kr/article/1")
        assert result is not None
        assert "백일섭" in result.text
        assert result.domain == "yna.co.kr"

    def test_www는_도메인에서_제거된다(self, monkeypatch):
        self._wire_ok(monkeypatch, url="https://www.yna.co.kr/article/1",
                     final_url="https://www.yna.co.kr/article/1")
        result = article_fetch.fetch_article_text("https://www.yna.co.kr/article/1")
        assert result.domain == "yna.co.kr"

    def test_리다이렉트_후_도메인도_확인한다(self, monkeypatch):
        # geturl()이 최종 도착지를 알려주면 그 호스트도 공인망인지 다시 본다.
        self._wire_ok(monkeypatch, url="https://yna.co.kr/short/1",
                     final_url="https://news.yna.co.kr/article/1")
        result = article_fetch.fetch_article_text("https://yna.co.kr/short/1")
        assert result.domain == "news.yna.co.kr"

    def test_html이_아니면_버린다(self, monkeypatch):
        self._wire_ok(monkeypatch, content_type="application/pdf")
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None

    def test_응답이_너무_크면_버린다(self, monkeypatch):
        monkeypatch.setattr(article_fetch, "config",
                            replace(config, evidence_fetch_max_bytes=100))
        self._wire_ok(monkeypatch, body=ARTICLE_HTML)
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None

    def test_본문이_너무_짧으면_버린다(self, monkeypatch):
        self._wire_ok(monkeypatch, body="<html><body><p>속보</p></body></html>")
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None

    def test_연결_오류는_조용히_none을_돌려준다(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: _addrinfo("8.8.8.8"))
        opener = _FakeOpener(error=urllib.error.URLError("connection refused"))
        monkeypatch.setattr(article_fetch.urllib.request, "build_opener", lambda *h: opener)
        assert article_fetch.fetch_article_text("https://yna.co.kr/x") is None


class TestSafeRedirectHandler:
    def test_https가_아닌_리다이렉트는_거절한다(self, monkeypatch):
        handler = article_fetch._SafeRedirectHandler()
        with pytest.raises(urllib.error.URLError):
            handler.redirect_request(None, None, 302, "", {}, "http://yna.co.kr/x")

    def test_사설망으로_가는_리다이렉트는_거절한다(self, monkeypatch):
        monkeypatch.setattr(article_fetch, "_is_public_host", lambda host: False)
        handler = article_fetch._SafeRedirectHandler()
        with pytest.raises(urllib.error.URLError):
            handler.redirect_request(None, None, 302, "", {}, "https://internal.example/x")

    def test_공인망_https_리다이렉트는_허용한다(self, monkeypatch):
        monkeypatch.setattr(article_fetch, "_is_public_host", lambda host: True)
        calls = []

        def fake_super(self, req, fp, code, msg, headers, newurl):
            calls.append(newurl)
            return "ok"

        monkeypatch.setattr(
            article_fetch.urllib.request.HTTPRedirectHandler, "redirect_request", fake_super
        )
        handler = article_fetch._SafeRedirectHandler()
        result = handler.redirect_request(None, None, 302, "", {}, "https://yna.co.kr/x")
        assert result == "ok"
        assert calls == ["https://yna.co.kr/x"]
