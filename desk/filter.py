"""filter.py — THE ORDER IS THE WHOLE POINT.

An entry has to survive every gate, in this order:

    1. KILLS    manual kills, drawdown halt, daily loss lock, losing-streak cooldown
    2. CRAWLER  enough history, data not stale
    3. HARD     arithmetic: volatility band, funding sanity, spread, depth
    4. SCAN     is there a breakout at all (most bars: WAIT)
    5. SOFT     the agents' scores: trend strength, efficiency, extension, crowding
    6. SIZE     can we size it to the risk budget above the minimum ticket
    7. JEV      optional second opinion, asked last because it's the only gate that costs anything

Cheap, objective checks run first so the expensive or subjective ones never get a
vote on a trade that was already dead. Every gate is still evaluated (except Jev)
so the board can show you exactly why the desk is or isn't trading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import indicators as ind
from .models import GateResult, MarketContext, Side, Verdict
from .thresholds import Params

ORDER = ["kills", "crawler", "hard", "scan", "soft", "size", "jev"]


@dataclass
class EntryDecision:
    side: Side | None                      # None = no trade this bar
    direction: Side                        # the side the desk was evaluating (for the board)
    gates: list[GateResult] = field(default_factory=list)
    qty: float = 0.0
    stop: float = 0.0
    notional: float = 0.0
    leverage: float = 0.0
    scores: dict = field(default_factory=dict)
    jev_answers: dict | None = None
    setup: str | None = None               # "breakout" or "pullback" when the SCAN gate passed

    @property
    def blocked_by(self) -> GateResult | None:
        for g in self.gates:
            if g.verdict in (Verdict.DROP, Verdict.WAIT):
                return g
        return None


def _hard(name, value, thr, op, owner, note="") -> GateResult:
    if value is None:
        return GateResult(name, Verdict.SKIP, None, thr, op, owner, note or "no data")
    ok = value >= thr if op == "min" else value <= thr
    return GateResult(name, Verdict.PASS if ok else Verdict.DROP, value, thr, op, owner, note)


def _soft(name, value, thr, op, owner, margin, note="") -> GateResult:
    if thr is None:
        return GateResult(name, Verdict.SKIP, value, None, op, owner, note or "shown, not blocking")
    if op == "min":
        if value < thr:
            v = Verdict.DROP
        elif value < thr * (1 + margin):
            v = Verdict.MARK
        else:
            v = Verdict.PASS
    else:
        if value > thr:
            v = Verdict.DROP
        elif value > thr * (1 - margin):
            v = Verdict.MARK
        else:
            v = Verdict.PASS
    return GateResult(name, v, value, thr, op, owner, note)


def evaluate_entry(p: Params, f: ind.Features, i: int, *, equity: float, kills: list[GateResult],
                   ctx: MarketContext | None = None, now_ms: int | None = None, jev=None,
                   jev_enabled: bool = True) -> EntryDecision:
    s, h, so, z = p.strategy, p.hard, p.soft, p.sizing
    bar = f.bars[i]
    close = bar.close
    a, ef, es = f.atr[i], f.ema_fast[i], f.ema_slow[i]
    ready = i + 1 >= h.min_history_bars and None not in (a, ef, es, f.don_hi[i], f.don_lo[i], f.efficiency[i])

    # Which way is the desk looking? The trend decides; the breakout confirms.
    direction = Side.LONG if (ef is None or es is None or ef >= es) else Side.SHORT
    gates: list[GateResult] = list(kills)

    # 2. CRAWLER
    gates.append(_hard("history_bars", float(i + 1), float(h.min_history_bars), "min", "crawler"))
    age = None
    if now_ms is not None:
        age = max(0.0, (now_ms - (bar.ts + f.bar_ms)) / f.bar_ms)
    gates.append(_hard("data_age_bars", age, h.max_data_age_bars, "max", "crawler"))

    if not ready:
        return EntryDecision(None, direction, gates)

    d = direction.sign
    atr_pct = a / close * 100

    # 3. HARD
    gates.append(_hard("atr_pct_min", atr_pct, h.min_atr_pct, "min", "vet", "dead tape"))
    gates.append(_hard("atr_pct_max", atr_pct, h.max_atr_pct, "max", "vet", "disorderly"))
    gates.append(_hard("abs_funding_apr", abs(f.funding_apr[i]), h.max_abs_funding_apr, "max", "crowd"))
    gates.append(_hard("spread_bps", ctx.spread_bps if ctx else None, h.max_spread_bps, "max", "fills"))
    gates.append(_hard("depth_usd", ctx.depth_usd if ctx else None, h.min_depth_usd, "min", "fills"))

    # 4. SCAN — the setup: a breakout, a pullback inside the trend, or either (strategy.entry_mode)
    level = f.don_hi[i] if d > 0 else f.don_lo[i]
    broke = (close > level) if d > 0 else (close < level)
    dip_level = ef - d * s.pullback_atr * a
    dipped = ((close < dip_level) if d > 0 else (close > dip_level)) and \
        (not s.pullback_hold_slow or (close - es) * d > 0)
    modes = {"breakout": ("breakout",), "pullback": ("pullback",), "both": ("breakout", "pullback")}[s.entry_mode]
    hit = [m for m in modes if (broke if m == "breakout" else dipped)]
    hi_lo = "high" if d > 0 else "low"
    setup = None
    if direction is Side.SHORT and not s.allow_shorts:
        gates.append(GateResult("setup", Verdict.WAIT, close, level, "", "scan", "shorts disabled"))
    elif hit:
        setup = hit[0]
        if setup == "breakout":
            gates.append(GateResult("setup", Verdict.PASS, close, level, "min" if d > 0 else "max", "scan",
                                    f"breakout: {s.breakout_bars}-bar {hi_lo} taken out"))
        else:
            gates.append(GateResult("setup", Verdict.PASS, close, dip_level, "max" if d > 0 else "min", "scan",
                                    f"pullback: {s.pullback_atr:g} ATR through the {s.ema_fast}-EMA, trend intact"))
    else:
        want = []
        if "breakout" in modes:
            want.append(f"a close {'above' if d > 0 else 'below'} the {s.breakout_bars}-bar {hi_lo} ${level:,.0f}")
        if "pullback" in modes:
            want.append(f"a dip to ${dip_level:,.0f}")
        thr = level if "breakout" in modes else dip_level
        op = ("min" if d > 0 else "max") if "breakout" in modes else ("max" if d > 0 else "min")
        gates.append(GateResult("setup", Verdict.WAIT, close, thr, op, "scan", "waiting for " + " or ".join(want)))

    # 5. SOFT
    scores = {
        "trend_strength": ind.trend_strength(ef, es, a, d),
        "efficiency": f.efficiency[i],
        "extension": ind.extension(close, ef, a, d),
        "crowding": ind.crowding(f.funding_apr[i], d),
    }
    gates.append(_soft("trend_strength", scores["trend_strength"], so.min_trend_strength, "min", "scan", so.mark_margin))
    gates.append(_soft("efficiency", scores["efficiency"], so.min_efficiency, "min", "vet", so.mark_margin))
    gates.append(_soft("momentum_already_spent", scores["extension"], so.max_extension, "max", "vet", so.mark_margin))
    gates.append(_soft("crowding", scores["crowding"], so.max_crowding, "max", "crowd", so.mark_margin))

    # 6. SIZE
    stop = close - d * s.stop_atr * a
    risk_per_unit = abs(close - stop)
    qty = equity * z.risk_per_trade / risk_per_unit if risk_per_unit > 0 else 0.0
    qty = min(qty, equity * z.max_leverage / close) if equity > 0 else 0.0
    notional = qty * close
    gates.append(_hard("ticket_usd", notional, z.min_notional_usd, "min", "size"))

    dec = EntryDecision(None, direction, gates, qty=qty, stop=stop, notional=notional,
                        leverage=notional / equity if equity > 0 else 0.0, scores=scores, setup=setup)

    alive = all(g.verdict in (Verdict.PASS, Verdict.MARK, Verdict.SKIP) for g in gates)

    # 7. JEV — only consulted for a trade that is otherwise going ahead.
    if alive and jev is not None and jev_enabled and p.jev.mode != "off":
        from . import jev as jev_mod
        state = jev_mod.describe(jev_mod.snapshot(f, i, direction, p))
        dec.jev_answers = jev.ask(state)
        jev_gates = jev_mod.gates_from_answers(dec.jev_answers, p)
        gates.extend(jev_gates)
        alive = all(g.verdict in (Verdict.PASS, Verdict.MARK, Verdict.SKIP) for g in gates)

    if alive:
        dec.side = direction
    return dec
