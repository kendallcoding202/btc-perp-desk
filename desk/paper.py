"""Paper trading ledger — the paper experiments (one $150 account per coin).

`step()` is idempotent and catch-up safe: it loads the ledger, processes every bar
that has closed since the last run (exactly like the backtest: orders fill at
the next bar's open, stops are checked against each bar's high/low, funding is
charged per bar), then saves the ledger and the board.

That means it can run from anywhere on any schedule — a laptop loop, or a GitHub
Actions cron every 15 minutes — and the account carries on from the same ledger,
with no gaps and no double-counting.

Each coin's ledger lives in ledger/<COIN>/ (saved to the paper-ledger branch, so the
experiment's history is permanent and auditable): paper.json + kills.json + board.json.

`step_basket()` does the same for the VT basket (desk/voltrend.py) in ledger/VT/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import board as board_mod
from . import indicators as ind
from . import voltrend
from .brokers.sim import SimBroker
from .engine import Desk, Kills
from .filter import evaluate_entry
from .thresholds import VOLTREND, Params, VolTrend, report_name

LOOKBACK_BARS = 1500
FUNDING_WINDOW_MS = 4 * 86400_000
VT_LOOKBACK_DAYS = 120    # daily candles per VT coin per run: the 50-day average and vol need 50, the rest is margin


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str, separators=(",", ":")))
    tmp.replace(path)


def step(p: Params, ledger_dir: str | Path = "ledger", feed=None, jev=None, log=print,
         now_ms: int | None = None) -> dict:
    from .feeds.hyperliquid import HyperliquidFeed

    ledger_dir = Path(ledger_dir)
    lpath = ledger_dir / "paper.json"
    feed = feed or HyperliquidFeed(p.market.symbol, p.market.interval)
    now = now_ms or int(time.time() * 1000)

    broker = SimBroker(p)
    desk = Desk(p, broker, jev=jev)
    meta = {"started_ts": now, "start_equity": p.sizing.starting_equity, "last_bar_ts": 0, "runs": 0,
            "interval": p.market.interval}
    if lpath.exists():
        saved = json.loads(lpath.read_text())
        broker.load_dict(saved["broker"])
        desk.load_dict(saved["desk"])
        meta.update(saved["meta"])
        if meta.get("interval") != p.market.interval:
            raise SystemExit(f"ledger was started on {meta['interval']} bars; refusing to continue on {p.market.interval}")
    desk.kills = Kills.load(ledger_dir / "kills.json")

    raw = feed.candles(start_ms=now - LOOKBACK_BARS * feed.bar_ms, closed_only=False)
    forming = raw[-1] if raw and raw[-1].ts + feed.bar_ms > now else None
    bars = raw[:-1] if forming else raw
    # Funding only matters for bars we're about to process and the 3-day average at the newest bar.
    # Fetching just that window keeps each step to ~4 light requests (Hyperliquid rate-limits by weight).
    since = min(meta["last_bar_ts"] or now, now) - FUNDING_WINDOW_MS
    funding = feed.funding(max(bars[0].ts, since))
    f = ind.compute(bars, funding, p)
    ctx = feed.context()
    seen = {id(e) for e in desk.events}

    def note_fills(n_trades_before: int, had_position: bool, ts: int) -> None:
        """Say what FILLS and RISK just did, so the board and phone notifications show it right away."""
        for t in broker.trades[n_trades_before:]:
            verb = "SOLD" if t.side.value == "long" else "COVERED SHORT"
            desk.say(ts, "risk" if t.reason in ("stop", "take_profit") else "fills",
                     f"{verb} {t.qty:.5f} {p.market.symbol} @ ${t.exit_price:,.2f} ({t.reason.replace('_', ' ')}) · "
                     f"trade {t.net_pnl:+,.2f} · equity ${broker.cash:,.2f}")
        if broker.position is not None and (not had_position or len(broker.trades) > n_trades_before):
            pos = broker.position
            verb = "BOUGHT" if pos.side.value == "long" else "SHORTED"
            desk.say(ts, "fills", f"{verb} {pos.qty:.5f} {p.market.symbol} @ ${pos.entry_price:,.2f} · stop ${pos.stop:,.2f}")

    processed = 0
    if not meta["last_bar_ts"]:
        # Day one: start from the next bar that closes. History is for the indicators, not for trading.
        meta["last_bar_ts"] = bars[-1].ts
        eq = broker.equity(bars[-1].close)
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                            ctx=ctx, now_ms=now, jev=jev)
        desk.say(now, "chief", f"${p.sizing.starting_equity:,.0f} paper account opened. "
                               f"first decision on the next {p.market.interval} close.")
        log(f"paper ledger opened with ${p.sizing.starting_equity:,.2f}; trading starts at the next bar close")
    else:
        for i, bar in enumerate(bars):
            if bar.ts <= meta["last_bar_ts"]:
                continue
            n0, had = len(broker.trades), broker.position is not None
            broker.fill_pending(bar.open, bar.ts)
            broker.check_stop_bar(bar)
            note_fills(n0, had, bar.ts)
            broker.apply_funding(f.funding_bar[i], bar.close)
            is_latest = i == len(bars) - 1
            orders = desk.on_bar_close(f, i, ctx=ctx if is_latest else None, now_ms=bar.ts + f.bar_ms)
            meta["last_bar_ts"] = bar.ts
            processed += 1
            eq = broker.equity(bar.close)
            blocked = desk.last_decision.blocked_by if desk.last_decision else None
            log(f"[{time.strftime('%Y-%m-%d %H:%M', time.gmtime((bar.ts + f.bar_ms) / 1000))} UTC] "
                f"close {bar.close:,.0f} · equity ${eq:,.2f} · "
                + (", ".join(f"{o.action} {o.side.value} {o.qty:.5f}" for o in orders) if orders
                   else f"no trade ({blocked.name if blocked else 'holding'})"))

    if forming is not None and forming.ts > meta["last_bar_ts"]:
        # The bar that's still forming: orders decided at the last close fill at its open (the backtest's price),
        # and the stop is checked against its range so far. Same fills as the backtest, minus the wait for the bar to close.
        n0, had = len(broker.trades), broker.position is not None
        broker.fill_pending(forming.open, forming.ts)
        broker.check_stop_bar(forming)
        note_fills(n0, had, now)

    if processed == 0 and desk.last_decision is None:
        eq = broker.equity(ctx.mid or bars[-1].close)
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                            ctx=ctx, now_ms=now)

    new_events = [e for e in desk.events if id(e) not in seen]

    meta["runs"] += 1
    meta["last_run_ts"] = now
    _write(lpath, {"meta": meta, "broker": broker.to_dict(), "desk": desk.to_dict()})

    goal_cagr = None
    rep = Path(f"reports/{report_name(p.market.symbol)}.json")
    if rep.exists():
        goal_cagr = json.loads(rep.read_text()).get("full", {}).get("cagr")
    state = board_mod.build(desk, f, len(f.bars) - 1, mode="paper", ctx=ctx, jev_client=jev, now_ms=now,
                            goal_cagr=goal_cagr, elapsed_days=(now - meta["started_ts"]) / 86400_000, crawler={
                                "source": f"hyperliquid · {p.market.interval}", "funding_prints": len(funding),
                                "requests": feed.requests, "latency_ms": feed.last_latency_ms,
                                "age_s": (now - (f.bars[-1].ts + f.bar_ms)) / 1000})
    state["paper"] = {"days": (now - meta["started_ts"]) / 86400_000, "start_equity": meta["start_equity"],
                      "runs": meta["runs"], "bars_processed_this_run": processed,
                      "new_events": [{"ts": e.ts, "agent": e.agent, "text": e.text} for e in new_events],
                      "changed": bool(processed or new_events)}
    _write(ledger_dir / "board.json", state)
    return state


def step_basket(vt: VolTrend = VOLTREND, ledger_dir: str | Path = "ledger/VT", feeds: dict | None = None, log=print,
                now_ms: int | None = None) -> dict:
    """The VT basket's step, with `step`'s contract: idempotent and catch-up safe.

    Every daily close since the last run goes through voltrend.advance, the backtest's own loop. Day one opens the
    account flat; the first decision is at the next 00:00 UTC close. Orders decided at a close fill at the open of
    the bar that's forming now: the backtest's price, without waiting a day. Hyperliquid rate-limits, so a run makes
    one candle request per coin, plus one funding request per coin only when a day has closed to process.
    """
    from .feeds.hyperliquid import HyperliquidFeed

    ledger_dir = Path(ledger_dir)
    lpath = ledger_dir / "paper.json"
    feeds = feeds or {c: HyperliquidFeed(c, vt.interval) for c in vt.coins}
    now = now_ms or int(time.time() * 1000)
    day = voltrend.DAY_MS
    need = max(vt.sma_bars, vt.vol_lookback + 1)

    book = voltrend.Book(vt)
    meta = {"started_ts": now, "start_equity": vt.starting_equity, "last_bar_ts": 0, "runs": 0,
            "interval": vt.interval, "strategy": "voltrend"}
    if lpath.exists():
        saved = json.loads(lpath.read_text())
        book.load_dict(saved["broker"])
        meta.update(saved["meta"])
        if meta.get("strategy") != "voltrend" or meta.get("interval") != vt.interval:
            # an error, not SystemExit: cmd_step reports it and the desk accounts still run
            raise RuntimeError(f"{lpath} isn't a {vt.interval} vol-trend ledger; refusing to continue")
    seen = {id(e) for e in book.events}

    # Enough history for the 50-day average at the oldest close still to process (after an outage, fetch more).
    since = now - VT_LOOKBACK_DAYS * day
    if meta["last_bar_ts"]:
        since = min(since, meta["last_bar_ts"] - (need + 10) * day)
    # Judged by this run's clock: an answer that arrives after 00:00 (a retried request) also has the new day's bar,
    # which would make the day that just closed look like neither closed nor forming.
    raw = {c: [b for b in feeds[c].candles(start_ms=since, closed_only=False) if b.ts <= now] for c in vt.coins}
    closed = {c: [b for b in bars if b.ts + day <= now] for c, bars in raw.items()}
    forming = {c: bars[-1] for c, bars in raw.items() if bars and bars[-1].ts + day > now}
    for c in vt.coins:
        if len(closed[c]) < need:   # a short or broken answer must never read as "below the average": sell everything
            raise RuntimeError(f"{c}: only {len(closed[c])} closed daily candles from the feed, need {need}")
    # A day is processed once every coin has a closed bar on or after it. A coin with no bar that day sits it out, as
    # in the backtest, but only once a later bar of its has closed: a bar missing from one answer is waited for.
    covered = min(closed[c][-1].ts for c in vt.coins)

    processed = 0
    if not meta["last_bar_ts"]:
        # Day one: open flat. The history is for the averages, not for trading.
        meta["last_bar_ts"] = covered
        book.say(now, "chief", f"${vt.starting_equity:,.0f} paper account opened: {', '.join(vt.coins)}, a third each. "
                               "first decision at the next 00:00 UTC close.")
        log(f"VT paper ledger opened with ${vt.starting_equity:,.2f}; the first decision is at the next 00:00 UTC close")
    else:
        days = sorted({b.ts for c in vt.coins for b in closed[c] if meta["last_bar_ts"] < b.ts <= covered})
        if days:
            for c in vt.coins:
                if sum(1 for b in closed[c] if b.ts <= days[0]) < need:
                    raise RuntimeError(f"{c}: not enough daily history before {days[0]} for the {vt.sma_bars}-day average")
            # Funding only for the days being processed: (first day's open, now].
            fbar = {c: ind.funding_per_bar(closed[c], feeds[c].funding(days[0]), day) for c in vt.coins}

            def after(b, d):
                log(f"[{time.strftime('%Y-%m-%d', time.gmtime(d / 1000))} close] VT equity ${b.equity_curve[-1][1]:,.2f} · "
                    + (", ".join(f"{c} {o['kind']} {o['usd']:+,.2f}" for c, o in b.pending.items()) or "no orders"))

            voltrend.advance(book, closed, fbar, days, after=after)
            meta["last_bar_ts"] = days[-1]
            processed = len(days)

    # The bar that's forming now: orders decided at the last close fill at its open, as in the backtest. Every coin
    # together and in coin order, as on_day fills them, and only once every closed bar is processed (otherwise an
    # order belongs to an earlier open). If a coin's forming bar isn't in the answer, they all wait for the next run.
    last = meta["last_bar_ts"]
    if all(c in forming and forming[c].ts > last for c in vt.coins) and \
            not any(b.ts > last for c in vt.coins for b in closed[c]):
        for c in vt.coins:
            book.fill_pending(c, forming[c].open, forming[c].ts)
    marks = {c: (forming[c] if c in forming else closed[c][-1]).close for c in vt.coins}
    preview = {}
    for c in vt.coins:
        if c not in book.signals:   # day one: no decision yet, show what the rule sees at the last close
            sig = voltrend.signal([b.close for b in closed[c] if b.ts <= meta["last_bar_ts"]], vt)
            preview[c] = {**sig, "target": sig["raw"], "held": 0.0, "ts": None}

    new_events = [e for e in book.events if id(e) not in seen]
    meta["runs"] += 1
    meta["last_run_ts"] = now
    # Stored under "broker" like the desk's ledgers: the workflow's commit message reads broker.cash from each one.
    _write(lpath, {"meta": meta, "broker": book.to_dict()})

    goal_cagr = None
    rep = Path(f"reports/{report_name('VT')}.json")
    if rep.exists():
        goal_cagr = json.loads(rep.read_text()).get("full", {}).get("cagr")
    state = board_mod.basket(book, marks=marks, now_ms=now, meta=meta, goal_cagr=goal_cagr, preview=preview, crawler={
        "source": f"hyperliquid · {vt.interval}", "bars": sum(len(v) for v in closed.values()),
        "requests": sum(getattr(f, "requests", 0) for f in feeds.values()),
        "latency_ms": max((getattr(f, "last_latency_ms", 0.0) for f in feeds.values()), default=0.0),
        "age_s": (now - (meta["last_bar_ts"] + day)) / 1000})
    state["paper"] = {"days": (now - meta["started_ts"]) / 86400_000, "start_equity": meta["start_equity"],
                      "runs": meta["runs"], "bars_processed_this_run": processed,
                      "new_events": [{"ts": e.ts, "agent": e.agent, "text": e.text} for e in new_events],
                      "changed": bool(processed or new_events)}
    _write(ledger_dir / "board.json", state)
    return state
