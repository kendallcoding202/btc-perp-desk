"""CoinbaseREST (desk/brokers/coinbase_rest.py) against the local fake Coinbase (tests/fake_coinbase.py): signing, the
read allowlist, the order lock, error mapping, retries, the clock. Keys are generated fresh per test; nothing leaves
127.0.0.1 (tests/conftest.py)."""

from __future__ import annotations

import socket

import pytest

from desk import coinbase_live
from desk.brokers import coinbase_rest as cr
from desk.brokers.coinbase_auth import load_cdp_key
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, good_proof, make_ec_key, make_ed_key


@pytest.fixture
def fake():
    with FakeCoinbase() as f:
        yield f


class Sleeps(list):
    def __init__(self, fake):
        super().__init__()
        self.fake = fake

    def __call__(self, s):
        self.append(s)
        self.fake.sleep(s)


def client(fake, fk=None, register=True, **kw):
    fk = fk or make_ec_key()
    if register:
        fake.add_key(fk)
    kw.setdefault("clock", fake.clock)
    kw.setdefault("sleep", fake.sleep)
    return cr.CoinbaseREST(load_cdp_key(fk.file_json()), base_url=fake.base_url, min_gap_s=0, **kw)


def armed(fake, fk):
    fake.add_key(fk)
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), env=ARM_ENV, max_notional=1_000.0,
                            gate=[("backtest: passes", True, "ok")], shadow_proof=good_proof(fake.clock_ms()),
                            base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep, out=lambda *a: None)
    assert got is not None
    return got


# ---------------------------------------------------------------- signing and reads

@pytest.mark.parametrize("make", [make_ec_key, lambda: make_ed_key(form="raw64")])
def test_a_signed_read_is_accepted_by_a_server_that_verifies_the_jwt(fake, make):
    c = client(fake, make())
    out = c.get(f"{V3}/cfm/balance_summary")
    assert "balance_summary" in out
    r = fake.requests[-1]
    assert (r["method"], r["path"], r["status"]) == ("GET", f"{V3}/cfm/balance_summary", 200)


def test_an_unknown_key_gets_coinbases_bare_401_mapped_to_the_owners_hint_and_isnt_retried(fake):
    c = client(fake, register=False)
    with pytest.raises(cr.CoinbaseAuthError) as e:
        c.get(f"{V3}/key_permissions")
    text = str(e.value)
    assert "401 Unauthorized" in text and "gives no reason" in text and "isn't ECDSA" in text and "clock" in text
    assert len(fake.requests) == 1 and fake.requests[0]["status"] == 401


def test_a_key_without_the_permission_gets_403_missing_scopes(fake):
    c = client(fake, make_ec_key(view=False))
    with pytest.raises(cr.CoinbasePermissionError, match="lacks the permission"):
        c.get(f"{V3}/cfm/positions")


def test_reads_off_the_allowlist_are_refused_before_anything_is_sent(fake):
    c = client(fake)
    for path in (f"{V3}/accounts/x", f"{V3}/payment_methods", f"{V3}/orders/historical/../../v2/user",
                 f"{V3}/cfm/positions/../sweeps", "/v2/user", f"{V3}/key_permissions?x=1", f"{V3}/orders%2fedit"):
        with pytest.raises(cr.PathNotAllowed):
            c.get(path)
    assert fake.requests == []


def test_base_url_is_coinbase_or_the_local_fake_only():
    k = load_cdp_key(make_ec_key().file_json())
    for bad in ("http://api.coinbase.com", "https://api.coinbase.com.evil.example", "http://10.0.0.1:80",
                "https://api-sandbox.coinbase.com", "http://127.0.0.1"):
        with pytest.raises(ValueError):
            cr.CoinbaseREST(k, base_url=bad)
    cr.CoinbaseREST(k)                                  # the default: https://api.coinbase.com (nothing sent)


def test_repeated_query_keys_and_cursor_paging(fake):
    c = client(fake)
    fake.page_size = 2
    for k in range(5):
        fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": f"c{k}"}, "stop",
                    {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "80000", "limit_price": "76000",
                                                   "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}, "QUEUED")
    rows = c.get_pages(f"{V3}/orders/historical/batch", {"order_status": ["PENDING", "QUEUED"], "limit": 100}, "orders")
    assert len(rows) == 5
    reads = [r for r in fake.requests if r["path"].endswith("/batch")]
    assert len(reads) == 3 and all(r["query"]["order_status"] == ["PENDING", "QUEUED"] for r in reads)
    with pytest.raises(cr.CoinbaseDataError, match="still going after 2 pages"):
        c.get_pages(f"{V3}/orders/historical/batch", {"order_status": "QUEUED"}, "orders", max_pages=2)


def test_v2_lists_follow_next_uri(fake):
    c = client(fake)
    for k in range(7):
        fake.deposit(10.0 + k)
    rows = c.get_v2_pages(f"/v2/accounts/{fake.usd_account}/transactions", {"limit": 3})
    assert [float(r["amount"]["amount"]) for r in rows] == [16.0, 15.0, 14.0, 13.0, 12.0, 11.0, 10.0]
    assert len([r for r in fake.requests if r["path"].endswith("/transactions")]) == 3


# ---------------------------------------------------------------- the order lock

ORDER_BODY = {"client_order_id": "x", "product_id": PRODUCT, "side": "BUY",
              "order_configuration": {"market_market_ioc": {"base_size": "1"}}}


def test_a_client_without_the_token_refuses_every_post_before_a_socket_opens(fake, monkeypatch):
    c = client(fake)
    opened = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: opened.append(addr) or (_ for _ in ()).throw(
        AssertionError("a socket was opened")))
    for path in (*sorted(cr.ORDER_PATHS), f"{V3}/orders/preview", f"{V3}/orders/edit", f"{V3}/cfm/sweeps/schedule",
                 f"{V3}/cfm/intraday/margin_setting", "/v2/accounts/x/transactions"):
        with pytest.raises(cr.OrdersLocked, match="orders are locked"):
            c.post(path, ORDER_BODY)
    assert opened == [] and fake.requests == [] and c.posts_sent == 0
    assert not hasattr(c, "delete") and not hasattr(c, "put")
    with pytest.raises(cr.PathNotAllowed):
        c._request("DELETE", f"{V3}/cfm/sweeps", None, None, auth=True, retries=0)


def test_an_armed_token_cant_be_forged_and_expires_after_a_run(fake):
    with pytest.raises(TypeError, match="only by coinbase_live.arm"):
        coinbase_live.Armed(object(), PRODUCT, 1000.0, (), fake.clock())
    fk = make_ec_key(trade=True)
    c, token = armed(fake, fk)
    assert token.valid(fake.clock())
    fake.sleep(coinbase_live.ARM_TTL_S + 1)
    with pytest.raises(cr.OrdersLocked):
        c.post(f"{V3}/orders", ORDER_BODY)
    assert fake.non_get() == []


def test_armed_only_the_three_order_calls_pass(fake):
    c, _ = armed(fake, make_ec_key(trade=True))
    for path in (f"{V3}/orders/preview", f"{V3}/orders/edit", f"{V3}/cfm/sweeps/schedule",
                 f"{V3}/cfm/intraday/margin_setting", f"{V3}/portfolios/move_funds", "/v2/accounts/x/transactions",
                 f"{V3}/orders/batch_cancel/", f"{V3}/orders?x=1"):
        with pytest.raises(cr.PathNotAllowed):
            c.post(path, ORDER_BODY)
    assert fake.non_get() == []
    out = c.post(f"{V3}/orders", ORDER_BODY)
    assert out["success"] is True and len(fake.non_get()) == 1


# ---------------------------------------------------------------- trouble

@pytest.mark.parametrize("trouble", [dict(status=429), dict(status=503), dict(status=502), dict(status=None),
                                     dict(status=200, body=b"<html>maintenance</html>")])
def test_reads_are_retried_with_backoff_through_429_5xx_drops_and_non_json(fake, trouble):
    sleeps = Sleeps(fake)
    c = client(fake, sleep=sleeps)
    fake.fail_next("GET", f"{V3}/cfm/positions", times=2, **trouble)
    assert c.get(f"{V3}/cfm/positions") == {"positions": []}
    assert sleeps == [2.0, 4.0]


def test_reads_give_up_after_their_tries_with_a_clean_error(fake):
    sleeps = Sleeps(fake)
    c = client(fake, sleep=sleeps)
    fake.fail_next("GET", f"{V3}/cfm/positions", status=503, times=10)
    with pytest.raises(cr.CoinbaseUnavailable, match="after 4 tries") as e:
        c.get(f"{V3}/cfm/positions")
    assert e.value.transient and sleeps == [2.0, 4.0, 8.0]


def test_a_post_whose_answer_was_lost_is_resent_byte_for_byte_and_makes_one_order(fake):
    c, _ = armed(fake, make_ec_key(trade=True))
    fake.fail_next("POST", f"{V3}/orders", status=None, after=True)       # Coinbase took it; the answer was lost
    out = c.post(f"{V3}/orders", {**ORDER_BODY, "client_order_id": "same-id"})
    posts = fake.non_get()
    assert len(posts) == 2 and posts[0]["raw"] == posts[1]["raw"]
    assert len(fake.orders) == 1 and out["success_response"]["order_id"] == next(iter(fake.orders))


def test_a_post_that_never_gets_a_clear_answer_is_uncertain_not_failed(fake):
    c, _ = armed(fake, make_ec_key(trade=True))
    fake.fail_next("POST", f"{V3}/orders", status=503, times=5, after=True)
    with pytest.raises(cr.CoinbaseUncertain, match="may have reached Coinbase"):
        c.post(f"{V3}/orders", ORDER_BODY)
    assert len(fake.non_get()) == 3 and len(fake.orders) == 1


@pytest.mark.parametrize("status", [200, 400])
def test_a_rejected_order_is_a_definite_refusal_with_its_codes_whatever_the_http_status(fake, status):
    c, _ = armed(fake, make_ec_key(trade=True))
    fake.reject_status = status
    with pytest.raises(cr.CoinbaseRejected) as e:
        c.post(f"{V3}/orders", {**ORDER_BODY, "order_configuration": {"market_market_ioc": {"base_size": "0.5"}}})
    assert e.value.has("INVALID_SIZE_PRECISION", "PREVIEW_INVALID_SIZE_PRECISION") and e.value.status == status
    assert "size must be whole contracts" not in str(e.value) and "size must be whole contracts" in e.value.detail()
    assert len(fake.non_get()) == 1                         # a definite answer isn't retried


def test_a_redirect_is_never_followed(fake):
    c = client(fake)
    fake.fail_next("GET", f"{V3}/cfm/sweeps", status=302, body=b"moved")
    with pytest.raises(cr.CoinbaseDataError, match="redirected"):
        c.get(f"{V3}/cfm/sweeps")


# ---------------------------------------------------------------- the clock, the deadline, the log

def test_a_clock_more_than_30_s_off_refuses_before_any_private_call(fake):
    c = client(fake, clock=lambda: fake.now_s + 41.2)
    with pytest.raises(cr.ClockSkewError, match=r"\+41\.2 s off Coinbase's"):
        c.server_skew()
    assert [r["path"] for r in fake.requests] == [f"{V3}/time"]


def test_tokens_are_signed_on_coinbases_clock_once_the_skew_is_measured(fake):
    c = client(fake, clock=lambda: fake.now_s + 20.0)       # this machine runs 20 s fast
    with pytest.raises(cr.CoinbaseAuthError):
        c.get(f"{V3}/key_permissions")                       # nbf 20 s in Coinbase's future: a bare 401
    assert abs(c.server_skew() + 20.0) < 0.5
    assert c.get(f"{V3}/key_permissions")["can_view"] is True


def test_nothing_is_sent_once_the_runs_time_is_up(fake):
    c = client(fake, deadline_s=fake.now_s - 1)
    with pytest.raises(cr.DeadlineExceeded):
        c.get(f"{V3}/cfm/positions")
    assert fake.requests == []
    sleeps = Sleeps(fake)
    c = client(fake, sleep=sleeps, deadline_s=fake.now_s + 3)
    fake.fail_next("GET", f"{V3}/cfm/positions", status=503, times=5)
    with pytest.raises(cr.CoinbaseUnavailable):
        c.get(f"{V3}/cfm/positions")
    assert sleeps == [2.0]                                   # a 4 s backoff wouldn't fit in what's left


def test_every_token_is_masked_on_actions_and_no_error_carries_one(fake):
    out: list[str] = []
    fk = make_ec_key()
    c = client(fake, fk, emit=out.append, env={"GITHUB_ACTIONS": "true"})
    c.get(f"{V3}/key_permissions")
    c.get(f"{V3}/cfm/positions")
    masks = [ln for ln in out if ln.startswith("::add-mask::")]
    assert len(masks) == 2 and all(m.count(".") == 2 for m in masks)       # each request's JWT, masked first
    c2 = client(fake, register=False, emit=out.append)
    with pytest.raises(cr.CoinbaseError) as e:
        c2.get(f"{V3}/key_permissions")
    assert "Bearer" not in str(e.value) and "eyJ" not in str(e.value) and fk.name not in str(e.value)
