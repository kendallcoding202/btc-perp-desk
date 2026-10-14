"""board["rules"]: the strategy settings a desk board carries for the dashboard (the live trend row recomputes the
EMAs with these periods; the log labels its lines with them). They must be the Params the desk ran with, for
every account, so the page never hard-codes a setting."""

import json
import math
import random

from desk import board as board_mod
from desk import paper
from desk.models import Bar, FundingEvent, MarketContext
from desk.thresholds import COINS, DEFAULT, INTERVAL_SECONDS, params_for

KEYS = {"interval", "ema_fast", "ema_slow", "breakout_bars", "stop_atr", "trail_atr", "risk_per_trade", "allow_shorts"}


def _expected(p):
    s = p.strategy
    return {"interval": p.market.interval, "ema_fast": s.ema_fast, "ema_slow": s.ema_slow,
            "breakout_bars": s.breakout_bars, "stop_atr": s.stop_atr, "trail_atr": s.trail_atr,
            "risk_per_trade": p.sizing.risk_per_trade, "allow_shorts": s.allow_shorts}


def test_rules_are_each_accounts_params():
    for coin in COINS:
        p = params_for(coin)
        r = board_mod.rules(p)
        assert set(r) == KEYS
        assert r == _expected(p), coin
    assert board_mod.rules(params_for("ETH"))["allow_shorts"] is True       # the paper experiment
    assert board_mod.rules(params_for("BTC"))["risk_per_trade"] == DEFAULT.sizing.risk_per_trade


class _Feed:
    def __init__(self, bars, funding, upto, bar_ms):
        self.bars, self.funding_, self.upto, self.bar_ms = bars, funding, upto, bar_ms
        self.requests, self.last_latency_ms = 0, 1.0

    def candles(self, start_ms=0, end_ms=None, closed_only=True):
        return [b for b in self.bars[:self.upto + 1] if b.ts >= start_ms]

    def funding(self, start_ms, end_ms=None):
        return [e for e in self.funding_ if e.ts >= start_ms]

    def context(self):
        return MarketContext(mid=self.bars[self.upto - 1].close, spread_bps=1.0, depth_usd=1e7)


def test_a_paper_step_writes_them_to_the_board(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    p = params_for("ETH").with_(hard={"max_data_age_bars": 1e9})
    bar_ms = INTERVAL_SECONDS[p.market.interval] * 1000
    rnd, px, t0, bars = random.Random(2), 2000.0, 1_700_000_000_000 // bar_ms * bar_ms, []
    for k in range(700):
        o = px
        c = o * math.exp(rnd.gauss(0.0003, 0.01))
        bars.append(Bar(t0 + k * bar_ms, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1.0))
        px = c
    funding = [FundingEvent(t0 + k * 3600_000, 0.00001) for k in range(0, 700 * 2)]
    feed = _Feed(bars, funding, 600, bar_ms)
    for upto in (600, 601, 603):
        feed.upto = upto
        paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=bars[upto].ts + bar_ms // 2)
    b = json.loads((tmp_path / "board.json").read_text())
    assert b["rules"] == _expected(p)
    # what the dashboard's live trend row steps from: the last EMA values, with the periods above
    assert b["chart"]["ema_fast"][-1] is not None and b["chart"]["ema_slow"][-1] is not None


# ---------------------------------------------------------------- the real Coinbase account's boards (SHADOW, LIVE)

def _venue_board(mode, start_equity=None, cash=500.0):
    from desk import indicators as ind
    from desk.backtest import make_desk

    p = params_for("BTC").with_(hard={"max_data_age_bars": 1e9})
    bar_ms = INTERVAL_SECONDS[p.market.interval] * 1000
    rnd, px, t0, bars = random.Random(5), 80_000.0, 1_790_000_000_000 // bar_ms * bar_ms, []
    for k in range(600):
        o = px
        c = o * math.exp(rnd.gauss(0.0003, 0.008))
        bars.append(Bar(t0 + k * bar_ms, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1.0))
        px = c
    f = ind.compute(bars, [FundingEvent(t0 + k * 3600_000, 1e-5) for k in range(1200)], p)
    broker, desk = make_desk(p)
    broker.cash = cash
    desk.risk.peak_equity = cash
    return board_mod.build(desk, f, len(bars) - 1, mode=mode, start_equity=start_equity, now_ms=bars[-1].ts + bar_ms)


def test_the_paper_coinbase_tagline_is_unchanged_and_shadow_and_live_say_what_they_are():
    assert _venue_board("paper")["header"]["tagline"] == "PAPER · SIMULATED ON COINBASE RULES · NO ORDERS SENT"
    assert _venue_board("paper")["venue"]["stop_fill"] == ("stops simulated as filled at the stop (Coinbase: stop-limit, "
                                                           "which can miss in a fast market)")
    s = _venue_board("shadow")
    assert s["header"]["mode"] == "SHADOW"
    assert s["header"]["tagline"] == "SHADOW · YOUR REAL COINBASE ACCOUNT, READ ONLY · NO ORDERS SENT · TRADES SIMULATED"
    assert s["venue"]["stop_fill"].startswith("stops simulated from Coinbase's 5-minute candles as stop-limits")
    live = _venue_board("live")
    assert live["header"]["tagline"] == "LIVE · YOUR REAL COINBASE ACCOUNT · REAL ORDERS · REAL MONEY"
    assert "rests on Coinbase as a stop-limit" in live["venue"]["stop_fill"]


def test_an_account_that_starts_at_zero_builds_a_board_with_no_progress_and_no_projection():
    b = _venue_board("shadow", start_equity=0.0, cash=0.0)
    assert b["ticker"]["pnl_pct"] == 0.0 and b["ticker"]["bank"] == 0.0
    assert b["goal"]["progress"] == 0.0 and b["goal"]["test_cagr"] is None and b["goal"]["realised_cagr"] is None
