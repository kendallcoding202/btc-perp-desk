import json
import math
import random
import threading
import urllib.request
from pathlib import Path

import pytest

from desk import backtest as bt
from desk import indicators as ind
from desk import jev as jev_mod
from desk import paper
from desk import voltrend
from desk.brokers.sim import SimBroker
from desk.engine import Desk, Kills
from desk.filter import evaluate_entry
from desk.models import Bar, FundingEvent, MarketContext, Order, Side, Verdict
from desk.thresholds import DEFAULT, INTERVAL_SECONDS, VOLTREND

H4 = INTERVAL_SECONDS["4h"] * 1000
# Tests run a 4h desk on synthetic 4h bars, independent of whatever thresholds.py currently defaults to.
P = DEFAULT.with_(market={"interval": "4h"},
                  strategy={"breakout_bars": 30, "ema_fast": 50, "ema_slow": 200, "atr_bars": 14},
                  hard={"min_history_bars": 250},
                  sizing={"starting_equity": 10_000.0, "risk_per_trade": 0.02, "max_leverage": 3.0})


def series(n=900, seed=7, drift=0.0006, vol=0.012, start=30_000.0):
    rnd = random.Random(seed)
    bars, px, t0 = [], start, 1_600_000_000_000 - 1_600_000_000_000 % H4
    for k in range(n):
        o = px
        c = o * math.exp(rnd.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 3)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 3)))
        bars.append(Bar(t0 + k * H4, o, h, l, c, 100.0))
        px = c
    funding = [FundingEvent(t0 + k * 8 * 3600_000, 0.0001) for k in range(n // 2)]
    return bars, funding


# ---------------------------------------------------------------- indicators

def test_ema_matches_definition():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ind.ema(vals, 3)
    a = 0.5
    acc = 1.0
    for v in vals[1:]:
        acc = a * v + (1 - a) * acc
    assert out[:2] == [None, None]
    assert out[-1] == pytest.approx(acc)


def test_donchian_excludes_current_bar():
    bars = [Bar(k, 10, 10 + k, 9, 10) for k in range(5)]
    hi, lo = ind.donchian(bars, 3)
    assert hi[3] == 10 + 2       # bars 0..2 only
    assert hi[4] == 10 + 3


def test_efficiency_ratio_straight_line_is_one():
    assert ind.efficiency_ratio([1, 2, 3, 4, 5], 4)[-1] == pytest.approx(1.0)
    assert ind.efficiency_ratio([1, 2, 1, 2, 1], 4)[-1] == pytest.approx(0.0)


def test_funding_assigned_to_the_bar_it_was_charged_in():
    bars = [Bar(0, 1, 1, 1, 1), Bar(H4, 1, 1, 1, 1)]
    ev = [FundingEvent(H4 // 2, 0.001), FundingEvent(H4, 0.002), FundingEvent(H4 + 1, 0.004)]
    assert ind.funding_per_bar(bars, ev, H4) == pytest.approx([0.003, 0.004])


# ---------------------------------------------------------------- broker

def test_fees_slippage_and_pnl_add_up():
    b = SimBroker(P)
    b.submit(Order("open", Side.LONG, 1.0, "t", stop=90.0))
    b.fill_pending(100.0, 0)
    entry = 100 * (1 + 3e-4)
    assert b.position.entry_price == pytest.approx(entry)
    b.submit(Order("close", Side.LONG, 1.0, "t"))
    b.fill_pending(110.0, 1)
    exit_ = 110 * (1 - 3e-4)
    fees = (entry + exit_) * 4.5e-4
    t = b.trades[0]
    assert t.gross_pnl == pytest.approx(exit_ - entry)
    assert t.fees == pytest.approx(fees)
    assert b.cash == pytest.approx(10_000 + exit_ - entry - fees)


def test_stop_gap_fills_at_open_not_stop():
    b = SimBroker(P)
    b.submit(Order("open", Side.LONG, 1.0, "t", stop=95.0))
    b.fill_pending(100.0, 0)
    t = b.check_stop_bar(Bar(1, 90.0, 91.0, 85.0, 88.0))
    assert t.reason == "stop"
    assert t.exit_price == pytest.approx(90.0 * (1 - 3e-4))   # gapped through: filled at the open


def test_longs_pay_positive_funding_shorts_receive_it():
    b = SimBroker(P)
    b.submit(Order("open", Side.LONG, 2.0, "t", stop=1.0))
    b.fill_pending(100.0, 0)
    before = b.cash
    b.apply_funding(0.001, 100.0)
    assert before - b.cash == pytest.approx(0.2)
    s = SimBroker(P)
    s.submit(Order("open", Side.SHORT, 2.0, "t", stop=1000.0))
    s.fill_pending(100.0, 0)
    before = s.cash
    s.apply_funding(0.001, 100.0)
    assert s.cash - before == pytest.approx(0.2)


def test_broker_roundtrips_through_json_including_pending():
    b = SimBroker(P)
    b.submit(Order("open", Side.LONG, 1.0, "t", stop=90.0))
    b.fill_pending(100.0, 0)
    b.submit(Order("close", Side.LONG, 1.0, "trend_flip"))
    c = SimBroker(P)
    c.load_dict(json.loads(json.dumps(b.to_dict())))
    assert c.position.entry_price == b.position.entry_price
    assert [o.reason for o in c.pending] == ["trend_flip"]


# ---------------------------------------------------------------- desk / backtest

def test_no_lookahead_decisions_ignore_future_bars():
    bars, funding = series()
    full = ind.compute(bars, funding, P)
    for i in (400, 600, 850):
        cut = ind.compute(bars[: i + 1], funding, P)
        a = evaluate_entry(P, full, i, equity=10_000, kills=[])
        b = evaluate_entry(P, cut, i, equity=10_000, kills=[])
        assert [g.verdict for g in a.gates] == [g.verdict for g in b.gates]
        assert a.qty == pytest.approx(b.qty)


def test_orders_fill_at_next_bar_open():
    bars, funding = series(seed=3, drift=0.002)
    r = bt.run(bars, funding, P)
    assert r.broker.trades, "expected the trending series to trade"
    opens = {b.ts: b.open for b in bars}
    for t in r.broker.trades:
        assert t.entry_price == pytest.approx(opens[t.entry_ts] * (1 + 3e-4))


def test_risk_never_exceeds_budget_or_leverage():
    bars, funding = series(seed=3, drift=0.002)
    r = bt.run(bars, funding, P)
    for t in r.broker.trades:
        assert t.qty * t.entry_price <= P.sizing.max_leverage * 10_000 * 3   # equity can grow, but not 3x here


def test_kills_block_entries_even_on_a_breakout():
    bars, funding = series(seed=3, drift=0.002)
    f = ind.compute(bars, funding, P)
    i = next(k for k in range(300, len(bars)) if
             evaluate_entry(P, f, k, equity=10_000, kills=[]).side is not None)
    desk = Desk(P, SimBroker(P), kills=Kills(entry_kill=True))
    d = evaluate_entry(P, f, i, equity=10_000, kills=desk.kill_gates(bars[i].ts, 10_000))
    assert d.side is None
    assert d.blocked_by.name == "manual_kill"


def test_drawdown_halt_flattens_and_stops_trading():
    bars, funding = series(seed=11, drift=-0.003, vol=0.03)
    p = P.with_(strategy={"allow_shorts": False}, risk={"max_drawdown_halt": 0.02}, sizing={"risk_per_trade": 0.05})
    r = bt.run(bars, funding, p)
    if r.desk.risk.halted:
        halt_ts = next(e.ts for e in r.desk.events if "HALT" in e.text)
        assert all(t.entry_ts <= halt_ts for t in r.broker.trades)


def test_summary_and_promotion_are_well_formed(tmp_path):
    bars, funding = series(seed=3, drift=0.002, n=2500)
    r = bt.run(bars, funding, P)
    s = bt.summarize(r)
    assert set(s) >= {"full", "in_sample", "out_of_sample", "per_year"}
    path = bt.write_report(s, P, out_dir=tmp_path)
    assert path.read_text().startswith("# Backtest")
    assert len(bt.promotion_check(s, P)) == 5


def _trap_series():
    """Flat range around 100, a breakout to 106, then a fall back through the old high."""
    t0 = 1_600_000_000_000 - 1_600_000_000_000 % H4
    closes = [100 + (k % 5) * 0.2 for k in range(300)] + [106, 105, 100.5, 99.0, 98.0]
    bars = [Bar(t0 + k * H4, c, c + 0.3, c - 0.3, c) for k, c in enumerate(closes)]
    return bars


def test_bull_trap_short_fires_once_on_the_failure_bar():
    from desk.filter import _short_setup

    p = P.with_(strategy={"allow_shorts": True, "short_mode": "failed_breakout", "trap_window_bars": 6})
    bars = _trap_series()
    f = ind.compute(bars, [], p)
    fired = [i for i in range(301, len(bars)) if _short_setup(p.strategy, f, i, Side.LONG)[0] == "failed_breakout"]
    assert fired == [len(bars) - 3]          # the first close back below the broken high, and only that one


def test_counter_trend_short_is_not_closed_by_the_trend_flip_exit():
    p = P.with_(strategy={"allow_shorts": True, "short_mode": "failed_breakout"})
    bars = _trap_series()
    f = ind.compute(bars, [], p)
    b = SimBroker(p)
    desk = Desk(p, b)
    b.submit(Order("open", Side.SHORT, 1.0, "failed_breakout", stop=200.0))
    b.fill_pending(bars[-2].open, bars[-2].ts)
    assert f.ema_fast[-1] > f.ema_slow[-1]   # still an uptrend: a with-trend short would be flipped out here
    assert desk._manage(f, len(bars) - 1) == []


def test_trigger_outlook_matches_the_future_breakout_level():
    from desk.board import trigger_outlook

    bars, funding = series(seed=5, n=400)
    i, n = 350, P.strategy.breakout_bars
    f = ind.compute(bars[: i + 1], funding, P)
    outlook = trigger_outlook(f, i, n, limit=n)
    # extend with bars that never make a new high, then check every step against the real don_hi
    low = min(b.low for b in bars[: i + 1])
    future = [Bar(bars[i].ts + k * H4, low, low, low, low) for k in range(1, n + 1)]
    g = ind.compute(bars[: i + 1] + future, funding, P)
    for step in outlook:
        j = (step["ts"] - bars[0].ts) // H4 - 1           # the bar whose close makes that decision
        assert g.don_hi[j] == step["level"]
    assert outlook[0]["ts"] == bars[i].ts + 2 * H4 and outlook[0]["level"] == g.don_hi[i + 1]


def test_short_outlook_matches_the_future_breakdown_level():
    from desk.board import trigger_outlook

    bars, funding = series(seed=5, n=400)
    i, n = 350, P.strategy.breakout_bars
    f = ind.compute(bars[: i + 1], funding, P)
    outlook = trigger_outlook(f, i, n, limit=n, short=True)
    high = max(b.high for b in bars[: i + 1])              # future bars never make a new low
    future = [Bar(bars[i].ts + k * H4, high, high, high, high) for k in range(1, n + 1)]
    g = ind.compute(bars[: i + 1] + future, funding, P)
    for step in outlook:
        j = (step["ts"] - bars[0].ts) // H4 - 1
        assert g.don_lo[j] == step["level"]
    assert [s["level"] for s in outlook] == sorted(s["level"] for s in outlook)   # only ever steps up


# ---------------------------------------------------------------- paper ledger

class FakeFeed:
    """Serves a growing prefix of a synthetic series, like a real exchange over time."""

    def __init__(self, bars, funding, upto):
        self.all_bars, self.all_funding, self.upto = bars, funding, upto
        self.requests, self.last_latency_ms, self.bar_ms = 0, 1.0, H4

    def candles(self, start_ms=0, end_ms=None, closed_only=True):
        n = self.upto if closed_only else min(len(self.all_bars), self.upto + 1)  # bar `upto` is still forming
        return [b for b in self.all_bars[:n] if b.ts >= start_ms]

    def funding(self, start_ms, end_ms=None):
        end = self.all_bars[self.upto - 1].ts + H4
        return [e for e in self.all_funding if start_ms <= e.ts <= end]

    def context(self):
        return MarketContext(mid=self.all_bars[self.upto - 1].close, spread_bps=1.0, depth_usd=1e7)


@pytest.mark.parametrize("shorts", [False, True])
def test_paper_ledger_stepping_matches_the_backtest(tmp_path, monkeypatch, shorts):
    """Running step() every bar (or in catch-up batches) must equal one backtest over the same bars."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)   # synthetic bars are from 2020; fetch them all
    bars, funding = series(seed=3, drift=-0.002 if shorts else 0.002, n=700)
    start = 300
    p = P.with_(hard={"max_data_age_bars": 1e9},
                strategy={"allow_shorts": True, "short_mode": "breakdown"} if shorts else {})
    feed = FakeFeed(bars, funding, start)
    clock = lambda: bars[feed.upto].ts + H4 // 2 if feed.upto < len(bars) else bars[-1].ts + 2 * H4
    paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
    upto = start
    rnd = random.Random(1)
    while upto < len(bars):
        upto = min(len(bars), upto + rnd.choice([1, 1, 2, 5]))   # sometimes the cron misses runs
        feed.upto = upto
        paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
    led = json.loads((tmp_path / "paper.json").read_text())

    ref_broker = SimBroker(p)
    ref = Desk(p, ref_broker)
    f = ind.compute(bars, funding, p)
    for i, bar in enumerate(bars):
        if i < start:
            continue
        ref_broker.fill_pending(bar.open, bar.ts)
        ref_broker.check_stop_bar(bar)
        ref_broker.apply_funding(f.funding_bar[i], bar.close)
        ref.on_bar_close(f, i, now_ms=bar.ts + f.bar_ms)
    assert len(led["broker"]["trades"]) == len(ref_broker.trades) > 0
    assert led["broker"]["cash"] == pytest.approx(ref_broker.cash)
    sides = {t.side.value for t in ref_broker.trades}
    assert sides == ({"short"} if shorts else {"long"})
    said = " ".join(e["text"] for e in led["desk"]["events"])
    assert ("SHORTED" in said) == shorts and ("BOUGHT" in said) != shorts
    assert json.loads((tmp_path / "board.json").read_text())["goal"]["target"] == p.sizing.goal_equity


# ---------------------------------------------------------------- several coins

def test_each_coin_gets_its_own_symbol_and_risk():
    from desk.thresholds import COINS, params_for, report_name

    assert set(COINS) == {"BTC", "ETH", "SOL"}
    btc, eth, sol = (params_for(c) for c in ("BTC", "ETH", "SOL"))
    assert (btc.market.symbol, eth.market.symbol, sol.market.symbol) == ("BTC", "ETH", "SOL")
    assert btc.sizing.risk_per_trade == DEFAULT.sizing.risk_per_trade
    assert eth.sizing.risk_per_trade == sol.sizing.risk_per_trade == 0.02    # 5% hit the drawdown halt in backtests
    assert all(q.sizing.starting_equity == 150 for q in (btc, eth, sol))
    assert eth.strategy.allow_shorts and eth.strategy.short_mode == "breakdown"   # the user's paper experiment
    assert not btc.strategy.allow_shorts and not sol.strategy.allow_shorts
    assert (report_name("BTC"), report_name("eth")) == ("backtest", "backtest-ETH")


def test_board_is_labelled_with_its_coin(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    bars, funding = series(seed=5, n=400, start=150.0)
    p = P.with_(market={"symbol": "SOL"}, hard={"max_data_age_bars": 1e9})
    feed = FakeFeed(bars, funding, 350)
    paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=bars[350].ts + H4 // 2)
    b = json.loads((tmp_path / "board.json").read_text())
    assert b["header"]["coin"] == "SOL" and b["header"]["title"] == "SOL PERP DESK"
    assert b["ticker"]["cash"] == pytest.approx(b["ticker"]["bank"]) and b["ticker"]["pos"] is None


def test_step_runs_every_coin_moves_the_old_ledger_and_survives_one_failing(tmp_path, monkeypatch):
    import argparse

    from desk import __main__ as cli

    (tmp_path / "paper.json").write_text('{"old": "btc ledger"}')
    (tmp_path / "kills.json").write_text('{"entry_kill": false}')
    seen = []

    def fake_step(p, ledger_dir, jev=None, **kw):
        seen.append((p.market.symbol, Path(ledger_dir).name, p.sizing.risk_per_trade))
        if p.market.symbol == "ETH":
            raise TimeoutError("rate limited")
        return {"paper": {"changed": False, "new_events": []}, "updated": 1, "header": {"coin": p.market.symbol},
                "ticker": {"bank": 50.0, "pnl": 0.0, "pnl_pct": 0.0, "position": "FLAT", "position_detail": "",
                           "record": "0W 0L", "price": 100.0},
                "goal": {"start": 50.0, "target": 100_000.0, "progress": 0.0}}

    monkeypatch.setattr(paper, "step", fake_step)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="BTC,ETH,SOL", ledger=str(tmp_path), interval=None, risk=None,
                              jev_mode="off", coin=None)
    assert cli.cmd_step(args) == 1                        # ETH failed, so the run says so...
    assert seen == [("BTC", "BTC", DEFAULT.sizing.risk_per_trade), ("ETH", "ETH", 0.02), ("SOL", "SOL", 0.02)]
    assert json.loads((tmp_path / "BTC" / "paper.json").read_text()) == {"old": "btc ledger"}
    assert (tmp_path / "BTC" / "kills.json").exists() and not (tmp_path / "paper.json").exists()
    idx = json.loads((tmp_path / "index.json").read_text())["coins"]
    assert [c["coin"] for c in idx] == ["BTC", "ETH", "SOL"]  # ...but BTC and SOL still ran and are listed
    assert "rate limited" in idx[1]["error"] and idx[2]["bank"] == 50.0
    assert cli.migrate_ledger(tmp_path) is False          # only moves once


# ---------------------------------------------------------------- VT: the vol-trend basket

DAY = voltrend.DAY_MS


def daily(n=260, seed=1, drift=0.002, vol=0.035, start=100.0, skip=()):
    """Synthetic daily bars with overnight gaps (open != last close); `skip` = day indexes with no bar."""
    rnd = random.Random(seed)
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % DAY
    bars, px = [], start
    for k in range(n):
        o = px * math.exp(rnd.gauss(0, vol / 5))
        c = o * math.exp(rnd.gauss(drift, vol))
        if k not in skip:
            bars.append(Bar(t0 + k * DAY, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1.0))
        px = c
    return bars


def test_vt_units_match_a_plain_computation():
    import statistics

    closes = [b.close for b in daily(n=220, seed=4)]
    above = below = 0
    for i in range(len(closes)):
        got = voltrend.signal(closes[: i + 1])["raw"]
        if i < 49:                                    # fewer than 50 closes: not warmed up
            assert got == 0.0
            continue
        sma = statistics.fmean(closes[i - 49: i + 1])     # today's close included
        rv = statistics.stdev([closes[j] / closes[j - 1] - 1 for j in range(i - 9, i + 1)]) * math.sqrt(365)
        want = min(2.0, 0.40 / rv) if closes[i] > sma else 0.0
        assert got == pytest.approx(want, rel=1e-12, abs=0)
        above, below = above + (want > 0), below + (want == 0)
    assert above > 20 and below > 20
    assert voltrend.signal([100.0] * 45 + [110.0] * 11)["raw"] == 2.0    # no vol at all: capped at 2 units
    assert voltrend.signal([100.0 + k for k in range(49)])["raw"] == 0.0  # 49 closes: no 50-day average yet


def test_vt_band_keeps_the_held_units_unless_more_than_25pct_away():
    s = voltrend.sticky
    assert s(1.3, 0.0, 0.25) == 1.3                       # entries always go through
    assert s(0.0, 1.0, 0.25) == 0.0                       # so do exits
    assert s(1.2, 1.0, 0.25) == 1.0 and s(0.76, 1.0, 0.25) == 1.0
    assert s(1.25, 1.0, 0.25) == 1.0                      # exactly 25%: held ("more than")
    assert s(1.26, 1.0, 0.25) == 1.26 and s(0.74, 1.0, 0.25) == 0.74


def _sig(raw, close=100.0):
    return {"close": close, "sma": close * 0.95, "vol": 0.3, "raw": raw, "warm": True}


def test_vt_ten_dollar_minimum_skips_entries_and_resizes_never_exits():
    book = voltrend.Book(VOLTREND, start_equity=40.0)
    eth = book.sleeves["ETH"]
    book.decide("ETH", _sig(1.0), 40.0, DAY)              # $40 x 0.65 / 3 = $8.67: under $10
    assert eth.units == 0.0 and "ETH" not in book.pending and book.orders_blocked == 1
    book.decide("ETH", _sig(1.0), 60.0, 2 * DAY)          # still flat, so checked again: $13 goes through
    assert eth.units == 1.0 and book.pending["ETH"]["kind"] == "entry"
    book.fill_pending("ETH", 100.0, 2 * DAY)
    qty = eth.qty
    book.decide("ETH", _sig(1.3), 60.0, 3 * DAY)          # 1.0 -> 1.3 units is a $3.90 resize: no order...
    assert "ETH" not in book.pending and eth.qty == qty and book.orders_blocked == 2
    assert eth.units == 1.3                               # ...but the band now measures from 1.3
    book.decide("ETH", _sig(1.5), 60.0, 4 * DAY)
    assert eth.units == 1.3 and book.orders_blocked == 2  # 15% away: inside the band, nothing to block
    book.decide("ETH", _sig(0.0), 60.0, 5 * DAY)          # a $13 exit, or any size: always
    assert book.pending["ETH"]["kind"] == "exit"
    book.fill_pending("ETH", 101.0, 5 * DAY)
    assert eth.qty == 0.0 and eth.units == 0.0 and len(book.trades) == 1 and book.orders_filled == 2


def _long(qty=2.0, cash=1000.0):
    book = voltrend.Book(VOLTREND, start_equity=cash)
    book.pending["BTC"] = {"kind": "entry", "qty": qty, "usd": qty * 100, "units": 1.0, "ts": 0}
    book.fill_pending("BTC", 100.0, 0)
    return book


def test_vt_longs_pay_positive_funding_and_receive_negative():
    for rate in (0.001, -0.001):
        book = _long(2.0)
        cash = book.cash
        book.on_day(DAY, {"BTC": Bar(DAY, 100, 105, 95, 104)}, {"BTC": [104.0]}, {"BTC": rate})
        assert cash - book.cash == pytest.approx(2.0 * 104 * rate)    # qty x the bar's close x its funding
        assert book.funding_total == pytest.approx(2.0 * 104 * rate)


def test_vt_perp_accounting_realises_pnl_as_the_position_shrinks():
    book = _long(2.0)
    e1, e2 = 100 * 1.0003, 110 * 1.0003
    book.pending["BTC"] = {"kind": "resize", "qty": 1.0, "usd": 110.0, "units": 1.5, "ts": DAY}
    book.fill_pending("BTC", 110.0, DAY)
    avg = (2 * e1 + e2) / 3
    assert book.sleeves["BTC"].avg_entry == pytest.approx(avg)
    book.pending["BTC"] = {"kind": "resize", "qty": -1.5, "usd": -180.0, "units": 0.8, "ts": 2 * DAY}
    book.fill_pending("BTC", 120.0, 2 * DAY)
    x1 = 120 * 0.9997
    book.pending["BTC"] = {"kind": "exit", "qty": -1.5, "usd": -135.0, "units": 0.0, "ts": 3 * DAY}
    book.fill_pending("BTC", 90.0, 3 * DAY)
    x2 = 90 * 0.9997
    fees = (2 * e1 + e2 + 1.5 * x1 + 1.5 * x2) * 4.5e-4
    pnl = 1.5 * (x1 - avg) + 1.5 * (x2 - avg)
    assert book.cash == pytest.approx(1000 + pnl - fees) and book.equity() == book.cash
    t = book.trades[0]
    assert t["net_pnl"] == pytest.approx(pnl - fees) and t["entry_ts"] == 0 and t["exit_ts"] == 3 * DAY
    said = " ".join(e.text for e in book.events)
    assert "BOUGHT" in said and "ADDED" in said and "TRIMMED" in said and "SOLD" in said


# ---------------------------------------------------------------- jev

def test_jev_gates_advisory_never_drop_gate_mode_can():
    ans = {"momentum_already_spent": {"noul": 0.9}, "follow_through": {"noul": 0.9},
           "regime": {"probabilities": {"trend": 0.9}}}
    adv = jev_mod.gates_from_answers(ans, P.with_(jev={"mode": "advisory"}))
    gate = jev_mod.gates_from_answers(ans, P.with_(jev={"mode": "gate"}))
    assert {g.verdict for g in adv} <= {Verdict.PASS, Verdict.MARK}
    assert Verdict.DROP in {g.verdict for g in gate}
    assert jev_mod.gates_from_answers(None, DEFAULT)[0].verdict == Verdict.SKIP


def test_jev_snapshot_contains_no_absolute_price_or_date():
    bars, funding = series(seed=3, drift=0.002)
    f = ind.compute(bars, funding, P)
    text = jev_mod.describe(jev_mod.snapshot(f, 500, Side.LONG, P))
    assert f"{bars[500].close:.0f}" not in text and "20" not in text.split("bars")[0][:4]
    assert "%" in text and "ATR" in text


def test_jev_client_uses_cache_without_network(tmp_path):
    cache = tmp_path / "jev.jsonl"
    c = jev_mod.JevClient(api_key="", cache_path=cache)
    assert c.ask("state") is None            # no key, no cache: unavailable, never raises
    key_line = {"key": __import__("hashlib").sha256(json.dumps(["jev-latest", "state", jev_mod.QUESTIONS],
                                                                sort_keys=True).encode()).hexdigest(),
                "answers": {"x": 1}}
    cache.write_text(json.dumps(key_line) + "\n")
    assert jev_mod.JevClient(api_key="", cache_path=cache).ask("state") == {"x": 1}


# ---------------------------------------------------------------- dashboard server

def test_kill_switch_endpoint_writes_the_kills_file(tmp_path):
    from desk.server import Board, serve

    board = Board(tmp_path / "kills.json")
    board.set({"ok": True})
    httpd = serve(board, "127.0.0.1", 0)
    port = httpd.server_address[1]
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/kill", method="POST",
                                     data=json.dumps({"switch": "entry_kill", "on": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["entry_kill"] is True
        assert Kills.load(tmp_path / "kills.json").entry_kill is True
        assert json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read()) == {"ok": True}
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "thresholds.py" in html
    finally:
        httpd.shutdown()
