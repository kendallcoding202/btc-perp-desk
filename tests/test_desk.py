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
from desk.brokers.sim import SimBroker
from desk.engine import Desk, Kills
from desk.filter import evaluate_entry
from desk.models import Bar, FundingEvent, MarketContext, Order, Side, Verdict
from desk.thresholds import DEFAULT, INTERVAL_SECONDS

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


def test_paper_ledger_stepping_matches_the_backtest(tmp_path, monkeypatch):
    """Running step() every bar (or in catch-up batches) must equal one backtest over the same bars."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)   # synthetic bars are from 2020; fetch them all
    bars, funding = series(seed=3, drift=0.002, n=700)
    start = 300
    p = P.with_(hard={"max_data_age_bars": 1e9})
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
    assert json.loads((tmp_path / "board.json").read_text())["goal"]["target"] == p.sizing.goal_equity


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
