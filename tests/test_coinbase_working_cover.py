"""Round 6, LIVE (still locked: only these tests arm it, against their local fake Coinbase): a stop is cover only while
it can still fill, a working stop cancelled for a close that fails goes back at its own level, and the pass says
"protected" only where it read or placed that cover. Each fails on the code before it (6115c44):

- a stop of yours (placed in the app) that had triggered and couldn't fill counted as cover: a protect-only run said
  "It protected the position it holds", and the watchdog of a refused run pushed nothing urgent;
- a close that failed after its stops were cancelled put the stop back at the desk's level, which the price was
  through: nothing rested, though the stop it had cancelled still worked (LiveBroker, and the protect-only pass, whose
  client refused that stop as wider than the saved one);
- cutting back desk stops kept a gapped stop and cancelled a working one too big for the position, or cancelled the
  gapped one and left the contracts it had covered with no stop, while Coinbase was closed;
- a close that didn't go (closing orders of yours that aren't stops on the whole position, a gapped stop that won't
  cancel) left the pass ending "protected" by default;
- a trade-scope client let any stop-limit out (an entry by stop, 5000 contracts, no position) and cancelled any order
  id (one placed in the app);
- a full run's ALERT said the position "is closed at market" while Coinbase was closed and nothing was sent.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json

import pytest

from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase import CoinbaseExchange
from desk.brokers.coinbase_rest import OrdersLocked
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, Pos, view_rest
from test_coinbase_kills_fail_closed import _next, world  # noqa: F401
from test_coinbase_protect_only import _armed_long, _live, _protect_pushes
from test_coinbase_stop_cover import _client, _take_profit

FLAT = {"DESK_COINBASE_KILL": "flat"}
PROTECT = {"DESK_LIVE_CONFIRM": None}


def _urgent(sh, n0):
    return [(t, b) for t, b, p, *_ in sh.pushes[n0:] if p == "urgent"]


def _sl(o):
    return o["order_configuration"]["stop_limit_stop_limit_gtc"]


def _working(fake):
    """Stops resting that can still fill as stops (OPEN, or QUEUED while Coinbase is closed)."""
    return [o for o in fake.stops() if o["status"] in ("OPEN", "QUEUED") and o["trigger_status"] == "STOP_PENDING"]


def _cover(fake):
    return sum(int(_sl(o)["base_size"]) - int(float(o["filled_size"])) for o in _working(fake))


def _desk_stop(fake, n, trig, label="dup"):
    body = co.stop_limit(co.client_order_id("stop", "BTC", label, n, trig), "SELL", n, trig)
    return fake._order(body, "stop", body["order_configuration"], "OPEN")


def _app_stop(fake, n, trig):
    body = co.stop_limit("app-stop-1", "SELL", n, trig)
    o = fake._order(body, "stop", body["order_configuration"], "OPEN")
    o["order_placement_source"] = "RETAIL_SIMPLE"
    return o


def _never_protected(sh, n0, r, protect):
    assert r.safety_end != "protected", (r.safety_end, r.safety_said)
    if protect:
        got = _protect_pushes(sh, n0)
        assert got and "It protected the position it holds" not in got[0][1], got
        assert "the open position is protected" not in r.public_line(), r.public_line()


# ---------------------------------------------------------------- a stop of yours that gapped is no cover

@pytest.mark.parametrize("protect", [True, False])
def test_your_own_stop_that_gapped_is_not_cover(world, protect):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.stops()[0]["status"] = "CANCELLED"             # the desk's stop, cancelled in the app
    app = _app_stop(fake, 1, level)                      # yours, at the same level
    fake.trade(level * 0.93)
    assert app["trigger_status"] == "STOP_TRIGGERED"
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    _never_protected(sh, n0, r, protect)
    assert r.safety_end == "unprotected"
    nostop = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: NO STOP"]
    assert nostop and "aren't stops that can still fill" in nostop[0], [t for t, _ in _urgent(sh, n0)]
    if protect:
        assert fake.position() is not None and not _working(fake)
    else:                           # a full run goes on to close at market (LiveBroker doesn't count yours)
        assert fake.position() is None


def test_the_watchdog_flags_a_position_whose_only_stop_is_yours_and_gapped():
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
        app = _app_stop(fake, 1, 80_000.0)
        fake.trade(74_000.0)
        assert app["trigger_status"] == "STOP_TRIGGERED"
        found = CoinbaseExchange(view_rest(fake)).watchdog()
        gapped = [t for k, t in found if k == "gapped"]
        assert gapped and gapped[0].startswith(f"your stop {app['order_id']} at 80,000 (not the desk's)"), found
        fake.trade(76_000.0)        # the control: one of yours that still works is cover
        app2 = _app_stop(fake, 1, 70_000.0)
        app["status"] = "CANCELLED"
        found = CoinbaseExchange(view_rest(fake)).watchdog()
        assert not [k for k, _ in found if k in ("gapped", "unprotected")], (found, app2["status"])


def test_a_refused_run_pushes_your_gapped_stop_urgently(world):
    """A run refused over a check any order needs (the key can move money now) runs the watchdog alone: your gapped
    stop is STOP DIDN'T FILL, urgently. Before: 'foreign' only, which isn't pushed."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.stops()[0]["status"] = "CANCELLED"
    _app_stop(fake, 1, level)
    fake.trade(level * 0.93)
    job.tk.transfer = True
    _next(fake)
    n0, posts = len(sh.pushes), len(fake.non_get())
    code, r, changed, saved = _live(job)
    assert code != 0 and len(fake.non_get()) == posts
    gapped = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: STOP DIDN'T FILL"]
    assert gapped and "your stop" in gapped[0] and "(not the desk's)" in gapped[0], \
        [(t, p) for t, b, p, *_ in sh.pushes[n0:]]


# ---------------------------------------------------------------- a working stop cancelled for a failed close goes back

def _fail_close(fake, times):
    body = json.dumps({"success": False, "failure_reason": "UNKNOWN_FAILURE_REASON", "order_id": "",
                       "error_response": {"error": "INVALID_REQUEST", "message": "x", "error_details": "x",
                                          "new_order_failure_reason": "INVALID_REQUEST",
                                          "preview_failure_reason": "UNKNOWN_PREVIEW_FAILURE_REASON"}}).encode()
    fake.fail_next("POST", f"{V3}/orders/close_position", status=400, times=times, body=body)


def _close_fails(fake, how, times):
    if how == "rejected":
        _fail_close(fake, times)
    elif how == "503":
        fake.fail_next("POST", f"{V3}/orders/close_position", status=503, times=3 * times)
    else:
        fake.liquidity = 0


@pytest.mark.parametrize("how", ["rejected", "503"])
@pytest.mark.parametrize("protect", [True, False])
def test_a_working_stop_cancelled_for_a_gapped_close_that_fails_is_put_back(world, protect, how):
    """Desk stop G at the saved level (it triggers and can't fill), desk stop W looser (still working); Coinbase open:
    the close cancels both, and fails. A stop goes back at W's level (on a protect-only run too: its client takes the
    level of a desk stop it cancelled this run), never at G's, which the price is through."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    _desk_stop(fake, 1, level * 0.90)
    fake.trade(level * 0.93)
    _close_fails(fake, how, 3)
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    assert fake.position() is not None
    kept = _working(fake)
    assert _cover(fake) == 1 and float(_sl(kept[0])["stop_price"]) == co.round_price(level * 0.90), \
        (r.safety_said, [(o["status"], _sl(o)) for o in fake.stops()])
    if protect:
        assert "the stop is back at 75,605 on 1 of the 1 contract(s) still held" in r.safety_said, r.safety_said
        assert r.safety_end == "protected"


@pytest.mark.parametrize("how", ["rejected", "503", "no_liquidity"])
def test_a_trail_move_whose_close_fails_puts_back_the_old_working_stop(world, how):
    """A full run. The desk's stop was trailed up to its saved level, but the old stop (0.90 of it) still rests (the
    move waited). A fresh trade between the two: the desk's stop is hit, so LiveBroker closes at market, cancel-first.
    The close doesn't go: the stop goes back at the old level, not at the desk's (the price is through it)."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    st = fake.stops()[0]
    old = co.round_price(level * 0.90)
    _sl(st)["stop_price"], _sl(st)["limit_price"] = str(old), str(co.round_price(old * 0.95))
    _next(fake)
    fake.trade(level * 0.95, dt=1)
    _close_fails(fake, how, 6)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job)
    assert fake.position() is not None
    kept = _working(fake)
    assert _cover(fake) == 1 and float(_sl(kept[0])["stop_price"]) == old, \
        ([(o["status"], _sl(o)) for o in fake.stops()], r.lines[-12:])
    assert not [t for t, _ in _urgent(sh, n0) if t == "BTC LIVE: NO STOP"]


# ---------------------------------------------------------------- the cut-back never trades a working stop for nothing

@pytest.mark.parametrize("protect", [True, False])
def test_the_cut_back_places_an_oversized_working_stop_again_at_its_right_size(world, protect):
    """Long 1 (you sold 2 of 3 in the app): desk stop G for 1 at the saved level (gapped), desk stop W for 2 looser
    (still working); Coinbase closed. G goes; W, too big alone, is cancelled and placed again for 1 at its own level
    (a stop-limit queues while Coinbase is closed). Before: W was cancelled and G kept, and at the reopen the long had
    no working stop. Keeping W as it is would flip the long at the reopen (no reduce-only)."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    w = _desk_stop(fake, 2, level * 0.90)
    fake.trade(level * 0.93)
    fake.close_market()
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    kept = _working(fake)
    assert len(kept) == 1 and _sl(kept[0])["base_size"] == "1" and \
        float(_sl(kept[0])["stop_price"]) == co.round_price(level * 0.90), (r.safety_said, [_sl(o) for o in kept])
    assert w["status"] == "CANCEL_QUEUED" and r.safety_end == "protected"
    gapped = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: STOP DIDN'T FILL"]
    assert gapped and "still rests and can still fill" in gapped[0]
    fake.reopen(price=level * 0.88)
    assert fake.position() is None          # closed by the stop placed again, not flipped short


@pytest.mark.parametrize("protect", [True, False])
def test_the_cut_back_tops_up_what_the_gapped_stop_covered_while_the_close_is_held_back(world, protect):
    """Long 2: desk stop G for 2 at the saved level (gapped), desk stop W for 1 looser (working); Coinbase closed. G
    goes, W stays, and a stop for the other contract is placed at W's level. Before: that contract had no stop through
    the reopen."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.positions[PRODUCT].n = 2
    _sl(fake.stops()[0])["base_size"] = "2"
    _desk_stop(fake, 1, level * 0.90)
    fake.trade(level * 0.93)
    fake.close_market()
    _next(fake)
    code, r, changed, saved = _live(job, over=PROTECT if protect else None)
    assert _cover(fake) == 2 and r.safety_end == "protected", (r.safety_said, [_sl(o) for o in _working(fake)])
    assert any("no working stop covered" in s for s in r.safety_said), r.safety_said
    fake.reopen(price=level * 0.88)
    assert fake.position() is None


# ---------------------------------------------------------------- never "protected" by default

@pytest.mark.parametrize("flat", [False, True])
def test_a_close_left_to_your_take_profit_is_no_stop_and_never_protected(world, flat):
    """Protect only. Your take-profit SELL limit covers the whole long, so the close isn't sent (no reduce-only): the
    desk's stop gapped (or, with flat_kill, gone), the position has no working stop: NO STOP, never 'protected'."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    if flat:
        fake.stops()[0]["status"] = "CANCELLED"
    _take_profit(fake)
    if not flat:
        fake.trade(level * 0.93)
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={**PROTECT, **(FLAT if flat else {})})
    assert fake.position() is not None and not _working(fake)
    _never_protected(sh, n0, r, True)
    assert "BTC LIVE: NO STOP" in [t for t, _ in _urgent(sh, n0)]


@pytest.mark.parametrize("flat", [False, True])
def test_a_gapped_stop_that_wont_cancel_is_pushed_and_never_protected(world, flat):
    """Protect only. The desk's stop triggered and can't fill; its cancel gets no answer, and it still reads OPEN: the
    close isn't sent beside it. Before: the pass ended 'protected' with nothing urgent from it."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.trade(level * 0.93)
    fake.fail_next("POST", f"{V3}/orders/batch_cancel", status=503, times=3)
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={**PROTECT, **(FLAT if flat else {})})
    assert fake.position() is not None and not _working(fake)
    _never_protected(sh, n0, r, True)
    assert r.safety_end == "unprotected"
    gapped = [b for t, b in _urgent(sh, n0) if t == "BTC LIVE: STOP DIDN'T FILL"]
    assert gapped and "won't cancel" in gapped[0], [t for t, _ in _urgent(sh, n0)]


# ---------------------------------------------------------------- the trade-scope client

@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        yield f


def test_a_trade_scope_client_sends_no_stop_limit_with_no_position_or_over_it(fake):
    client, token = _client(fake, ARM_ENV)
    assert token.scope == "trade" and fake.position() is None
    with pytest.raises(OrdersLocked, match="there is no position to protect"):
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("x", "BTC", 1), "BUY", 5000, fake.last * 1.01))
    fake.positions[PRODUCT] = Pos("LONG", 1, fake.last, fake.last)
    with pytest.raises(OrdersLocked, match="doesn't close a long position"):
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("x", "BTC", 2), "BUY", 1, fake.last * 1.01))
    with pytest.raises(OrdersLocked, match="more could open a position"):
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("x", "BTC", 3), "SELL", 2, fake.last * 0.9))
    assert fake.non_get() == []                     # before: the 5000-contract BUY rested on the fake
    client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("x", "BTC", 4), "SELL", 1, fake.last * 0.9))
    assert len(fake.stops()) == 1                   # the control: the stop the long needs goes


def test_a_trade_scope_client_cancels_only_the_desks_own_orders(fake):
    client, token = _client(fake, ARM_ENV)
    app = _take_profit(fake)
    with pytest.raises(OrdersLocked, match="isn't one of the desk's own orders"):
        client.post(f"{V3}/orders/batch_cancel", co.batch_cancel([app["order_id"]]))
    assert app["status"] == "OPEN" and fake.non_get() == []


# ---------------------------------------------------------------- Coinbase closed: no "closed at market"

def test_a_full_run_never_says_closed_at_market_while_coinbase_is_closed(world):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.trade(level * 0.93)                        # the stop triggers and can't fill
    fake.close_market()
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job)
    assert fake.position() is not None
    said = [b for t, b, *_ in sh.pushes[n0:] if "past its limit" in b and t == "BTC LIVE: ALERT"]
    assert said and all("so the position is closed at market" not in b for b in said), said
    assert "as soon as Coinbase takes market orders (it's closed now" in said[0], said
