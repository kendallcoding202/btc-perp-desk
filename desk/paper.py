"""Paper trading ledger — the paper experiments (BTC $500 on Coinbase's rules, ETH and SOL $150, VT $500).

`step()` is idempotent and catch-up safe: it loads the ledger, processes every bar
that has closed since the last run (exactly like the backtest: orders fill at
the next bar's open, stops are checked against each bar's high/low, funding is
charged per bar), then saves the ledger and the board.

That means it can run from anywhere on any schedule — a laptop loop, or a GitHub
Actions cron every 15 minutes — and the account carries on from the same ledger,
with no gaps and no double-counting.

Each coin's ledger lives in ledger/<COIN>/ (saved to the paper-ledger branch, so the
experiment's history is permanent and auditable): paper.json + kills.json + board.json. BTC's trades under its
venue's rules (desk/venue.py: Coinbase's whole contracts, margin, sessions, fees) on Coinbase's own data
(feeds/coinbase.py), exactly as the backtest applies them; the others read Hyperliquid.

`step_basket()` does the same for the VT basket (desk/voltrend.py) in ledger/VT/.

Both take the account's deposits and withdrawals (desk/flows.py, from deposits.json): each one is applied once and
kept in the ledger's "flows" list (a ledger that has none has no "flows" key, as before). A run on time puts it in
at this run's price, the one the board marks the account at, after the forming bar's fills: the performance figure
reads the same just before and just after it. A run that catches up bars it missed puts it in at the open of the
first of them on or after its date's 00:00 UTC, before that bar's fills. A withdrawal the open position still needs
waits for the first bar open where it fits (flows.withdrawal_wait), with everything dated after it.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from . import board as board_mod
from . import flows as flows_mod
from . import indicators as ind
from . import venue as venue_mod
from . import voltrend
from .backtest import make_desk
from .brokers.sim import SimBroker
from .engine import Desk, Kills
from .filter import evaluate_entry
from .models import Bar
from .thresholds import VOLTREND, Params, VolTrend, venue_for

LOOKBACK_BARS = 1500
LOOKBACK_SPARE = 60       # Coinbase: the window asked for is this many bars longer (its closures have no candles)
FUNDING_WINDOW_MS = 4 * 86400_000
# Coinbase (a venue account) only:
SESSION_FRESH_MS = 15 * 60_000   # Coinbase's "session open" flag speaks for a close's fill only this soon after it
HOLE_RECENT_BARS = 90            # a missing candle this close to now moves the breakout, ATR and trail: waited for
HOLE_WAIT_MS = 6 * 3600_000      # ...until two answers lacked it and it closed this long ago: then never published
HOLE_KEEP_MS = 200 * 86400_000   # an accepted gap is remembered while it's in the history paper reads (130 days)
VT_LOOKBACK_DAYS = 120    # daily candles per VT coin per run: the 50-day average and vol need 50, the rest is margin
VT_MEMORY_BARS = 70       # closed daily bars per VT coin the ledger keeps: the 50-day window, plus 20 days to spare
VT_GAP_WAIT_DAYS = 3      # a bar no answer has had is waited for this long after its close (and for a second answer)


def _new_events(before: list, events) -> list:
    """The events appended since `before = list(events)` was taken. The caller holds `before` until now: it keeps the
    loaded events alive, so once the deque (maxlen 200) drops the oldest, a new Event can't take over a dropped
    one's id() and be mistaken for an old one (its fill would never reach the board or the phone)."""
    seen = {id(e) for e in before}
    return [e for e in events if id(e) not in seen]


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str, separators=(",", ":")))
    tmp.replace(path)


def _saved(meta: dict, recorded: list[dict], **parts) -> dict:
    """The ledger file's content: "flows" only once the account has one, so a ledger without stays as it was."""
    return {"meta": meta, **parts, **({"flows": recorded} if recorded else {})}


class LedgerRefused(RuntimeError):
    """A saved ledger from another venue (a Hyperliquid BTC account, now that BTC simulates Coinbase): never converted
    or overwritten. That account's step stops, loudly, until a human archives the old ledger."""


def _check_venue(saved: dict, p: Params, lpath: Path) -> None:
    """The ledger's venue (meta "venue"; a ledger from before venues had none: Hyperliquid) must be the account's."""
    was, now = saved.get("meta", {}).get("venue", "hyperliquid"), p.market.venue
    if was == now:
        return
    m, b = saved.get("meta", {}), saved.get("broker", {})
    opened = time.gmtime(m.get("started_ts", 0) / 1000)
    venue, coin = venue_for(p), p.market.symbol
    raise LedgerRefused(
        f"{lpath} is a {was.capitalize()} paper account (opened {time.strftime('%Y-%m-%d %H:%M UTC', opened)}, "
        f"${b.get('cash', 0):,.2f}, {len(b.get('trades', []))} trades, {m.get('runs', 0)} runs), but {coin} now "
        "simulates " + (f"{venue.display} ({venue.product}, desk/thresholds.py)" if venue else now) + ". Not "
        f"converting it: archive it by hand on the paper-ledger branch (move ledger/{coin}/paper.json to ledger/archive/"
        f"{coin}-{was}-{m.get('start_equity', 0):.0f}usd-from-{time.strftime('%Y-%m-%d', opened)}.json, as the earlier "
        f"runs were) and the next run opens a fresh ${p.sizing.starting_equity:,.0f} account.")


def step(p: Params, ledger_dir: str | Path = "ledger", feed=None, jev=None, log=print,
         now_ms: int | None = None, flows: list | None = None) -> dict:
    """`flows`: this account's deposits and withdrawals (flows.schedule), or None for none.

    An account with venue rules (thresholds.VENUES: BTC on Coinbase) reads Coinbase (feeds/coinbase.py) and trades
    under desk/venue.py, as the backtest does: whole contracts, sessions, the overnight margin check, the fee estimate.
    Its ledger also keeps a "venue" record: the margin rates and maintenance windows Coinbase listed, and the funding
    log (Coinbase keeps no funding history). A ledger from another venue is refused, never converted."""
    from .feeds.hyperliquid import HyperliquidFeed

    ledger_dir = Path(ledger_dir)
    lpath = ledger_dir / "paper.json"
    spec = venue_for(p)
    now = now_ms or int(time.time() * 1000)
    saved = json.loads(lpath.read_text()) if lpath.exists() else None
    if saved is not None:
        _check_venue(saved, p, lpath)                                     # before any request
    if spec is not None:    # and so is a deposits.json the ledger disagrees with: before Coinbase's product request
        flows_mod.due(flows or [], list((saved or {}).get("flows") or []), now, p.market.symbol)
    if feed is None:
        if spec is not None:
            from .feeds.coinbase import CoinbaseFeed

            feed = CoinbaseFeed(spec, p.market.interval)
        else:
            feed = HyperliquidFeed(p.market.symbol, p.market.interval)

    vstate = None
    if spec is None:
        broker = SimBroker(p)
        desk = Desk(p, broker, jev=jev)
    else:
        # This run's word on the contract first (margin, session, maintenance, the funding rate), the saved one if
        # this answer has none: the rules for every bar this run processes. The funding log keeps what a catch-up
        # after a long outage still needs: back to the funding window before the last bar processed.
        details = feed.product() if hasattr(feed, "product") else None
        last0 = ((saved or {}).get("meta") or {}).get("last_bar_ts") or 0
        vstate = venue_mod.remember((saved or {}).get("venue"), details, now, spec,
                                    keep_from_ms=min(last0, now) - FUNDING_WINDOW_MS if last0 else None)
        if hasattr(feed, "log"):
            feed.log = venue_mod.FundingLog(vstate["funding_log"], spec.funding_log_days)
        broker, desk = make_desk(p, jev=jev, windows=vstate["windows"], margin=vstate["margin"])
        holes0 = vstate.get("holes") or {}
        if isinstance(feed, _coinbase_feed()):  # what explains a missing candle, from the ledger
            feed.sessions, feed.gaps = desk.venue.sessions, list(holes0.get("accepted") or [])
    meta = {"started_ts": now, "start_equity": p.sizing.starting_equity, "last_bar_ts": 0, "runs": 0,
            "interval": p.market.interval}
    if spec is not None:
        meta.update({"venue": spec.name, "product": spec.product})
    recorded: list[dict] = []
    if saved is not None:
        broker.load_dict(saved["broker"])
        desk.load_dict(saved["desk"])
        meta.update(saved["meta"])
        recorded = list(saved.get("flows") or [])
        if meta.get("interval") != p.market.interval:
            raise SystemExit(f"ledger was started on {meta['interval']} bars; refusing to continue on {p.market.interval}")
    desk.kills = Kills.load(ledger_dir / "kills.json")
    todo = flows_mod.due(flows or [], recorded, now, p.market.symbol)    # before any request: a bad file stops here

    def take_flows(ts: int, price: float) -> None:
        """Every flow due by `ts`, in at `price`: money in or out, the brakes scaled so they read the same percentage,
        nothing else touched. A withdrawal the open position needs stops the queue there, until a later bar."""
        while todo and todo[0].ts <= ts:
            rec = flows_mod.apply_desk(desk, todo[0], ts, price, meta["start_equity"], recorded)
            if rec is None:
                return
            todo.pop(0)
            log(f"{p.market.symbol}: {flows_mod.describe(rec)}")

    if spec is None:
        raw = feed.candles(start_ms=now - LOOKBACK_BARS * feed.bar_ms, closed_only=False)
    else:   # Coinbase has no candle while it's closed: ask for a little more, keep LOOKBACK_BARS closed ones
        raw = feed.candles(start_ms=now - (LOOKBACK_BARS + LOOKBACK_SPARE) * feed.bar_ms, closed_only=False)
        raw = raw[-(LOOKBACK_BARS + 1):]
    forming = raw[-1] if raw and raw[-1].ts + feed.bar_ms > now else None
    bars = raw[:-1] if forming else raw
    if spec is not None and forming is None:
        bars = bars[-LOOKBACK_BARS:]
    before = list(desk.events)
    cut, holes_changed = None, False
    if spec is not None:    # a candle Coinbase should have and this answer hasn't: the bars from it on wait for it
        cut, holes_changed = _wait_for_holes(desk, vstate, bars, feed.bar_ms, now, meta["last_bar_ts"], log)
    # Funding only matters for bars we're about to process and the 3-day average at the newest bar.
    # Fetching just that window keeps each step to ~4 light requests (Hyperliquid rate-limits by weight).
    since = min(meta["last_bar_ts"] or now, now) - FUNDING_WINDOW_MS
    funding = feed.funding(max(bars[0].ts, since))
    f = ind.compute(bars, funding, p)
    ctx = feed.context()
    warned = False
    if spec is not None:
        ctx, warned = _venue_context(desk, vstate, ctx, now, spec)

    def qty(q: float) -> str:     # Coinbase: "3 contracts (0.03 BTC)"
        return desk.venue.describe(q, p.market.symbol) if desk.venue is not None else f"{q:.5f} {p.market.symbol}"

    def note_fills(n_trades_before: int, had_position: bool, ts: int) -> None:
        """Say what FILLS and RISK just did, so the board and phone notifications show it right away."""
        for t in broker.trades[n_trades_before:]:
            verb = "SOLD" if t.side.value == "long" else "COVERED SHORT"
            desk.say(ts, "risk" if t.reason in ("stop", "take_profit") else "fills",
                     f"{verb} {qty(t.qty)} @ ${t.exit_price:,.2f} ({t.reason.replace('_', ' ')}) · "
                     f"trade {t.net_pnl:+,.2f} · equity ${broker.cash:,.2f}")
        if broker.position is not None and (not had_position or len(broker.trades) > n_trades_before):
            pos = broker.position
            verb = "BOUGHT" if pos.side.value == "long" else "SHORTED"
            desk.say(ts, "fills", f"{verb} {qty(pos.qty)} @ ${pos.entry_price:,.2f} · stop ${pos.stop:,.2f}")

    processed = 0
    if not meta["last_bar_ts"]:
        # Day one: start from the next bar that closes. History is for the indicators, not for trading.
        meta["last_bar_ts"] = bars[-1].ts
        eq = broker.equity(bars[-1].close)
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                            ctx=ctx, now_ms=now, jev=jev, venue=desk.venue)
        desk.say(now, "chief", f"${p.sizing.starting_equity:,.0f} paper account opened"
                               + (f" on {spec.display} ({spec.product}, {spec.contract_size:g} BTC contracts)"
                                  if spec is not None else "")
                               + f". first decision on the next {p.market.interval} close.")
        log(f"paper ledger opened with ${p.sizing.starting_equity:,.2f}; trading starts at the next bar close")
    else:
        for i, bar in enumerate(bars):
            if bar.ts <= meta["last_bar_ts"]:
                continue
            if cut is not None and bar.ts >= cut:
                break
            take_flows(bar.ts, bar.open)
            n0, had = len(broker.trades), broker.position is not None
            broker.fill_pending(bar.open, bar.ts)
            broker.check_stop_bar(bar)
            note_fills(n0, had, bar.ts)
            broker.apply_funding(f.funding_bar[i], bar.close)
            is_latest = i == len(bars) - 1
            bar_ctx = ctx if is_latest else None
            if bar_ctx is not None and spec is not None and now - (bar.ts + f.bar_ms) > SESSION_FRESH_MS:
                # decided late (missed runs): Coinbase's "open now" says nothing about this close's fill, which the
                # schedule decides, as in the backtest
                bar_ctx = replace(bar_ctx, session_open=None, session_note=None)
            orders = desk.on_bar_close(f, i, ctx=bar_ctx, now_ms=bar.ts + f.bar_ms)
            meta["last_bar_ts"] = bar.ts
            processed += 1
            eq = broker.equity(bar.close)
            blocked = desk.last_decision.blocked_by if desk.last_decision else None
            log(f"[{time.strftime('%Y-%m-%d %H:%M', time.gmtime((bar.ts + f.bar_ms) / 1000))} UTC] "
                f"close {bar.close:,.0f} · equity ${eq:,.2f} · "
                + (", ".join(f"{o.action} {o.side.value} {o.qty:.5f}" for o in orders) if orders
                   else f"no trade ({blocked.name if blocked else 'holding'})"))

    if forming is not None and forming.ts > meta["last_bar_ts"] and cut is None:
        # The bar that's still forming: orders decided at the last close fill at its open (the backtest's price),
        # and the stop is checked against its range so far. Same fills as the backtest, minus the wait for the bar to close.
        n0, had = len(broker.trades), broker.position is not None
        broker.fill_pending(forming.open, forming.ts)
        broker.check_stop_bar(forming)
        note_fills(n0, had, now)
        # Money in or out due by now goes in now, after those fills, at the price the board marks the account at
        # (board.build): its units at the NAV the board shows, so the performance figure doesn't move.
        take_flows(now, ctx.mid if ctx and ctx.mid else bars[-1].close)

    if processed == 0 and desk.last_decision is None:
        eq = broker.equity(ctx.mid or bars[-1].close)
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                            ctx=ctx, now_ms=now, venue=desk.venue)

    if todo:    # a withdrawal the open position needs, or no forming bar in this answer: the next run tries again
        log(f"{p.market.symbol}: {', '.join(fl.id for fl in todo)} due, not in yet: "
            + ("a withdrawal waiting for the open position to release it" if todo[0].amount < 0 and broker.position
               else "recorded at the next run"))
    new_events = _new_events(before, desk.events)

    meta["runs"] += 1
    meta["last_run_ts"] = now
    venue_part = {} if vstate is None else {"venue": vstate}
    _write(lpath, _saved(meta, recorded, broker=broker.to_dict(), desk=desk.to_dict(), **venue_part))
    # Coinbase: a funding hour this run logged that the saved ledger hasn't is worth saving (it's the only history)
    logged = vstate is not None and {r[0] for r in vstate["funding_log"]} - {
        r[0] for r in ((saved or {}).get("venue") or {}).get("funding_log") or []}

    done = {r["id"] for r in recorded}
    state = board_mod.build(desk, f, len(f.bars) - 1, mode="paper", ctx=ctx, jev_client=jev, now_ms=now,
                            goal_pace=board_mod.report_pace(p.market.symbol),
                            flows=flows_mod.State(meta["start_equity"], recorded,
                                                  [fl for fl in flows or [] if fl.id not in done]),
                            elapsed_days=(now - meta["started_ts"]) / 86400_000, crawler={
                                "source": f"{spec.name if spec else 'hyperliquid'} · {p.market.interval}",
                                "funding_prints": len(funding),
                                "requests": feed.requests, "latency_ms": feed.last_latency_ms,
                                "age_s": (now - (f.bars[-1].ts + f.bar_ms)) / 1000}, venue_state=vstate)
    state["paper"] = {"days": (now - meta["started_ts"]) / 86400_000, "start_equity": meta["start_equity"],
                      "runs": meta["runs"], "bars_processed_this_run": processed,
                      "new_events": board_mod.events(new_events),       # times in Mountain Time
                      "changed": bool(processed or new_events or logged or holes_changed or warned)}
    _write(ledger_dir / "board.json", state)
    return state


def _coinbase_feed():
    from .feeds.coinbase import CoinbaseFeed

    return CoinbaseFeed


def _when(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def _wait_for_holes(desk, vstate: dict, bars: list, bar_ms: int, now: int, last_bar_ts: int, log) -> tuple:
    """A candle Coinbase should have (no closure explains it) that this answer hasn't: the bars from it on wait, as a
    stop or signal on it would otherwise be lost for good (only bars after the last one processed are ever processed),
    and so does everything while one is missing among the newest HOLE_RECENT_BARS (it moves the breakout, the ATR, the
    trail). It's a candle Coinbase never published once an earlier saved run's answer lacked it too and it closed
    HOLE_WAIT_MS ago: then a desk event says so and the account goes on without it (an accepted gap, remembered
    HOLE_KEEP_MS). Older holes (CoinbaseFeed raises past MAX_HOLES) only move the slow average a little: logged.
    Day one waits for nothing: it doesn't trade, and its holes are accepted.

    Returns (cut, changed): bars opening at `cut` or later wait (None: none do); changed = the hole memory moved."""
    rec = vstate.get("holes") or {}
    seen = {int(k): v for k, v in (rec.get("seen") or {}).items()}
    accepted = [int(x) for x in rec.get("accepted") or []]
    newest_closed = now - now % bar_ms - bar_ms
    holes = venue_mod.missing([b.ts for b in bars], desk.venue.sessions, bars[0].ts if bars else now,
                              newest_closed, bar_ms, accepted) if bars else []
    recent = newest_closed - HOLE_RECENT_BARS * bar_ms
    wait, old = [], []
    for h in holes:
        if not last_bar_ts:
            accepted.append(h)
            log(f"{desk.p.market.symbol}: Coinbase has no {_when(h)} candle (no closure explains it); the account "
                "opens without it")
        elif h > last_bar_ts or h >= recent:
            if h in seen and seen[h] < now and now >= h + bar_ms + HOLE_WAIT_MS:
                accepted.append(h)
                desk.say(now, "crawler", f"Coinbase never published the {_when(h)} candle (missing from every answer "
                                         f"since {_when(seen[h])}); going on without it")
            else:
                seen.setdefault(h, now)
                wait.append(h)
        else:
            old.append(h)
    if old:
        log(f"{desk.p.market.symbol}: {len(old)} older candle(s) missing from Coinbase's answer ({_when(old[0])}"
            + (f" to {_when(old[-1])}" if len(old) > 1 else "") + "); only the slow averages see them")
    cut = None
    if wait:
        cut = wait[0] if wait[0] > last_bar_ts else last_bar_ts + 1
        log(f"{desk.p.market.symbol}: waiting for Coinbase's {_when(wait[0])} candle"
            + (f" (and {len(wait) - 1} more)" if len(wait) > 1 else "") + ": missing from this answer, no closure "
            "explains it. " + ("Bars from it on" if cut == wait[0] else "Every bar") + " waits for the next run")
    new = {"seen": {str(h): seen[h] for h in sorted(seen) if h in wait},
           "accepted": sorted({a for a in accepted if a >= now - HOLE_KEEP_MS})[-100:]}
    changed = new != {"seen": rec.get("seen") or {}, "accepted": rec.get("accepted") or []}
    if new["seen"] or new["accepted"] or rec:
        vstate["holes"] = new
    if wait:
        vstate["waiting_for"] = wait[0]     # for the board
    else:
        vstate.pop("waiting_for", None)
    return cut, changed


def _venue_context(desk, vstate: dict, ctx, now: int, spec) -> tuple:
    """The newest bar's context with what only paper knows (how old the newest logged funding rate is: the funding
    gate), and the warnings worth one desk event each while they last (said when they start): no new Coinbase
    funding rate for 3 hours, then for a day (entries wait), an answer without the session details, Coinbase
    reporting closed outside its schedule.
    Returns (ctx, changed)."""
    log_rows = vstate.get("funding_log") or []
    age = now - log_rows[-1][0] if log_rows else None
    if ctx is not None and age is not None:
        ctx = replace(ctx, funding_age_ms=age)
    ses = vstate.get("session") or {}
    warn = {}
    if age is not None and age >= venue_mod.FUNDING_STALE_MS:
        warn["funding"] = ("crowd", f"no new Coinbase funding rate since {_when(log_rows[-1][0])}; charging the last "
                                    "logged rate (est.)")
    if age is not None and age >= venue_mod.FUNDING_UNKNOWN_MS:     # and when the funding gate starts waiting
        warn["funding_unknown"] = ("crowd", f"still no new Coinbase funding rate (the last is from "
                                            f"{_when(log_rows[-1][0])}): no entry until Coinbase answers one")
    if ctx is not None and getattr(ctx, "session_note", None):
        warn["session"] = ("crawler", ctx.session_note)
    elif ses.get("open") is False and ses.get("read_ts") == now and desk.venue.sessions.closure(now) is None:
        warn["closed"] = ("crawler", f"Coinbase reports its session closed ({ses.get('state') or 'no state given'}) "
                                     "outside its schedule; no entry while it lasts")
    was = dict(vstate.get("warn") or {})
    for k, (agent, text) in warn.items():
        if k not in was:
            desk.say(now, agent, text)
    kept = {k: was.get(k, now) for k in warn}
    if kept or was:
        vstate["warn"] = kept
    return ctx, set(kept) != set(was)


def step_basket(vt: VolTrend = VOLTREND, ledger_dir: str | Path = "ledger/VT", feeds: dict | None = None, log=print,
                now_ms: int | None = None, flows: list | None = None) -> dict:
    """The VT basket's step, with `step`'s contract: idempotent and catch-up safe.

    Every daily close since the last run goes through voltrend.advance, the backtest's own loop. Day one opens the
    account flat; the first decision is at the next 00:00 UTC close. Orders decided at a close fill at the open of
    the bar that's forming now: the backtest's price, without waiting a day. Hyperliquid rate-limits, so a run makes
    one candle request per coin, plus one funding request per coin only when a day has closed to process.

    The ledger keeps each coin's last VT_MEMORY_BARS closed daily bars, so a bar missing from one answer (an API
    glitch) is filled in from an earlier one, and the averages see the bars the backtest sees.
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
    remembered = {}
    recorded: list[dict] = []
    if lpath.exists():
        saved = json.loads(lpath.read_text())
        book.load_dict(saved["broker"])
        meta.update(saved["meta"])
        remembered = saved.get("bars", {})       # [[ts, open, high, low, close], ...] per coin; none in older ledgers
        recorded = list(saved.get("flows") or [])
        if meta.get("strategy") != "voltrend" or meta.get("interval") != vt.interval:
            # an error, not SystemExit: cmd_step reports it and the desk accounts still run
            raise RuntimeError(f"{lpath} isn't a {vt.interval} vol-trend ledger; refusing to continue")
    before = list(book.events)
    todo = flows_mod.due(flows or [], recorded, now, "VT")

    def take_flows(ts: int, marks: dict) -> bool:
        """Every flow due by `ts`, with each coin marked at `marks` (a coin not in it at its last close). VT has no
        brakes: only the cash and the units move. False when a withdrawal the holdings need stops the queue there."""
        while todo and todo[0].ts <= ts:
            rec = flows_mod.apply_book(book, todo[0], ts, marks, meta["start_equity"], recorded)
            if rec is None:
                return False
            todo.pop(0)
            log(f"VT: {flows_mod.describe(rec)}")
        return True

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
    # Then the closed bars earlier answers had fill the holes in this one; the answer wins where both have a bar. The
    # memory keeps a bar this run's clock calls still open (a clock a little behind the last run's) but doesn't use it.
    memory, reach = {}, {c: closed[c][-1].ts for c in vt.coins}      # reach: the answer's newest closed bar
    for c in vt.coins:
        kept = {x[0]: Bar(*x) for x in remembered.get(c, [])}
        kept.update((b.ts, b) for b in closed[c])
        memory[c] = [kept[t] for t in sorted(kept)][-VT_MEMORY_BARS:]
        closed[c] = [kept[t] for t in sorted(kept) if t + day <= now]
    # A day is processed once every coin has a closed bar on or after it. A coin with no bar that day sits it out, as
    # in the backtest, but only once a later bar of its has closed: a bar missing from one answer is waited for.
    covered = min(closed[c][-1].ts for c in vt.coins)
    # So is a bar missing between two of a coin's bars that no answer has had (the first run after a day without one,
    # say, got an answer with a hole): the days from it on wait for it. It's a day the exchange has no bar for only
    # once two answers lacked it: the saved memory already reached past it, this run's answer reaches past it too,
    # and it closed VT_GAP_WAIT_DAYS ago. Then the coin sits it out, as in the backtest. (A bar missed during a cron
    # outage, or before the ledger had a memory, waits one saved run more.) Holes too old to move a 50-day window
    # still to be decided don't count.
    seen = {c: max((x[0] for x in remembered.get(c, [])), default=0) for c in vt.coins}   # the saved memory's newest
    lo = meta["last_bar_ts"] - (need + 10) * day
    hole = min(((t, c) for c in vt.coins for a, b in zip(closed[c], closed[c][1:]) for t in range(a.ts + day, b.ts, day)
                if t > lo and (t > reach[c] or t > seen[c] or t + day + VT_GAP_WAIT_DAYS * day > now)), default=None)
    if hole and hole[0] <= covered:
        if 0 < meta["last_bar_ts"] < covered:     # it holds back days that would be processed now
            log(f"VT: no answer has had {hole[1]}'s {time.strftime('%Y-%m-%d', time.gmtime(hole[0] / 1000))} bar yet; "
                f"the days from it wait for it (a day with no bar once a later answer still lacks it, "
                f"{VT_GAP_WAIT_DAYS} days after its close)")
        covered = hole[0] - day

    processed = 0
    if not meta["last_bar_ts"]:
        # Day one: open flat. The history is for the averages, not for trading. Start after the newest close any coin
        # has: a coin whose answer is behind must not make the next run decide (and fill) on closes from before today.
        meta["last_bar_ts"] = max(closed[c][-1].ts for c in vt.coins)
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

            if not todo:
                voltrend.advance(book, closed, fbar, days, after=after)
            else:   # catching up: the same loop, cut at each flow's day so the flow goes in at that day's open
                opens = {c: {b.ts: b.open for b in closed[c]} for c in vt.coins}
                run: list[int] = []
                for d in days:
                    if todo and todo[0].ts <= d:
                        if run:
                            voltrend.advance(book, closed, fbar, run, after=after)
                        run = []
                        take_flows(d, {c: opens[c][d] for c in vt.coins if d in opens[c]})
                    run.append(d)
                voltrend.advance(book, closed, fbar, run, after=after)
            meta["last_bar_ts"] = days[-1]
            processed = len(days)

    # The bar that's forming now: orders decided at the last close fill at its open, as in the backtest. Every coin
    # together and in coin order, as on_day fills them, and only when it's the bar right after the last processed
    # close (otherwise an order belongs to an earlier open: a closed bar missing from every answer after a day with
    # no run). If a coin's forming bar isn't in the answer, they all wait for the next run.
    last = meta["last_bar_ts"]
    marks = {c: (forming[c] if c in forming else closed[c][-1]).close for c in vt.coins}
    if all(c in forming and forming[c].ts == last + day for c in vt.coins) and \
            not any(b.ts > last for c in vt.coins for b in closed[c]):
        for c in vt.coins:
            book.fill_pending(c, forming[c].open, forming[c].ts)
        # Money in or out due by now goes in now, after those fills, at the marks the board shows: its units at the
        # NAV the board shows, so the performance figure doesn't move.
        take_flows(now, marks)
    preview = {}
    for c in vt.coins:
        if c not in book.signals:   # day one: no decision yet, show what the rule sees at the last close
            sig = voltrend.signal([b.close for b in closed[c] if b.ts <= meta["last_bar_ts"]], vt)
            preview[c] = {**sig, "target": sig["raw"], "held": 0.0, "ts": None}

    if todo:
        log(f"VT: {', '.join(fl.id for fl in todo)} due, not in yet: " +
            ("a withdrawal waiting for the holdings to release it" if todo[0].amount < 0 and any(
                s.qty for s in book.sleeves.values()) else "recorded at the next run with every coin's forming bar"))
    new_events = _new_events(before, book.events)
    meta["runs"] += 1
    meta["last_run_ts"] = now
    # Stored under "broker" like the desk's ledgers: the workflow's commit message reads broker.cash from each one.
    _write(lpath, _saved(meta, recorded, broker=book.to_dict(),
                         bars={c: [[b.ts, b.open, b.high, b.low, b.close] for b in memory[c]] for c in vt.coins}))

    done = {r["id"] for r in recorded}
    state = board_mod.basket(book, marks=marks, now_ms=now, meta=meta, goal_pace=board_mod.report_pace("VT"),
                             flows=flows_mod.State(meta["start_equity"], recorded,
                                                   [fl for fl in flows or [] if fl.id not in done]),
                             preview=preview, crawler={
        "source": f"hyperliquid · {vt.interval}", "bars": sum(len(v) for v in closed.values()),
        "requests": sum(getattr(f, "requests", 0) for f in feeds.values()),
        "latency_ms": max((getattr(f, "last_latency_ms", 0.0) for f in feeds.values()), default=0.0),
        "age_s": (now - (meta["last_bar_ts"] + day)) / 1000})
    state["paper"] = {"days": (now - meta["started_ts"]) / 86400_000, "start_equity": meta["start_equity"],
                      "runs": meta["runs"], "bars_processed_this_run": processed,
                      "new_events": board_mod.events(new_events),       # times in Mountain Time
                      # or the memory reaches a newer bar (once a day, with the close): saved, so the next run knows
                      # an answer reached past a bar that's missing
                      "changed": bool(processed or new_events or any(memory[c][-1].ts > seen[c] for c in vt.coins))}
    _write(ledger_dir / "board.json", state)
    return state
