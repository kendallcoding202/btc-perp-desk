"""The board — turns the desk's state into the JSON the dashboard draws.

Each agent gets a card (name, role, a number, a status line) and a speech bubble.
The bubbles are generated from the same numbers the gates used, so what the board
says is always what the desk actually thought.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import indicators as ind
from . import mountain
from . import venue as venue_mod
from .models import Verdict
from .thresholds import EXPERIMENTS, report_name

AGENTS = [
    ("chief", "CHIEF", "never takes a position"),
    ("scan", "SCAN", "finding what moves"),
    ("vet", "VET", "is the move real"),
    ("crowd", "CROWD", "who's on the other side"),
    ("size", "SIZE", "how much"),
    ("fills", "FILLS", "execution"),
    ("risk", "RISK", "the stop"),
    ("crawler", "CRAWLER", "reads first, then the desk trades"),
]


def _usd(x: float | None, dp: int | None = None) -> str:
    """Whole dollars for BTC/ETH-sized prices, cents below $1,000 (SOL, small balances)."""
    if x is None:
        return "—"
    if dp is None:
        dp = 0 if abs(x) >= 1000 else 2
    return f"${x:,.{dp}f}"


# ---- Mountain Time ------------------------------------------------------------------------------------------------
# The owner reads every time in Mountain Time (desk/mountain.py: America/Denver's own rules, MDT in summer, MST in
# winter). The desk's own words keep UTC (they're in the ledger, which never changes for this); the board's copy of
# them, and every time the board writes itself, say MT. Stored timestamps stay epoch ms.
_UTC_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) UTC\b")      # "2026-10-12 22:00 UTC"
_UTC_CLOCK = re.compile(r"\b(\d{2}):(\d{2}) UTC\b")                           # "reopens 23:00 UTC"
_UTC_TOMORROW = re.compile(r"\buntil tomorrow UTC\b")                         # the daily loss limit's day
_DAY_MS = 86400_000


def day_turns(ms: int) -> str:
    """When the desk's day turns after `ms` (00:00 UTC: the daily loss limit's day, VT's daily close), as Mountain
    Time: "6:00 pm MT" while Denver is on MDT, "5:00 pm MT" on MST."""
    return mountain.clock((ms // _DAY_MS + 1) * _DAY_MS)


def in_mountain_time(text, ref_ms: int):
    """A board string's UTC times, as Mountain Time: "the 2026-10-12 22:00 UTC candle" -> "the Oct 12, 4:00 pm MT
    candle"; a time of day with no date ("reopens 23:00 UTC", "the next 00:00 UTC close") is the first one at or after
    `ref_ms` (when the text was said: they all look ahead), so it's right on either side of a clock change; "until
    tomorrow UTC" -> "until the desk's day turns at 6:00 pm MT". Anything else is returned as it was."""
    if not isinstance(text, str) or "UTC" not in text:
        return text

    def stamp(m):
        d = datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return mountain.when(int(d.timestamp() * 1000))

    def clock(m):
        t = ref_ms // _DAY_MS * _DAY_MS + (int(m[1]) * 60 + int(m[2])) * 60_000
        return mountain.clock(t if t >= ref_ms else t + _DAY_MS)

    text = _UTC_STAMP.sub(stamp, text)
    text = _UTC_TOMORROW.sub(lambda m: f"until the desk's day turns at {day_turns(ref_ms)}", text)
    return _UTC_CLOCK.sub(clock, text)


def events(evs) -> list[dict]:
    """Desk events as the board and the phone show them: their words with the times in Mountain Time."""
    return [{"ts": e.ts, "agent": e.agent, "text": in_mountain_time(e.text, e.ts)} for e in evs]


def _gate_row(g, now_ms: int) -> dict:
    """A gate as the board shows it: its note's times in Mountain Time (a closure's reopening, say)."""
    d = g.as_dict()
    if d.get("note"):
        d["note"] = in_mountain_time(d["note"], now_ms)
    return d


def _gate(decision, name):
    if decision is None:
        return None
    for g in decision.gates:
        if g.name == name:
            return g
    return None


def _thin(seq: list, n: int) -> list:
    if len(seq) <= n:
        return seq
    step = len(seq) / n
    out = [seq[int(k * step)] for k in range(n)]
    out[-1] = seq[-1]
    return out


def oos_pace(summary: dict | None) -> dict | None:
    """The pace the goal panel projects at: a backtest's out-of-sample CAGR, with that period's dates. Not the full
    period's: it includes the years the settings were picked on, so it flatters (BTC +50%/yr, +28%/yr since 2024)."""
    oos = (summary or {}).get("out_of_sample") or {}
    if oos.get("cagr") is None:
        return None
    return {"cagr": oos["cagr"], "from": oos.get("from"), "to": oos.get("to")}


def report_pace(account: str, reports_dir: str | Path = "reports") -> dict | None:
    """oos_pace from the account's backtest report (reports/backtest[-COIN].json); None if it has none.

    A report with a fresh-account pace (BTC on Coinbase: a fresh $500 opened 2024-01-01, $100 a month after, under the
    venue's rules) gives that one instead: it's this account's situation, and at $500 whole contracts are coarse, so
    the full run's out-of-sample pace (from an account that held thousands by 2024) flatters it."""
    path = Path(reports_dir) / f"{report_name(account)}.json"
    if not path.exists():
        return None
    s = json.loads(path.read_text())
    fresh = s.get("pace") or {}
    if fresh.get("cagr") is not None:
        return {"cagr": fresh["cagr"], "from": fresh.get("start"), "to": fresh.get("to"),
                "label": f"a fresh ${fresh.get('start_equity', 0):,.0f} + ${fresh.get('monthly', 0):,.0f}/mo"}
    return oos_pace(s)


def goal(equity: float, start: float, target: float, pace: dict | None, elapsed_days: float | None,
         flows=None, now_ms: int | None = None) -> dict:
    """Progress toward the target on a log scale ($150→$100k is 667x: $300 is 11% of the way, $1,500 is 35%), and
    the years left at `pace` (oos_pace) and, once the account is 30 days old, at its own pace.

    With deposits or withdrawals recorded (`flows`, a flows.State), the progress is still the bank's, but each pace is
    a time-weighted one (the account's own: its NAV, never the money put in) and the years left count the deposits
    still planned, as they come."""
    import math

    if flows is not None and flows.active:
        return _goal_with_flows(equity, start, target, pace, elapsed_days, flows, now_ms)

    progress = math.log(equity / start) / math.log(target / start) if equity > 0 and target > start else 0.0
    left = math.log(target / equity) if equity > 0 else math.inf

    def years(rate):
        return left / math.log(1 + rate) if rate and rate > 0 else None

    realised = None
    if elapsed_days and elapsed_days >= 30 and equity > 0:
        realised = (equity / start) ** (365.25 / elapsed_days) - 1
    test = (pace or {}).get("cagr")
    return {"start": start, "target": target, "equity": equity, "progress": max(0.0, min(1.0, progress)),
            "multiple_left": target / equity if equity > 0 else None,
            "test_cagr": test, "test_from": (pace or {}).get("from"), "test_to": (pace or {}).get("to"),
            **({"test_label": pace["label"]} if (pace or {}).get("label") else {}),   # whose pace, when not the run's
            "years_at_test_rate": years(test),
            # the same numbers under the old names, for an app.js from before test_cagr (a tab left open across a
            # deploy): it shows them as "at backtest pace", which they are
            "backtest_cagr": test, "years_at_backtest_rate": years(test),
            "realised_cagr": realised, "years_at_realised_rate": years(realised)}


def _goal_with_flows(equity, start, target, pace, elapsed_days, flows, now_ms) -> dict:
    import math

    from .flows import years_to

    g = goal(equity, start, target, pace, None)       # progress, multiple left, and the paces' years without the plan
    future = flows.future(now_ms or 0)
    nav = flows.nav(equity)
    realised = None
    if elapsed_days and elapsed_days >= 30 and nav > 0:
        realised = nav ** (365.25 / elapsed_days) - 1      # the NAV's pace: deposits aren't growth
    test = g["test_cagr"]
    left = math.log(target / equity) if equity > 0 else math.inf

    def no_plan(rate):
        return left / math.log(1 + rate) if rate and rate > 0 else None

    s = flows.summary(equity, now_ms or 0)
    years = years_to(target, equity, test, future)
    g.update({
        "years_at_test_rate": years, "years_at_backtest_rate": years,
        "realised_cagr": realised, "years_at_realised_rate": years_to(target, equity, realised, future)
        if realised is not None else None,
        "years_at_test_rate_without_plan": no_plan(test),
        "deposited": s["net"], "deposits_in": s["deposited"], "withdrawn": s["withdrawn"],
        "planned_left": s["planned"]["count"], "planned_left_usd": s["planned"]["usd"],
        "plan_included": bool(future), "twr": s["twr"],
        "next_extra": (s["planned"]["extras"] or [None])[0],
    })
    return g


def trigger_outlook(f: ind.Features, i: int, n: int, limit: int = 10, short: bool = False) -> list[dict]:
    """When the breakout line steps down if price makes no new high: old highs age out of the n-bar window.
    With short=True, the same for the breakdown line: it steps up as old lows age out.

    The decision on bar j compares its close with max(high of bars j-n .. j-1), so the decision k bars
    after bar i (closing at bars[i].ts + (k+1) bars) uses the already-known highs i+k-n .. i.
    """
    xs = [b.low for b in f.bars] if short else [b.high for b in f.bars]
    pick = min if short else max
    out, prev = [], None
    for k in range(1, n + 1):
        lo = i + k - n
        if lo < 0:
            continue
        level = pick(xs[lo:i + 1])
        if level != prev:
            out.append({"ts": f.bars[i].ts + (k + 1) * f.bar_ms, "level": level})
            prev = level
    return out[:limit]


def build(desk, f: ind.Features, i: int, *, mode: str, ctx=None, crawler: dict | None = None,
          jev_client=None, now_ms: int | None = None, candles: int = 140, goal_pace: dict | None = None,
          elapsed_days: float | None = None, flows=None, start_equity: float | None = None,
          venue_state: dict | None = None) -> dict:
    """start_equity: the account's own starting balance (a live account's first exchange balance); paper boards
    leave it out and start from the paper setting. `venue_state`: paper's record of the venue (venue.remember) for
    an account with venue rules, else None."""
    p, broker, dec = desk.p, desk.broker, desk.last_decision
    vrules = desk.venue         # venue rules (Coinbase for BTC), or None
    coin = p.market.symbol
    shorts = p.strategy.allow_shorts and p.strategy.short_mode == "breakdown"
    bars = f.bars
    bar = bars[i]
    price = ctx.mid if ctx and ctx.mid else bar.close
    now_ms = now_ms or (bar.ts + f.bar_ms)
    equity = broker.equity(price)
    pos = broker.position
    trades = broker.trades
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = len(trades) - wins
    a = f.atr[i] or 0.0
    start_eq = p.sizing.starting_equity if start_equity is None else start_equity
    bars_per_day = max(1, int(86400_000 / f.bar_ms))
    prev = bars[max(0, i - bars_per_day)].close
    chg_24h = price / prev - 1 if prev else 0.0
    first_ts = desk.equity_curve[0][0] if desk.equity_curve else now_ms
    day_n = int((now_ms - first_ts) / 86400_000) + 1

    blocked = dec.blocked_by if dec else None
    side_txt = pos.side.value.upper() if pos else "FLAT"
    hold = pos.unrealized(price) / (pos.qty * pos.entry_price) if pos else 0.0

    # ---- agents ---------------------------------------------------------
    cards = {}
    quotes = {}

    if pos:
        plan, chief_state = f"HOLD {side_txt}", "active"
        quotes["chief"] = f"we're {side_txt.lower()} from {_usd(pos.entry_price)}. risk owns it now; nobody else gets a vote."
    elif dec and dec.side:
        plan, chief_state = f"GO {dec.side.value.upper()}", "alert"
        quotes["chief"] = "every gate passed. the order is the whole point."
    elif blocked is not None and blocked.verdict == Verdict.DROP:
        plan, chief_state = "STAND DOWN", "blocked"
        quotes["chief"] = f"{blocked.owner} dropped it: {blocked.name.replace('_', ' ')}. nobody overrides a drop."
    else:
        plan, chief_state = "SCAN → WAIT", "idle"
        quotes["chief"] = "kills → data → hard → scan → soft → size → jev → fills. in that order, every bar."
    cards["chief"] = dict(value=plan, detail=f"bar {i + 1:,} · {p.market.interval} · {mode}", state=chief_state)

    bo = _gate(dec, "setup")
    ts_score = dec.scores.get("trend_strength") if dec else None
    if bo and bo.verdict == Verdict.PASS:
        quotes["scan"], scan_state = f"{bo.note}. that's a setup.", "alert"
    elif bo:
        quotes["scan"], scan_state = f"nothing yet. {bo.note}.", "idle"
    else:
        quotes["scan"], scan_state = "warming up the averages.", "idle"
    cards["scan"] = dict(value=f"{ts_score:.2f}" if ts_score is not None else "—",
                         detail=f"{p.strategy.entry_mode}.{p.strategy.breakout_bars} · {coin}", score=ts_score,
                         state=scan_state)

    eff = dec.scores.get("efficiency") if dec else None
    ext = dec.scores.get("extension") if dec else None
    if eff is not None:
        quotes["vet"] = (f"efficiency {eff:.2f}. " +
                         ("that's a real move, not noise." if eff >= 0.3 else "mostly chop. trend's not proven.") +
                         (f" already {ext:.2f} extended." if ext and ext > 0.6 else ""))
    else:
        quotes["vet"] = "need more bars before I can call it."
    atr_pct = a / bar.close * 100 if bar.close else 0
    cards["vet"] = dict(value=f"{eff:.2f}" if eff is not None else "—",
                        detail=f"atr {atr_pct:.2f}% · eff.{p.strategy.breakout_bars}", score=eff,
                        state="active" if eff and eff >= 0.3 else "idle", asks_jev=p.jev.mode != "off")

    crowd = dec.scores.get("crowding") if dec else None
    fapr = f.funding_apr[i]
    if fapr > 40:
        quotes["crowd"] = f"longs are paying {fapr:.0f}% a year to stay long. that's a crowd."
    elif fapr < -10:
        quotes["crowd"] = f"shorts are paying {abs(fapr):.0f}% apr. longs get paid to wait."
    else:
        quotes["crowd"] = f"funding {fapr:+.1f}% apr. nobody's crowded, nobody's squeezed."
    cards["crowd"] = dict(value=f"{crowd:.2f}" if crowd is not None else "—",
                          detail=f"funding {fapr:+.1f}% apr" + (" (est.)" if vrules else "")
                          + (f" · oi {_usd(ctx.open_interest_usd / 1e9, 2)}B" if ctx and ctx.open_interest_usd else ""),
                          score=crowd, state="blocked" if _gate(dec, "crowding") and
                          _gate(dec, "crowding").verdict == Verdict.DROP else "idle", asks_jev=p.jev.mode != "off")

    held = (lambda q: vrules.describe(q, coin)) if vrules is not None else (lambda q: f"{q:.4f} {coin}")
    skip = _venue_skip(dec) if vrules is not None else None
    if pos:
        notional = pos.qty * price
        quotes["size"] = f"{held(pos.qty)} on, {_usd(notional)} notional, {notional / equity:.2f}x." + (
            f" margin {_usd(vrules.margin_in_use(pos.qty, price, pos.side.sign))} overnight." if vrules else "")
        size_val = _usd(notional)
    elif skip is not None:
        quotes["size"], size_val = f"skipped: {in_mountain_time(skip.note, now_ms)}.", "SKIP"
    elif dec and dec.qty and vrules is not None:     # whole contracts: what they risk, not the budget they fit in
        d = abs(bar.close - dec.stop)
        eq_dec = dec.notional / dec.leverage if dec.leverage else equity
        quotes["size"] = (f"{held(dec.qty)} at a {_usd(d)} stop risks {dec.qty * d / eq_dec:.1%} (budget "
                          f"{p.sizing.risk_per_trade:.0%}; whole contracts round down), {dec.leverage:.2f}x.")
        size_val = _usd(dec.notional)
    elif dec and dec.qty:
        quotes["size"] = (f"{p.sizing.risk_per_trade:.1%} risk with a {_usd(abs(bar.close - dec.stop))} stop "
                          f"= {held(dec.qty)}, {dec.leverage:.2f}x.")
        size_val = _usd(dec.notional)
    else:
        quotes["size"], size_val = "nothing to size.", "$0"
    cards["size"] = dict(value=size_val, detail=f"risk {p.sizing.risk_per_trade:.1%} · ≤{p.sizing.max_leverage:.0f}x"
                         + (f" · {vrules.spec.contract_size:g} {coin} contracts" if vrules else ""),
                         state="active" if pos else "blocked" if skip is not None else "idle")

    lf = broker.last_fill
    if lf:
        quotes["fills"] = (f"{'bought' if (lf['action'] == 'open') == (lf['side'] == 'long') else 'sold'} "
                           f"{held(lf['qty']) if vrules else format(lf['qty'], '.4f')} @ {_usd(lf['price'])}, "
                           f"fee {_usd(lf['fee'], 2)}{' (est.)' if vrules else ''}"
                           + (f", net {lf['net_pnl']:+,.2f}" if 'net_pnl' in lf else "") + ".")
    else:
        quotes["fills"] = "no fills yet. taker in, taker out."
    cards["fills"] = dict(value=f"{broker.orders_filled}/{broker.orders_submitted}",
                          detail=(f"fee est. max(${vrules.spec.fee_min_per_contract:.2f}/contract, "
                                  f"{p.costs.taker_fee_bps:g}bps) + slip {p.costs.slippage_bps:g}bps" if vrules else
                                  f"taker {p.costs.taker_fee_bps}bps + slip {p.costs.slippage_bps}bps"),
                          state="active" if broker.pending else "idle")

    r = desk.risk
    if r.halted:
        quotes["risk"], risk_state = f"HALTED. {r.halt_reason}. a human resets this, not me.", "blocked"
    elif desk.day_locked:
        quotes["risk"], risk_state = f"daily loss limit hit. flat until the desk's day turns at {day_turns(now_ms)}.", \
            "blocked"
    elif pos:
        dist = abs(price - pos.stop) / price
        quotes["risk"] = f"stop {_usd(pos.stop)}, {dist:.1%} away, trailing {p.strategy.trail_atr:g} ATR."
        risk_state = "active"
    else:
        quotes["risk"] = f"nobody buys the top of a {p.strategy.breakout_bars}-bar range without a stop."
        risk_state = "idle"
    cards["risk"] = dict(value=f"{wins}W {losses}L", detail=f"stop {_usd(pos.stop) if pos else '—'}", state=risk_state)

    cr = crawler or {}
    quotes["crawler"] = (f"read {len(bars):,} bars + {cr.get('funding_prints', 0):,} funding prints"
                         + (f" in {cr['latency_ms']:.0f}ms" if cr.get("latency_ms") else "") + ", then the desk trades.")
    cards["crawler"] = dict(value=f"{len(bars):,}", detail=cr.get("source", "history"),
                            state="active", stats={
                                "bars": len(bars), "funding": cr.get("funding_prints", 0),
                                "requests": cr.get("requests", 0), "age_s": cr.get("age_s")})

    agents = []
    for key, name, role in AGENTS:
        c = cards[key]
        agents.append({"key": key, "name": name, "role": role, "quote": quotes[key], **c})

    # ---- chart ------------------------------------------------------------
    lo = max(0, i + 1 - candles)
    window = bars[lo:i + 1]
    markers = []
    for t in trades:
        if t.entry_ts >= window[0].ts:
            markers.append({"ts": t.entry_ts, "price": t.entry_price, "kind": "entry", "side": t.side.value})
        if t.exit_ts >= window[0].ts:
            markers.append({"ts": t.exit_ts, "price": t.exit_price, "kind": "exit", "side": t.side.value,
                            "reason": t.reason, "pnl": t.net_pnl})
    if pos and pos.entry_ts >= window[0].ts:
        markers.append({"ts": pos.entry_ts, "price": pos.entry_price, "kind": "entry", "side": pos.side.value})

    def series(xs):
        return [None if x is None else round(x, 2) for x in xs[lo:i + 1]]

    # ---- thresholds.py panel ----------------------------------------------
    thr_rows = []
    if dec:
        for g in dec.gates:
            if g.threshold is None and g.value is None:
                continue
            thr_rows.append({
                "section": "SOFT" if g.name in ("trend_strength", "efficiency", "momentum_already_spent", "crowding")
                or g.owner == "jev" else ("KILL" if g.owner in ("risk", "chief") else "HARD"),
                **_gate_row(g, now_ms)})

    curve = _thin(desk.equity_curve, 400)
    k = desk.kills
    title = f"{coin} PERP DESK" if vrules is None else \
        f"{vrules.spec.display} ({vrules.spec.contract_size:g} {coin} contracts)"
    state = with_flows({
        "updated": now_ms,
        "header": {
            "title": title, "coin": coin, "risk_per_trade": p.sizing.risk_per_trade,
            "tagline": ("SCAN PICKS · RISK SIZES · FILLS TRADES" + (" · LONG + SHORT" if p.strategy.allow_shorts else "")
                        if vrules is None else    # its title is the real product's: say it's simulated, up front
                        f"{mode.upper()} · SIMULATED ON {vrules.spec.name.upper()} RULES · NO ORDERS SENT"),
            "shorts": p.strategy.allow_shorts, "experiment": EXPERIMENTS.get(coin),
            "mode": mode.upper(), "venue": p.market.venue, "interval": p.market.interval,
            "day": day_n, "date": mountain.day(now_ms).upper(), "time": mountain.clock(now_ms, label=False),
        },
        "ticker": {
            "price": price, "change_24h": chg_24h, "bank": equity, "pnl": equity - start_eq,
            "pnl_pct": equity / start_eq - 1, "position": side_txt,
            "position_detail": ((f"{vrules.contracts(pos.qty)} contract{'s' if vrules.contracts(pos.qty) != 1 else ''} "
                                 f"@ {pos.entry_price:,.0f}" if vrules else
                                 f"{pos.qty:.4f} @ {pos.entry_price:,.0f}") if pos else ""),
            "hold_pct": hold, "record": f"{wins}W {losses}L", "fees": broker.fees_total, "cash": broker.cash,
            "pos": None if pos is None else {"side": pos.side.value, "qty": pos.qty, "entry": pos.entry_price,
                                             "stop": pos.stop},
            "funding": broker.funding_total,
        },
        "agents": agents,
        "chart": {
            "candles": [[b.ts, b.open, b.high, b.low, b.close] for b in window],
            "ema_fast": series(f.ema_fast), "ema_slow": series(f.ema_slow),
            "don_hi": series(f.don_hi), "don_lo": series(f.don_lo),
            "stop": pos.stop if pos else None, "entry": pos.entry_price if pos else None,
            "markers": markers, "bar_ms": f.bar_ms,
        },
        "equity": [[ts, round(eq, 2)] for ts, eq in curve],
        "pnl_bars": [round(t.net_pnl, 2) for t in trades[-80:]],
        "analysis": {
            "stats": {
                "price": price, "change_24h": chg_24h, "atr_pct": atr_pct, "funding_apr": fapr,
                "open_interest_usd": ctx.open_interest_usd if ctx else None,
                "spread_bps": ctx.spread_bps if ctx else None, "depth_usd": ctx.depth_usd if ctx else None,
            },
            "gates": [_gate_row(g, now_ms) for g in dec.gates] if dec else [],
            "outlook": trigger_outlook(f, i, p.strategy.breakout_bars, short=dec.direction.sign < 0)
            if (pos is None and dec is not None and (dec.direction.sign > 0 or shorts)) else [],
            "outlook_side": "short" if dec is not None and dec.direction.sign < 0 else "long",
            "scanned": i + 1,
            "direction": dec.direction.value if dec else None,
        },
        "thresholds": thr_rows,
        "goal": goal(equity, start_eq, p.sizing.goal_equity, goal_pace, elapsed_days, flows, now_ms),
        "kills": {"flat_kill": k.flat_kill, "entry_kill": k.entry_kill, "jev_kill": k.jev_kill,
                  "halted": r.halted, "daily_locked": desk.day_locked, "cooldown": now_ms < r.cooldown_until_ts},
        "jev": {
            "mode": p.jev.mode, "enabled": bool(jev_client and jev_client.enabled) and not k.jev_kill,
            "calls": getattr(jev_client, "calls", 0), "answers": dec.jev_answers if dec else None,
        },
        "events": events(list(desk.events)[-30:]),
        "rules": rules(p),
    }, flows, equity, now_ms)
    if vrules is not None:      # only an account with venue rules: the other boards are as they were
        state["venue"] = venue_block(desk, f, i, price, equity, now_ms, venue_state)
    return state


def _venue_skip(dec):
    """The venue gate that stopped an entry the desk would otherwise have taken, or None."""
    from .venue import skip_note

    return skip_note(dec.gates) if dec is not None else None


def venue_block(desk, f, i: int, price: float, equity: float, now_ms: int, venue_state: dict | None) -> dict:
    """The venue's panel: contracts held, margin in use, the funding estimate, the session, and the entries it
    skipped. Everything marked estimated is: the fees (the real tier isn't known) and the funding (Coinbase keeps no
    history; paper logs one rate a run)."""
    rules, pos, p = desk.venue, desk.broker.position, desk.p
    sp, vs = rules.spec, venue_state or {}
    log = vs.get("funding_log") or []
    nxt = rules.sessions.next_closure(now_ms)
    ses = vs.get("session") or {}
    memo = desk.venue_memo
    skip = _venue_skip(desk.last_decision)
    # closed now: a scheduled closure (why, until when), or Coinbase saying so outside its schedule (this run's answer)
    cur = rules.sessions.closure(now_ms)
    fresh = ses.get("read_ts") == now_ms
    closed_now = {"why": cur[2], "until": cur[1], "scheduled": True} if cur else \
        {"why": "Coinbase reports its session closed; not a scheduled closure", "until": None, "scheduled": False} \
        if fresh and ses.get("open") is False else \
        {"why": "Coinbase's answer has no session details", "until": None, "scheduled": False} \
        if fresh and ses.get("open") is None else None
    stale = log[-1][0] if log and now_ms - log[-1][0] >= venue_mod.FUNDING_STALE_MS else None
    return {
        "name": sp.name, "product": sp.product, "display": sp.display, "contract_size": sp.contract_size,
        "max_leverage": sp.max_leverage,
        "contracts": rules.contracts(pos.qty) if pos else 0,
        "notional": pos.qty * price if pos else 0.0,
        "margin_rate": rules.overnight_margin(pos.side.sign if pos else 1),
        "intraday_margin_rate": rules.margin["intraday_long"],
        "margin_in_use": rules.margin_in_use(pos.qty, price, pos.side.sign) if pos else 0.0,
        "margin_free": equity - (rules.margin_in_use(pos.qty, price, pos.side.sign) if pos else 0.0),
        "one_contract_usd": sp.contract_size * price,
        "fee_estimate": f"max(${sp.fee_min_per_contract:.2f} x contracts, {p.costs.taker_fee_bps:g} bps) per side + "
                        f"{p.costs.slippage_bps:g} bps slippage",
        "funding": {"estimate": True, "rate_hourly": log[-1][1] if log else None,
                    "rate_ts": log[-1][0] if log else None, "apr": f.funding_apr[i], "logged": len(log),
                    "paid": desk.broker.funding_total, "stale_since": stale},
        "session": {"open": ses.get("open"), "read_ts": ses.get("read_ts"),
                    "next_closure": None if nxt is None else {"start": nxt[0], "end": nxt[1], "why": nxt[2]}},
        "closed_now": closed_now,
        "waiting_for": vs.get("waiting_for"),      # a missing Coinbase candle the bars from it on wait for
        "stop_fill": "stops simulated as filled at the stop (Coinbase: stop-limit, which can miss in a fast market)",
        "skipping": None if skip is None else {"gate": skip.name, "note": in_mountain_time(skip.note, now_ms)},
        "skipped": dict(memo.get("skipped", {})), "last_skip": _last_skip(memo.get("last_skip")),
    }


def _last_skip(rec: dict | None) -> dict | None:
    """The venue's last skipped entry ({ts, gate, note}), its note's times in Mountain Time."""
    return None if not rec else {**rec, "note": in_mountain_time(rec.get("note"), rec.get("ts") or 0)}


def with_flows(state: dict, flows, equity: float, now_ms: int) -> dict:
    """Deposits and withdrawals on the board, once the account has one: P&L in dollars is equity - start - net
    flows, the percentage is the NAV's (time-weighted), and "flows" carries the totals, the units, the plan still to
    come and a mark for each one on the equity chart. A board without any is returned as it was."""
    if flows is None or not flows.active:
        return state
    s = flows.summary(equity, now_ms)
    state["ticker"].update({"pnl": s["pnl"], "pnl_pct": s["twr"], "net_flows": s["net"]})
    state["flows"] = s
    return state


def basket(book, *, now_ms: int, marks: dict[str, float], meta: dict, crawler: dict | None = None,
           goal_pace: dict | None = None, preview: dict | None = None, equity_points: int = 400, flows=None) -> dict:
    """The VT basket's board (desk/voltrend.py). Its own panels are under "basket". The keys the desk boards carry
    are kept, empty where they don't apply (no agents, no candle chart, no gates), so a desk renderer still works.

    `preview` = what the rule sees at the last close for a coin it hasn't decided on yet (day one): shown, with
    decided_ts None, so the table isn't empty until the first decision."""
    vt = book.vt
    day_ms = 86400_000
    equity = book.equity(marks)
    start_eq = meta.get("start_equity", vt.starting_equity)
    holdings = []
    for c in vt.coins:
        s, sig = book.sleeves[c], book.signals.get(c) or (preview or {}).get(c, {})
        mark = marks.get(c, s.mark)
        close, sma = sig.get("close"), sig.get("sma")
        holdings.append({
            "coin": c, "decided_ts": sig.get("ts"), "close": close, "sma50": sma,
            "sma_gap": close / sma - 1 if close and sma else None, "above_sma": bool(close and sma and close > sma),
            "vol10": sig.get("vol"), "warm": sig.get("warm", False),
            "target_units": sig.get("target"), "held_units": s.units,
            "qty": s.qty, "avg_entry": s.avg_entry, "entry_ts": s.entry_ts or None,      # this position's first fill
            "mark": mark, "notional": s.qty * mark,
            "weight": s.qty * mark / equity if equity > 0 else 0.0, "unrealised": s.qty * (mark - s.avg_entry),
            "pending": book.pending.get(c),
        })
    held = [h for h in holdings if h["qty"] > 0]
    notional = sum((h["notional"] for h in held), 0.0)
    unreal = sum((h["unrealised"] for h in held), 0.0)
    cost = sum((h["qty"] * h["avg_entry"] for h in held), 0.0)
    wins = sum(1 for t in book.trades if t["net_pnl"] > 0)
    losses = len(book.trades) - wins
    started = meta.get("started_ts", now_ms)
    return with_flows({
        "updated": now_ms,
        "header": {
            "title": "VT VOL-TREND BASKET", "coin": "VT", "kind": "basket", "coins": list(vt.coins),
            "tagline": f"{' · '.join(vt.coins)} · A THIRD EACH · LONG ABOVE THE {vt.sma_bars}-DAY AVERAGE",
            "experiment": EXPERIMENTS.get("VT"), "risk_per_trade": None, "shorts": False,
            "mode": "PAPER", "venue": "hyperliquid", "interval": vt.interval,
            "day": int((now_ms - started) / day_ms) + 1, "date": mountain.day(now_ms).upper(),
            "time": mountain.clock(now_ms, label=False),
        },
        "ticker": {
            "price": None, "change_24h": None, "bank": equity, "pnl": equity - start_eq,
            "pnl_pct": equity / start_eq - 1, "position": f"LONG {len(held)}/{len(vt.coins)}" if held else "FLAT",
            "position_detail": " · ".join(f"{h['coin']} {h['held_units']:.2f}u" for h in held),
            "hold_pct": unreal / cost if cost else 0.0, "record": f"{wins}W {losses}L", "fees": book.fees_total,
            "cash": book.cash, "pos": None, "funding": book.funding_total, "unrealised": unreal,
            "notional": notional, "exposure": notional / equity if equity > 0 else 0.0,
        },
        "basket": {
            "holdings": holdings,
            "next_decision_ts": (now_ms // day_ms + 1) * day_ms,           # every 00:00 UTC
            "last_close_ts": meta["last_bar_ts"] + day_ms if meta.get("last_bar_ts") else None,
            "orders": {"submitted": book.orders_submitted, "filled": book.orders_filled,
                       "blocked": book.orders_blocked},
            "fills": book.fills[-30:], "trades": book.trades[-30:],
            "rules": asdict(vt),
            "sleeve": 1 / len(vt.coins), "crawler": crawler or {},
        },
        "equity": [[ts, round(eq, 2)] for ts, eq in _thin(book.equity_curve, equity_points)],
        "pnl_bars": [round(t["net_pnl"], 2) for t in book.trades[-80:]],
        "goal": goal(equity, start_eq, vt.goal_equity, goal_pace, (now_ms - started) / day_ms, flows, now_ms),
        "events": events(list(book.events)[-30:]),
        # what the desk boards have and the basket doesn't
        "agents": [],
        "chart": {"candles": [], "ema_fast": [], "ema_slow": [], "don_hi": [], "don_lo": [], "stop": None,
                  "entry": None, "markers": [], "bar_ms": day_ms},
        "analysis": {"stats": {"price": None, "change_24h": None, "atr_pct": None, "funding_apr": None,
                               "open_interest_usd": None, "spread_bps": None, "depth_usd": None},
                     "gates": [], "outlook": [], "outlook_side": "long", "scanned": len(book.equity_curve),
                     "direction": None},
        "thresholds": [],
        "kills": {"flat_kill": False, "entry_kill": False, "jev_kill": False, "halted": False, "daily_locked": False,
                  "cooldown": False},
        "jev": {"mode": "off", "enabled": False, "calls": 0, "answers": None},
    }, flows, equity, now_ms)


def rules(p) -> dict:
    """The strategy settings a desk board's page recomputes with: the EMA periods for its live trend row, the
    breakout window and stop widths for its labels. Read from Params, so the page never hard-codes them."""
    s = p.strategy
    return {"interval": p.market.interval, "ema_fast": s.ema_fast, "ema_slow": s.ema_slow,
            "breakout_bars": s.breakout_bars, "stop_atr": s.stop_atr, "trail_atr": s.trail_atr,
            "risk_per_trade": p.sizing.risk_per_trade, "allow_shorts": s.allow_shorts}
