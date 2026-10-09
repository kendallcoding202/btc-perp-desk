"""Research sections appended to the backtest report (`python -m desk backtest --research`).

Not for picking the best numbers — for checking the default isn't a lucky outlier:
if small changes to the settings flip the result from profit to loss, the "edge" is noise.
"""

from __future__ import annotations

import statistics as st
from datetime import datetime, timezone

from . import backtest as bt
from . import jev as jev_mod
from .thresholds import Params

SPLIT_YEAR = 2024
OPEN_SOFT = {"min_trend_strength": None, "min_efficiency": None, "max_extension": None, "max_crowding": None}


def _split_ms() -> int:
    return int(datetime(SPLIT_YEAR, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _row(r) -> tuple[dict, dict, dict]:
    s = bt.summarize(r)
    split = _split_ms()
    a = bt.segment(r.curve, r.broker.trades, r.features.bars, 0, split)
    b = bt.segment(r.curve, r.broker.trades, r.features.bars, split, 10 ** 14)
    return s["full"], a, b


def sensitivity(bars, funding, p: Params) -> str:
    lines = ["## Sensitivity — does a nearby setting still make money?", "",
             "Full-period total return / max drawdown / profit factor.", "",
             "| breakout bars \\ stop=trail ATR | 2.0 | 3.0 | 4.0 |", "|---|---|---|---|"]
    for nb in (20, 30, 45, 60):
        cells = []
        for k in (2.0, 3.0, 4.0):
            r = bt.run(bars, funding, p.with_(strategy={"breakout_bars": nb, "stop_atr": k, "trail_atr": k}))
            s = bt.summarize(r)["full"]
            cells.append(f"{s['return']:+.0%} / {s['max_drawdown']:.0%} / {s['profit_factor']:.2f}")
        lines.append(f"| {nb} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def ablation(bars, funding, p: Params) -> str:
    """Why the defaults are what they are. Selection was made on the in-sample column only."""
    old = {"min_trend_strength": 0.50, "min_efficiency": 0.15, "max_extension": 0.80, "max_crowding": 0.85}

    def soft(**on):
        d = dict(OPEN_SOFT)
        d.update({k: old[k] for k in on})
        return d

    variants = [
        ("original design: all 4 soft gates, longs+shorts", {"soft": old, "strategy": {"allow_shorts": True}}),
        ("all 4 soft gates, longs only", {"soft": old}),
        ("no soft gates, longs+shorts", {"soft": OPEN_SOFT, "strategy": {"allow_shorts": True}}),
        ("no soft gates, longs only", {"soft": OPEN_SOFT}),
        ("+ trend_strength only", {"soft": soft(min_trend_strength=1)}),
        ("+ efficiency only", {"soft": soft(min_efficiency=1)}),
        ("+ extension only", {"soft": soft(max_extension=1)}),
        ("+ crowding only  ← default", {}),
    ]
    lines = [f"## Ablation — which gates earn their place (chosen on {SPLIT_YEAR - 4}–{SPLIT_YEAR - 1} only)", "",
             "| variant | in-sample return | IS PF | out-of-sample return | OOS PF | OOS trades | hit drawdown halt |",
             "|---|---|---|---|---|---|---|"]
    for name, ov in variants:
        r = bt.run(bars, funding, p.with_(**ov) if ov else p)
        full, a, b = _row(r)
        halt = f"yes, {_halt_date(r)}" if full["halted"] else "no"
        lines.append(f"| {name} | {a['return']:+.1%} | {a['profit_factor']:.2f} | {b['return']:+.1%} | "
                     f"{b['profit_factor']:.2f} | {b['trades']} | {halt} |")
    return "\n".join(lines)


def _halt_date(r) -> str:
    for e in r.desk.events:
        if e.agent == "risk" and "HALT" in e.text:
            return bt._ymd(e.ts)
    return "?"


def sizing(bars, funding, p: Params) -> str:
    lines = ["## Position size — profit scales with risk, and so does the pain", "",
             f"| risk per trade | total return | CAGR | max drawdown | worst year | hit {p.risk.max_drawdown_halt:.0%} halt? |",
             "|---|---|---|---|---|---|"]
    for rk in (0.01, 0.02, 0.03, 0.05):
        r = bt.run(bars, funding, p.with_(sizing={"risk_per_trade": rk, "max_leverage": 5.0}))
        s = bt.summarize(r)
        f = s["full"]
        worst = min(v["return"] for v in s["per_year"].values() if v)
        lines.append(f"| {rk:.0%}{' ← default' if rk == p.sizing.risk_per_trade else ''} | {f['return']:+.0%} | "
                     f"{bt._pct(f['cagr'])} | {f['max_drawdown']:.1%} | {worst:+.1%} | {'yes' if f['halted'] else 'no'} |")
    return "\n".join(lines)


def trade_sooner(bars, funding, p: Params) -> str:
    """'Can it trade more often?' — tested: pullback (buy-the-dip) entries, alone and alongside breakouts."""
    variants = [("breakouts only ← default", {"entry_mode": "breakout"})]
    for k in (0.5, 1.0, 2.0):
        variants.append((f"pullbacks only, {k:g} ATR dip", {"entry_mode": "pullback", "pullback_atr": k}))
    for k in (0.5, 1.0, 2.0):
        variants.append((f"breakouts + {k:g} ATR pullbacks", {"entry_mode": "both", "pullback_atr": k}))
    variants.append(("breakouts + 1 ATR pullbacks + shorts", {"entry_mode": "both", "pullback_atr": 1.0,
                                                             "allow_shorts": True}))
    lines = ["## Trade sooner? — pullback entries and shorts", "",
             "Breakouts can mean days without a trade. These variants trade more often; chosen on in-sample only.", "",
             "| entries | trades/yr | in-sample return | out-of-sample return | max drawdown | hit drawdown halt |",
             "|---|---|---|---|---|---|"]
    for name, ov in variants:
        r = bt.run(bars, funding, p.with_(strategy=ov))
        full, a, b = _row(r)
        lines.append(f"| {name} | {full['trades'] / full['years']:.0f} | {a['return']:+.0%} | {b['return']:+.0%} | "
                     f"{full['max_drawdown']:.0%} | {'yes, ' + _halt_date(r) if full['halted'] else 'no'} |")
    return "\n".join(lines)


def take_profit(bars, funding, p: Params) -> str:
    """'Shouldn't it sell when it's up?' — tested: a fixed profit target vs letting the trailing stop exit."""
    lines = ["## Take profit — sell when it's up, or let the trailing stop do it?", "",
             "The trailing stop already sells winners: it ratchets up behind every new high and never moves down. "
             "This adds a fixed profit target on top, in ATRs from the entry.", "",
             "| take profit at | in-sample return | out-of-sample return | total return | avg win ÷ avg loss |",
             "|---|---|---|---|---|"]
    for k in (None, 2, 4, 6, 12):
        r = bt.run(bars, funding, p.with_(strategy={"take_profit_atr": k}))
        full, a, b = _row(r)
        ratio = full["avg_win"] / -full["avg_loss"] if full["avg_loss"] else 0.0
        label = "off (trailing stop only) ← default" if k is None else f"+{k} ATR"
        lines.append(f"| {label} | {a['return']:+.0%} | {b['return']:+.0%} | {full['return']:+.0%} | {ratio:.1f}x |")
    return "\n".join(lines)


def costs(bars, funding, p: Params) -> str:
    lines = ["## Cost stress", "", "| taker fee bps | slippage bps | total return | PF |", "|---|---|---|---|"]
    for fee, slip in ((p.costs.taker_fee_bps, p.costs.slippage_bps), (5.0, 10.0), (7.0, 15.0)):
        r = bt.run(bars, funding, p.with_(costs={"taker_fee_bps": fee, "slippage_bps": slip}))
        s = bt.summarize(r)["full"]
        lines.append(f"| {fee:g} | {slip:g} | {s['return']:+.1%} | {s['profit_factor']:.2f} |")
    return "\n".join(lines)


def jev_experiment(bars, funding, p: Params, client) -> str:
    """Replay every historical entry through Jev and see if its answers predict the outcome."""
    r = bt.run(bars, funding, p)
    idx = {b.ts: k for k, b in enumerate(bars)}
    rows = []
    for t in r.broker.trades:
        i = idx[t.entry_ts] - 1  # the decision bar is the one before the fill
        ans = client.ask(jev_mod.describe(jev_mod.snapshot(r.features, i, t.side, p)))
        if ans:
            rows.append((ans["momentum_already_spent"]["noul"], ans["follow_through"]["noul"],
                         ans["regime"]["probabilities"].get("trend", 0.0), t.net_pnl))
    if len(rows) < 10:
        return "## Jev experiment\n\nSkipped — JEV_API_KEY not set or Jev unreachable."
    pnl = [x[3] for x in rows]

    def corr(xs, ys):
        mx, my, sx, sy = st.mean(xs), st.mean(ys), st.pstdev(xs), st.pstdev(ys)
        return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs) / (sx * sy) if sx and sy else 0.0

    lines = ["## Jev experiment — does a second opinion predict which trades win?", "",
             f"Every one of the {len(rows)} historical entries was described to Jev (relative numbers only, no dates "
             "or prices) and asked the desk's three questions. 0 correlation = no predictive value.", "",
             "| question | answer range | correlation with trade P&L | P&L when Jev was below median | above median |",
             "|---|---|---|---|---|"]
    for k, name in enumerate(("momentum_already_spent (noul)", "follow_through (noul)", "regime = trend (choice)")):
        v = [x[k] for x in rows]
        ordered = sorted(zip(v, pnl))
        h = len(ordered) // 2
        lines.append(f"| {name} | {min(v):.2f}–{max(v):.2f} | {corr(v, pnl):+.2f} | "
                     f"${sum(x for _, x in ordered[:h]):,.0f} | ${sum(x for _, x in ordered[h:]):,.0f} |")
    gated = bt.run(bars, funding, p.with_(jev={"mode": "gate"}), jev=client)
    g, base = bt.summarize(gated)["full"], bt.summarize(r)["full"]
    lines += ["", f"With Jev allowed to veto at the default thresholds: {g['return']:+.1%} "
              f"({g['trades']} trades) vs {base['return']:+.1%} ({base['trades']} trades) without.",
              "", "Verdict: Jev stays **advisory** (shown on the board, no veto) unless a future test says otherwise."]
    return "\n".join(lines)
