"""Shared test setup. The Coinbase tests (tests/test_coinbase_*.py) never reach the internet: any connection or name
lookup for a host other than this machine fails the test, so none of them can touch api.coinbase.com."""

from __future__ import annotations

import socket

import pytest

_LOCAL = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _coinbase_tests_stay_local(request, monkeypatch):
    if not request.module.__name__.split(".")[-1].startswith("test_coinbase_"):
        yield
        return
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOCAL:
            raise AssertionError(f"a Coinbase test tried to connect to {host}: tests stay on 127.0.0.1")
        return real_connect(self, address)

    def getaddrinfo(host, *a, **kw):
        if host not in _LOCAL and host is not None:
            raise AssertionError(f"a Coinbase test tried to look up {host}: tests stay on 127.0.0.1")
        return real_getaddrinfo(host, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
