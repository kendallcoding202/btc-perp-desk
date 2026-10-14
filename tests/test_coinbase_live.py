"""LIVE on Coinbase, LOCKED: LiveBroker (desk/brokers/live.py) driving CoinbaseExchange against the local fake Coinbase
(tests/fake_coinbase.py) through a client unlocked by coinbase_live.arm() with every check satisfied by the fake and a
test SHADOW record. Nothing here can reach Coinbase (tests/conftest.py), and no key is real.

The fake has no reduce-only, as Coinbase documents none: a stop resting with no position opens one when it fires, and
two full-size stops can flip a position. So every scenario ends with the check that matters most: flat means nothing
rests, and never more closing contracts rested than the position held (fake.max_closing == 0)."""

from __future__ import annotations

import json

import pytest

from desk import coinbase_live
from desk.brokers import coinbase as cb
from desk.brokers.coinbase_auth import load_cdp_key
from desk.brokers.live import LiveBroker, LiveSetupError
from desk.models import Order, Side
from desk.thresholds import COINBASE, params_for
from desk.venue import Rules
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, Pos, arm_rest, good_proof, make_ec_key, view_rest

ATR = 1_000.0


class Killed(BaseException):
    """The job killed mid-run (a timeout, a lost runner): no except clause in the desk catches it."""


@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        yield f


def broker(fake, tmp_path, rest=None, *, cap=1_000.0, alerts=None, rules=True, **kw):
    rest = rest or arm_rest(fake)
    ex = cb.CoinbaseExchange(rest)
    alerts = alerts if alerts is not None else []
    b = LiveBroker(params_for("BTC"), exchange=ex, state_path=tmp_path / "BTC" / "live_position.json",
                   max_notional_usd=cap, log=lambda *a: None, on_alert=alerts.append, clock=fake.clock_ms,
                   sleep=fake.sleep, rules=Rules(COINBASE) if rules else None, unexplained_wait_ms=3 * 3600_000,
                   provisional_tries=8, **kw)
    b.alerts_seen = alerts
    return b


def entry(qty=0.01, stop=80_000.0, close=83_000.0, side=Side.LONG):
    return Order("open", side, qty, "breakout", stop=stop, meta={"notional": qty * close, "leverage": 1.0})


def go_long(b, fake, **kw):
    b.submit(entry(**kw))
    b.execute_now(fake.last, fake.clock_ms())


def guard(b):
    """The runner's no-stop guard (desk/runner.py): a position with no stop gets one at 3 ATR straight away."""
    if b.position is not None and not b.position.stop:
        b.update_stop(b.position.entry_price - b.position.side.sign * 3 * ATR)


def stop_prices(fake):
    return [(o["side"], o["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"],
             o["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"],
             o["order_configuration"]["stop_limit_stop_limit_gtc"]["limit_price"]) for o in fake.stops()]


def posts(fake):
    return [r["path"].rsplit("/", 1)[-1] for r in fake.non_get()]


def safe(fake):
    p = fake.position()
    assert fake.max_closing <= 0, "more closing contracts rested than the position held"
    if p is None:
        assert fake.resting() == [], "flat with an order resting"
    else:
        assert len(fake.stops()) <= 1


# ---------------------------------------------------------------- it's locked

def test_coinbase_live_refuses_without_its_own_adapter_or_with_ccxt():
    with pytest.raises(LiveSetupError, match="LIVE on it is locked"):
        LiveBroker(params_for("BTC"), exchange=object(), state_path="/nonexistent/x.json")
    with pytest.raises(LiveSetupError, match="LIVE on it is locked"):
        LiveBroker(params_for("BTC"), state_path="/nonexistent/x.json")


def test_with_a_view_only_client_the_broker_reads_everything_and_sends_nothing(fake, tmp_path):
    b = broker(fake, tmp_path, view_rest(fake), allow_empty=True)
    b.submit(entry())
    with pytest.raises(Exception, match="orders are locked"):
        b.execute_now(fake.last, fake.clock_ms())
    assert fake.non_get() == [] and fake.position() is None


def test_an_empty_account_refuses_live_unless_shadow_asks_for_it(fake, tmp_path):
    fake.cbi = 0.0
    with pytest.raises(LiveSetupError, match="reads \\$0.00"):
        broker(fake, tmp_path)
    assert broker(fake, tmp_path / "s", view_rest(fake), allow_empty=True).cash == 0.0


@pytest.mark.parametrize("missing", ["phrase", "product", "gate", "shadow_days", "shadow_stale", "shadow_dirty",
                                     "no_deposit", "unfunded", "transfer", "no_trade", "usdc", "not_flat", "cap",
                                     "portfolio", "other_portfolio"])
def test_arm_refuses_for_each_missing_condition_and_sends_nothing(fake, missing):
    fk = make_ec_key("test-trade-key-not-real", trade=True, transfer=missing == "transfer")
    if missing == "no_trade":
        fk.trade = False
    if missing == "portfolio":
        fk.portfolio_type = "CONSUMER"
    fake.add_key(fk)
    env = dict(ARM_ENV)
    proof = good_proof(fake.clock_ms())
    gate = [("backtest: passes", True, "ok"), ("paper traded >= 14 days", True, "20 days")]
    cap = 1_000.0
    if missing == "phrase":
        env["DESK_LIVE_CONFIRM"] = "yes"
    if missing == "product":
        env["DESK_LIVE_COINBASE"] = "BIT-30OCT26-CDE"
    if missing == "gate":
        gate[1] = ("paper traded >= 14 days", False, "3 days")
    if missing == "shadow_days":
        proof["started_ts"] = fake.clock_ms() - 5 * 86400_000
    if missing == "shadow_stale":
        proof["last_run_ts"] = fake.clock_ms() - 3 * 3600_000
    if missing == "shadow_dirty":
        next(iter(proof["days"].values()))["unexplained"] = 1
    if missing == "no_deposit":
        proof["deposits_reconciled"] = 0
    if missing == "unfunded":
        for d in proof["days"].values():
            d["funded"] = False
    if missing == "other_portfolio":
        proof["portfolio_uuid"] = "99999999-2222-3333-4444-555555555555"
    if missing == "usdc":
        fake.usdc = 25.0
    if missing == "not_flat":
        fake.positions["ETP-20DEC30-CDE"] = Pos("LONG", 1, 2500.0, 2500.0)
    if missing == "cap":
        cap = 500.0
    out: list[str] = []
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), env=env, max_notional=cap, gate=gate, shadow_proof=proof,
                            base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep, out=out.append)
    assert got is None
    assert any(ln.lstrip().startswith("FAIL") for ln in out) and any("Not going live on Coinbase" in ln for ln in out)
    assert fake.non_get() == []
    if missing in ("phrase", "product", "gate", "shadow_days", "shadow_stale", "shadow_dirty", "no_deposit",
                   "unfunded"):
        assert fake.requests == []                          # a local check: not one request of any kind


def test_force_unproven_overrides_the_paper_gate_and_nothing_else(fake):
    fk = fake.add_key(make_ec_key("test-trade-key-not-real", trade=True))
    gate = [("paper traded >= 14 days", False, "3 days")]
    kw = dict(max_notional=1_000.0, gate=gate, force_unproven=True, base_url=fake.base_url, clock=fake.clock,
              sleep=fake.sleep, out=lambda *a: None)
    assert coinbase_live.arm(load_cdp_key(fk.file_json()), env=ARM_ENV, shadow_proof=good_proof(fake.clock_ms()),
                             **kw) is not None
    for env, proof in (({**ARM_ENV, "DESK_LIVE_CONFIRM": ""}, good_proof(fake.clock_ms())),
                       ({**ARM_ENV, "DESK_LIVE_COINBASE": ""}, good_proof(fake.clock_ms())), (ARM_ENV, None)):
        assert coinbase_live.arm(load_cdp_key(fk.file_json()), env=env, shadow_proof=proof, **kw) is None


def test_after_the_first_start_the_flat_check_and_the_proof_arent_asked_again(fake):
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
    fk = fake.add_key(make_ec_key("test-trade-key-not-real", trade=True))
    kw = dict(env=ARM_ENV, max_notional=1_000.0, gate=[], base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep,
              out=lambda *a: None)
    assert coinbase_live.arm(load_cdp_key(fk.file_json()), shadow_proof=good_proof(fake.clock_ms()), **kw) is None
    assert coinbase_live.arm(load_cdp_key(fk.file_json()), live_state={"armed_at": 1}, **kw) is not None


# ---------------------------------------------------------------- entries and their stop

def test_an_entry_is_whole_contracts_and_returns_only_with_its_stop_resting(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    p = fake.position()
    assert (p.side, p.n) == ("LONG", 1)
    assert stop_prices(fake) == [("SELL", "1", "80000", "76000")]
    assert posts(fake) == ["orders", "orders"]          # the entry, then its stop: nothing else
    assert b.position.qty == pytest.approx(0.01) and b.stop_px == 80_000.0
    entry_body = fake.non_get()[0]["body"]
    assert entry_body["order_configuration"] == {"market_market_ioc": {"base_size": "1"}}
    assert entry_body["client_order_id"] == b.ex.client_order_id("entry", (fake.clock_ms() // 7_200_000 - 1) *
                                                                  7_200_000)
    safe(fake)


def test_an_entry_is_sized_again_at_the_ask_when_it_goes_out(fake, tmp_path):
    fake.cbi = 1_300.0
    b = broker(fake, tmp_path)
    fake.trade(84_600.0)            # the desk decided 2 contracts at 83,000; the price ran 1,600 before the order
    b.submit(entry(qty=0.02))
    b.execute_now(fake.last, fake.clock_ms())
    assert fake.position().n == 1   # 5% of $1,300 = $65 of risk; one contract risks 0.01 x 4,605 = $46 at the ask
    safe(fake)


def test_an_entry_too_close_to_its_stop_now_or_with_no_fresh_price_isnt_sent(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake.trade(80_400.0)            # within half an ATR (1,000 / 2) of the 80,000 stop
    go_long(b, fake)
    assert fake.non_get() == [] and "half an ATR" in b.skipped["why"]
    fake.trade(83_000.0)
    fake.sleep(120)                 # no trade for two minutes: the best bid/ask read is older than a minute
    fake.last_trade_s -= 120
    real = fake._route
    fake._route = lambda m, p, q, b_, k: (200, {"pricebooks": [{"product_id": PRODUCT, "bids": [{"price": "82995"}],
                                                                "asks": [{"price": "83005"}], "time": "2026-10-13T"
                                                                "13:00:00Z"}]}) if p.endswith("best_bid_ask") \
        else real(m, p, q, b_, k)
    go_long(b, fake)
    assert fake.non_get() == [] and "no fresh best bid/ask" in b.skipped["why"]
    fake._route = real
    fake.close_market()
    b.ex.load_markets(reload=True)  # each run reads the product, and with it the session
    go_long(b, fake)
    assert fake.non_get() == [] and "closed" in b.skipped["why"]


def test_a_stop_that_never_rests_closes_the_position_and_asks_for_entry_kill(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake.band = 0.001               # every stop-limit's limit is out of the band, at 5%, 2% and 1%
    go_long(b, fake)
    assert fake.position() is None and b.position is None
    assert b.trades[-1].reason == "no_stop" and "entry_kill is on" in (b.entry_kill_why or "")
    safe(fake)


def test_an_entry_whose_answer_is_lost_is_found_adopted_and_protected_in_the_same_check(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake.fail_next("POST", f"{V3}/orders", status=None, after=True, times=3)     # every answer dropped
    go_long(b, fake)
    entries = [o for o in fake.orders.values() if "market_market_ioc" in o["order_configuration"]]
    assert len(entries) == 1 and fake.position().n == 1                          # one order, sent three times
    assert b.position is not None and stop_prices(fake) == [("SELL", "1", "80000", "76000")]
    assert any("answer was lost" in a for a in b.alerts_seen)
    safe(fake)


def test_sizes_that_disagree_between_fills_order_and_position_ask_for_entry_kill(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake.fill_units = "btc"         # fills report 0.01 where the order and the position say 1 contract
    go_long(b, fake)
    assert "sizes don't agree" in (b.entry_kill_why or "")
    safe(fake)


# ---------------------------------------------------------------- trailing: cancel first, never two stops

def test_a_trail_move_cancels_and_confirms_the_old_stop_before_placing_the_new(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(85_000.0)
    n = len(fake.requests)
    b.update_stop(82_000.0)
    seq = [(r["method"], r["path"].rsplit("/", 1)[-1]) for r in fake.requests[n:]]
    cancel = seq.index(("POST", "batch_cancel"))
    place = max(i for i, x in enumerate(seq) if x == ("POST", "orders"))
    assert cancel < place and ("GET", "batch") in seq[cancel:place]              # confirmed gone in between
    assert stop_prices(fake) == [("SELL", "1", "82000", "77900")]
    assert fake.max_stops == 1 and b.stop_px == 82_000.0
    safe(fake)


def test_a_cancel_that_finds_the_stop_filled_books_the_exit(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.on_cancel = lambda: (fake.trade(79_990.0, dt=1), fake.trade(81_500.0, dt=1))   # it fires as the cancel lands
    b.update_stop(81_000.0)
    assert fake.position() is None and b.position is None
    assert b.trades[-1].reason == "stop (exchange)" and b.trades[-1].exit_price == pytest.approx(fake.fills[1][
        "price"] and float(fake.fills[1]["price"]))
    safe(fake)


def test_a_cancel_that_fails_keeps_the_old_stop_and_the_move_is_retried_next_check(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(85_000.0)
    fake.fail_next("POST", f"{V3}/orders/batch_cancel", status=503, times=3)
    b.update_stop(82_000.0)
    assert stop_prices(fake) == [("SELL", "1", "80000", "76000")] and b.position.stop == 82_000.0
    assert any("still protects the position" in a for a in b.alerts_seen)
    b.sync(fake.last)
    assert stop_prices(fake) == [("SELL", "1", "82000", "77900")]
    safe(fake)


def test_a_new_level_the_price_is_already_through_closes_at_market(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(81_200.0)
    b.update_stop(81_500.0)
    assert fake.position() is None and b.trades[-1].reason == "stop"
    safe(fake)


def test_no_stop_moves_while_coinbase_is_closed_but_a_missing_stop_is_still_queued(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(85_000.0)
    fake.close_market()
    b.ex.load_markets(reload=True)
    b.update_stop(82_000.0)
    assert stop_prices(fake) == [("SELL", "1", "80000", "76000")]               # unmoved
    fake.orders[fake.stops()[0]["order_id"]]["status"] = "CANCELLED"            # gone by hand, say
    b.sync(fake.last)
    assert [o["status"] for o in fake.stops()] == ["QUEUED"]                    # a stop-limit queues while closed
    assert b.stop_px == 82_000.0
    safe(fake)


def test_not_enough_of_the_run_left_means_no_move_is_started(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(85_000.0)
    b.ex.rest.deadline_s = fake.now_s + 10
    n = len(fake.non_get())
    b.update_stop(82_000.0)
    assert len(fake.non_get()) == n and stop_prices(fake) == [("SELL", "1", "80000", "76000")]


def test_after_many_checks_exactly_one_stop_rests(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    for px in (83_500, 84_000, 83_800, 84_500, 85_000, 84_900):
        fake.trade(px)
        b.sync(fake.last)
        assert len(b._resting_stops("sell")) == 1
    assert len(fake.stops()) == 1 and fake.max_stops == 1
    safe(fake)


def test_a_queued_stop_counts_as_resting_and_isnt_doubled(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.close_market()
    stop = fake.stops()[0]
    stop["status"] = "QUEUED"
    b.sync(fake.last)
    assert len(fake.stops()) == 1 and b.stop_order_id == stop["order_id"]


# ---------------------------------------------------------------- exits

def test_a_close_cancels_the_stop_first_then_closes_the_position(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(84_000.0)
    t = b.close_now(fake.last, fake.clock_ms(), "trend_flip")
    assert posts(fake)[-2:] == ["batch_cancel", "close_position"]
    assert fake.position() is None and fake.resting() == []
    assert t.reason == "trend_flip" and t.exit_price == pytest.approx(fake.bid) and t.qty == pytest.approx(0.01)
    assert t.fees == pytest.approx(0.415 + max(0.20, 0.0005 * 0.01 * fake.bid), abs=0.01)
    safe(fake)


def test_a_close_that_fails_puts_a_stop_back_on_what_is_still_open(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.fail_next("POST", f"{V3}/orders/close_position", status=503, times=3)
    with pytest.raises(Exception):
        b.close_now(fake.last, fake.clock_ms(), "trend_flip")
    assert fake.position().n == 1 and stop_prices(fake) == [("SELL", "1", "80000", "76000")]
    assert b.close_wanted == "trend_flip"
    t = b.retry_close(fake.last, fake.clock_ms())
    assert t is not None and fake.position() is None
    safe(fake)


def test_a_close_that_went_through_though_its_answer_was_lost_is_booked_and_leaves_nothing(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.fail_next("POST", f"{V3}/orders/close_position", status=None, times=3, after=True)
    t = b.close_now(fake.last, fake.clock_ms(), "daily_loss")
    assert fake.position() is None and fake.resting() == [] and b.position is None
    assert t is not None and t.reason == "daily_loss"
    safe(fake)


def test_a_close_that_fills_only_partly_gets_a_stop_on_the_rest(fake, tmp_path):
    fake.cbi = 5_000.0
    b = broker(fake, tmp_path, cap=5_000.0)
    go_long(b, fake, qty=0.03)
    assert fake.position().n == 3
    fake.liquidity = 1
    with pytest.raises(RuntimeError, match="left 0.02"):
        b.close_now(fake.last, fake.clock_ms(), "trend_flip")
    assert fake.position().n == 2 and stop_prices(fake) == [("SELL", "2", "80000", "76000")]
    fake.liquidity = None
    b.retry_close(fake.last, fake.clock_ms())
    assert fake.position() is None and b.trades[-1].qty == pytest.approx(0.03)
    safe(fake)


def test_a_close_while_coinbase_is_closed_waits_for_the_reopen(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.close_market()
    b.ex.load_markets(reload=True)
    n = len(fake.non_get())
    assert b.close_now(fake.last, fake.clock_ms(), "trend_flip") is None
    assert len(fake.non_get()) == n and b.close_wanted == "trend_flip" and len(fake.stops()) == 1
    fake.reopen()
    b.ex.load_markets(reload=True)
    assert b.retry_close(fake.last, fake.clock_ms()) is not None and fake.position() is None
    safe(fake)


def test_a_stop_gapped_past_its_limit_is_closed_at_market(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(75_000.0)            # through the 80,000 trigger and the 76,000 limit at once: triggered, unfilled
    assert fake.position().n == 1 and fake.stops()[0]["trigger_status"] == "STOP_TRIGGERED"
    b.sync(fake.last)
    assert fake.position() is None and b.trades[-1].reason == "stop"
    assert any("past its limit" in a for a in b.alerts_seen)
    safe(fake)


def test_a_stop_that_fills_while_away_is_booked_at_its_fill(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(79_900.0)
    assert fake.position() is None
    b.sync(fake.last)
    t = b.trades[-1]
    assert t.reason == "stop (exchange)" and t.exit_price == pytest.approx(79_895.0)
    safe(fake)


# ---------------------------------------------------------------- what's the desk's

def test_a_stop_left_resting_with_no_position_is_cancelled_now(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": "left-behind"}, "stop",
                {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "80000", "limit_price": "76000",
                                               "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}, "OPEN")
    b.sync(fake.last)
    assert fake.resting() == [] and b.entry_kill_why is None


def test_a_stray_stop_that_cant_be_cancelled_is_the_urgent_alert(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": "left-behind"}, "stop",
                {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "80000", "limit_price": "76000",
                                               "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}, "OPEN")
    fake.fail_next("POST", f"{V3}/orders/batch_cancel", status=503, times=50)
    b.sync(fake.last)
    assert b.entry_kill_why.startswith("STOP WITH NO POSITION") and "would open a position" in b.entry_kill_why


def test_a_position_opened_by_something_other_than_a_desk_entry_is_closed_and_entries_stop(fake, tmp_path):
    b = broker(fake, tmp_path)
    o = fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": "by-hand"}, "market",
                    {"market_market_ioc": {"base_size": "2"}}, "OPEN")
    fake._fill(o, 2, fake.bid)                       # a short the desk never opened
    b.sync(fake.last)
    assert fake.position() is None and b.trades[-1].reason == "not_the_desks"
    assert "not one of the desk's entries" in b.entry_kill_why
    safe(fake)


def test_positions_or_orders_on_another_product_block_entries_and_are_never_touched(fake, tmp_path):
    b = broker(fake, tmp_path)
    fake.positions["ETP-20DEC30-CDE"] = Pos("LONG", 1, 2_500.0, 2_500.0)     # opened after LIVE armed, by hand
    b.sync(fake.last)
    go_long(b, fake)
    assert fake.position() is None and fake.non_get() == [] and "not the desk's" in b.skipped["why"]
    assert any("never touches" in a for a in b.alerts_seen)
    assert fake.positions["ETP-20DEC30-CDE"].n == 1


def test_a_balance_that_doesnt_add_up_blocks_entries_but_not_the_stop(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.orders[fake.stops()[0]["order_id"]]["status"] = "CANCELLED"
    real = fake._balance

    def odd():
        st, out = real()
        out["balance_summary"]["available_margin"] = fake._amt(5_000.0)
        return st, out
    fake._balance = odd
    b.sync(fake.last)
    assert len(fake.stops()) == 1                            # the safety pass ran whatever the balance said
    assert "doesn't add up" in b.ex.entry_block()


# ---------------------------------------------------------------- killed after any POST: the next run is safe

def _kill_after(monkeypatch, rest, path_end, nth=1):
    real, seen = rest.post, [0]

    def post(path, body):
        out = real(path, body)
        if path.endswith(path_end):
            seen[0] += 1
            if seen[0] == nth:
                raise Killed(path)
        return out
    monkeypatch.setattr(rest, "post", post)


def _restart(fake, tmp_path, saved: bytes | None):
    """The next run: a new client and broker, from the state the last finished run saved (a killed run saves
    nothing on Actions)."""
    f = tmp_path / "BTC" / "live_position.json"
    if saved is None:
        f.unlink(missing_ok=True)
    else:
        f.write_bytes(saved)
    b = broker(fake, tmp_path, arm_rest(fake, live_state={"armed_at": 1}))
    guard(b)
    return b


def _one_stop_for_the_position(fake):
    p = fake.position()
    entries = [o for o in fake.orders.values() if "market_market_ioc" in o["order_configuration"]
               and not o["client_order_id"].startswith(("by-hand",)) and o["side"] == "BUY"]
    assert len(entries) == 1, "a second entry went out"
    if p is None:
        assert fake.resting() == []
    else:
        st = fake.stops()
        assert len(st) == 1 and int(st[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"]) == p.n
    assert fake.max_closing <= 0


@pytest.mark.parametrize("when", ["entry", "stop", "trail-cancel", "trail-place", "close-cancel", "close"])
def test_a_run_killed_after_any_post_leaves_the_next_run_with_exactly_one_stop(fake, tmp_path, monkeypatch, when):
    b = broker(fake, tmp_path)
    f = tmp_path / "BTC" / "live_position.json"
    saved = f.read_bytes()
    rest = b.ex.rest
    if when == "entry":
        _kill_after(monkeypatch, rest, "/orders", 1)
        with pytest.raises(Killed):
            go_long(b, fake)
    elif when == "stop":
        _kill_after(monkeypatch, rest, "/orders", 2)
        with pytest.raises(Killed):
            go_long(b, fake)
    else:
        go_long(b, fake)
        saved = f.read_bytes()
        fake.trade(85_000.0)
        if when.startswith("trail"):
            _kill_after(monkeypatch, rest, "/batch_cancel" if when == "trail-cancel" else "/orders")
            with pytest.raises(Killed):
                b.update_stop(82_000.0)
        else:
            _kill_after(monkeypatch, rest, "/batch_cancel" if when == "close-cancel" else "/close_position")
            with pytest.raises(Killed):
                b.close_now(fake.last, fake.clock_ms(), "trend_flip")
    monkeypatch.undo()
    b2 = _restart(fake, tmp_path, saved if when not in ("entry", "stop") else None)
    _one_stop_for_the_position(fake)
    assert (b2.position is None) == (fake.position() is None)
    # the desk's own position is kept (adopted from its entry's id when the state didn't have it), never closed as
    # someone else's; only the close that went through leaves it flat
    assert (fake.position() is None) == (when == "close")
    assert not any(t.reason == "not_the_desks" for t in b2.trades) and b2.entry_kill_why is None
    b3 = _restart(fake, tmp_path, f.read_bytes())            # and the run after that changes nothing
    _one_stop_for_the_position(fake)
    assert (b3.position is None) == (fake.position() is None)


def test_a_reused_stop_id_whose_order_was_cancelled_takes_the_next_one(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    oid = fake.stops()[0]["order_id"]
    fake.orders[oid]["status"] = "CANCELLED"
    b.order_seq = 0                                          # as if the state that counted past it were lost
    b.sync(fake.last)
    assert len(fake.stops()) == 1 and fake.stops()[0]["order_id"] != oid and b.order_seq >= 2


def test_the_state_file_is_written_whole(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    f = tmp_path / "BTC" / "live_position.json"
    d = json.loads(f.read_text())
    assert d["symbol"] == PRODUCT and d["position"]["qty"] == pytest.approx(0.01) and d["order_seq"] >= 1
    assert not (tmp_path / "BTC" / "live_position.json.tmp").exists()


# ---------------------------------------------------------------- the account's value: settlement, sweeps, holds

def test_an_exit_and_the_settlement_after_it_dont_look_like_money_moved(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.trade(85_000.0)
    b.sync(fake.last)
    b.close_now(fake.last, fake.clock_ms(), "trend_flip")
    assert b.balance_confirmed and b.flow_gap is None and b.ex.balance_block is None
    fake.settle()                                           # 3:00 AM ET: today's P&L moves into the futures account
    fake.sleep(3600)
    b.sync(fake.last)
    assert b.balance_confirmed and b.flow_gap is None and b.ex.balance_block is None
    assert b.cash == pytest.approx(fake.equity())


def test_a_settlement_with_a_position_open_and_a_sweep_in_transit_change_nothing(fake, tmp_path):
    b = broker(fake, tmp_path)
    go_long(b, fake)
    fake.margin_transfer(300.0)
    fake.trade(84_000.0)
    fake.charge_funding(0.0001)
    fake.settle()
    fake.sweep_start(100.0)
    b.sync(fake.last)
    assert b.balance_confirmed and b.flow_gap is None and b.ex.balance_block is None
    fake.sweep_arrive()                                     # derivatives_settlement on the USD wallet: inside
    b.sync(fake.last)
    assert b.balance_confirmed and b.flow_gap is None and b.flows == []


# ---------------------------------------------------------------- deposits and withdrawals

def _flat(fake, tmp_path, **kw):
    flows: list[dict] = []
    b = broker(fake, tmp_path, view_rest(fake), allow_empty=True, **kw)
    b.on_flow = flows.append
    return b, flows


def test_a_deposit_and_a_withdrawal_are_read_once_each_with_their_sign(fake, tmp_path):
    b, flows = _flat(fake, tmp_path)
    fake.deposit(500.0)
    b.sync(fake.last)
    b.sync(fake.last)
    fake.withdraw(100.0, signed=True)
    fake.withdraw(50.0, signed=False, fee=1.0)
    b.sync(fake.last)
    assert [(f["amount"], f["what"]) for f in flows] == [(500.0, "bank deposit"), (-100.0, "bank withdrawal"),
                                                         (-51.0, "bank withdrawal")]
    assert b.flow_gap is None and b.cash == pytest.approx(1_349.0)


def test_a_pending_deposit_explains_only_a_move_that_matches_it_and_is_counted_once(fake, tmp_path):
    b, flows = _flat(fake, tmp_path)
    tx = fake.deposit(200.0, status="pending", credited=False)     # pending, not in the balance yet
    b.sync(fake.last)
    assert flows == [] and b.balance_confirmed
    fake.set_status(tx, "pending", credit=200.0)                   # credited while still pending
    b.sync(fake.last)
    assert [f["amount"] for f in flows] == [200.0] and b.balance_confirmed
    fake.set_status(tx, "completed")
    b.sync(fake.last)
    assert [f["amount"] for f in flows] == [200.0]                 # completed later: not again


def test_a_pending_deposit_that_doesnt_match_the_move_isnt_used(fake, tmp_path):
    b, flows = _flat(fake, tmp_path)
    fake.deposit(200.0, status="pending", credited=False)
    fake.cbi += 150.0                                              # something else moved 150
    b.sync(fake.last)
    assert flows == [] and not b.balance_confirmed


def test_usdc_sold_for_margin_is_money_in_not_profit(fake, tmp_path):
    fake.usdc = 100.0
    b, flows = _flat(fake, tmp_path)
    fake.usdc_sell(60.0)
    b.sync(fake.last)
    assert [f["amount"] for f in flows] == [60.0] and "not profit" in flows[0]["what"]


def test_a_move_nothing_explains_waits_three_hours_across_runs_then_stops_entries(fake, tmp_path):
    b, flows = _flat(fake, tmp_path)
    fake.spot_trade(80.0)                                          # a spot buy: a type the desk doesn't count
    b.sync(fake.last)
    assert not b.balance_confirmed and b.flow_gap is None
    assert any("doesn't know" in a for a in b.alerts_seen)
    for _ in range(2):                                             # the next runs, an hour apart
        fake.sleep(3600)
        b, flows = _flat(fake, tmp_path)
        assert b.flow_gap is None and not b.balance_confirmed
    fake.sleep(3600 + 60)
    b, flows = _flat(fake, tmp_path)
    assert b.flow_gap and "isn't explained" in b.flow_gap and flows == []


def test_a_deposit_on_an_ach_hold_counts_but_entries_are_sized_on_what_can_be_margin(fake, tmp_path):
    fake.cbi = 300.0
    b = broker(fake, tmp_path)
    flows: list[dict] = []
    b.on_flow = flows.append
    fake.deposit(700.0, hold=True)
    b.sync(fake.last)
    assert [f["amount"] for f in flows] == [700.0] and b.cash == pytest.approx(1_000.0)
    assert b.ex.usable == pytest.approx(300.0)
    b.submit(entry(stop=81_200.0))
    b.execute_now(fake.last, fake.clock_ms())
    assert fake.position() is None and "sized again" in b.skipped["why"]      # $300 can't hold one at 5% risk


def test_a_deposit_read_late_with_a_position_open_is_priced_at_its_own_time(fake, tmp_path):
    b = broker(fake, tmp_path)
    flows: list[dict] = []
    b.on_flow = flows.append
    go_long(b, fake)
    fake.trade(84_000.0)
    fake.deposit(250.0, dt=5)
    fake.trade(85_000.0, dt=900)                    # read 15 minutes later, the price 1,000 higher
    b.sync(fake.last)
    assert [f["amount"] for f in flows] == [250.0] and flows[0]["priced"] == "at its time"
    assert b.flow_gap is None and b.balance_confirmed
