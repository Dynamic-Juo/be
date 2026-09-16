"""Regression tests must never reach the server, Internet or paid providers."""
import socket

import pytest

from deepcheck import article_fetch


@pytest.fixture(autouse=True)
def stub_article_fetch(monkeypatch):
    """근거 원문 확보(`article_fetch.fetch_article_text`)는 기본적으로 "못 찾음"으로 둔다.

    이 함수는 실제 네트워크 요청을 시도하므로 block_network 픽스처에 바로 걸린다.
    대부분의 테스트는 원문 확보 자체를 검증하려는 게 아니라 검증 로직을 보는
    거라 기본값을 안전하게 둔다. 이 동작 자체를 검증하는 테스트는 각자
    `monkeypatch.setattr(article_fetch, "fetch_article_text", ...)`로 다시 덮어쓴다.
    """
    monkeypatch.setattr(article_fetch, "fetch_article_text", lambda url: None)


@pytest.fixture(autouse=True, scope="session")
def block_network():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def deny_lookup(*args, **kwargs):
        raise AssertionError("Network/DNS access is forbidden in unit tests; use a fake")

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            deny_lookup()
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            deny_lookup()
        return original_connect_ex(sock, address)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "getaddrinfo", deny_lookup)
        patch.setattr(socket, "create_connection", deny_lookup)
        patch.setattr(socket.socket, "connect", connect)
        patch.setattr(socket.socket, "connect_ex", connect_ex)
        yield
