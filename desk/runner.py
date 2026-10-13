"""The trading loop for paper and live, plus a replay mode that drives the board from history.

Every `poll` seconds:
    - read the kill switches (dashboard or state/kills.json)
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
from .thresholds import Params

LOOKBACK_BARS = 1500


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


def run(p: Params, mode: str, *, state_dir: str = "state", poll_s: float = 15.0, host: str = "127.0.0.1",
        port: int = 8787, dashboard: bool = True, max_notional: float | None = None, jev=None, log=print):
    """Live trading loop: orders go to the exchange immediately at each bar close."""
    from .feeds.hyperliquid import HyperliquidFeed

    state_dir_p = Path(state_dir)
    kills_path = state_dir_p / "kills.json"
    desk_path = state_dir_p / f"{mode}_desk.json"
    feed = HyperliquidFeed(p.market.symbol, p.market.interval)

    from .brokers.live import LiveBroker
    broker = LiveBroker(p, state_path=state_dir_p / "live_position.json", max_notional_usd=max_notional, log=log)

    desk = Desk(p, broker, jev=jev)

    def on_alert(text: str) -> None:    # the broker's alerts: on the board, and on the phone
        desk.say(_now_ms(), "risk", f"ALERT: {text}")
        _push(f"{p.market.symbol} LIVE: ALERT", text)

    broker.on_alert = on_alert
    for _, text in broker.alerts:       # raised while the broker started up, before the board existed
        on_alert(text)
    meta = {"started_ts": _now_ms(), "start_equity": broker.cash, "last_bar_ts": 0}
    if desk_path.exists():
        saved = json.loads(desk_path.read_text())
        desk.load_dict(saved)
        meta.update(saved.get("meta", {}))

    board = Board(kills_path)
    if dashboard:
        serve(board, host, port)
        log(f"board: http://{host}:{port}")

    bars = feed.candles(start_ms=_now_ms() - LOOKBACK_BARS * feed.bar_ms)
    funding = feed.funding(bars[0].ts)
    f = ind.compute(bars, funding, p)
    ctx = feed.context()
    if not meta["last_bar_ts"]:
        # Don't act on a bar that closed before we started: show it, trade from the next one.
        meta["last_bar_ts"] = bars[-1].ts
        desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=broker.equity(ctx.mid),
                                            kills=desk.kill_gates(_now_ms(), broker.equity(ctx.mid)), ctx=ctx,
                                            now_ms=_now_ms())
    goal_cagr = None
    rep = Path("reports/backtest.json")
    if rep.exists():
        goal_cagr = json.loads(rep.read_text()).get("full", {}).get("cagr")
    log(f"{mode}: {len(bars)} {p.market.interval} bars, {len(funding)} funding prints, "
        f"equity ${broker.equity(ctx.mid):,.2f}, {p.market.symbol} {ctx.mid:,.1f}")

    while True:
        try:
            now = _now_ms()
            desk.kills = Kills.load(kills_path)
            mid = feed.mid()
            broker.check_stop_price(mid, now)

            if desk.kills.flat_kill and broker.position is not None:
                desk.flatten_now(mid, now, "flat_kill")

            if broker.position is not None and not broker.position.stop:
                # adopted or restored without a stop: protect it immediately
                a = f.atr[-1] or 0.0
                broker.update_stop(broker.position.entry_price - broker.position.side.sign * p.strategy.stop_atr * a)
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
                i = len(bars) - 1
                if bars[i].ts > meta["last_bar_ts"]:
                    broker.apply_funding(f.funding_bar[i], bars[i].close)
                    orders = desk.on_bar_close(f, i, ctx=ctx, now_ms=now)
                    broker.execute_now(mid, now)
                    meta["last_bar_ts"] = bars[i].ts
                    blocked = desk.last_decision.blocked_by if desk.last_decision else None
                    log(f"[{time.strftime('%Y-%m-%d %H:%M', time.gmtime(now / 1000))}] close {bars[i].close:,.0f} "
                        f"eq ${broker.equity(mid):,.2f} "
                        + (f"orders {[o.action + ' ' + o.side.value for o in orders]}" if orders else
                           f"no trade ({blocked.name if blocked else 'holding'})"))

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
        time.sleep(poll_s)


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
