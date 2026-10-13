"""The trading loop for paper and live, plus a replay mode that drives the board from history.

Every `poll` seconds:
    - read the kill switches (dashboard or state/<COIN>/kills.json)
    - read the mid price; paper checks its stop, live reconciles with the exchange
    - when a new bar has closed: CRAWLER refreshes candles/funding/book, the desk decides, FILLS executes
    - publish the board
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from . import board as board_mod
from . import indicators as ind
from .brokers.sim import SimBroker
from .engine import Desk, Kills
from .filter import evaluate_entry
from .server import Board, serve
from .thresholds import Params, report_name

LOOKBACK_BARS = 1500
# A close counts as fresh only this soon after it: paper fills its entry at the next bar's open, the moment the bar
# closes. Normally live sees each close within ~20 s (polls every 15 s, candles read 5 s after the close).
FRESH_MS = 5 * 60_000

# Live state lives in state/<COIN>/: one coin's position, desk and kill switches can't clobber another's.
# These are the files the desk kept before that, shared by every coin.
SHARED_LIVE_FILES = ("live_position.json", "live_desk.json", "kills.json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _push(title: str, body: str) -> None:
    """A live alert to the phone via ntfy.sh, if NTFY_TOPIC is set (the same topic the paper desk notifies on)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
                                 headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # a missed notification must never stop the loop
        print(f"  alert push failed: {type(exc).__name__}")


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(path)


def run_paper(p: Params, *, ledger_dir: str = "ledger", poll_s: float = 60.0, host: str = "127.0.0.1",
              port: int = 8787, dashboard: bool = True, jev=None, log=print):
    """Paper = the ledger step (same fills as the backtest) on a loop, with the board served live."""
    from . import paper

    board = Board(Path(ledger_dir) / "kills.json")
    if dashboard:
        serve(board, host, port)
        log(f"board: http://{host}:{port}")
    while True:
        try:
            board.set(paper.step(p, ledger_dir, jev=jev, log=log))
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # a network blip just means we catch up on the next poll
            log(f"step error: {type(exc).__name__}: {exc}")
        time.sleep(poll_s)


def live_paths(state_dir: str | Path, coin: str, mode: str = "live") -> dict[str, Path]:
    """Every file one coin's live desk keeps, all under state/<COIN>/."""
    d = Path(state_dir) / coin.upper()
    return {"dir": d, "position": d / "live_position.json", "desk": d / f"{mode}_desk.json",
            "kills": d / "kills.json", "lock": d / "live.lock"}


def refuse_shared_state(state_dir: str | Path) -> None:
    """Live state used to be shared by every coin (state/live_position.json, live_desk.json, kills.json). Such a
    file can't be given to a coin safely: the desk file may be any coin's, and the old code sent every coin's
    orders to BTC-PERP, so a position it describes is on BTC whatever --coin was. Refuse until a human sorts it."""
    from .brokers.live import LiveSetupError

    old = [Path(state_dir) / n for n in SHARED_LIVE_FILES if (Path(state_dir) / n).exists()]
    if old:
        raise LiveSetupError(
            f"found live state from before it was kept per coin: {', '.join(str(o) for o in old)}. It can't be "
            f"given to a coin safely (the old code sent every coin's orders to BTC-PERP, so a position it describes "
            f"is on BTC). Check the exchange for open positions and orders, then move each file into "
            f"{Path(state_dir)}/<COIN>/ for the coin it belongs to, or delete it. Live won't start until then.")


def _lock(path: Path):
    """Hold an exclusive lock for this coin's live desk while the process runs: a second `live --coin X` would
    trade the same position from the same files. The OS drops it when the process exits, even on a crash."""
    from .brokers.live import LiveSetupError

    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        import fcntl
    except ImportError:     # not on a POSIX system: no lock, as before
        return fh
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise LiveSetupError(f"another live process holds {path}: one desk per coin")
    return fh


def _keep_trying(what: str, fn, *, coin: str, kills_path: Path, wait_s: float, sleep, log=print):
    """Startup, retried the way the loop retries: an exchange error while starting (a 429, a timeout) must not end
    the process, least of all on a restart whose job is to put a missing stop back on. Each failure is logged, sets
    entry_kill and, the first time and every 20th, goes to the phone. A LiveSetupError still refuses at once."""
    from .brokers.live import LiveSetupError

    n = 0
    while True:
        try:
            return fn()
        except LiveSetupError:
            raise
        except Exception as exc:
            n += 1
            text = f"{what} failed ({type(exc).__name__}: {exc}); trying again in {wait_s:g} s (try {n})"
            log(f"startup: {text}")
            if n == 1 or n % 20 == 0:
                _push(f"{coin} LIVE: not started yet", f"{text}. Until it starts, nothing checks that an open position "
                                                       f"has its stop on the exchange.")
            kills = Kills.load(kills_path)
            if not kills.entry_kill:
                kills.entry_kill = True
                kills.save(kills_path)
            sleep(wait_s)


def _utc(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def process_closed_bars(desk: Desk, broker, f, new: list[int], *, ctx, now: int, mid: float, meta: dict,
                        log=print) -> None:
    """Every bar that closed since the last one processed, oldest first, through paper.step's steps (desk/paper.py):
    fill what the previous close decided, check the stop against the bar, charge funding, let the desk decide.

    Paper fills a close's orders at the next bar's open; live sends them the moment the bar closes. When bars were
    missed (the process slept, the network was down, a restart), the desk decides on each missed bar in order,
    exactly as paper does (same bars, same order, the bar's own close as the clock, no live order-book context),
    and then only what is still actionable now is executed:
      * an exit decided at a missed bar is sent now: paper is out from the next bar on, so live gets out;
      * a trail the desk moved at a missed bar is checked against the bars after it, as paper checks its stop; if
        one went through it, paper is out at that bar, so live gets out now. The final trail is sent once;
      * an entry decided at a missed bar is NOT taken: paper filled it at an open that has passed, and live would
        be acting on a stale signal at today's price. It is said on the board and logged.
    The newest close is fresh only if it closed less than FRESH_MS ago: then its orders go to the exchange now, as when
    no bar was missed. If it closed longer ago (waking from a gap late in the bar, a restart), it is handled like a
    missed bar: its exits and trail go, its entry is not taken."""
    newest = new[-1]
    age_ms = now - (f.bars[newest].ts + f.bar_ms)
    fresh = age_ms <= FRESH_MS
    broker.defer_stops = True
    try:
        for i in new:
            bar = f.bars[i]
            stale = i != newest or not fresh
            # 1. orders decided at the previous (missed) close: paper fills them at this bar's open
            pending, broker.pending = broker.pending, []
            for o in pending:
                if o.action == "close" and broker.position is not None:
                    t = broker.close_now(mid, now, o.reason)
                    desk.say(now, "fills", f"caught up: {o.reason.replace('_', ' ')} exit from the {_utc(bar.ts)} "
                                           f"close sent now" + (f" @ {t.exit_price:,.2f}, net {t.net_pnl:+,.2f}" if t else ""))
                elif o.action == "open":
                    desk.say(now, "chief", f"caught up: the {o.side.value} entry decided at the {_utc(bar.ts)} close "
                                           f"was not taken. Paper filled it at that bar's open, which has passed.")
                    log(f"stale entry not taken: {o.side.value} {o.qty:.5f} from the {_utc(bar.ts)} close")
            # 2. the stop paper holds, against this bar's range
            if broker.missed_stop(bar):
                t = broker.close_now(mid, now, "stop")
                desk.say(now, "risk", f"caught up: the bar that opened {_utc(bar.ts)} went through the trailing stop "
                                      f"the desk moved while away; out now" + (f" @ {t.exit_price:,.2f}" if t else ""))
            # 3. funding (the exchange charges the real thing; this is the board's estimate)
            broker.apply_funding(f.funding_bar[i], bar.close)
            # 4. decide on this close, as paper does: live context and the real clock only for the fresh close
            orders = desk.on_bar_close(f, i, ctx=None if stale else ctx, now_ms=bar.ts + f.bar_ms if stale else now)
            broker.bar_ts = meta["last_bar_ts"] = bar.ts
            blocked = desk.last_decision.blocked_by if desk.last_decision else None
            log(f"[{_utc(bar.ts + f.bar_ms)}] close {bar.close:,.2f} eq ${broker.equity(mid):,.2f} "
                + (("(missed bar) " if i != newest else f"(closed {age_ms / 60_000:.0f} min ago) ") if stale else "")
                + (f"orders {[o.action + ' ' + o.side.value for o in orders]}" if orders else
                   f"no trade ({blocked.name if blocked else 'holding'})"))
    finally:
        broker.defer_stops = False
    if not fresh:
        for o in [o for o in broker.pending if o.action == "open"]:
            broker.pending.remove(o)
            desk.say(now, "chief", f"the {o.side.value} entry decided at the {_utc(f.bars[newest].ts + f.bar_ms)} close "
                                   f"was not taken: that close was {age_ms / 60_000:.0f} min ago, too late to act on at "
                                   f"today's price. Paper filled it at the next bar's open.")
            log(f"late entry not taken: {o.side.value} {o.qty:.5f} from the close {age_ms / 60_000:.0f} min ago")
    broker.execute_now(mid, now)
    broker.flush_stop()


def run(p: Params, mode: str, *, state_dir: str = "state", poll_s: float = 15.0, host: str = "127.0.0.1",
        port: int = 8787, dashboard: bool = True, max_notional: float | None = None, jev=None, log=print,
        feed=None, broker=None, clock=None, sleep=time.sleep):
    """Live trading loop: orders go to the exchange immediately at each bar close, and every bar missed while away
    is caught up first (process_closed_bars). `feed`, `broker`, `clock` and `sleep` let the tests drive it against
    a fake exchange in process; the CLI leaves them out."""
    from .feeds.hyperliquid import HyperliquidFeed

    clock = clock or _now_ms
    paths = live_paths(state_dir, p.market.symbol, mode)
    refuse_shared_state(state_dir)
    lock = _lock(paths["lock"])     # noqa: F841 (held until the process exits)
    kills_path, desk_path = paths["kills"], paths["desk"]
    feed = feed or HyperliquidFeed(p.market.symbol, p.market.interval)

    retrying = dict(coin=p.market.symbol, kills_path=kills_path, wait_s=poll_s, sleep=sleep, log=log)
    if broker is None:
        from .brokers.live import LiveBroker
        broker = _keep_trying("connecting to the exchange", lambda: LiveBroker(
            p, state_path=paths["position"], max_notional_usd=max_notional, log=log, clock=clock), **retrying)
    elif Path(broker.state_path) != paths["position"]:
        from .brokers.live import LiveSetupError
        raise LiveSetupError(f"the broker keeps its position in {broker.state_path}, not {paths['position']}")

    desk = Desk(p, broker, jev=jev)

    def on_alert(text: str) -> None:    # the broker's alerts: on the board, and on the phone
        desk.say(clock(), "risk", f"ALERT: {text}")
        _push(f"{p.market.symbol} LIVE: ALERT", text)

    meta = {"started_ts": clock(), "start_equity": broker.cash, "last_bar_ts": 0}
    if desk_path.exists():
        saved = json.loads(desk_path.read_text())
        desk.load_dict(saved)
        meta.update(saved.get("meta", {}))
    broker.on_alert = on_alert
    for _, text in broker.alerts:       # raised while the broker started up, before the board existed
        on_alert(text)

    board = Board(kills_path)
    if dashboard:
        serve(board, host, port)
        log(f"board: http://{host}:{port}")

    def first_read():
        bars = feed.candles(start_ms=clock() - LOOKBACK_BARS * feed.bar_ms)
        return bars, feed.funding(bars[0].ts), feed.context()

    bars, funding, ctx = _keep_trying("reading the market's candles", first_read, **retrying)
    f = ind.compute(bars, funding, p)
    if hasattr(broker, "check_cap"):
        broker.check_cap(ctx.mid or bars[-1].close)    # a cap too small for any ticket refuses here, not silently later
    if not meta["last_bar_ts"]:
        # Don't act on a bar that closed before we started: show it, trade from the next one.
        meta["last_bar_ts"] = bars[-1].ts
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=broker.equity(ctx.mid),
                                            kills=desk.kill_gates(clock(), broker.equity(ctx.mid)), ctx=ctx,
                                            now_ms=clock())
    goal_cagr = None
    rep = Path(f"reports/{report_name(p.market.symbol)}.json")
    if rep.exists():
        goal_cagr = json.loads(rep.read_text()).get("full", {}).get("cagr")
    log(f"{mode}: {len(bars)} {p.market.interval} bars, {len(funding)} funding prints, "
        f"equity ${broker.equity(ctx.mid):,.2f}, {p.market.symbol} {ctx.mid:,.1f}")

    while True:
        try:
            now = clock()
            desk.kills = Kills.load(kills_path)
            mid = feed.mid()
            broker.check_stop_price(mid, now)

            if broker.close_wanted and broker.position is not None:
                # a close the desk decided that didn't go through: send it again now, not at the next bar close
                reason = broker.close_wanted
                try:
                    t = broker.retry_close(mid, now)
                    if t is not None:
                        desk.say(now, "fills", f"the {reason.replace('_', ' ')} exit went through on a retry @ "
                                               f"{t.exit_price:,.2f}, net {t.net_pnl:+,.2f}")
                except Exception as exc:  # the bars still get processed; the next check tries again
                    log(f"close retry ({reason}) failed: {type(exc).__name__}: {exc}")
                    desk.kills.entry_kill = True
                    desk.kills.save(kills_path)

            if desk.kills.flat_kill and broker.position is not None:
                desk.flatten_now(mid, now, "flat_kill")

            if broker.position is not None and not broker.position.stop:
                # adopted or restored without a stop: protect it immediately
                a = f.atr[-1] or 0.0
                broker.update_stop(broker.position.entry_price - broker.position.side.sign * p.strategy.stop_atr * a)
                if broker.position is not None:
                    desk.say(now, "risk", f"position had no stop. placed one at {broker.position.stop:,.0f}.")

            if now >= meta["last_bar_ts"] + 2 * feed.bar_ms + 5_000:
                fresh = feed.candles(start_ms=bars[-1].ts - 5 * feed.bar_ms)
                merged = {b.ts: b for b in bars}
                merged.update({b.ts: b for b in fresh})
                bars = [merged[k] for k in sorted(merged)][-LOOKBACK_BARS:]
                new_funding = feed.funding(funding[-1].ts + 1 if funding else bars[0].ts)
                funding = (funding + new_funding)[-LOOKBACK_BARS * 8:]
                ctx = feed.context()
                f = ind.compute(bars, funding, p)
                new = [k for k, b in enumerate(bars) if b.ts > meta["last_bar_ts"]]
                if new and bars[0].ts > meta["last_bar_ts"]:
                    # Away longer than the history window: some missed bars can't be rebuilt. Refuse, don't guess.
                    raise SystemExit(
                        f"live {p.market.symbol}: the last bar processed closed {_utc(meta['last_bar_ts'] + feed.bar_ms)}"
                        f" UTC, too long ago to catch up: the {len(bars)}-bar history starts after it. Not trading "
                        f"on a stale view. The exchange-side stop still protects any position. Check the account by "
                        f"hand, then restart with a fresh {desk_path} (move the old one aside).")
                if new:
                    process_closed_bars(desk, broker, f, new, ctx=ctx, now=now, mid=mid, meta=meta, log=log)

            state = board_mod.build(desk, f, len(f.bars) - 1, mode=mode, ctx=ctx, jev_client=jev, now_ms=now,
                                    goal_cagr=goal_cagr, elapsed_days=(now - meta["started_ts"]) / 86400_000, crawler={
                "source": f"hyperliquid · {p.market.interval}", "funding_prints": len(funding),
                "requests": feed.requests, "latency_ms": feed.last_latency_ms,
                "age_s": (now - (f.bars[-1].ts + f.bar_ms)) / 1000})
            state["ticker"]["price"] = mid
            board.set(state)
            _atomic_write(desk_path, {**desk.to_dict(), "meta": meta})
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # network blips must not kill the loop; the exchange-side stop protects us
            log(f"loop error: {type(exc).__name__}: {exc}")
            desk.kills.entry_kill = True
            desk.kills.save(kills_path)
            log("entry_kill set after an error in live mode — clear it from the board once you've checked.")
        sleep(poll_s)


def replay(bars, funding, p: Params, *, speed: float = 25.0, start_index: int = 0, host: str = "127.0.0.1",
           port: int = 8787, jev=None, loop: bool = True, log=print):
    """Drive the board from history so you can watch the desk work (and judge it) bar by bar."""
    board = Board(Path("state") / "replay_kills.json")
    serve(board, host, port)
    log(f"replay board: http://{host}:{port}  ({speed:g} bars/sec)")
    f = ind.compute(bars, funding, p)
    from .backtest import run as bt_run, summarize
    goal_cagr = summarize(bt_run(bars, funding, p))["full"]["cagr"]
    while True:
        broker = SimBroker(p)
        desk = Desk(p, broker, jev=jev)
        for i, bar in enumerate(bars):
            broker.fill_pending(bar.open, bar.ts)
            broker.check_stop_bar(bar)
            broker.apply_funding(f.funding_bar[i], bar.close)
            desk.on_bar_close(f, i, now_ms=bar.ts + f.bar_ms)
            if i >= start_index:
                board.set(board_mod.build(desk, f, i, mode="replay", jev_client=jev, goal_cagr=goal_cagr,
                                          elapsed_days=(bar.ts - bars[0].ts) / 86400_000, crawler={
                    "source": "history replay", "funding_prints": len(funding)}))
                time.sleep(1.0 / speed)
        if not loop:
            return desk
        time.sleep(5)
