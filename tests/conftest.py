"""Shared test setup. The Coinbase tests (tests/test_coinbase_*.py) never reach the internet: any connection or name
lookup for a host other than this machine fails the test, so none of them can touch api.coinbase.com.

LIVE is locked in code (desk/coinbase_live.py: LIVE_REVIEWED = False). The tests that exercise LIVE against the local
fake Coinbase run it as it would run once a review comes back clean: the lock open, for that test only. A test marked
live_locked sees the lock as the code has it (tests/test_coinbase_live_lock.py), and DESK_TEST_LIVE_LOCK=code runs the
whole suite that way: every LIVE test then refuses at the lock, and SHADOW's are unchanged."""

from __future__ import annotations

import os
import socket

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live_locked: the test sees LIVE locked, as desk/coinbase_live.py has it")


@pytest.fixture(autouse=True)
def _live_lock_open_for_the_live_tests(request):
    """(Set and put back here, not with the test's own monkeypatch: a test's monkeypatch.undo() leaves it open.)"""
    if request.node.get_closest_marker("live_locked") or os.environ.get("DESK_TEST_LIVE_LOCK") == "code":
        yield
        return
    from desk import coinbase_live

    was = coinbase_live.LIVE_REVIEWED
    coinbase_live.LIVE_REVIEWED = True
    try:
        yield
    finally:
        coinbase_live.LIVE_REVIEWED = was


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
