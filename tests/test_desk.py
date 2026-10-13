import dataclasses
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
from desk.thresholds import DEFAULT, EXPERIMENTS, INTERVAL_SECONDS, VOLTREND

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


def _reported_every_new_event(before: list[dict], after: list[dict], reported: list[dict]) -> bool:
    """The ledger's events only ever get appended to (the oldest dropped past 200): a run reported all of its new
    events when the last event from before the run sits right in front of the ones it reported."""
    n = len(reported)
    return after[len(after) - n - 1] == before[-1] and after[len(after) - n:] == reported


def test_paper_step_reports_every_new_event_once_the_ledger_holds_200(tmp_path, monkeypatch):
    """The desk keeps its last 200 events. Once that's full, a run still reports every event it adds (the board's
    new_events, which the phone alerts are sent from): none mistaken for an old one."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    feed = FakeFeed(bars, funding, 300)
    clock = lambda: bars[feed.upto].ts + H4 // 2 if feed.upto < len(bars) else bars[-1].ts + 2 * H4
    paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
    led = json.loads((tmp_path / "paper.json").read_text())
    ev = led["desk"]["events"]
    ev[:0] = [{"ts": 1, "agent": "chief", "text": f"old {k}"} for k in range(200 - len(ev))]
    (tmp_path / "paper.json").write_text(json.dumps(led))
    rnd, multi = random.Random(4), 0
    while feed.upto < len(bars):
        feed.upto = min(len(bars), feed.upto + rnd.choice([1, 2, 5]))
        before = json.loads((tmp_path / "paper.json").read_text())["desk"]["events"]
        s = paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
        after = json.loads((tmp_path / "paper.json").read_text())["desk"]["events"]
        assert len(after) == 200 and _reported_every_new_event(before, after, s["paper"]["new_events"])
        multi += len(s["paper"]["new_events"]) >= 2
    assert multi >= 3


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


def hourly_funding(bars, seed):
    """Hyperliquid-style prints: every hour, a few ms late, mostly longs paying."""
    rnd = random.Random(seed)
    t0, t1 = bars[0].ts, bars[-1].ts + DAY
    return [FundingEvent(t, rnd.gauss(1e-5, 2e-5)) for t in range(t0 + 3600_000 + 17, t1 + 1, 3600_000)]


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


class FakeDailyFeed:
    """One coin's daily candles as an exchange serves them at clock["now"]: the closed ones plus the one forming.
    `lag` hides the newest N of them, like a feed that hasn't caught up yet (they show up on a later call); `hide`
    drops bars from the answers until it's cleared (a hole); `late_ms` answers as of that much after the clock
    (a request retried after a 429)."""

    def __init__(self, bars, funding, clock):
        self.bars, self.all_funding, self.clock = bars, funding, clock
        self.requests, self.last_latency_ms, self.bar_ms, self.lag = 0, 1.0, DAY, 0
        self.hide, self.late_ms = set(), 0

    def candles(self, start_ms=0, end_ms=None, closed_only=True):
        self.requests += 1
        now = self.clock["now"] + self.late_ms
        out = [b for b in self.bars if start_ms <= b.ts <= now and b.ts not in self.hide
               and (not closed_only or b.ts + DAY <= now)]
        return out[:len(out) - self.lag]

    def funding(self, start_ms, end_ms=None):
        self.requests += 1
        return [e for e in self.all_funding if start_ms <= e.ts <= self.clock["now"]]


def _vt_market():
    series = {"BTC": daily(seed=11), "ETH": daily(seed=12, skip=(170, 171)), "SOL": daily(seed=13, vol=0.05, skip=(200,))}
    return series, {c: hourly_funding(b, k) for k, (c, b) in enumerate(series.items())}


def test_vt_report_is_well_formed(tmp_path):
    series, funding = _vt_market()                        # 2023-11 to 2024-07: both sides of the 2024 split
    rep = voltrend.report(series, funding)
    path = voltrend.write_report(rep, out_dir=tmp_path)
    assert path.read_text().startswith("# Backtest — VT vol-trend basket")
    saved = json.loads((tmp_path / "backtest-VT.json").read_text())
    assert saved["full"]["cagr"] == rep["full"]["cagr"] and saved["out_of_sample"]["from"] == "2024-01-01"
    assert [r["start"] for r in rep["fresh_starts"]["out_of_sample"]] == [50, 150, 250, 500, 1000, None]
    small, free = rep["fresh_starts"]["out_of_sample"][0], rep["fresh_starts"]["out_of_sample"][-1]
    assert small["blocked"] > free["blocked"] == 0 and free["orders"] > 0
    assert set(rep["per_year"]) == {"2023", "2024"} and rep["order_sizes"]["entry"]["orders"] > 0


def test_vt_paper_stepping_matches_the_backtest(tmp_path):
    """Stepping the VT ledger as the cron does must give exactly what one backtest over the same days gives (same
    fills, cash, equity curve and events): through missed days, an 80-day outage, a feed that's a day behind for
    one coin, days a coin has no bar at all, and orders filled at the open of the bar that's still forming."""
    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0, start = series["BTC"][0].ts, 60
    clock["now"] = t0 + start * DAY + DAY // 2           # day 60 is forming: the account opens, flat
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    rnd, k, caught_up, forming_fills, lagged = random.Random(2), start, 0, 0, 0
    while k < 260:
        k = min(260, k + (80 if 100 <= k < 105 else rnd.choice([1, 1, 2, 3, 5])))   # the cron misses runs
        clock["now"] = t0 + k * DAY + DAY // 2 + (DAY if k == 260 else 0)
        for f in feeds.values():                        # a coin's newest closed bar and the forming one, late
            f.lag = 2 if k < 260 and rnd.random() < 0.25 else 0
        lagged += any(f.lag for f in feeds.values())
        s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
        caught_up = max(caught_up, s["paper"]["bars_processed_this_run"])
        led = json.loads((tmp_path / "paper.json").read_text())["broker"]
        forming_fills += sum(1 for f in led["fills"] if f["ts"] == t0 + k * DAY and k < 260)   # filled before it closed
    assert caught_up >= 80 and forming_fills > 0 and lagged > 3
    _assert_vt_ledger_is_the_backtest(tmp_path, series, funding, t0 + start * DAY)


def _assert_vt_ledger_is_the_backtest(ledger, series, funding, start_ms):
    """The ledger, after a last run with every bar closed, against one backtest from its first day: the same fills
    and events in the same order, the same cash, equity curve and positions."""
    led = json.loads((ledger / "paper.json").read_text())
    ref = voltrend.run(series, funding, VOLTREND, start_ms=start_ms).book
    b = led["broker"]
    assert {f["kind"] for f in ref.fills} == {"entry", "resize", "exit"} and len(ref.fills) > 10
    assert b["fills"] == ref.fills and b["trades"] == ref.trades and b["pending"] == ref.pending
    assert b["cash"] == ref.cash and b["funding_total"] == ref.funding_total != 0
    assert [tuple(x) for x in b["equity_curve"]] == ref.equity_curve
    assert b["sleeves"] == {c: s.__dict__ for c, s in ref.sleeves.items()}
    events = [(e["ts"], e["agent"], e["text"]) for e in b["events"]]
    assert "opened" in events[0][2] and len(events) < 200
    assert events[1:] == [(e.ts, e.agent, e.text) for e in ref.events]
    assert led["meta"]["strategy"] == "voltrend" and led["meta"]["interval"] == "1d"
    assert led["meta"]["last_bar_ts"] == series["BTC"][-1].ts


def test_vt_paper_matches_the_backtest_through_feed_glitches(tmp_path):
    """The same, through what a real feed does now and then: one coin's forming bar not in its answer yet, the bar
    that just closed missing from one answer only, and an answer that arrives just after 00:00 (a retried request),
    so it already holds the new day's bar while the run's clock is still on the old day."""
    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0, start = series["BTC"][0].ts, 60
    clock["now"] = t0 + start * DAY + DAY // 2
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    rnd, k, glitches = random.Random(5), start, {"forming": 0, "hole": 0, "late": 0}
    while k < 258:
        k += rnd.choice([1, 1, 2])
        clock["now"] = t0 + k * DAY + 4 * 60_000         # 00:04: bar k is forming
        f, glitch = rnd.choice(list(feeds.values())), rnd.choice([*glitches, None])
        if glitch == "forming":
            f.lag = 1
        elif glitch == "hole":
            f.hide = {t0 + (k - 1) * DAY}
        elif glitch == "late":                           # 23:59:50 on day k, that coin answered at 00:00:20
            clock["now"], f.late_ms = t0 + (k + 1) * DAY - 10_000, 30_000
        glitches[glitch] = glitches.get(glitch, 0) + 1
        paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
        f.lag, f.hide, f.late_ms = 0, set(), 0
    clock["now"] = t0 + 261 * DAY                        # every bar closed
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    assert min(glitches.values()) >= 10
    _assert_vt_ledger_is_the_backtest(tmp_path, series, funding, t0 + start * DAY)


def test_vt_every_new_event_is_reported_once_the_ledger_holds_200(tmp_path):
    """The VT ledger keeps its last 200 events (a year or so of trading). Once that's full, every run still reports
    all the events it adds, fills included, so every trade still reaches the board and the phone."""
    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0 = series["BTC"][0].ts
    clock["now"] = t0 + 60 * DAY + DAY // 2
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    led = json.loads((tmp_path / "paper.json").read_text())
    ev = led["broker"]["events"]
    ev[:0] = [{"ts": 1, "agent": "size", "text": f"old {k}"} for k in range(200 - len(ev))]
    (tmp_path / "paper.json").write_text(json.dumps(led))
    multi = fills = 0
    for k in range(61, 260):
        clock["now"] = t0 + k * DAY + 4 * 60_000           # 00:04 every day: the close decided, its orders filled
        before = json.loads((tmp_path / "paper.json").read_text())["broker"]["events"]
        s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
        after = json.loads((tmp_path / "paper.json").read_text())["broker"]["events"]
        assert len(after) == 200 and _reported_every_new_event(before, after, s["paper"]["new_events"])
        multi += len(s["paper"]["new_events"]) >= 2
        fills += any(e["agent"] == "fills" for e in s["paper"]["new_events"])
    assert multi >= 10 and fills >= 10


def test_vt_orders_wait_for_their_own_open_when_every_answer_lacks_the_bar_that_closed(tmp_path):
    """After a day with no run, every coin's answer can lack the bar that just closed while the next one is forming.
    The orders decided at the close before belong to the missing bar's open, not the forming one's: they wait for the
    next run, which fills them at the backtest's price."""
    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0, start = series["BTC"][0].ts, 60
    ref = voltrend.run(series, funding, VOLTREND, start_ms=t0 + start * DAY).book
    d = next((f["ts"] - t0) // DAY for f in ref.fills if (f["ts"] - t0) // DAY > 120)   # an order fills at day d's open
    clock["now"] = t0 + start * DAY + DAY // 2
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    for k in range(start + 1, d):                        # daily runs up to day d-1
        clock["now"] = t0 + k * DAY + 4 * 60_000
        paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    n0 = len(json.loads((tmp_path / "paper.json").read_text())["broker"]["fills"])
    clock["now"] = t0 + (d + 1) * DAY + 4 * 60_000      # no run on day d; at d+1 00:04 no answer has bar d yet
    for f in feeds.values():
        f.hide = {t0 + d * DAY}
    s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    led = json.loads((tmp_path / "paper.json").read_text())["broker"]
    assert s["paper"]["bars_processed_this_run"] == 1 and led["pending"] and len(led["fills"]) == n0
    for f in feeds.values():
        f.hide = set()
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    led = json.loads((tmp_path / "paper.json").read_text())["broker"]
    assert led["fills"][n0] == ref.fills[n0] and led["fills"][n0]["ts"] == t0 + d * DAY
    clock["now"] = t0 + 261 * DAY
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    _assert_vt_ledger_is_the_backtest(tmp_path, series, funding, t0 + start * DAY)


def test_vt_day_one_never_trades_on_closes_from_before_the_account_opened(tmp_path):
    """The account opens while one coin's answer is a few closes behind. The first decision is still at the next
    00:00 close, and every fill is dated after the account opened: the same as a backtest from that day."""
    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0, start = series["BTC"][0].ts, 120
    clock["now"] = t0 + start * DAY + 4 * 60_000         # opens at 00:04 on day 120...
    feeds["SOL"].lag = 4                                  # ...while SOL's answer ends 3 closes early, with no forming bar
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    feeds["SOL"].lag = 0
    meta = json.loads((tmp_path / "paper.json").read_text())["meta"]
    assert meta["last_bar_ts"] == t0 + (start - 1) * DAY  # the close that just happened, not SOL's last one
    clock["now"] += 3600_000                              # the next run, SOL caught up: nothing to decide yet
    s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    assert s["paper"]["bars_processed_this_run"] == 0
    assert not json.loads((tmp_path / "paper.json").read_text())["broker"]["fills"]
    for k in range(start + 1, 261):
        clock["now"] = t0 + k * DAY + 4 * 60_000
        paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    led = json.loads((tmp_path / "paper.json").read_text())
    assert min(f["ts"] for f in led["broker"]["fills"]) >= led["meta"]["started_ts"]
    _assert_vt_ledger_is_the_backtest(tmp_path, series, funding, t0 + start * DAY)


def test_vt_board_carries_the_experiment_label(tmp_path):
    series, funding = _vt_market()
    clock = {"now": series["BTC"][0].ts + 150 * DAY + DAY // 2}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    clock["now"] += 3 * DAY
    s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    b = json.loads((tmp_path / "board.json").read_text())
    assert b == json.loads(json.dumps(s))
    assert b["header"]["experiment"] == EXPERIMENTS["VT"] and "paper experiment" in b["header"]["experiment"]
    assert b["header"]["coin"] == "VT" and b["header"]["kind"] == "basket"
    assert [h["coin"] for h in b["basket"]["holdings"]] == ["BTC", "ETH", "SOL"]
    assert b["basket"]["next_decision_ts"] % DAY == 0 and 0 < b["basket"]["next_decision_ts"] - clock["now"] <= DAY
    held = sum(h["qty"] * (h["mark"] - h["avg_entry"]) for h in b["basket"]["holdings"])
    assert b["ticker"]["bank"] == pytest.approx(b["ticker"]["cash"] + held)   # re-markable from live mids
    assert b["goal"]["start"] == VOLTREND.starting_equity == 500 and b["paper"]["runs"] == 2


# What dashboard/app.js reads on the VT tab (renderBasket, withLiveBasket, renderCoins, renderRules).
VT_HOLDING_KEYS = {"coin", "decided_ts", "close", "sma50", "sma_gap", "above_sma", "vol10", "warm", "target_units",
                   "held_units", "qty", "avg_entry", "entry_ts", "mark", "notional", "weight", "unrealised", "pending"}
VT_TICKER_KEYS = {"bank", "cash", "pnl", "pnl_pct", "position", "position_detail", "record", "hold_pct", "fees",
                  "funding", "notional", "exposure", "unrealised"}


def test_vt_board_has_what_the_dashboard_draws(tmp_path):
    series, funding = _vt_market()
    t0 = series["BTC"][0].ts
    clock = {"now": t0 + 120 * DAY + DAY // 2}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    day1 = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    rows = day1["basket"]["holdings"]
    assert all(VT_HOLDING_KEYS <= set(h) for h in rows) and VT_TICKER_KEYS <= set(day1["ticker"])
    # day one previews what the rule sees at the last close: nothing decided, nothing held, but the averages are there
    assert all(h["decided_ts"] is None and h["qty"] == 0 and h["sma50"] and h["vol10"] for h in rows)
    assert day1["basket"]["last_close_ts"] == t0 + 120 * DAY and day1["header"]["coins"] == list(VOLTREND.coins)
    assert set(day1["basket"]["rules"]) == {f.name for f in dataclasses.fields(VOLTREND)}

    held = None
    for k in range(121, 200):
        clock["now"] = t0 + k * DAY + DAY // 2
        s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
        held = [h for h in s["basket"]["holdings"] if h["qty"] > 0]
        if len(held) >= 2:
            break
    assert held and all(VT_HOLDING_KEYS <= set(h) for h in s["basket"]["holdings"])
    fills = json.loads((tmp_path / "paper.json").read_text())["broker"]["fills"]
    for h in held:
        assert h["decided_ts"] % DAY == 0 and h["held_units"] > 0 and h["above_sma"] == (h["sma_gap"] > 0)
        last_entry = max(f["ts"] for f in fills if f["coin"] == h["coin"] and f["kind"] == "entry")
        assert h["entry_ts"] == last_entry and h["weight"] == pytest.approx(h["notional"] / s["ticker"]["bank"])

    # the ALL tab re-marks the basket from live mids with the index row alone: cash + qty x (mid - average entry)
    book = voltrend.Book(VOLTREND)
    book.load_dict(json.loads((tmp_path / "paper.json").read_text())["broker"])
    mids = {h["coin"]: h["mark"] * (1.07 if k % 2 else 0.95) for k, h in enumerate(s["basket"]["holdings"])}
    row = {"cash": s["ticker"]["cash"], "holdings": [{k: h[k] for k in ("coin", "qty", "avg_entry", "mark")}
                                                     for h in s["basket"]["holdings"]]}
    remarked = row["cash"] + sum(h["qty"] * (mids[h["coin"]] - h["avg_entry"]) for h in row["holdings"])
    assert remarked == pytest.approx(book.equity(mids)) and remarked != pytest.approx(s["ticker"]["bank"])


def test_step_runs_vt_and_one_failure_never_stops_the_others(tmp_path, monkeypatch):
    import argparse
    import time as time_mod

    from desk import __main__ as cli
    from desk.feeds import hyperliquid

    today = int(time_mod.time() * 1000) // DAY * DAY
    bars = {c: [Bar(today - (200 - k) * DAY, b.open, b.high, b.low, b.close) for k, b in enumerate(daily(n=201, seed=s))]
            for c, s in (("BTC", 21), ("ETH", 22), ("SOL", 23))}

    class Feed(FakeDailyFeed):           # what cmd_step builds for VT, minus the network
        def __init__(self, coin, interval):
            super().__init__(bars[coin], [], {"now": int(time_mod.time() * 1000)})

    def desk_step(p, ledger_dir, jev=None, **kw):
        raise TimeoutError("rate limited")

    monkeypatch.setattr(hyperliquid, "HyperliquidFeed", Feed)
    monkeypatch.setattr(paper, "step", desk_step)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="ETH,VT", ledger=str(tmp_path), interval=None, risk=None, jev_mode="off", coin=None)
    assert cli.cmd_step(args) == 1                        # ETH failed...
    idx = json.loads((tmp_path / "index.json").read_text())["coins"]
    assert [c["coin"] for c in idx] == ["ETH", "VT"] and "rate limited" in idx[0]["error"]
    assert idx[1]["kind"] == "basket" and idx[1]["bank"] == 500.0 and len(idx[1]["holdings"]) == 3   # ...VT ran
    assert json.loads((tmp_path / "VT" / "paper.json").read_text())["meta"]["strategy"] == "voltrend"

    def vt_step(vt, ledger_dir, **kw):
        raise ValueError("bad candles")

    ran = []
    monkeypatch.setattr(paper, "step_basket", vt_step)
    monkeypatch.setattr(paper, "step", lambda p, d, jev=None, **kw: ran.append(p.market.symbol) or {
        "paper": {"changed": False, "new_events": []}, "updated": 1, "header": {"coin": p.market.symbol},
        "ticker": {"bank": 150.0, "pnl": 0.0, "pnl_pct": 0.0, "position": "FLAT", "position_detail": "",
                   "record": "0W 0L", "price": 100.0}, "goal": {"start": 150.0, "target": 1e5, "progress": 0.0}})
    args.coins = "BTC,VT,SOL"
    assert cli.cmd_step(args) == 1 and ran == ["BTC", "SOL"]   # VT failed; BTC and SOL still ran
    idx = json.loads((tmp_path / "index.json").read_text())["coins"]
    assert "bad candles" in idx[1]["error"] and idx[2]["bank"] == 150.0 and idx[0]["kind"] == "desk"


def test_vt_fills_are_pushed_as_trades(monkeypatch):
    from desk import __main__ as cli

    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or io_ok())
    state = {"header": {"coin": "VT", "kind": "basket"},
             "ticker": {"bank": 512.3, "pnl_pct": 0.0246, "position": "LONG 2/3", "price": None},
             "paper": {"new_events": [{"agent": "fills", "text": "ADDED 0.01 ETH @ $2,500.00 (resize to 1.20 units)"}]}}
    cli._notify(state)
    assert sent[0].headers["Title"] == "VT basket: TRADE" and b"ADDED" in sent[0].data and b"$512.30" in sent[0].data


def io_ok():
    import io

    return io.BytesIO(b"ok")


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
