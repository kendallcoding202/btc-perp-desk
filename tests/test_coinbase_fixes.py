"""Regression tests for the third review of the Coinbase connection: LIVE's failure paths around cancel-first stop
moves (money), fidelity to Coinbase's current Advanced Trade spec (API), and the honesty and privacy of SHADOW's
pushes and records (ops). Each test names the finding it covers; each fails on the code before the fix.

Everything runs against tests/fake_coinbase.py on 127.0.0.1 (tests/conftest.py fails any other connection); every key
is generated fresh and named "...-not-real". Nothing here can reach Coinbase, and LIVE stays locked outside the tests.
"""

from __future__ import annotations

import json
import re

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from desk import statebox
from desk.brokers import coinbase as cb
from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase_rest import CoinbaseAuthError, CoinbaseDataError, CoinbaseUncertain
from fake_coinbase import (PRODUCT, V3, FakeCoinbase, Pos, arm_rest, dcid, make_ec_key, make_ed_key, view_rest)
from test_coinbase_live import broker, go_long, safe
from test_coinbase_shadow import IT, KEY, Shadow, _long, _write_proof, events

STOP_CFG = {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "81000", "limit_price": "76950",
                                          "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}


@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        yield f


@pytest.fixture
def zero():
    with FakeCoinbase(cbi=0.0) as f:
        f.now_s += 8 * 60           # a run 8 minutes after the 14:00 close
        yield f


def levels(fake):
    return [o["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"] for o in fake.stops()]


def sent(fake, n0=0):
    return [r["path"].rsplit("/", 1)[-1] for r in fake.non_get()[n0:]]


def hook(b):
    got = []
    b.on_urgent = lambda kind, text: got.append((kind, text))
    return got


def live_with_position(tmp_path):
    """A LIVE --once account on the fake holding a long 1 contract with its stop (two runs: day one, a breakout)."""
    fake = FakeCoinbase(cbi=1_000.0).__enter__()
    fake.now_s += 8 * 60
    sh = Shadow(fake, tmp_path, make_ec_key("test-trade-key-not-real", trade=True))
    _write_proof(sh, fake)
    assert sh.run("live")[0] == 0
    sh.breakout()
    assert sh.run("live")[0] == 0
    assert fake.position().n == 1 and len(fake.stops()) == 1
    return fake, sh


# ================================================================ money: LIVE's failure paths

# ---- finding 1: the emergency close was skipped by the time budget
def test_money1_a_move_doesnt_start_without_the_time_for_the_move_and_a_close(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    n0 = len(fake.non_get())
    b.ex.rest.deadline_s = fake.clock() + 50        # a move fits (45 s), a move and its fallback close (90 s) don't
    b.update_stop(81_000.0)
    assert sent(fake, n0) == [] and levels(fake) == ["80000"]      # the old stop stays: nothing was cancelled
    safe(fake)


def test_money1_after_the_cancel_a_stop_that_wont_rest_is_closed_at_market_past_the_deadline(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    b.ex.rest.deadline_s = fake.clock() + 95
    fake.band = 0.001                               # every stop-limit offset refused (5%, 2%, 1%)
    fake.on_cancel = lambda: fake.sleep(92)         # the cancel eats the run's time: the close goes past the deadline
    b.update_stop(81_000.0)
    assert b.ex.rest.time_left() < 0 < b.ex.rest.time_left() + b.ex.rest.protect_s
    assert fake.position() is None and "close_position" in sent(fake) and b.trades[-1].reason == "no_stop"
    assert any("was closed at market rather than held unprotected" in a for a in b.alerts_seen)
    assert "entry_kill is on" in b.entry_kill_why
    safe(fake)


def test_money1_an_entry_whose_stop_wont_rest_is_closed_even_when_time_is_short(fake, tmp_path):
    """The reviewer's repro: 95 s left, 503 bursts after the entry, every stop refused."""
    b = broker(fake, tmp_path)
    b.ex.rest.deadline_s = fake.clock() + 95
    fake.band = 0.001
    orig = fake._post_order

    def post_order(body):
        out = orig(body)
        if "market_market_ioc" in (body.get("order_configuration") or {}):
            fake.fail_next("GET", f"{V3}/orders/historical/batch", 503, times=7)
            fake.fail_next("GET", f"{V3}/cfm/positions", 503, times=7)
        return out
    fake._post_order = post_order
    go_long(b, fake)
    assert fake.position() is None and "close_position" in sent(fake) and b.entry_kill_why
    safe(fake)


def test_money1_a_close_that_fails_too_is_the_urgent_no_stop_never_closed(fake, tmp_path):
    b = broker(fake, tmp_path)
    got = hook(b)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.band = 0.001
    fake.fail_next("POST", f"{V3}/orders/close_position", 400, times=10)
    with pytest.raises(Exception):
        b.update_stop(81_000.0)
    assert fake.position() is not None and fake.stops() == []
    assert [k for k, _ in got] == ["no_stop"] and got[0][1].startswith("NO STOP")
    assert not any("closed at market" in a for a in b.alerts_seen)
    assert b.entry_kill_why.startswith("NO STOP") and "entry_kill is on" in b.entry_kill_why
    assert sum(a.startswith("NO STOP") for a in b.alerts_seen) == 1      # said once, not once per layer


# ---- finding 2: a read failure or the deadline between cancel and re-place
def test_money2_reads_failing_after_the_cancel_dont_stop_the_new_stop(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    b.ex.rest.deadline_s = fake.clock() + 120
    fake.on_cancel = lambda: fake.fail_next("GET", f"{V3}/cfm/positions", 503, times=1000)
    b.update_stop(81_000.0)                         # the reviewer's repro raised DeadlineExceeded with no stop
    assert levels(fake) == ["81000"] and fake.position().n == 1 and b.stop_order_id
    safe(fake)


def test_money2_a_live_run_cut_off_mid_trail_ends_with_its_stop_and_says_what_it_sent(tmp_path):
    fake, sh = live_with_position(tmp_path)
    try:
        close = fake.last
        fake.trade(close * 1.01, dt=1800)
        sh.close(close * 1.03)                      # the next 2h close: the desk trails the stop up
        fake.on_cancel = lambda: (fake.fail_next("GET", f"{V3}/cfm/positions", 503, times=10_000),
                                  fake.fail_next("GET", f"{V3}/orders/historical/batch", 503, times=10_000))
        n0 = len(fake.non_get())
        code, r = sh.run("live")
        assert sent(fake, n0) == ["batch_cancel", "orders"]
        assert fake.position().n == 1 and len(fake.stops()) == 1 and levels(fake) != ["84005"]    # moved, resting
        bodies = [p[1] for p in sh.pushes]
        assert not any("Nothing was sent" in t for t in bodies)
        if code:                                    # it stopped part way: the push says what went out
            notw = [p for p in sh.pushes if p[0] == "BTC LIVE: not working"]
            assert notw and "2 order requests went to Coinbase this run (batch_cancel, orders)" in notw[-1][1]
    finally:
        fake.__exit__(None, None, None)


def test_money2_a_stop_that_cant_go_back_on_is_the_urgent_push_and_entry_kill_even_on_a_read_error(tmp_path):
    fake, sh = live_with_position(tmp_path)
    try:
        close = fake.last
        fake.trade(close * 1.01, dt=1800)
        sh.close(close * 1.03)
        fake.band = 0.0001                          # no stop-limit rests at any offset
        fake.on_cancel = lambda: fake.fail_next("POST", f"{V3}/orders/close_position", 503, times=100)
        code, r = sh.run("live")
        assert fake.position() is not None and fake.stops() == []
        urgent = [p for p in sh.pushes if p[2] == "urgent"]
        assert urgent and urgent[0][0] == "BTC LIVE: NO STOP"
        assert r.kills_set.get("entry_kill") is True
    finally:
        fake.__exit__(None, None, None)


# ---- finding 3: the stop is checked before any market read (the safety pass)
def test_money3_a_live_run_that_cant_read_candles_still_puts_the_stop_back(tmp_path):
    fake, sh = live_with_position(tmp_path)
    try:
        for o in fake.stops():
            o["status"] = "CANCELLED"               # the stop is gone from Coinbase
        fake.now_s += 900
        fake.fail_next("GET", f"{V3}/products/{PRODUCT}/candles", 503, times=1000)
        n0 = len(fake.non_get())
        code, r = sh.run("live")
        assert code == 1 and r.problem == "read"
        assert sent(fake, n0) == ["orders"] and len(fake.stops()) == 1     # put back before the candles were read
        notw = [p for p in sh.pushes if p[0] == "BTC LIVE: not working"]
        assert notw and "1 order request went to Coinbase this run (orders)" in notw[-1][1]
    finally:
        fake.__exit__(None, None, None)


@pytest.mark.parametrize("edit", [{"future_product_details": {"contract_size": "0.1"}}, {"base_increment": "0.5"}])
def test_money3_a_product_change_with_a_position_still_manages_the_stop(tmp_path, edit):
    fake, sh = live_with_position(tmp_path)
    try:
        for o in fake.stops():
            o["status"] = "CANCELLED"
        fake.product_edits = edit
        fake.now_s += 900
        n0 = len(fake.non_get())
        code, r = sh.run("live")
        assert len(fake.stops()) == 1 and "orders" in sent(fake, n0)
        assert code == 0 and any("still managed" in a for a in r.alerts)    # the run goes on, entries blocked
    finally:
        fake.__exit__(None, None, None)


def test_money3_flat_kill_closes_even_when_the_candles_cant_be_read(tmp_path):
    fake, sh = live_with_position(tmp_path)
    try:
        fake.now_s += 900
        fake.fail_next("GET", f"{V3}/products/{PRODUCT}/candles", 503, times=1000)
        sh.env["DESK_COINBASE_KILL"] = "flat"
        code, r = sh.run("live")
        assert code == 1 and fake.position() is None and fake.resting() == []
        fake.fail = []
        fake.now_s += 900
        assert sh.run("live")[0] == 0
        trades = json.loads(sh.state("live")["files"]["live_position.json"])["trades"]
        assert trades[-1]["reason"] == "flat_kill"
    finally:
        fake.__exit__(None, None, None)


def test_money3_a_stop_left_resting_with_no_position_is_cancelled_before_any_market_read(tmp_path):
    fake, sh = live_with_position(tmp_path)
    try:
        fake.positions.pop(PRODUCT)                 # closed by hand in the app; the desk's stop still rests
        fake.now_s += 900
        fake.fail_next("GET", f"{V3}/products/{PRODUCT}/candles", 503, times=1000)
        code, r = sh.run("live")
        assert code == 1 and fake.resting() == []
    finally:
        fake.__exit__(None, None, None)


# ---- finding 4: a lost cancel answer was taken as "still protects"
def test_money4_a_cancel_whose_answers_are_lost_is_read_back_and_the_move_completes(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.fail_next("POST", f"{V3}/orders/batch_cancel", None, times=3, after=True)     # done, answer dropped
    assert b._ensure_stop(81_000.0, fake.last, fake.clock_ms(), keep_old=True) is True
    assert levels(fake) == ["81000"] and not any("still protects" in a for a in b.alerts_seen)
    safe(fake)


def test_money4_still_protects_is_said_only_when_the_stop_reads_open(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.fail_next("POST", f"{V3}/orders/batch_cancel", None, times=3)                 # never reached Coinbase
    assert b._ensure_stop(81_000.0, fake.last, fake.clock_ms(), keep_old=True) is False
    assert levels(fake) == ["80000"]
    assert any("it reads open, so it stays and still protects" in a for a in b.alerts_seen)
    safe(fake)


# ---- finding 5: an entry retry answered with a rejection
def test_money5_a_post_refused_after_a_try_with_no_answer_is_uncertain_not_refused(fake):
    rest = arm_rest(fake)
    orig = fake._post_order
    fake._post_order = lambda body: (400, {"error": "INVALID_ARGUMENT", "message": "dup", "error_details": "dup"}) \
        if body.get("client_order_id") in fake.cids else orig(body)
    fake.fail_next("POST", f"{V3}/orders", None, times=1, after=True)
    with pytest.raises(CoinbaseUncertain) as e:
        rest.post(f"{V3}/orders", co.market(dcid("e"), "BUY", 1))
    assert e.value.codes == ("INVALID_ARGUMENT",)


def test_money5_an_entry_whose_retry_is_refused_is_adopted_and_protected_in_the_same_check(fake, tmp_path):
    b = broker(fake, tmp_path)
    orig = fake._post_order

    def post_order(body):
        if body.get("client_order_id") in fake.cids and "market_market_ioc" in body["order_configuration"]:
            return 400, {"error": "INVALID_ARGUMENT", "message": "duplicate", "error_details": "duplicate"}
        return orig(body)
    fake._post_order = post_order
    fake.fail_next("POST", f"{V3}/orders", None, times=1, after=True)
    go_long(b, fake)
    assert fake.position().n == 1 and levels(fake) == ["80000"] and b.position is not None
    safe(fake)


# ---- finding 6: an adopted position got no stop while Coinbase was closed
def test_money6_an_adopted_position_gets_a_queued_stop_while_coinbase_is_closed(fake, tmp_path):
    rest = arm_rest(fake)
    b = broker(fake, tmp_path, rest)
    go_long(b, fake)
    for o in fake.stops():
        o["status"] = "CANCELLED"
    (tmp_path / "BTC" / "live_position.json").unlink()
    fake.close_market()
    b2 = broker(fake, tmp_path / "again", rest)
    assert b2.position is not None and b2.position.stop == 0.0
    assert any("with no stop resting: the desk places one this run" in a for a in b2.alerts_seen)
    b2.update_stop(b2.position.entry_price - 3 * 1_000.0)          # the one-shot runner's guard
    assert [o["status"] for o in fake.resting()] == ["QUEUED"] and b2.stop_order_id


# ---- finding 7: a tighter resting stop was moved back down
def test_money7_a_tighter_resting_stop_is_kept_never_loosened(fake, tmp_path):
    import shutil
    rest = arm_rest(fake)
    b = broker(fake, tmp_path, rest)
    go_long(b, fake)
    saved = tmp_path / "saved.json"
    shutil.copy(tmp_path / "BTC" / "live_position.json", saved)      # the state as last sealed: stop 80,000
    fake.trade(86_000.0)
    b.update_stop(82_000.0)
    (tmp_path / "again" / "BTC").mkdir(parents=True)
    shutil.copy(saved, tmp_path / "again" / "BTC" / "live_position.json")
    b2 = broker(fake, tmp_path / "again", rest)
    assert levels(fake) == ["82000"] and b2.position.stop == 82_000.0
    safe(fake)


# ================================================================ API: Coinbase's current spec

# ---- finding 1: the documented price-band codes
@pytest.mark.parametrize("fields", [("new_order_failure_reason",), ("preview_failure_reason",)])
@pytest.mark.parametrize("side", ["long", "short"])
def test_api1_a_documented_price_band_refusal_is_retried_tighter(fake, fields, side):
    rest = arm_rest(fake)
    ex = cb.CoinbaseExchange(rest, on_alert=lambda t: None)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy" if side == "long" else "sell", 1, 83_000, {"clientOrderId": dcid("e")})
    fake.band, fake.band_fields = 0.06, fields      # a limit more than 6% from the last trade is refused
    trig = 80_000 if side == "long" else 86_000
    o = ex.create_order(PRODUCT, "market", "sell" if side == "long" else "buy", 1, trig,
                        {"clientOrderId": dcid("s"), "stopLossPrice": trig})
    want = "78400" if side == "long" else "87720"                   # the 2% limit, after the 5% was refused
    assert o["status"] == "open" and [s["order_configuration"]["stop_limit_stop_limit_gtc"]["limit_price"]
                                      for s in fake.stops()] == [want]
    tries = [r["body"]["order_configuration"]["stop_limit_stop_limit_gtc"]["limit_price"] for r in fake.requests
             if r["method"] == "POST" and "stop_limit_stop_limit_gtc" in ((r["body"] or {}).get("order_configuration")
                                                                         or {})]
    assert tries == [("76000" if side == "long" else "90300"), want]    # the 5% one refused, then the 2% one


# ---- finding 2: orders placed in the Coinbase app (RETAIL_SIMPLE)
def test_api2_an_app_placed_stop_is_seen_counted_never_touched_and_pushed(fake, tmp_path):
    b = broker(fake, tmp_path)
    got = hook(b)
    go_long(b, fake)
    o = fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": "app-placed-1"}, "stop",
                    STOP_CFG, "OPEN")
    o["order_placement_source"] = "RETAIL_SIMPLE"
    ex = b.ex
    assert [x["id"] for x in ex.fetch_open_orders(PRODUCT)] == [b.stop_order_id]     # the desk's only
    assert [x["id"] for x in ex.foreign_on_product()] == [o["order_id"]]
    assert "not the desk's" in ex.entry_block()
    b.sync(fake.last)
    assert [k for k, _ in got] == ["foreign"] and o["status"] == "OPEN"
    # a move: the guard counts the app's contracts, so no second full-size stop of the desk's goes on beside it; the
    # desk can't protect the position itself, so it closes it, and the app's order (never touched) is pushed
    fake.trade(84_000.0)
    n0 = len(fake.requests)
    b.update_stop(81_000.0)
    placed = [r for r in fake.requests[n0:] if r["method"] == "POST" and r["path"].endswith("/orders") and
              "stop_limit_stop_limit_gtc" in (r["body"] or {}).get("order_configuration", {})]
    assert placed == [] and o["status"] == "OPEN"
    assert fake.position() is None and b.trades[-1].reason == "no_stop"
    assert [k for k, _ in got] == ["foreign", "stray-foreign"]


def test_api2_the_desks_own_orders_under_another_source_are_learned_and_found(fake, tmp_path):
    orig = fake._order

    def tagged(body, kind, cfg, status):
        o = orig(body, kind, cfg, status)
        o["order_placement_source"] = "RETAIL_RAISE"
        return o
    fake._order = tagged
    alerts: list[str] = []
    b = broker(fake, tmp_path, alerts=alerts)
    go_long(b, fake)
    assert len(fake.stops()) == 1 and b.stop_order_id == fake.stops()[0]["order_id"]
    assert any("placement source RETAIL_RAISE" in a for a in alerts)
    b.sync(fake.last)
    assert len(fake.stops()) == 1                   # found again next time: no second stop
    safe(fake)


# ---- finding 3: pending_cancel
def test_api3_an_open_order_whose_cancel_is_in_flight_isnt_protection():
    o = {"order_id": "o1", "client_order_id": dcid("o1"), "product_id": PRODUCT, "side": "SELL", "status": "OPEN",
         "pending_cancel": True, "order_configuration": STOP_CFG}
    p = co.parse_order(o)
    assert p["status"] == "canceling" and "cancel pending" in p["coinbase_status"]
    assert co.parse_order({**o, "pending_cancel": False})["status"] == "open"


def test_api3_a_cancel_that_completes_after_a_few_seconds_finishes_the_move(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.cancel_delay_s = 3.0
    b.update_stop(81_000.0)
    assert levels(fake) == ["81000"] and fake.max_closing <= 0
    safe(fake)


def test_api3_a_cancel_still_in_flight_is_the_urgent_push_never_a_second_stop_then_the_next_check_fixes_it(fake,
                                                                                                         tmp_path):
    b = broker(fake, tmp_path)
    got = hook(b)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.cancel_delay_s = 600.0
    b.update_stop(81_000.0)
    assert [k for k, _ in got] == ["no_stop"] and not any("still protects" in a for a in b.alerts_seen)
    assert fake.max_closing <= 0 and len(fake.resting()) == 1      # the old one, its cancel in flight, alone
    fake.sleep(600)                                 # the cancel completes
    b.sync(fake.last)
    assert levels(fake) == ["81000"]
    safe(fake)


# ---- finding 5: Ed25519 keys
def test_api5_the_coinbase_app_reads_refuse_an_ed25519_key(fake):
    ex = cb.CoinbaseExchange(view_rest(fake, make_ed_key()))
    with pytest.raises(CoinbaseAuthError):
        ex.wallets()


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_api5_shadow_and_live_refuse_an_ed25519_key_before_any_network_call(zero, tmp_path, mode):
    sh = Shadow(zero, tmp_path, make_ed_key(trade=mode == "live"))
    if mode == "live":
        _write_proof(sh, zero)
    code, r = sh.run(mode)
    assert code == 2 and r.problem == "key" and zero.requests == []
    assert "ECDSA" in r.lines[-1] and r.checks.rows[0]["ok"] is False


# ---- finding 6: the USD wallet is the key's portfolio's
def test_api6_the_usd_wallet_is_the_one_in_the_keys_portfolio(fake):
    other = "bbbbbbbb-0000-4000-8000-000000000009"
    fake.extra_accounts = [{"uuid": other, "name": "Cash (USD)", "currency": "USD", "retail_portfolio_id":
                            "99999999-2222-3333-4444-555555555555", "platform": "ACCOUNT_PLATFORM_CONSUMER",
                            "available_balance": {"value": "250", "currency": "USD"}, "hold": {"value": "0",
                                                                                             "currency": "USD"}}]
    fake.extra_v2_accounts = [{"id": other, "currency": {"code": "USD"}, "balance": {"amount": "250.00",
                                                                                     "currency": "USD"}}]
    ex = cb.CoinbaseExchange(view_rest(fake))
    assert ex.wallets()["usd_account"] == fake.usd_account
    fake.deposit(100.0)
    assert [r["type"] for r in ex.fetch_ledger(None, fake.clock_ms())] == ["fiat_deposit"]


def test_api6_a_wallet_the_coinbase_app_doesnt_list_or_two_usd_accounts_refuse(fake):
    ex = cb.CoinbaseExchange(view_rest(fake))
    fake.usd_account = "cccccccc-0000-4000-8000-000000000007"     # v3 says one id, v2 lists another
    real_route = fake._route

    def route(m, p, q, b, k):
        st, out = real_route(m, p, q, b, k)
        if p == "/v2/accounts":
            for a in out["data"]:
                if a["id"] == fake.usd_account:
                    a["id"] = "aaaaaaaa-0000-4000-8000-000000000001"
        return st, out
    fake._route = route
    with pytest.raises(CoinbaseDataError, match="isn't among the Coinbase App"):
        ex.wallets()
    fake._route = real_route
    fake.extra_accounts = [{"uuid": "dddddddd-0000-4000-8000-000000000008", "currency": "USD",
                            "retail_portfolio_id": "11111111-2222-3333-4444-555555555555",
                            "platform": "ACCOUNT_PLATFORM_CONSUMER"}]
    with pytest.raises(CoinbaseDataError, match="two spot USD accounts"):
        ex.wallets()


# ---- finding 7: session_state
@pytest.mark.parametrize("state", ["FCM_TRADING_SESSION_STATE_HALTED", "FCM_TRADING_SESSION_STATE_PRE_OPEN_NO_CANCEL",
                                   "FCM_TRADING_SESSION_STATE_PRE_OPEN", "FCM_TRADING_SESSION_STATE_UNDEFINED"])
def test_api7_a_session_state_other_than_open_reads_closed(fake, state):
    fake.session_state = state                      # is_session_open stays true
    ex = cb.CoinbaseExchange(view_rest(fake))
    ex.load_markets()
    assert ex.session_open() is False and ex.entry_block() == "Coinbase's market is closed now"
    assert ex.halted() is (state.endswith("HALTED"))
    assert ex.cancels_refused() is (state.endswith(("HALTED", "NO_CANCEL")))


def test_api7_no_cancel_first_move_while_cancels_are_refused_and_nothing_at_all_while_halted(fake, tmp_path):
    b = broker(fake, tmp_path)
    got = hook(b)
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.session_state = "FCM_TRADING_SESSION_STATE_PRE_OPEN_NO_CANCEL"
    b.ex.load_markets(reload=True)
    n0 = len(fake.non_get())
    b.update_stop(81_000.0)
    assert sent(fake, n0) == [] and levels(fake) == ["80000"]
    fake.session_state = "FCM_TRADING_SESSION_STATE_HALTED"
    b.ex.load_markets(reload=True)
    for o in fake.stops():
        o["status"] = "CANCELLED"
    b.sync(fake.last)
    assert sent(fake, n0) == [] and [k for k, _ in got] == ["no_stop"]        # nothing sent, said urgently


# ---- finding 8: the balance check fails safe both ways
def test_api8_a_profit_counted_twice_blocks_entries_instead_of_reading_as_a_hold(fake):
    fake.realized_in_cfm = True                     # the other reading: Coinbase books today's P&L into cfm at once
    fake.positions[PRODUCT] = Pos("LONG", 3, 83_000.0, 83_000.0)
    fake.trade(86_000.0)
    o = fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": dcid("c")}, "close",
                    {"market_market_ioc": {"base_size": "3"}}, "OPEN")
    fake._fill(o, 3, 86_000.0)
    ex = cb.CoinbaseExchange(view_rest(fake))
    ex.fetch_balance()
    assert ex.balance["equity"] > ex.balance["cross"] + 1 and ex.balance_block and "no hold" in ex.balance_block


def test_api8_an_ach_hold_the_usd_account_shows_is_left_out_of_sizing_not_blocked(fake):
    fake.deposit(300.0, hold=True)
    ex = cb.CoinbaseExchange(view_rest(fake))
    assert ex.fetch_balance()["total"]["USD"] == pytest.approx(1_300.0)
    assert ex.balance_block is None and ex.usable == pytest.approx(1_000.0) and ex.usd_hold == pytest.approx(300.0)


# ================================================================ ops: honesty and privacy

def _halt(sh):
    """Put SHADOW's desk in a drawdown halt (as a big loss would), in its encrypted state."""
    b = sh.state()
    d = json.loads(b["files"]["desk.json"])
    d["risk"].update({"halted": True, "halt_reason": "a test halt"})
    b["files"]["desk.json"] = json.dumps(d)
    (sh.dir / "BTC" / "state-shadow.enc.json").write_text(
        statebox.seal_json(b, KEY, statebox.state_aad("shadow"), iterations=IT))


# ---- finding 1: money moves of types the desk didn't know were counted as profit or loss
def test_ops1_a_transfer_to_another_portfolio_is_money_out_not_a_loss(zero, tmp_path):
    sh = Shadow(zero, tmp_path)
    sh.run()
    zero.deposit(1_000.0, dt=600)
    zero.now_s += 300
    sh.run()
    with zero.lock:
        zero.now_s += 60
        zero.cbi -= 600.0
        zero._tx("transfer", -600.0)
    zero.now_s += 900
    sh.run()
    d = sh.file("shadow", "desk.json")
    assert d["risk"]["halted"] is False and sh.board()["ticker"]["pnl"] == pytest.approx(0.0, abs=1e-6)
    assert [(f["amount"], f["note"]) for f in d["flows"]] == [(-600.0, "transfer between your own Coinbase accounts "
                                                                       "or portfolios")]


@pytest.mark.parametrize("kind,amount", [("subscription", -29.99), ("earn_payout", 12.5)])
def test_ops1_coinbase_charges_and_payouts_are_money_in_or_out(zero, tmp_path, kind, amount):
    sh = Shadow(zero, tmp_path)
    sh.run()
    zero.deposit(500.0, dt=600)
    zero.now_s += 300
    sh.run()
    with zero.lock:
        zero.now_s += 60
        zero.cbi += amount
        zero._tx(kind, amount)
    zero.now_s += 900
    sh.run()
    assert sh.file("shadow", "desk.json")["flows"][-1]["amount"] == pytest.approx(amount)
    assert sh.board()["ticker"]["pnl"] == pytest.approx(0.0, abs=1e-6)


def test_ops1_a_move_nothing_explains_is_booked_as_money_in_or_out_and_listed(zero, tmp_path):
    sh = Shadow(zero, tmp_path)
    sh.run()
    zero.deposit(500.0, dt=600)
    zero.now_s += 300
    sh.run()
    with zero.lock:
        zero.now_s += 60
        zero.cbi += 500.0
        zero._tx("tx", 500.0)                       # the uncategorised type: still not guessed at
    killed = False
    for _ in range(14):
        zero.now_s += 15 * 60
        sh.run()
        killed = killed or bool(sh.r.kills_set.get("entry_kill"))
    b = sh.board()
    assert b["ticker"]["pnl"] == pytest.approx(0.0, abs=1e-6)
    assert [u["amount"] for u in b["shadow"]["unexplained"]] == [500.0]
    assert any("booked as money in, not profit or loss" in e for e in events(sh))
    assert killed           # entry_kill, as LIVE: the save step adds it to the branch's kills.json


def test_ops1_reset_halt_at_in_kills_json_clears_a_halt_once(zero, tmp_path):
    sh = Shadow(zero, tmp_path)
    sh.run()
    _halt(sh)
    kills = sh.dir / "BTC" / "kills.json"
    kills.write_text(json.dumps({"entry_kill": False, "reset_halt_at": "2026-10-14 first look"}))
    sh.run()
    assert sh.file("shadow", "desk.json")["risk"]["halted"] is False
    assert any("halt reset from kills.json" in e for e in events(sh))
    _halt(sh)
    sh.run()                                        # the same value: not again
    assert sh.file("shadow", "desk.json")["risk"]["halted"] is True
    kills.write_text(json.dumps({"reset_halt_at": "2026-10-15"}))
    sh.run()
    assert sh.file("shadow", "desk.json")["risk"]["halted"] is False
    assert cs.check_kills({"entry_kill": True, "reset_halt_at": "2026-10-15"}, "k")
    with pytest.raises(cs.StateCheckError):
        cs.check_kills({"reset_halt_at": 5}, "k")


# ---- finding 2: pushes carried real balances in plain text
def _scripted_pushes(zero, tmp_path, env=None):
    sh = Shadow(zero, tmp_path)
    sh.env.update(env or {})
    zero.deposit(2_345.67, dt=0)
    _long(sh)
    stop = [x for x in sh.file("shadow", "book.json")["orders"].values() if x["kind"] == "stop"][0]
    zero.trade(stop["stop"] - 50, dt=600)
    zero.now_s += 600
    sh.run()
    zero.deposit(100.0, dt=600)
    zero.now_s += 300
    sh.run()
    return sh


def test_ops2_without_ntfy_token_no_push_says_an_amount(zero, tmp_path):
    sh = _scripted_pushes(zero, tmp_path)
    assert len(sh.pushes) >= 4
    for title, body, *_ in sh.pushes:
        assert not re.search(r"\$\d", title + body), (title, body)
    assert any("amounts left out" in body for _, body, *_ in sh.pushes)


def test_ops2_with_ntfy_token_the_amounts_are_in_and_the_token_goes_as_a_bearer(zero, tmp_path, monkeypatch):
    sh = _scripted_pushes(zero, tmp_path, {"NTFY_TOKEN": "tk_test-token-not-real"})
    assert any(re.search(r"\$\d", body) for _, body, *_ in sh.pushes)
    seen = []

    class Resp:
        def read(self):
            return b""
    monkeypatch.setattr(cr.urllib.request, "urlopen", lambda req, timeout=10: seen.append(req) or Resp())
    assert cr.ntfy_push("t", "b", env={"NTFY_TOPIC": "topic-x", "NTFY_TOKEN": "tk_test-token-not-real"})
    assert seen[0].get_header("Authorization") == "Bearer tk_test-token-not-real"
    cr.ntfy_push("t", "b", env={"NTFY_TOPIC": "topic-x"})
    assert seen[1].get_header("Authorization") is None


# ---- finding 3: simulated events pushed as real ones
def test_ops3_a_simulated_stop_gapped_past_its_limit_is_one_sell_never_a_stop_fill(zero, tmp_path):
    sh = Shadow(zero, tmp_path)
    _long(sh)
    stop = [x for x in sh.file("shadow", "book.json")["orders"].values() if x["kind"] == "stop"][0]
    n0 = len(sh.pushes)
    zero.trade(stop["limit"] - 500, dt=600)
    zero.now_s += 600
    sh.run()
    new = sh.pushes[n0:]
    assert [t for t, *_ in new if "would SELL" in t] == ["BTC SHADOW: would SELL (no order sent)"]
    alerts = [b for t, b, *_ in new if t == "BTC SHADOW: ALERT"]
    assert alerts and all(b.startswith("Simulated book (nothing rests on Coinbase): ") for b in alerts)


# ---- finding 4: an unreachable Coinbase read as a failed clock check
def test_ops4_coinbase_not_answering_is_its_own_check_and_the_clock_isnt_failed(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {"COINBASE_CDP_KEY": make_ec_key().file_json(), "DESK_STATE_KEY": KEY}
    code, r = cr.run_shadow(tmp_path, env, base_url=f"http://127.0.0.1:{port}", sleep=lambda s: None,
                            push=lambda *a: None, mask_emit=lambda line: None, iterations=IT)
    rows = {c["key"]: c for c in r.checks.ordered()}
    assert code == 1 and rows["answered"]["ok"] is False and rows["clock"]["ok"] is None
    assert rows["clock"]["note"] == "not checked: Coinbase didn't answer"


# ---- finding 5: a DESK_STATE_KEY slip left no status at all
def test_ops5_a_short_state_key_writes_a_status_and_the_save_check_takes_it_alone(zero, tmp_path, monkeypatch):
    sh = Shadow(zero, tmp_path)
    out = tmp_path / "gh_output"
    sh.env.update({"DESK_STATE_KEY": "my-20-char-password!", "GITHUB_OUTPUT": str(out)})
    code, r = sh.run()
    assert code == 2 and out.read_text() == "changed=true\n" and zero.requests == []
    st = sh.status()
    assert st["problem"] == "key" and st["ok"] is False and st["runs"] == 0
    assert st["note"] == "DESK_STATE_KEY is too short (it needs thirty-two characters or more)"
    assert "DESK_STATE_KEY is too short" in r.public_line() and not re.search(r"\d", st["note"])
    assert not (sh.dir / "BTC" / "state-shadow.enc.json").exists()
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    assert cs.check(sh.dir, "refs/remotes/origin/coinbase-state") == [
        "a status-only save: SHADOW was refused over its secrets before it had a state"]
