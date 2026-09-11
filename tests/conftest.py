"""Regression tests must never reach the server, Internet or paid providers."""
import socket

import pytest


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
