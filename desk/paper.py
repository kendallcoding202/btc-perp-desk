"""Paper trading ledger — the $50 experiments (one account per coin).

`step()` is idempotent and catch-up safe: it loads the ledger, processes every bar
that has closed since the last run (exactly like the backtest: orders fill at
the next bar's open, stops are checked against each bar's high/low, funding is
charged per bar), then saves the ledger and the board.

That means it can run from anywhere on any schedule — a laptop loop, or a GitHub
Actions cron every 15 minutes — and the account carries on from the same ledger,
with no gaps and no double-counting.

Each coin's ledger lives in ledger/<COIN>/ (saved to the paper-ledger branch, so the
experiment's history is permanent and auditable): paper.json + kills.json + board.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import board as board_mod
from . import indicators as ind
from .brokers.sim import SimBroker
from .engine import Desk, Kills
from .filter import evaluate_entry
from .thresholds import Params, report_name

LOOKBACK_BARS = 1500
FUNDING_WINDOW_MS = 4 * 86400_000


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
            desk.say(ts, "risk" if t.reason in ("stop", "take_profit") else "fills",
                     f"SOLD {t.qty:.5f} {p.market.symbol} @ ${t.exit_price:,.2f} ({t.reason.replace('_', ' ')}) · "
                     f"trade {t.net_pnl:+,.2f} · equity ${broker.cash:,.2f}")
        if broker.position is not None and (not had_position or len(broker.trades) > n_trades_before):
            pos = broker.position
            desk.say(ts, "fills", f"BOUGHT {pos.qty:.5f} {p.market.symbol} @ ${pos.entry_price:,.2f} · stop ${pos.stop:,.2f}")

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
