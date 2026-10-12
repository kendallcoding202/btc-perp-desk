"""Extended desk engine for the desk_upgrades_p hunt. NEVER edits the repo: subclasses desk.engine.Desk and
desk.brokers.sim.SimBroker and replays exactly like desk.backtest.run (fill at next open, stops vs bar high/low,
funding per bar, same fees/slippage). With ext={} it must reproduce bt.run bit-for-bit (checked in check_equiv()).

ext keys (all optional, default off):
  reentry: int        after a STOP exit with the trend still intact (EMA100 vs EMA400 still on our side), the next
                      entry may trigger on a close beyond the prior `reentry`-bar extreme instead of the 60-bar one.
                      The re-arm lasts until the next entry or until the trend flips.
  time_stop: (N, k)   single check at the close of the N-th bar held: exit if (close-entry)*dir < k*ATR.
  trail_on: "high"    chandelier from best HIGH (long) / best LOW (short) since entry instead of best close.
  trail_regime: (alpha, W)  trail multiplier = trail_atr * clip((ATR_W/ATR28)^alpha, 0.67, 1.5), ATR_W = Wilder ATR(W).
  pyramid: (max_units, cond)  while in a position, on a new breakout bar (close beyond prior 60-bar extreme) add one
                      unit (same qty formula as a fresh entry at the current equity, total notional capped at
                      max_leverage) if cond holds: "be" = current stop already at/through avg entry, "2atr" = close
                      at least 2 ATR beyond avg entry. Filled next open with fee+slippage; avg entry is updated.
  size_atr_w: int     risk-based size computed with ATR(W) instead of ATR(28) (stop distance unchanged) — slow-vol
                      targeting.
  htf_sma: int        long entries need the last COMPLETED daily close > SMA(n) of completed daily closes
                      (shorts: <). Daily = UTC day built from 12 2h bars.
"""
from __future__ import annotations

import math
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = str(__import__("pathlib").Path(__file__).resolve().parents[4])
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from desk import backtest as bt  # noqa: E402
from desk import indicators as ind  # noqa: E402
from desk.brokers.sim import SimBroker  # noqa: E402
from desk.engine import Desk  # noqa: E402
from desk.filter import evaluate_entry  # noqa: E402
from desk.models import Order, Side  # noqa: E402
from desk.thresholds import params_for  # noqa: E402

HERE = Path(__file__).parent
SPLIT_MS = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
OOS_END_MS = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)


def history(coin: str):
    """Same as desk.__main__._history(p, 'binance', '2020-01', None), cached as a pickle in this directory."""
    cache = HERE / f"hist_{coin}.pkl"
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    from desk.feeds import binance_vision
    bars, funding = binance_vision.load(symbol=f"{coin}USDT", interval="2h", start="2020-01", end=None,
                                        data_dir=f"{REPO}/data")
    cache.write_bytes(pickle.dumps((bars, funding)))
    return bars, funding


class XBroker(SimBroker):
    def fill_pending(self, price, ts):
        pending, self.pending = self.pending, []
        for o in pending:
            if o.action == "open" and self.position is None:
                self._open(o, price, ts)
            elif o.action == "close" and self.position is not None:
                self._close(price, ts, o.reason)
            elif o.action == "add" and self.position is not None and o.side is self.position.side:
                pos = self.position
                fill = price * (1 + self.slip * o.side.sign)
                fee = o.qty * fill * self.fee_rate
                self.cash -= fee
                self.fees_total += fee
                pos.entry_price = (pos.entry_price * pos.qty + fill * o.qty) / (pos.qty + o.qty)
                pos.qty += o.qty
                pos.fees_paid += fee
                pos.units = getattr(pos, "units", 1) + 1
                self.orders_filled += 1

    execute_now = fill_pending


class XDesk(Desk):
    ext: dict = None

    def setup_ext(self, f, ext):
        self.ext = ext or {}
        self.armed = 0                       # +1 / -1 = re-entry armed for long / short
        self._ntrades_seen_x = 0
        bars = f.bars
        e = self.ext
        if e.get("reentry"):
            self.re_hi, self.re_lo = ind.donchian(bars, e["reentry"])
        if e.get("trail_regime"):
            self.atr_w = ind.atr(bars, e["trail_regime"][1])
        if e.get("size_atr_w"):
            self.atr_size = ind.atr(bars, e["size_atr_w"])
        if e.get("htf_sma"):
            n = e["htf_sma"]
            # completed daily closes: a UTC day is complete after its 12th 2h bar (open 22:00) closes
            self.htf_ok = [0] * len(bars)        # +1 long ok, -1 short ok, 0 neither / unknown
            closes, sm, state = [], 0.0, 0
            for i, b in enumerate(bars):
                end = b.ts + f.bar_ms
                if end % 86_400_000 == 0:        # this bar closes a UTC day
                    closes.append(b.close)
                    if len(closes) >= n:
                        m = sum(closes[-n:]) / n
                        state = 1 if closes[-1] > m else -1
                self.htf_ok[i] = state

    # ---------------------------------------------------------------- copied from Desk.on_bar_close + hooks
    def on_bar_close(self, f, i, ctx=None, now_ms=None):
        e = self.ext
        bar = f.bars[i]
        ts_close = bar.ts + f.bar_ms
        price = bar.close
        equity = self.broker.equity(price)
        orders = []

        # re-entry arming: look at trades closed since the last bar
        if e.get("reentry"):
            for t in self.broker.trades[self._ntrades_seen_x:]:
                d = t.side.sign
                ef, es = f.ema_fast[i], f.ema_slow[i]
                if t.reason == "stop" and ef is not None and (ef - es) * d > 0:
                    self.armed = d
                else:
                    self.armed = 0
            self._ntrades_seen_x = len(self.broker.trades)
            if self.armed and f.ema_fast[i] is not None and (f.ema_fast[i] - f.ema_slow[i]) * self.armed <= 0:
                self.armed = 0

        flatten = self._update_risk(ts_close, equity, f.bar_ms)
        pos = self.broker.position

        if pos is not None:
            if flatten or self.risk.halted:
                orders.append(Order("close", pos.side, pos.qty, (flatten or ["drawdown_halt"])[0]))
            else:
                orders += self._manage(f, i)

        # entry: temporarily swap the breakout level / sizing ATR for this bar only
        saved = {}
        if e.get("reentry") and self.armed and pos is None:
            if self.armed > 0 and self.re_hi[i] is not None:
                saved["don_hi"] = f.don_hi[i]
                f.don_hi[i] = min(f.don_hi[i], self.re_hi[i]) if f.don_hi[i] is not None else None
            if self.armed < 0 and self.re_lo[i] is not None:
                saved["don_lo"] = f.don_lo[i]
                f.don_lo[i] = max(f.don_lo[i], self.re_lo[i]) if f.don_lo[i] is not None else None
        decision = evaluate_entry(
            self.p, f, i, equity=equity, kills=self.kill_gates(ts_close, equity), ctx=ctx, now_ms=now_ms,
            jev=self.jev, jev_enabled=not self.kills.jev_kill,
        )
        if "don_hi" in saved:
            f.don_hi[i] = saved["don_hi"]
        if "don_lo" in saved:
            f.don_lo[i] = saved["don_lo"]
        self.last_decision, self.last_i = decision, i

        if decision.side is not None and e.get("htf_sma") and self.htf_ok[i] != decision.side.sign:
            decision.side = None
        if decision.side is not None and e.get("size_atr_w"):
            aw = self.atr_size[i]
            z = self.p.sizing
            if aw is None:
                decision.side = None
            else:
                s = self.p.strategy
                d = decision.side.sign
                k = s.short_stop_atr if (d < 0 and s.short_stop_atr) else s.stop_atr
                qty = equity * z.risk_per_trade / (k * aw)
                qty = min(qty, equity * z.max_leverage / price) if equity > 0 else 0.0
                decision.qty = qty
                decision.notional = qty * price
                decision.leverage = decision.notional / equity if equity > 0 else 0.0
                if decision.notional < z.min_notional_usd:
                    decision.side = None

        if decision.side is not None and pos is None and not orders:
            orders.append(Order("open", decision.side, decision.qty, decision.setup or "entry", stop=decision.stop,
                                meta={"leverage": decision.leverage, "notional": decision.notional,
                                      "take_profit": None}))
            self.armed = 0

        # pyramiding
        if e.get("pyramid") and pos is not None and not orders and not self.day_locked and not self.risk.halted \
                and ts_close >= self.risk.cooldown_until_ts:
            max_units, cond = e["pyramid"]
            d = pos.side.sign
            lvl = f.don_hi[i] if d > 0 else f.don_lo[i]
            a = f.atr[i]
            if lvl is not None and a and getattr(pos, "units", 1) < max_units and (price - lvl) * d > 0:
                ok = ((pos.stop - pos.entry_price) * d >= 0) if cond == "be" else \
                    ((price - pos.entry_price) * d >= 2 * a)
                if ok:
                    z, s = self.p.sizing, self.p.strategy
                    k = s.short_stop_atr if (d < 0 and s.short_stop_atr) else s.stop_atr
                    qty = equity * z.risk_per_trade / (k * a)
                    room = max(0.0, equity * z.max_leverage / price - pos.qty)
                    qty = min(qty, room)
                    if qty * price >= z.min_notional_usd:
                        orders.append(Order("add", pos.side, qty, "pyramid"))

        for o in orders:
            self.broker.submit(o)
        self.equity_curve.append((ts_close, equity))
        return orders

    def _manage(self, f, i):
        e = self.ext
        s = self.p.strategy
        pos = self.broker.position
        bar = f.bars[i]
        a, ef, es = f.atr[i], f.ema_fast[i], f.ema_slow[i]
        if a is None or ef is None or es is None:
            return []
        d = pos.side.sign
        if not hasattr(pos, "bars_held"):
            pos.bars_held = 0
            pos.best_ext = pos.entry_price
        pos.bars_held += 1
        if (bar.close - pos.best_close) * d > 0:
            pos.best_close = bar.close
        ext_px = bar.high if d > 0 else bar.low
        if (ext_px - pos.best_ext) * d > 0:
            pos.best_ext = ext_px
        k = s.short_stop_atr if (d < 0 and s.short_stop_atr) else s.trail_atr
        if e.get("trail_regime"):
            alpha, _w = e["trail_regime"]
            aw = self.atr_w[i]
            if aw:
                k = k * min(1.5, max(0.67, (aw / a) ** alpha))
        anchor = pos.best_ext if e.get("trail_on") == "high" else pos.best_close
        trail = anchor - d * k * a
        if (trail - pos.stop) * d > 0:
            self.broker.update_stop(trail)
        if s.exit_on_trend_flip and (ef - es) * d < 0:
            return [Order("close", pos.side, pos.qty, "trend_flip")]
        if e.get("time_stop"):
            n, kk = e["time_stop"]
            if pos.bars_held == n and (bar.close - pos.entry_price) * d < kk * a:
                return [Order("close", pos.side, pos.qty, "time_stop")]
        return []


def run(coin: str, strategy: dict | None = None, sizing: dict | None = None, ext: dict | None = None,
        bars=None, funding=None, costs: dict | None = None):
    p = params_for(coin)
    ov = {}
    if strategy:
        ov["strategy"] = strategy
    if sizing:
        ov["sizing"] = sizing
    if costs:
        ov["costs"] = costs
    if ov:
        p = p.with_(**ov)
    if bars is None:
        bars, funding = history(coin)
    f = ind.compute(bars, funding, p)
    broker = XBroker(p)
    desk = XDesk(p, broker)
    desk.setup_ext(f, ext)
    for i, bar in enumerate(bars):
        broker.fill_pending(bar.open, bar.ts)
        broker.check_stop_bar(bar)
        broker.apply_funding(f.funding_bar[i], bar.close)
        desk.on_bar_close(f, i, now_ms=bar.ts + f.bar_ms)
    return bt.Result(p, desk, broker, f)


def compact(r) -> dict:
    """What we keep from a run: equity curve, trades, halted flag."""
    return {"curve": list(r.curve), "trades": [t.as_dict() for t in r.broker.trades], "halted": r.desk.risk.halted,
            "halt_ts": next((ev.ts for ev in r.desk.events if ev.agent == "risk" and "HALT" in ev.text), None)}


def daily_from_curve(curve):
    import pandas as pd
    s = pd.Series([e for _, e in curve], index=pd.to_datetime([t for t, _ in curve], unit="ms", utc=True))
    return s.resample("1D").last().ffill().pct_change()


def seg_stats(c: dict, t0: int, t1: int) -> dict:
    """Daily Sharpe (same definition as make_baseline / harness), return, max DD, entries in [t0, t1)."""
    import pandas as pd
    d = daily_from_curve(c["curve"])
    lo, hi = pd.Timestamp(t0, unit="ms", tz="UTC"), pd.Timestamp(t1, unit="ms", tz="UTC")
    x = d[(d.index >= lo) & (d.index < hi)].dropna()
    eq = (1 + x).cumprod()
    tr = [t for t in c["trades"] if t0 <= t["entry_ts"] < t1]
    wins = sum(t["net_pnl"] for t in tr if t["net_pnl"] > 0)
    loss = -sum(t["net_pnl"] for t in tr if t["net_pnl"] <= 0)
    return {"sharpe": float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else 0.0,
            "return": float(eq.iloc[-1] - 1) if len(eq) else 0.0,
            "max_dd": float((1 - eq / eq.cummax()).max()) if len(eq) else 0.0,
            "entries": len(tr), "pf": wins / loss if loss > 0 else float("inf")}


IS0 = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def is_stats(c):
    return seg_stats(c, IS0, SPLIT_MS)


def oos_stats(c):
    return seg_stats(c, SPLIT_MS, OOS_END_MS)
