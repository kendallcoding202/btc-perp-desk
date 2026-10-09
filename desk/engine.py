"""The desk. One instance, same code, whether it's replaying 2020 or trading right now.

CHIEF never takes a position. On every closed bar it:
  1. updates the desk-level brakes (daily loss lock, drawdown halt, losing-streak cooldown)
  2. lets RISK manage an open position (trail the stop, exit on trend flip, flatten on a kill)
  3. if flat, runs the entry filter (filter.py) and, if everything passes, hands FILLS an order

The broker decides when orders fill: the backtest fills them at the next bar's open,
paper/live fill them immediately at the current price.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import indicators as ind
from .filter import EntryDecision, evaluate_entry
from .models import GateResult, MarketContext, Order, Verdict
from .thresholds import Params


@dataclass
class Kills:
    """Manual switches, flipped from the dashboard or by editing state/kills.json."""
    flat_kill: bool = False     # close the position now and take no new entries
    entry_kill: bool = False    # keep the position and its stop, take no new entries
    jev_kill: bool = False      # stop consulting Jev

    @classmethod
    def load(cls, path: Path) -> "Kills":
        try:
            d = json.loads(path.read_text())
            return cls(**{k: bool(d.get(k, False)) for k in ("flat_kill", "entry_kill", "jev_kill")})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        tmp.replace(path)


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    day_locked: str = ""                 # UTC date the daily loss limit tripped on
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    consec_losses: int = 0
    cooldown_until_ts: int = 0
    trades_seen: int = 0


@dataclass
class Event:
    ts: int
    agent: str
    text: str


@dataclass
class Desk:
    p: Params
    broker: object
    jev: object | None = None
    kills: Kills = field(default_factory=Kills)
    risk: RiskState = field(default_factory=RiskState)
    last_decision: EntryDecision | None = None
    last_i: int = -1
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    events: deque = field(default_factory=lambda: deque(maxlen=200))

    def __post_init__(self):
        if not self.risk.peak_equity:
            self.risk.peak_equity = self.broker.cash

    @property
    def day_locked(self) -> bool:
        return bool(self.risk.day) and self.risk.day_locked == self.risk.day

    # ------------------------------------------------------------------
    def say(self, ts: int, agent: str, text: str) -> None:
        self.events.append(Event(ts, agent, text))

    def kill_gates(self, ts: int, equity: float) -> list[GateResult]:
        r, k = self.risk, self.kills
        dd = 1 - equity / r.peak_equity if r.peak_equity > 0 else 0.0
        day_loss = 1 - equity / r.day_start_equity if r.day_start_equity > 0 else 0.0
        g = [
            GateResult("drawdown_halt", Verdict.DROP if r.halted else Verdict.PASS, dd,
                       self.p.risk.max_drawdown_halt, "max", "risk", r.halt_reason),
            GateResult("daily_loss", Verdict.DROP if self.day_locked else Verdict.PASS, day_loss,
                       self.p.risk.daily_loss_limit, "max", "risk"),
            GateResult("cooldown", Verdict.DROP if ts < r.cooldown_until_ts else Verdict.PASS,
                       float(r.consec_losses), float(self.p.risk.max_consecutive_losses), "max", "risk"),
            GateResult("manual_kill", Verdict.DROP if (k.flat_kill or k.entry_kill) else Verdict.PASS,
                       owner="chief", note="flat_kill" if k.flat_kill else ("entry_kill" if k.entry_kill else "")),
        ]
        pos = self.broker.position
        if pos is not None:
            g.append(GateResult("flat", Verdict.WAIT, owner="chief", note=f"holding {pos.side.value}"))
        return g

    def _update_risk(self, ts_close: int, equity: float, bar_ms: int) -> list[str]:
        """Returns reasons to flatten (empty = none)."""
        r, rp = self.risk, self.p.risk
        reasons = []
        day = datetime.fromtimestamp(ts_close / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != r.day:
            r.day, r.day_start_equity = day, equity
        r.peak_equity = max(r.peak_equity, equity)

        trades = self.broker.trades
        for t in trades[r.trades_seen:]:
            if t.net_pnl < 0:
                r.consec_losses += 1
                if r.consec_losses >= rp.max_consecutive_losses:
                    r.cooldown_until_ts = ts_close + rp.cooldown_bars * bar_ms
                    self.say(ts_close, "risk", f"{r.consec_losses} losers in a row. sitting out {rp.cooldown_bars} bars.")
                    r.consec_losses = 0
            else:
                r.consec_losses = 0
        r.trades_seen = len(trades)

        if not r.halted and r.peak_equity > 0 and equity <= r.peak_equity * (1 - rp.max_drawdown_halt):
            r.halted = True
            r.halt_reason = f"equity {equity:,.0f} is {1 - equity / r.peak_equity:.0%} below peak {r.peak_equity:,.0f}"
            self.say(ts_close, "risk", f"DRAWDOWN HALT — {r.halt_reason}. flat and done until a human resets it.")
            reasons.append("drawdown_halt")
        if r.day_locked != day and r.day_start_equity > 0 and equity <= r.day_start_equity * (1 - rp.daily_loss_limit):
            r.day_locked = day
            self.say(ts_close, "risk", f"down {1 - equity / r.day_start_equity:.1%} today. flat, no entries until tomorrow UTC.")
            reasons.append("daily_loss")
        if self.kills.flat_kill:
            reasons.append("flat_kill")
        return reasons

    # ------------------------------------------------------------------
    def on_bar_close(self, f: ind.Features, i: int, ctx: MarketContext | None = None,
                     now_ms: int | None = None) -> list[Order]:
        bar = f.bars[i]
        ts_close = bar.ts + f.bar_ms
        price = bar.close
        equity = self.broker.equity(price)
        orders: list[Order] = []

        flatten = self._update_risk(ts_close, equity, f.bar_ms)
        pos = self.broker.position

        if pos is not None:
            if flatten or self.risk.halted:
                orders.append(Order("close", pos.side, pos.qty, (flatten or ["drawdown_halt"])[0]))
            else:
                orders += self._manage(f, i)

        decision = evaluate_entry(
            self.p, f, i, equity=equity, kills=self.kill_gates(ts_close, equity), ctx=ctx, now_ms=now_ms,
            jev=self.jev, jev_enabled=not self.kills.jev_kill,
        )
        self.last_decision, self.last_i = decision, i

        if decision.side is not None and pos is None and not orders:
            tp_atr = self.p.strategy.take_profit_atr
            take_profit = (bar.close + decision.side.sign * tp_atr * f.atr[i]) if tp_atr else None
            orders.append(Order("open", decision.side, decision.qty, decision.setup or "entry", stop=decision.stop,
                                meta={"leverage": decision.leverage, "notional": decision.notional,
                                      "take_profit": take_profit}))
            self.say(ts_close, "chief", f"go ({decision.setup}): {decision.side.value} {decision.qty:.4f} BTC "
                                        f"(${decision.notional:,.0f}, {decision.leverage:.1f}x), stop {decision.stop:,.0f}")

        for o in orders:
            self.broker.submit(o)
        self.equity_curve.append((ts_close, equity))
        return orders

    def _manage(self, f: ind.Features, i: int) -> list[Order]:
        """RISK's job while a position is open: trail the stop, get out when the trend is gone."""
        s = self.p.strategy
        pos = self.broker.position
        bar = f.bars[i]
        a, ef, es = f.atr[i], f.ema_fast[i], f.ema_slow[i]
        if a is None or ef is None or es is None:
            return []
        d = pos.side.sign
        if (bar.close - pos.best_close) * d > 0:
            pos.best_close = bar.close
        trail = pos.best_close - d * s.trail_atr * a
        if (trail - pos.stop) * d > 0:
            self.broker.update_stop(trail)
        if s.exit_on_trend_flip and (ef - es) * d < 0:
            self.say(bar.ts + f.bar_ms, "risk", f"trend flipped against the {pos.side.value}. out.")
            return [Order("close", pos.side, pos.qty, "trend_flip")]
        return []

    def flatten_now(self, price: float, ts: int, reason: str):
        """Immediate exit used by live/paper when a kill is hit between bars."""
        t = self.broker.close_now(price, ts, reason)
        if t is not None:
            self.say(ts, "fills", f"flattened {t.side.value} @ {t.exit_price:,.0f} ({reason}), net {t.net_pnl:+,.2f}")
        return t

    def reset_halt(self, equity: float) -> None:
        self.risk.halted = False
        self.risk.halt_reason = ""
        self.risk.peak_equity = equity

    # ---- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {"risk": self.risk.__dict__, "equity_curve": self.equity_curve[-5000:],
                "events": [e.__dict__ for e in self.events]}

    def load_dict(self, d: dict) -> None:
        self.risk = RiskState(**d.get("risk", {}))
        self.equity_curve = [tuple(x) for x in d.get("equity_curve", [])]
        self.events = deque((Event(**e) for e in d.get("events", [])), maxlen=200)
