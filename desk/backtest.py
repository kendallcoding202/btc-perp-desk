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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import flows as flows_mod
from . import indicators as ind
from . import venue as venue_mod
from .brokers.sim import SimBroker, VenueSimBroker
from .engine import Desk
from .models import Bar, FundingEvent, Trade
from .thresholds import INTERVAL_SECONDS, Params, venue_for

OOS_FRACTION = 0.4   # last 40% of the history is the out-of-sample check


@dataclass
class Result:
    p: Params
    desk: Desk
    broker: SimBroker
    features: ind.Features
    flows: list = field(default_factory=list)     # deposits and withdrawals recorded (run(flows=...)), as paper does
    dropped_bars: int = 0                          # bars wholly inside a venue closure, left out (venue only)

    @property
    def curve(self) -> list[tuple[int, float]]:
        return self.desk.equity_curve


def make_desk(p: Params, jev=None, windows=(), margin: dict | None = None, spec=None):
    """(broker, desk) for an account: SimBroker and a plain Desk, or, for a venue with its own rules
    (thresholds.VENUES: Coinbase for BTC; `spec` overrides them), VenueSimBroker and a Desk that sizes in whole
    contracts under them."""
    spec = spec or venue_for(p)
    if spec is None:
        broker = SimBroker(p)
        return broker, Desk(p, broker, jev=jev)
    rules = venue_mod.Rules(spec, windows, margin)
    broker = VenueSimBroker(p, rules)
    return broker, Desk(p, broker, jev=jev, venue=rules)


def run(bars: list[Bar], funding: list[FundingEvent], p: Params, jev=None, on_step=None, windows=(),
        flows=None, start_ms: int | None = None, spec=None) -> Result:
    """The desk over `bars`. With venue rules, the bars wholly inside one of its closures (known, plus `windows`) are
    left out first: the history is Binance's, which trades through them, and the venue has no candle then. `flows`
    (flows.Flow, in date order) go in as paper puts them in when it catches up: at the open of the first bar on or after
    their date's 00:00 UTC, before that bar's fills. `start_ms`: the account opens then; the bars before it only warm
    up the indicators."""
    broker, desk = make_desk(p, jev=jev, windows=windows, spec=spec)
    dropped = 0
    if desk.venue is not None:
        kept = venue_mod.tradable(bars, desk.venue.sessions, INTERVAL_SECONDS[p.market.interval] * 1000)
        dropped, bars = len(bars) - len(kept), kept
    f = ind.compute(bars, funding, p)
    todo, recorded = list(flows or []), []
    for i, bar in enumerate(bars):
        if start_ms is not None and bar.ts < start_ms:
            continue
        while todo and todo[0].ts <= bar.ts:
            if flows_mod.apply_desk(desk, todo[0], bar.ts, bar.open, p.sizing.starting_equity, recorded) is None:
                break
            todo.pop(0)
        broker.fill_pending(bar.open, bar.ts)
        broker.check_stop_bar(bar)
        broker.apply_funding(f.funding_bar[i], bar.close)
        desk.on_bar_close(f, i, now_ms=bar.ts + f.bar_ms)
        if on_step is not None:
            on_step(desk, f, i)
    return Result(p, desk, broker, f, recorded, dropped)


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
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval": r.p.market.interval,
        "coin": r.p.market.symbol,
        "params": r.p.as_dict(),
        "full": full,
        "in_sample": segment(curve, trades, bars, t_start, split),
        "out_of_sample": segment(curve, trades, bars, split + 1, t_end),
        "per_year": per_year,
        "trades": [t.as_dict() for t in trades],
    }
    if r.desk.venue is not None:        # only an account with venue rules: the others' reports are as they were
        out["venue"] = venue_summary(r)
    return out


def venue_comparison(bars, funding, p: Params, summary: dict) -> tuple[str, dict]:
    """The venue's rules against fractional sizing on the same history, one rule at a time: from the result as it
    stood before (Hyperliquid's 4.5 bps, 5x, any size) to this report, so each rule's cost shows on its own."""
    from dataclasses import replace

    spec = venue_for(p)
    frac = p.with_(market={"venue": "hyperliquid"})
    steps = [
        ("fractional_before", "fractional as before: Hyperliquid 4.5 bps, 5x (the previous report)",
         dict(q=frac.with_(sizing={"max_leverage": 5.0}, costs={"taker_fee_bps": 4.5}))),
        ("fractional_same_costs", f"fractional, {p.sizing.max_leverage:g}x, {p.costs.taker_fee_bps:g} bps", dict(q=frac)),
        ("contracts", f"+ whole {spec.contract_size:g} BTC contracts, the margin check",
         dict(q=p, spec=replace(spec, fee_min_per_contract=0.0, weekly_break=None, known_closures=()))),
        ("fee_minimum", f"+ the ${spec.fee_min_per_contract:.2f} a contract fee minimum",
         dict(q=p, spec=replace(spec, weekly_break=None, known_closures=()))),
        ("coinbase", "+ the sessions: **Coinbase rules** (this report)", None),
    ]
    below = spec.fee_min_per_contract / (p.costs.taker_fee_bps / 1e4)          # a contract worth less: the minimum
    bound = [t for t in summary["trades"] if t["qty"] * t["entry_price"] < below * round(t["qty"] / spec.contract_size)]
    years = sorted({_ymd(t["entry_ts"])[:4] for t in bound})
    lines = ["## Coinbase rules vs fractional sizing", "",
             "The same strategy on the same Binance history, Coinbase's rules added one at a time. The first row is this "
             "report as it stood before the BTC account moved to Coinbase rules. The fee minimum binds while a contract "
             f"is worth under ${below:,.0f}, BTC under ${below / spec.contract_size:,.0f}: at the entry of {len(bound)} "
             f"of the {len(summary['trades'])} trades" + (f" ({years[0]}-{years[-1][2:]})" if years else "") + ". The "
             "sessions skip entries into the weekly break and leave out the bars inside known maintenance windows.", "",
             "| sizing | total return | CAGR | max drawdown | out-of-sample (CAGR) | PF | trades | fees | funding | "
             "final |", "|---|---|---|---|---|---|---|---|---|---|"]
    out = {}
    for key, label, how in steps:
        s = summary if how is None else summarize(run(bars, funding, how["q"], spec=how.get("spec")))
        f, o = s["full"], s["out_of_sample"]
        lines.append(f"| {label} | {_pct(f['return'])} | {_pct(f['cagr'])} | {f['max_drawdown']:.1%} | "
                     f"{_pct(o['return'])} ({_pct(o['cagr'])}/yr) | {f['profit_factor']:.2f} | {f['trades']} | "
                     f"${f['fees']:,.0f} | ${f['funding']:,.0f} | ${f['final_equity']:,.0f} |")
        out[key] = {k: f[k] for k in ("return", "cagr", "max_drawdown", "trades", "fees", "funding", "final_equity",
                                      "profit_factor")} | {"oos_return": o["return"], "oos_cagr": o["cagr"]}
    return "\n".join(lines), out


def pace(bars, funding, p: Params, oos_cagr: float | None = None, start: str = "2024-01-01", monthly: float = 100.0,
         months: int = 60, target: float = 100_000.0) -> tuple[str, dict]:
    """The pace to `target` with the planned money: a fresh account opened at `start` with the start equity, plus
    `monthly` dollars on the 1st of every month after (as paper puts them in), traded to the end of the history. Its
    pace is the time-weighted one (the units' value: deposits aren't growth); the years to the target count the plan's
    deposits as they come (flows.years_to), from the start equity, at that pace and at the full run's out-of-sample
    one."""
    from datetime import date

    d0 = date.fromisoformat(start)
    t0 = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp() * 1000)
    plan, y, m = [], d0.year, d0.month
    for k in range(months):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        d = date(y, m, 1)
        plan.append(flows_mod.Flow(f"monthly-{d:%Y-%m}", d.isoformat(),
                                   int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000), monthly, "plan"))
    end = bars[-1].ts
    plan = [fl for fl in plan if fl.ts <= end]
    def fresh(q):
        r = run(bars, funding, q, flows=plan, start_ms=t0)
        st = flows_mod.State(q.sizing.starting_equity, r.flows)
        nav, k, units = [], 0, q.sizing.starting_equity        # the units' value at each close: deposits left out
        for ts, e in r.curve:
            while k < len(r.flows) and r.flows[k]["ts"] < ts:
                units += r.flows[k]["units"]
                k += 1
            nav.append(e / units)
        yrs = (r.curve[-1][0] - r.curve[0][0]) / (365.25 * 86400 * 1000)
        twr = st.nav(r.curve[-1][1]) - 1
        return r, st, yrs, twr, ((1 + twr) ** (1 / yrs) - 1 if yrs > 0.25 and twr > -1 else None), max_drawdown(nav)

    r, st, years, twr, cagr, dd = fresh(p)
    eq = r.curve[-1][1]
    frac = fresh(p.with_(market={"venue": "hyperliquid"}, sizing={"max_leverage": 5.0}, costs={"taker_fee_bps": 4.5}))
    future = [((k + 1) / 12, monthly) for k in range(months)]
    out = {"start": start, "to": _ymd(r.curve[-1][0]), "years": years, "start_equity": p.sizing.starting_equity,
           "monthly": monthly, "deposits": len(r.flows), "deposited": st.net, "final_equity": eq, "twr": twr,
           "cagr": cagr, "trades": len(r.broker.trades), "max_drawdown": dd,
           "fractional_before": {"final_equity": frac[0].curve[-1][1], "cagr": frac[4], "max_drawdown": frac[5],
                                 "trades": len(frac[0].broker.trades)},
           "skipped": dict(r.desk.venue_memo.get("skipped", {})) if r.desk.venue is not None else {},
           "target": target, "plan_months": months,
           "years_to_target": flows_mod.years_to(target, p.sizing.starting_equity, cagr, future),
           "oos_cagr": oos_cagr,
           "years_to_target_at_oos": flows_mod.years_to(target, p.sizing.starting_equity, oos_cagr, future)}

    def five(rate):     # what the plan leaves after its 5 years at `rate`
        e = p.sizing.starting_equity
        for _ in range(months):
            e = e * (1 + rate) ** (1 / 12) + monthly
        return e

    if cagr is not None:
        out["after_plan"] = five(cagr)
    lines = [f"## Pace to ${target:,.0f} with ${p.sizing.starting_equity:,.0f} + ${monthly:,.0f} a month", "",
             f"A fresh ${p.sizing.starting_equity:,.0f} account opened {start} under these rules, ${monthly:,.0f} put in "
             f"on the 1st of every month after it ({out['deposits']} deposits, ${st.net:,.0f}, to {out['to']}), as the "
             f"paper account will: it ended at ${eq:,.0f} with {out['trades']} trades, its worst drop "
             f"{out['max_drawdown']:.0%} (time-weighted). Its time-weighted pace (deposits aren't growth) was "
             f"{_pct(cagr)}/yr"
             + (f"; entries the venue skipped: {', '.join(f'{v} {k}' for k, v in out['skipped'].items())}"
                if out["skipped"] else "; the venue skipped no entry")
             + f". Fractional sizing as before (Hyperliquid, 4.5 bps, 5x) from the same start: "
               f"${out['fractional_before']['final_equity']:,.0f}, {_pct(frac[4])}/yr.", ""]
    yt = out["years_to_target"]
    lines.append(f"At that pace, ${p.sizing.starting_equity:,.0f} plus ${monthly:,.0f} a month for {months // 12} years "
                 f"reaches ${target:,.0f} in about {yt:.0f} years" if yt else
                 f"At that pace it doesn't reach ${target:,.0f}")
    if cagr is not None:
        lines[-1] += (f" (about ${out['after_plan']:,.0f} after the {months // 12} years, "
                      f"${p.sizing.starting_equity + months * monthly:,.0f} of it put in).")
    yo = out["years_to_target_at_oos"]
    if oos_cagr is not None:
        lines += ["", f"At the full run's out-of-sample pace ({_pct(oos_cagr)}/yr, the pace the board projects the "
                      f"account at), the same plan reaches it in about {yo:.0f} years." if yo else
                  f"At the full run's out-of-sample pace ({_pct(oos_cagr)}/yr) the plan doesn't reach it."]
    lines += ["", "Backtest paces, not forecasts: the settings were picked on 2020-23, and 2026 so far has lost money."]
    return "\n".join(lines), out


def venue_summary(r: Result) -> dict:
    """What the venue's rules did over the backtest: contracts per trade, entries skipped (per reason, once per
    episode), fees, and what's estimated."""
    rules = r.desk.venue
    n = sorted(rules.contracts(t.qty) for t in r.broker.trades)
    memo = r.desk.venue_memo
    return {
        "name": rules.name, "spec": asdict(rules.spec), "margin": dict(rules.margin),
        "contracts": {"min": n[0], "median": n[len(n) // 2], "max": n[-1]} if n else None,
        "skipped": dict(memo.get("skipped", {})), "dropped_bars": r.dropped_bars,
        "fee_estimate": f"max(${rules.spec.fee_min_per_contract:.2f} x contracts, {r.p.costs.taker_fee_bps:g} bps of "
                        f"notional) per side, plus {r.p.costs.slippage_bps:g} bps slippage",
        "estimated": ["fees (the real fee tier isn't known)", rules.spec.history_note,
                      "stop-limit fills at the stop (or the open, gapped through), as a stop-market would"],
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
    v = summary.get("venue")
    head = [
        f"# Backtest — {summary.get('coin', 'BTC')} perp, {summary['interval']} bars",
        "",
        f"{full['from']} → {full['to']} ({full['years']:.1f} years). Generated {summary['generated']}.",
        f"Costs charged: taker fee {p.costs.taker_fee_bps} bps + slippage {p.costs.slippage_bps} bps per side, "
        f"plus real funding prints. Start equity ${p.sizing.starting_equity:,.0f}, "
        f"risk {p.sizing.risk_per_trade:.1%}/trade, max {p.sizing.max_leverage:.0f}x.",
        "",
    ]
    if v:
        sp, c, sk = v["spec"], v["contracts"] or {}, v["skipped"]
        head[0] = (f"# Backtest — {summary.get('coin', 'BTC')} perp on {sp['name'].capitalize()} rules "
                   f"({sp['product']}, whole {sp['contract_size']:g} BTC contracts), {summary['interval']} bars")
        head[3:4] = [
            f"**{sp['display']} rules** ({sp['product']}, simulated): whole contracts of {sp['contract_size']:g} BTC at "
            f"each bar's price, at most {sp['max_leverage']:g}x, the overnight margin check at "
            f"{v['margin']['overnight_long']:.2%}, no entry into the weekly break (Fri 17:00-18:00 New York) or a "
            f"known maintenance window ({v['dropped_bars']} Binance bars wholly inside one left out). "
            f"Fees: {v['fee_estimate']}, an ESTIMATE until the real fee tier is known. History: "
            f"{sp['history_note']}; Coinbase keeps no funding history, so funding is an ESTIMATE too. Stops are "
            f"filled at the stop (or the open, gapped through), as Coinbase's stop-limit would if its limit is met. "
            f"Start equity ${p.sizing.starting_equity:,.0f}, risk {p.sizing.risk_per_trade:.1%}/trade.",
            "",
            f"Contracts per trade: {c.get('min', 0)} to {c.get('max', 0)} (median {c.get('median', 0)}). Entries "
            f"skipped by the venue (once per episode): {sk.get('contracts', 0)} too small for one contract, "
            f"{sk.get('margin', 0)} short of overnight margin, {sk.get('session', 0)} into a closure.",
        ]
    lines = head + [
        f"| | Desk | Buy & hold {summary.get('coin', 'BTC')} |",
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
