"""Round 5, LIVE (still locked: only these tests arm it, against their local fake Coinbase): what the safety pass keeps
as cover, what it says, and what the order client lets out. Each fails on the code before it (9259605):

- cutting back desk stops that covered more than the position kept the tightest, a stop that had triggered and couldn't
  fill, and cancelled the only one that could: a gap past the first stop's limit, Coinbase closed, and at the reopen
  the long had no working stop;
- arm() read Coinbase's clock and the key's permissions, then connect() read them again: a read failing there stopped
  the run before its safety pass (flat_kill ignored, a gapped stop left), and with the clock read failing, no
  watchdog either; a protect-only run whose safety pass never ran still said "It protected the position it holds";
- a stop covering part of the position (it grew in the app) was never topped up on a protect-only run, and the
  watchdog only flagged no stop at all; a take-profit limit of yours counted as cover;
- with Coinbase not answering at all, flat_kill on and a position saved, nothing urgent went out, and the push said
  "not armed" of a LIVE that has armed;
- the order client checked the product and the cap only on protect-only tokens, and let a protect-only token cancel
  any order id (the app's, another product's).

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from desk import coinbase_live
from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase import CoinbaseExchange
from desk.brokers.coinbase_auth import load_cdp_key
from desk.brokers.coinbase_rest import OrdersLocked
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, Pos, good_proof, make_ec_key, view_rest
from test_coinbase_kills_fail_closed import _next, world  # noqa: F401
from test_coinbase_protect_only import _armed_long, _live, _protect_pushes

FLAT = {"DESK_COINBASE_KILL": "flat"}
PROTECT = {"DESK_LIVE_CONFIRM": None}


def _urgent(sh, n0):
    return [(t, b) for t, b, p, *_ in sh.pushes[n0:] if p == "urgent"]


def _sl(o):
    return o["order_configuration"]["stop_limit_stop_limit_gtc"]


def _working(fake):
    return [o for o in fake.stops() if o["status"] == "OPEN" and o["trigger_status"] == "STOP_PENDING"]


# ---------------------------------------------------------------- the cut-back keeps a stop that can still fill

@pytest.mark.parametrize("protect", [True, False])
def test_cutting_back_two_stops_keeps_the_one_that_can_still_fill(world, protect):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    body = co.stop_limit(co.client_order_id("stop", "BTC", "dup", 1), "SELL", 1, level * 0.90)
    fake._order(body, "stop", body["order_configuration"], "OPEN")      # a second desk stop: looser, still working
    fake.trade(level * 0.93)            # the tight one triggers and can't fill (its limit is 5% under it)
    fake.close_market()                 # and the close at market can't go
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    kept = _working(fake)
    assert len(kept) == 1 and float(_sl(kept[0])["stop_price"]) == co.round_price(level * 0.90), r.safety_said
    assert r.safety_end == "protected"
    gapped = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: STOP DIDN'T FILL"]
    assert gapped and "still rests and can still fill" in gapped[0], [t for t, _ in _urgent(sh, n0)]
    if protect:
        assert _protect_pushes(sh, n0)[0][1].count("It protected the position it holds") == 1
    fake.reopen(price=level * 0.88)     # the kept stop triggers and fills (its limit is 0.855 of the level)
    assert fake.position() is None      # before: it had been cancelled, and the long stayed open with no stop


def test_cutting_back_when_both_stops_work_still_keeps_the_tightest(world):
    """The control: two working stops, the tighter one kept, as before."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    body = co.stop_limit(co.client_order_id("stop", "BTC", "dup", 1), "SELL", 1, level * 0.90)
    fake._order(body, "stop", body["order_configuration"], "OPEN")
    _next(fake)
    code, r, changed, saved = _live(job, over=PROTECT)
    kept = _working(fake)
    assert len(kept) == 1 and float(_sl(kept[0])["stop_price"]) == co.round_price(level)


# ---------------------------------------------------------------- nothing between arming and the safety pass

def _fail_time(fake, tries):
    """GET /time (public, answered before the fake's failure rules) answers 503 on the calls numbered in `tries`."""
    orig = fake._handle
    seen = {"n": 0}

    def handle(h, method):
        if method == "GET" and urlsplit(h.path).path == f"{V3}/time":
            seen["n"] += 1
            if seen["n"] in tries:
                rec = {"method": method, "path": f"{V3}/time", "query": {}, "body": None, "raw": b"", "key": None,
                       "status": None, "ts": fake.now_s}
                fake.requests.append(rec)
                return fake._send(h, rec, 503, b"<html>upstream error</html>", ctype="text/html")
        return orig(h, method)
    fake._handle = handle
    return lambda: setattr(fake, "_handle", orig), seen


@pytest.mark.parametrize("protect", [True, False])
def test_the_clock_read_once_by_arming_is_not_read_again_before_the_safety_pass(world, protect):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    _next(fake)
    undo, seen = _fail_time(fake, {2, 3, 4, 5})     # Coinbase's clock answers arm(), then not for a while
    code, r, changed, saved = _live(job, over={**FLAT, **(PROTECT if protect else {})})
    undo()
    assert seen["n"] == 1                           # arm()'s read only (before: read again, failed, run stopped)
    assert fake.position() is None and r.safety_end == "flat" and saved, (r.problem, r.lines[-4:])


@pytest.mark.parametrize("protect", [True, False])
def test_the_key_permissions_read_by_arming_are_not_read_again(world, protect):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    _next(fake)
    orig, seen = fake._route, {"n": 0}

    def route(method, path, q, body, key):
        if method == "GET" and path == f"{V3}/key_permissions":
            seen["n"] += 1
            if seen["n"] >= 2:
                return 503, {"error": "UNAVAILABLE", "message": "unavailable"}
        return orig(method, path, q, body, key)
    fake._route = route
    code, r, changed, saved = _live(job, over={**FLAT, **(PROTECT if protect else {})})
    fake._route = orig
    assert seen["n"] == 1 and fake.position() is None and r.safety_end == "flat", (r.problem, r.lines[-4:])


def test_a_gapped_stop_is_closed_though_the_clock_stops_answering_after_arming(world):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.trade(level * 0.90)                        # through the trigger and its 5% limit: triggered, unfilled
    _next(fake)
    undo, seen = _fail_time(fake, {2, 3, 4, 5})
    code, r, changed, saved = _live(job)
    undo()
    assert fake.position() is None, r.lines[-6:]    # before: the run stopped before its safety pass


def test_a_protect_only_run_whose_safety_pass_never_ran_never_says_protected(world, monkeypatch):
    """Anything that stops an armed run before its safety pass: the watchdog still looks (flat_kill pushed), and the
    PROTECT ONLY push and the public line say the pass didn't run, never that the position is protected."""
    from desk import coinbase_run as cr

    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    _next(fake)

    def boom(self, perms, work):
        raise RuntimeError("a bug between arming and the safety pass")
    monkeypatch.setattr(cr.LiveRun, "note_portfolio", boom)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={**FLAT, **PROTECT})
    assert fake.position() is not None and r.safety_end is None
    got = _protect_pushes(sh, n0)
    assert got and "It protected the position it holds" not in got[0][1], got
    assert "The safety pass didn't run this run: check the position and its stop in Coinbase" in got[0][1]
    line = r.public_line()
    assert "the open position is protected" not in line and "the safety pass didn't run" in line, line
    assert any(t == "BTC LIVE: FLAT_KILL NOT DONE" for t, _ in _urgent(sh, n0)), [t for t, _ in _urgent(sh, n0)]


# ---------------------------------------------------------------- only a stop is cover, and all of the position

@pytest.mark.parametrize("protect", [True, False])
def test_a_stop_covering_part_of_the_position_is_topped_up_at_the_saved_level(world, protect):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.positions[PRODUCT].n = 2                   # the long grew in the app; the desk's stop covers 1
    _next(fake)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    stops = _working(fake)
    assert sum(int(_sl(o)["base_size"]) for o in stops) == 2, (r.safety_said, [_sl(o) for o in stops])
    assert all(float(_sl(o)["stop_price"]) == co.round_price(level) for o in stops)
    assert any("no stop was resting on 1 of the long 2 contract(s): placed one at" in s for s in r.safety_said), \
        r.safety_said
    assert r.safety_end == "protected"


def test_the_watchdog_flags_stops_that_cover_only_part_of_the_position(world):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.positions[PRODUCT].n = 2
    found = CoinbaseExchange(view_rest(fake)).watchdog()
    assert ("unprotected", f"the stops resting on the long 2 contracts on {PRODUCT} cover only 1 of them: 1 have no "
                           f"stop on Coinbase") in found, found


def _take_profit(fake, n=1):
    cfg = {"limit_limit_gtc": {"base_size": str(n), "limit_price": str(co.round_price(fake.last * 1.2)),
                               "post_only": False}}
    tp = fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": "app-take-profit"}, "stop", cfg, "OPEN")
    tp["order_placement_source"], tp["trigger_status"] = "RETAIL_SIMPLE", "INVALID_ORDER_TYPE"
    return tp


def test_flat_kill_leaving_the_rest_to_your_take_profit_pushes_no_stop_and_never_says_protected(world):
    """Protect only (a full run goes on to close the rest at market, as LiveBroker does when your order stands where
    its stop must go)."""
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.positions[PRODUCT].n = 2
    _sl(fake.stops()[0])["base_size"] = "2"
    _take_profit(fake)                              # yours, in the app: a SELL limit for 1, far above
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={**FLAT, **PROTECT})
    assert fake.position() is not None and fake.position().n == 1 and not _working(fake)
    nostop = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: NO STOP"]
    assert nostop and "aren't stops" in nostop[0], [t for t, _ in _urgent(sh, n0)]
    assert r.safety_end == "unprotected"            # before: "covered by the closing orders resting", protected
    got = _protect_pushes(sh, n0)
    assert got and "The position may be without a working stop" in got[0][1]
    assert "the open position is protected" not in r.public_line()


# ---------------------------------------------------------------- Coinbase not answering at all

@pytest.mark.parametrize("protect", [True, False])
def test_coinbase_down_for_the_whole_run_with_flat_kill_on_is_pushed_urgently(world, protect):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    _next(fake)
    orig = fake._route
    fake._route = lambda method, path, q, body, key: (503, {"error": "UNAVAILABLE", "message": "unavailable"})
    n0, posts = len(sh.pushes), len(fake.non_get())
    code, r, changed, saved = _live(job, over={**FLAT, **(PROTECT if protect else {})})
    fake._route = orig
    urgent = _urgent(sh, n0)
    assert any(t == "BTC LIVE: FLAT_KILL NOT DONE" and "Coinbase didn't answer" in b for t, b in urgent), \
        [(t, p) for t, b, p, *_ in sh.pushes[n0:]]
    working = [b for t, b, p, *_ in sh.pushes[n0:] if t == "BTC LIVE: not working"]
    assert working and "didn't arm this run" in working[0] and "not armed" not in working[0], working
    assert len(fake.non_get()) == posts             # nothing was sent


# ---------------------------------------------------------------- the order client, whatever the token's scope

@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        yield f


def _client(fake, env, live_state=None, cap=1_000.0):
    fk = fake.add_key(make_ec_key("r5-trade-not-real", trade=True))
    out: list[str] = []
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), env=env, max_notional=cap, gate=[],
                            shadow_proof=good_proof(fake.clock_ms()), live_state={"armed_at": 1, **(live_state or {})},
                            base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep, out=out.append)
    assert got is not None, out
    client, token = got
    client.min_gap_s = 0
    return client, token


def _entry(n, product=PRODUCT):
    return {"client_order_id": co.client_order_id("entry", "BTC", n, product), "product_id": product, "side": "BUY",
            "order_configuration": {"market_market_ioc": {"base_size": str(n)}}}


def test_a_trade_scope_client_sends_nothing_for_another_product_or_over_its_cap(fake):
    client, token = _client(fake, ARM_ENV)
    assert token.scope == "trade"
    with pytest.raises(OrdersLocked, match="not BIP-20DEC30-CDE"):
        client.post(f"{V3}/orders", _entry(1, "ETP-20DEC30-CDE"))
    with pytest.raises(OrdersLocked, match="over the \\$1,000.00 cap"):
        client.post(f"{V3}/orders", _entry(5000))
    with pytest.raises(OrdersLocked, match="not BIP-20DEC30-CDE"):
        client.post(f"{V3}/orders/close_position", {"client_order_id": co.client_order_id("c", "BTC", 1),
                                                    "product_id": "ETP-20DEC30-CDE", "size": "1"})
    assert fake.non_get() == []                     # before: both orders left the process
    client.post(f"{V3}/orders", _entry(1))          # one contract, within the cap: it goes
    assert [q["path"] for q in fake.non_get()] == [f"{V3}/orders"] and fake.position().n == 1


def test_a_protect_scope_client_cancels_only_the_desks_own_orders_on_the_product(fake):
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
    client, token = _client(fake, {**ARM_ENV, "DESK_LIVE_CONFIRM": ""}, {"side": "long", "stop": 80_000.0})
    assert token.scope == "protect"
    client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 1), "SELL", 1, 80_000.0))
    mine = fake.stops()[-1]["order_id"]
    app = _take_profit(fake)
    other = co.stop_limit(co.client_order_id("stop", "BTC", "other", 1), "SELL", 1, 2_000.0)
    other["product_id"] = "ETP-20DEC30-CDE"
    theirs = fake._order(other, "stop", other["order_configuration"], "OPEN")
    posts = len(fake.non_get())
    for o in (app, theirs):
        with pytest.raises(OrdersLocked, match="isn't one of the desk's own orders"):
            client.post(f"{V3}/orders/batch_cancel", co.batch_cancel([o["order_id"]]))
    assert len(fake.non_get()) == posts and app["status"] == "OPEN" and theirs["status"] == "OPEN"
    # the desk's own stop: a cancel goes (the safety pass cancels one for a close, and puts it back if the close
    # doesn't go through: the client doesn't check the protection a cancel takes away, its docstring says so)
    client.post(f"{V3}/orders/batch_cancel", co.batch_cancel([mine]))
    assert fake.orders[mine]["status"] == "CANCELLED"
    from desk.brokers.coinbase_rest import CoinbaseREST
    assert "the caller stays responsible for putting back any protection" in " ".join(
        CoinbaseREST._order_check.__doc__.split())
