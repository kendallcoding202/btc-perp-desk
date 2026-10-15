"""LIVE's safety pass never cancels a stop for a close that may not go through, and says what it did as it happened;
the order client's protect-only check reads every open order and takes only the desk's own shapes; the documented
LIVE step can't be stopped before its run by a mistyped variable; a read that fails while arming is named as that;
a run refused over the clock still looks. Each test fails on the code before it (da2c7f8):

- a flat_kill while Coinbase was closed, its product unreadable, cancelled the stop (CANCEL_QUEUED) and the close was
  refused: the position reached the reopen with no stop, pushed as "protected";
- a slow Coinbase: the stop cancelled at 208 s, the close cut off past the deadline, no stop and NO STOP;
- a close that filled 1 of 2 contracts was said as "closed the long 2", the rest left with no stop;
- two full-size desk stops on one contract stayed resting on a protect-only run, and the watchdog had no check for it;
- the protect client counted only OPEN orders from the API: a QUEUED stop or the app's let a second full-size stop
  through, and a close with a full-size stop resting got through and opened a short when the stop fired;
- --max-notional "" (a deleted variable), "1,000" or "$1000" exited on argument parsing: no safety pass, no push;
- a 503 on /accounts was pushed and published as "USDC is in the portfolio";
- a run 45 s ahead of Coinbase's clock: the watchdog signed on that clock, got 401, and pushed nothing urgent.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import pytest

from desk import coinbase_run as cr
from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase_rest import OrdersLocked
from fake_coinbase import ARM_ENV, PRODUCT, V3, Pos
from test_coinbase_kills_fail_closed import _next, world  # noqa: F401
from test_coinbase_protect_only import _armed_long, _live, _protect_client, _protect_pushes, fake_cb  # noqa: F401
from test_coinbase_shadow import IT


def _fail_product_read(fake, tries=None):
    """GET /products/BIP-20DEC30-CDE answers 503 (the ticker and the candles still answer): every time, or only on
    the reads numbered in `tries` (1 is the first this run)."""
    orig, seen = fake._route, {"n": 0}

    def route(method, path, q, body, key):
        if method == "GET" and path == f"{V3}/products/{PRODUCT}":
            seen["n"] += 1
            if tries is None or seen["n"] in tries:
                return 503, {"error": "UNAVAILABLE", "message": "unavailable"}
        return orig(method, path, q, body, key)
    fake._route = route
    return lambda: setattr(fake, "_route", orig)


def _stop_statuses(fake):
    return [o["status"] for o in fake.orders.values() if "stop_limit_stop_limit_gtc" in o["order_configuration"]]


def _urgent(sh, n0):
    return [(t, b) for t, b, p, *_ in sh.pushes[n0:] if p == "urgent"]


def _over(fake):
    """Closing-side stop contracts resting beyond the position (with no reduce-only, that much opens the other way)."""
    p = fake.position()
    held = p.n if p else 0
    closing = "SELL" if (p and p.side == "LONG") else "BUY"
    return sum(int(o["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"]) for o in fake.stops()
               if not p or o["side"] == closing) - held


# ---------------------------------------------------------------- a closure: the stop stays

@pytest.mark.parametrize("protect", [True, False])
def test_flat_kill_while_closed_and_the_product_unreadable_keeps_the_stop_to_the_reopen(world, protect):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.close_market()
    undo = _fail_product_read(fake, None if protect else {2, 3, 4, 5})     # full run: only the safety pass's reads
    _next(fake)
    n0 = len(sh.pushes)
    over = {"DESK_COINBASE_KILL": "flat", **({"DESK_LIVE_CONFIRM": None} if protect else {})}
    code, r, changed, saved = _live(job, over=over)
    undo()
    assert "CANCEL_QUEUED" not in _stop_statuses(fake), r.safety_said          # before: cancelled, close refused
    assert any("isn't sent, and nothing is cancelled for it" in s for s in r.safety_said), r.safety_said
    if protect:
        got = _protect_pushes(sh, n0)
        assert len(got) == 1 and "nothing it could read to do" not in got[0][1]
        assert "the stop stays meanwhile" in got[0][1] and "the open position is protected" in r.public_line()
    fake.reopen()
    assert fake.position() is not None and fake.stops(), (_stop_statuses(fake), r.safety_said)


def test_flat_kill_over_two_closed_runs_then_a_gap_at_the_reopen_still_finds_the_stop(world):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.close_market()
    undo = _fail_product_read(fake)
    _next(fake)
    _live(job, over={"DESK_COINBASE_KILL": "flat"})
    undo()
    _next(fake)
    code, r, changed, saved = _live(job, over={"DESK_COINBASE_KILL": "flat"})
    assert _stop_statuses(fake) == ["OPEN"], r.safety_said                    # before: CANCEL_QUEUED, "stays"
    fake.reopen(price=level * 0.90)
    assert fake.position() is None or fake.stops()


def test_a_close_refused_after_its_stop_was_cancelled_puts_the_stop_back(world):
    """The product reads open, the cancel goes through, and Coinbase then refuses the close (its session closed in
    between): the stop goes back at its trigger, and the run says so, never "closed"."""
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.on_cancel = fake.close_market                      # Coinbase closes as the stop is cancelled
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={"DESK_COINBASE_KILL": "flat", "DESK_LIVE_CONFIRM": None})
    assert fake.position() is not None
    fake.reopen()
    stops = fake.stops()
    assert len(stops) == 1 and float(stops[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"]) == \
        co.round_price(level), (_stop_statuses(fake), r.safety_said)        # before: no stop at the reopen
    assert any("the stop is back at" in s for s in r.safety_said), r.safety_said
    assert not any(s.startswith("closed") or "reads the long" in s for s in r.safety_said)
    assert "the open position is protected" in r.public_line()
    got = _protect_pushes(sh, n0)
    assert got and "the stop is back at" in got[0][1]


# ---------------------------------------------------------------- a slow Coinbase: no close it can't finish

@pytest.mark.parametrize("delay", [12.0, 16.0, 20.0, 25.0])
@pytest.mark.parametrize("protect", [True, False])
def test_a_slow_coinbase_never_starts_a_flat_kill_close_it_cant_finish(world, delay, protect):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    orig = fake._route

    def route(method, path, q, body, key):
        fake.now_s += delay
        return orig(method, path, q, body, key)
    fake._route = route
    _next(fake)
    n0 = len(sh.pushes)
    over = {"DESK_COINBASE_KILL": "flat", **({"DESK_LIVE_CONFIRM": None} if protect else {})}
    code, r, changed, saved = _live(job, over=over)
    fake._route = orig
    urgent = _urgent(sh, n0)
    if fake.position() is not None:
        assert fake.stops(), ("left with no stop", r.safety_said, urgent)          # before: none (16 s and more)
        assert any(t == "BTC LIVE: FLAT_KILL NOT DONE" for t, _ in urgent), urgent


# ---------------------------------------------------------------- a close that fills only in part

@pytest.mark.parametrize("held", [2, 3])
def test_a_flat_kill_close_that_fills_in_part_is_said_so_and_the_rest_is_closed_or_protected(world, held):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    fake.position().n = held
    fake.stops()[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"] = str(held)
    fake.liquidity = 1                               # one contract at market per order
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={"DESK_COINBASE_KILL": "flat", "DESK_LIVE_CONFIRM": None})
    fake.liquidity = None
    assert not any(f"closed the long {held}" in s for s in r.safety_said)      # before: said closed, 1 left bare
    assert any(f"took 1 of {held}" in s for s in r.safety_said), r.safety_said
    left = fake.position()
    if held == 2:
        assert left is None and fake.resting() == []                         # the rest closed on a second try
    else:
        assert left is not None and left.n == 1
        stops = fake.stops()
        assert len(stops) == 1 and stops[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"] == "1"
        assert float(stops[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"]) == \
            co.round_price(level)
    assert fake.max_closing <= 0
    got = _protect_pushes(sh, n0)
    assert got and f"took 1 of {held}" in got[0][1]


# ---------------------------------------------------------------- desk stops covering more than the position

@pytest.mark.parametrize("protect", [True, False])
def test_two_full_size_desk_stops_on_one_position_are_cut_back_to_the_position(world, protect):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    body = co.stop_limit(co.client_order_id("stop", "BTC", "dup", 1), "SELL", 1, level - 100)
    fake._order(body, "stop", body["order_configuration"], "OPEN")       # a second desk stop, looser
    assert _over(fake) == 1
    _next(fake)
    code, r, changed, saved = _live(job, over={"DESK_LIVE_CONFIRM": None} if protect else None)
    assert _over(fake) <= 0 and len(fake.stops()) == 1, (r.safety_said, r.safety_state)
    assert float(fake.stops()[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"]) == \
        co.round_price(level)                                                # the tighter one kept
    if protect:
        assert any("closing more than" in s for s in r.safety_said), r.safety_said


def test_the_watchdog_pushes_closing_orders_that_cover_more_than_the_position(world):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    body = co.stop_limit("app-placed-not-a-desk-id", "SELL", 1, level - 100)
    o = fake._order(body, "stop", body["order_configuration"], "OPEN")
    o["order_placement_source"] = "RETAIL_SIMPLE"                        # yours, in the app: never touched
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, over={"DESK_LIVE_CONFIRM": None})    # protect only: then the watchdog
    assert code != 0 and len(fake.stops()) == 2                             # the desk never touches yours
    assert "BTC LIVE: STOPS COVER MORE THAN THE POSITION" in [t for t, _ in _urgent(sh, n0)], sh.pushes[n0:]


# ---------------------------------------------------------------- the order client's own check

def test_the_protect_client_counts_a_stop_queued_while_closed(fake_cb):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    client, _ = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    fake.close_market()
    client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 1), "SELL", 2, 80_000.0))
    assert [o["status"] for o in fake.stops()] == ["QUEUED"]
    with pytest.raises(OrdersLocked, match="already rest"):                  # before: let through, 4 on 2 held
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 2), "SELL", 2, 80_000.0))


def test_the_protect_client_counts_a_closing_order_placed_in_the_app(fake_cb):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    client, _ = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    body = co.stop_limit("app-placed-not-a-desk-id", "SELL", 2, 79_000.0)
    o = fake._order(body, "stop", body["order_configuration"], "OPEN")
    o["order_placement_source"] = "RETAIL_SIMPLE"
    with pytest.raises(OrdersLocked, match="already rest"):                  # before: let through
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 3), "SELL", 2, 80_000.0))


def test_the_protect_client_refuses_a_close_that_would_leave_a_stop_bigger_than_the_position(fake_cb):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    client, _ = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 4), "SELL", 2, 80_000.0))
    with pytest.raises(OrdersLocked, match="would leave them to open a position"):     # before: closed, then a short
        client.post(f"{V3}/orders/close_position", co.close_position(co.client_order_id("c", "BTC", 4), 2))
    assert fake.position().n == 2


@pytest.mark.parametrize("bad", ["extra_key", "attached", "direction", "limit_above", "limit_far", "not_desk_cid",
                                 "cancel_extra"])
def test_the_protect_client_takes_only_the_shapes_the_desk_builds(fake_cb, bad):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    client, _ = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    body = co.stop_limit(co.client_order_id("s", "BTC", bad), "SELL", 1, 80_000.0)
    sl = body["order_configuration"]["stop_limit_stop_limit_gtc"]
    path = f"{V3}/orders"
    if bad == "extra_key":
        body["leverage"] = "5"
    elif bad == "attached":
        body["attached_order_configuration"] = {"market_market_ioc": {"base_size": "5"}}
    elif bad == "direction":
        sl["stop_direction"] = "STOP_DIRECTION_STOP_UP"
    elif bad == "limit_above":
        sl["limit_price"] = "81000"
    elif bad == "limit_far":
        sl["limit_price"] = "60000"
    elif bad == "not_desk_cid":
        body["client_order_id"] = "hand-made-id"
    elif bad == "cancel_extra":
        path, body = f"{V3}/orders/batch_cancel", {"order_ids": ["x"], "product_id": PRODUCT}
    with pytest.raises(OrdersLocked, match="protect only"):                  # before: let through (all but cancels)
        client.post(path, body)
    assert fake.non_get() == []


def test_the_protect_client_still_takes_the_desks_own_stop_close_and_cancel(fake_cb):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    client, _ = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    res = client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("s", "BTC", 9), "SELL", 2, 80_000.0))
    oid = res["success_response"]["order_id"]
    client.post(f"{V3}/orders/batch_cancel", co.batch_cancel([oid]))
    client.post(f"{V3}/orders/close_position", co.close_position(co.client_order_id("c", "BTC", 9), 2))
    assert fake.position() is None and fake.resting() == []


# ---------------------------------------------------------------- the documented LIVE step, through the real CLI

@pytest.mark.parametrize("value", ["", "1,000", "$1000", "not a number", " 1000 "])
def test_the_documented_live_step_runs_its_safety_pass_whatever_max_notional_says(world, monkeypatch, value):
    from desk import __main__ as cli

    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.realtime = True                            # the CLI signs on this machine's clock
    br.load(sh.dir)
    env = {**sh.env, **ARM_ENV, "COINBASE_CDP_KEY": job.tk.file_json(), "DESK_COINBASE_TEST_URL": fake.base_url,
           "GITHUB_ACTIONS": "true", "DESK_COINBASE_KILL": "flat"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("NTFY_TOPIC", "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)
    n = len(fake.requests)
    code = cli.main(["live", "--coin", "BTC", "--once", "--max-notional", value, "--state-dir", str(sh.dir)])
    assert len(fake.requests) > n and fake.position() is None, (value, code)   # before: exit 2, no request at all


def test_max_notional_is_read_leniently():
    from desk.__main__ import max_notional

    assert [max_notional(v) for v in ("1000", "1,000", "$1,000.50", " 1_000 ", 50.0)] == [1000.0, 1000.0, 1000.5,
                                                                                          1000.0, 50.0]
    assert [max_notional(v) for v in ("", None, "abc", "0", "-5", "nan", "inf", "1.2.3")] == [None] * 8


def test_a_go_live_gate_that_cant_be_read_is_a_gate_that_doesnt_pass_never_a_crash(world, monkeypatch, capsys):
    """The gate is read inside the run, at a first start only: an armed LIVE never reads it, and a first start with
    a report that can't be read refuses with every check printed, not a traceback before the run."""
    from desk import __main__ as cli

    sh, fake, br, job = world

    def broken(p):
        raise ValueError("the report isn't JSON")
    monkeypatch.setattr(cli, "promotion_status", broken)
    fake.realtime = True
    br.load(sh.dir)
    env = {**sh.env, **ARM_ENV, "COINBASE_CDP_KEY": job.tk.file_json(), "DESK_COINBASE_TEST_URL": fake.base_url,
           "GITHUB_ACTIONS": "true"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("NTFY_TOPIC", "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)
    code = cli.main(["live", "--coin", "BTC", "--once", "--max-notional", "1000", "--state-dir", str(sh.dir)])
    out = capsys.readouterr().out
    assert code == 2 and "Traceback" not in out and "LIVE BTC: not armed" in out, out
    assert fake.non_get() == []


# ---------------------------------------------------------------- a read that fails while arming

@pytest.mark.parametrize("path,wrong,said", [
    (f"{V3}/accounts", "USDC is in the portfolio", "the portfolio's accounts couldn't be read to check for USDC"),
    (f"{V3}/cfm/balance_summary", "Coinbase's balance doesn't add up", "Coinbase's balance couldn't be read"),
    (f"{V3}/best_bid_ask", "--max-notional doesn't buy one contract",
     "Coinbase's price couldn't be read to check --max-notional"),
])
def test_a_read_that_fails_while_arming_is_named_as_the_read_not_the_check(world, path, wrong, said):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.fail_next("GET", path, 503, times=4)
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job)
    assert wrong not in r.protect_failed and said in r.protect_failed, r.protect_failed     # before: `wrong`
    got = _protect_pushes(sh, n0)
    assert got and said in got[0][1] and wrong not in got[0][1]
    assert said in job.status()["note"] and wrong not in r.public_line()


# ---------------------------------------------------------------- a run refused over the clock still looks

def test_a_run_refused_over_the_clock_still_pushes_what_cant_wait(world):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.trade(fake.last * 0.85)                    # the stop triggered, unfilled
    _next(fake)
    br.load(sh.dir)
    if job.out.exists():
        job.out.unlink()
    env = {**sh.env, "GITHUB_OUTPUT": str(job.out), **ARM_ENV, "COINBASE_CDP_KEY": job.tk.file_json(),
           "DESK_COINBASE_KILL": "flat"}
    n0, q0 = len(sh.pushes), len(fake.requests)
    code, r = cr.run_live_once(sh.dir, env, max_notional=1_000.0, gate=[("backtest: passes", True, "ok")],
                               base_url=fake.base_url, clock=lambda: fake.clock() + 45.0, sleep=fake.sleep,
                               push=lambda *a: sh.pushes.append(a), mask_emit=lambda s: None, iterations=IT)
    assert code == 2 and all(q["method"] == "GET" for q in fake.requests[q0:])
    titles = [t for t, _ in _urgent(sh, n0)]
    assert "BTC LIVE: STOP DIDN'T FILL" in titles and "BTC LIVE: FLAT_KILL NOT DONE" in titles, titles  # before: none


# ---------------------------------------------------------------- the README's LIVE edit

def test_the_readmes_live_edit_runs_on_the_trade_key_and_nothing_before_the_run_can_stop_it():
    """Before: the job stayed gated on the View key (deleting it, as the guide said to stop SHADOW, skipped LIVE with a
    position open), and the paper ledger's fetch had no timeout and could fail the step before LIVE ran."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "SHADOW_ON: ${{ secrets.COINBASE_CDP_TRADE_KEY != '' && secrets.DESK_STATE_KEY != '' }}" in readme
    step = readme[readme.index("- name: LIVE run (real orders)"):]
    step = step[:step.index("```")]
    fetch = next(ln.strip() for ln in step.splitlines() if "git fetch" in ln)
    assert fetch.startswith("timeout 60 git fetch") and fetch.endswith("|| true"), fetch
    guide = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    assert "the View key no longer starts or stops anything" in guide and "Never with a position open" in guide
