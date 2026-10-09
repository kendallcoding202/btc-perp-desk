"""Backtest — the same Desk, replayed over history, with every cost charged.

Timing is strict: the desk decides on a bar's close and its orders fill at the
NEXT bar's open (plus slippage and the taker fee). Stops are checked against each
bar's high/low. Funding is charged on every bar a position is held.

The report answers one question honestly: after costs, does this make money, and
does it keep making money on data it was never tuned on (the out-of-sample slice)?
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import indicators as ind
from .brokers.sim import SimBroker
from .engine import Desk
from .models import Bar, FundingEvent, Trade
from .thresholds import Params

OOS_FRACTION = 0.4   # last 40% of the history is the out-of-sample check


@dataclass
class Result:
    p: Params
    desk: Desk
    broker: SimBroker
    features: ind.Features

    @property
    def curve(self) -> list[tuple[int, float]]:
        return self.desk.equity_curve


def run(bars: list[Bar], funding: list[FundingEvent], p: Params, jev=None, on_step=None) -> Result:
    f = ind.compute(bars, funding, p)
    broker = SimBroker(p)
    desk = Desk(p, broker, jev=jev)
    for i, bar in enumerate(bars):
        broker.fill_pending(bar.open, bar.ts)
        broker.check_stop_bar(bar)
        broker.apply_funding(f.funding_bar[i], bar.close)
        desk.on_bar_close(f, i, now_ms=bar.ts + f.bar_ms)
        if on_step is not None:
            on_step(desk, f, i)
    return Result(p, desk, broker, f)


# ---------------------------------------------------------------- metrics

def _ymd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def daily_sharpe(curve: list[tuple[int, float]]) -> float:
    by_day: dict[str, float] = {}
    for ts, eq in curve:
        by_day[_ymd(ts)] = eq
    vals = list(by_day.values())
    rets = [vals[k] / vals[k - 1] - 1 for k in range(1, len(vals)) if vals[k - 1] > 0]
    if len(rets) < 2:
        return 0.0
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    return mu / sd * math.sqrt(365) if sd > 0 else 0.0


def trade_stats(trades: list[Trade]) -> dict:
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gw, gl = sum(wins), -sum(losses)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": gw / gl if gl > 0 else (math.inf if gw > 0 else 0.0),
        "avg_win": gw / len(wins) if wins else 0.0,
        "avg_loss": -gl / len(losses) if losses else 0.0,
        "net_pnl": gw - gl,
        "longs": sum(1 for t in trades if t.side.value == "long"),
        "shorts": sum(1 for t in trades if t.side.value == "short"),
    }


def segment(curve, trades, bars, t0: int, t1: int) -> dict:
    pts = [(ts, eq) for ts, eq in curve if t0 <= ts <= t1]
    bh = [b for b in bars if t0 <= b.ts <= t1]
    if len(pts) < 2:
        return {}
    years = (pts[-1][0] - pts[0][0]) / (365.25 * 86400 * 1000)
    ret = pts[-1][1] / pts[0][1] - 1
    seg_trades = [t for t in trades if t0 <= t.exit_ts <= t1]
    out = {
        "from": _ymd(pts[0][0]), "to": _ymd(pts[-1][0]), "years": years,
        "return": ret,
        "cagr": (1 + ret) ** (1 / years) - 1 if years > 0.25 and ret > -1 else None,
        "max_drawdown": max_drawdown([e for _, e in pts]),
        "sharpe": daily_sharpe(pts),
        "buy_hold_return": bh[-1].close / bh[0].open - 1 if bh else None,
        "buy_hold_drawdown": max_drawdown([b.close for b in bh]) if bh else None,
    }
    out.update(trade_stats(seg_trades))
    return out


def summarize(r: Result) -> dict:
    bars, curve, trades = r.features.bars, r.curve, r.broker.trades
    t_start, t_end = curve[0][0], curve[-1][0]
    split = t_start + int((t_end - t_start) * (1 - OOS_FRACTION))
    years = sorted({_ymd(ts)[:4] for ts, _ in curve})
    per_year = {}
    for y in years:
        t0 = int(datetime(int(y), 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        t1 = int(datetime(int(y) + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000) - 1
        per_year[y] = segment(curve, trades, bars, t0, t1)
    in_pos = sum(t.exit_ts - t.entry_ts for t in trades)
    full = segment(curve, trades, bars, t_start, t_end)
    full.update({
        "final_equity": curve[-1][1],
        "fees": r.broker.fees_total,
        "funding": r.broker.funding_total,
        "exposure": in_pos / (t_end - t_start) if t_end > t_start else 0.0,
        "halted": r.desk.risk.halted,
        "halt_reason": r.desk.risk.halt_reason,
        "open_position": None if r.broker.position is None else r.broker.position.side.value,
    })
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval": r.p.market.interval,
        "params": r.p.as_dict(),
        "full": full,
        "in_sample": segment(curve, trades, bars, t_start, split),
        "out_of_sample": segment(curve, trades, bars, split + 1, t_end),
        "per_year": per_year,
        "trades": [t.as_dict() for t in trades],
    }


def promotion_check(summary: dict, p: Params) -> list[tuple[str, bool, str]]:
    """The gate between paper and live. Every line must be True."""
    pr = p.promotion
    full, oos = summary["full"], summary["out_of_sample"]
    pf = oos.get("profit_factor", 0.0)
    return [
        ("history ≥ %.0f years" % pr.min_backtest_years, full["years"] >= pr.min_backtest_years,
         f"{full['years']:.1f} years"),
        ("out-of-sample return > %.0f%%" % (pr.min_oos_return * 100), oos.get("return", -1) > pr.min_oos_return,
         f"{oos.get('return', 0):+.1%}"),
        ("out-of-sample profit factor ≥ %.2f" % pr.min_oos_profit_factor, pf >= pr.min_oos_profit_factor,
         f"{pf:.2f}"),
        ("max drawdown ≤ %.0f%%" % (pr.max_backtest_drawdown * 100), full["max_drawdown"] <= pr.max_backtest_drawdown,
         f"{full['max_drawdown']:.1%}"),
        ("never hit the drawdown halt", not full["halted"], full["halt_reason"] or "ok"),
    ]


# ---------------------------------------------------------------- report

def _pct(x) -> str:
    return "—" if x is None else f"{x:+.1%}"


def write_report(summary: dict, p: Params, out_dir: str | Path = "reports", extra_md: str = "",
                 name: str = "backtest") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    full, ins, oos = summary["full"], summary["in_sample"], summary["out_of_sample"]
    checks = promotion_check(summary, p)
    lines = [
        f"# Backtest — BTC perp, {summary['interval']} bars",
        "",
        f"{full['from']} → {full['to']} ({full['years']:.1f} years). Generated {summary['generated']}.",
        f"Costs charged: taker fee {p.costs.taker_fee_bps} bps + slippage {p.costs.slippage_bps} bps per side, "
        f"plus real funding prints. Start equity ${p.sizing.starting_equity:,.0f}, "
        f"risk {p.sizing.risk_per_trade:.1%}/trade, max {p.sizing.max_leverage:.0f}x.",
        "",
        "| | Desk | Buy & hold BTC |",
        "|---|---|---|",
        f"| Total return | {_pct(full['return'])} | {_pct(full['buy_hold_return'])} |",
        f"| CAGR | {_pct(full['cagr'])} | |",
        f"| Max drawdown | {full['max_drawdown']:.1%} | {full['buy_hold_drawdown']:.1%} |",
        f"| Sharpe (daily) | {full['sharpe']:.2f} | |",
        f"| Final equity | ${full['final_equity']:,.0f} | |",
        "",
        f"Trades {full['trades']} ({full['longs']} long / {full['shorts']} short), win rate {full['win_rate']:.0%}, "
        f"profit factor {full['profit_factor']:.2f}, avg win ${full['avg_win']:,.0f}, avg loss ${full['avg_loss']:,.0f}. "
        f"Fees paid ${full['fees']:,.0f}, funding paid ${full['funding']:,.0f}. "
        f"In the market {full['exposure']:.0%} of the time.",
        "",
        "## In-sample vs out-of-sample",
        "",
        "| | From | To | Return | Max DD | PF | Trades | Buy & hold |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, s in (("In-sample (first 60%)", ins), ("Out-of-sample (last 40%)", oos)):
        if s:
            lines.append(f"| {label} | {s['from']} | {s['to']} | {_pct(s['return'])} | {s['max_drawdown']:.1%} | "
                         f"{s['profit_factor']:.2f} | {s['trades']} | {_pct(s['buy_hold_return'])} |")
    lines += ["", "## By year", "", "| Year | Return | Max DD | Trades | Win rate | Buy & hold |",
              "|---|---|---|---|---|---|"]
    for y, s in summary["per_year"].items():
        if s:
            lines.append(f"| {y} | {_pct(s['return'])} | {s['max_drawdown']:.1%} | {s['trades']} | "
                         f"{s['win_rate']:.0%} | {_pct(s['buy_hold_return'])} |")
    lines += ["", "## Go-live gate (`python -m desk live` checks this)", ""]
    for label, ok, val in checks:
        lines.append(f"- {'✅' if ok else '❌'} {label} — {val}")
    lines += ["", "Passing this gate is necessary, not sufficient: the desk also needs "
              f"{p.promotion.min_paper_days:.0f}+ days of profitable paper trading before it will trade real money.", ""]
    if extra_md:
        lines += [extra_md, ""]
    md = out_dir / f"{name}.md"
    md.write_text("\n".join(lines))
    (out_dir / f"{name}.json").write_text(json.dumps({**summary, "promotion": checks}, indent=1, default=str))
    return md
