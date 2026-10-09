"""python -m desk <command>

    backtest     replay history with every cost charged, write reports/backtest.md
    replay       watch the desk trade history on the board (http://127.0.0.1:8787)
    paper        the $50 paper experiment on live Hyperliquid prices, with the board (step 1)
    step         process any newly closed bars in the paper ledger once, then exit (for cron / GitHub Actions)
    live         trade real money — refuses unless the backtest AND paper record pass
    reset-halt   clear a drawdown halt after you've looked at what happened
    jev-test     check that JEV_API_KEY works
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

from . import backtest as bt
from .thresholds import DEFAULT, Params


def _load_env(path: str = ".env") -> None:
    """Minimal .env loader so secrets never have to be typed on the command line."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _params(args) -> Params:
    p = DEFAULT
    if getattr(args, "interval", None) and args.interval != p.market.interval:
        p = p.with_(market={"interval": args.interval})
    if getattr(args, "risk", None):
        p = p.with_(sizing={"risk_per_trade": args.risk})
    if getattr(args, "jev_mode", None):
        p = p.with_(jev={"mode": args.jev_mode})
    return p


def _jev(p: Params):
    from .jev import JevClient

    if p.jev.mode == "off" or not os.environ.get("JEV_API_KEY"):
        return None
    return JevClient(model=p.jev.model, timeout_s=p.jev.timeout_s, cache_path="data/jev_cache.jsonl")


def _history(p: Params, source: str, start: str, end: str | None):
    if source == "hyperliquid":
        from .feeds.hyperliquid import HyperliquidFeed

        cache = Path(f"data/hyperliquid_{p.market.interval}.pkl")
        feed = HyperliquidFeed(p.market.symbol, p.market.interval)
        bars = feed.candles()
        funding = feed.funding(bars[0].ts)
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(pickle.dumps((bars, funding)))
        print(f"  crawler: {len(bars):,} bars, {len(funding):,} funding prints from Hyperliquid")
        return bars, funding
    from .feeds import binance_vision

    return binance_vision.load(interval=p.market.interval, start=start, end=end)


def cmd_backtest(args) -> int:
    p = _params(args)
    bars, funding = _history(p, args.source, args.start, args.end)
    jev = _jev(p) if p.jev.mode == "gate" else None
    r = bt.run(bars, funding, p, jev=jev)
    s = bt.summarize(r)
    extra = ""
    if args.research:
        from . import research

        print("  research: sensitivity, ablation, take profit, sizing, costs" + (", jev" if _jev(p) else ""))
        parts = [research.sensitivity(bars, funding, p), research.ablation(bars, funding, p),
                 research.take_profit(bars, funding, p), research.sizing(bars, funding, p),
                 research.costs(bars, funding, p)]
        client = _jev(p.with_(jev={"mode": "advisory"}))
        if client:
            parts.append(research.jev_experiment(bars, funding, p, client))
        extra = "\n\n".join(parts)
    path = bt.write_report(s, p, extra_md=extra, name=args.name)
    f, o = s["full"], s["out_of_sample"]
    print(f"\n  {f['from']} → {f['to']}: {f['return']:+.1%} total ({bt._pct(f['cagr'])}/yr), max drawdown "
          f"{f['max_drawdown']:.1%}, {f['trades']} trades, profit factor {f['profit_factor']:.2f}")
    print(f"  out-of-sample {o['from']} → {o['to']}: {o['return']:+.1%}, PF {o['profit_factor']:.2f}")
    print(f"  buy & hold: {f['buy_hold_return']:+.1%} (max drawdown {f['buy_hold_drawdown']:.0%})")
    for label, ok, val in bt.promotion_check(s, p):
        print(f"  {'PASS' if ok else 'FAIL'}  {label} — {val}")
    print(f"\n  report: {path}")
    return 0


def cmd_replay(args) -> int:
    from . import runner

    p = _params(args)
    bars, funding = _history(p, args.source, args.start, args.end)
    start = next((k for k, b in enumerate(bars) if b.ts >= _ms(args.watch_from)), 0) if args.watch_from else 0
    runner.replay(bars, funding, p, speed=args.speed, start_index=start, host=args.host, port=args.port,
                  jev=_jev(p))
    return 0


def _ms(ym: str) -> int:
    from datetime import datetime, timezone

    return int(datetime.strptime(ym, "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)


def cmd_paper(args) -> int:
    from . import runner

    p = _params(args)
    runner.run_paper(p, ledger_dir=args.ledger, poll_s=args.poll, host=args.host, port=args.port,
                     dashboard=not args.no_dashboard, jev=_jev(p))
    return 0


def _notify(state: dict) -> None:
    """Push what the desk just did to your phone via ntfy.sh, if NTFY_TOPIC is set (install the ntfy app,
    subscribe to the same topic). Pick a long random topic name: anyone who knows it can read it."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    events = state.get("paper", {}).get("new_events", [])
    if not topic or not events:
        return
    import urllib.request

    t = state["ticker"]
    body = "\n".join(f"{e['agent'].upper()}: {e['text']}" for e in events)
    body += f"\n\nequity ${t['bank']:,.2f} ({t['pnl_pct']:+.2%}) · {t['position']} · BTC ${t['price']:,.0f}"
    trade = any(w in body for w in ("BOUGHT", "SOLD", "HALT", "daily"))
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), method="POST", headers={
        "Title": "BTC desk: " + ("TRADE" if trade else "update"), "Priority": "high" if trade else "default",
        "Tags": "chart_with_upwards_trend" if trade else "robot"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # a missed notification must never break the ledger
        print(f"  notify failed: {exc}")


def cmd_step(args) -> int:
    from . import paper

    p = _params(args)
    s = paper.step(p, args.ledger, jev=_jev(p))
    _notify(s)
    t, g = s["ticker"], s["goal"]
    print(f"  paper equity {t['bank']:,.2f} ({t['pnl_pct']:+.2%}) · {t['position']} {t['position_detail']} · "
          f"{t['record']} · {g['progress']:.1%} of the way to ${g['target']:,.0f}")
    return 0


def promotion_status(p: Params) -> list[tuple[str, bool, str]]:
    checks = []
    rep = Path("reports/backtest.json")
    if not rep.exists():
        return [("backtest report exists (run `python -m desk backtest`)", False, "missing")]
    s = json.loads(rep.read_text())
    checks += [(f"backtest: {label}", ok, val) for label, ok, val in bt.promotion_check(s, p)]
    ledger = Path("ledger/paper.json")
    if not ledger.exists():
        checks.append(("paper record exists (run `python -m desk paper`)", False, "missing"))
        return checks
    saved = json.loads(ledger.read_text())
    meta, broker = saved["meta"], saved["broker"]
    import time

    days = (time.time() * 1000 - meta.get("started_ts", time.time() * 1000)) / 86400_000
    ret = broker["cash"] / meta.get("start_equity", broker["cash"]) - 1 if meta.get("start_equity") else 0.0
    checks.append((f"paper traded ≥ {p.promotion.min_paper_days:.0f} days", days >= p.promotion.min_paper_days,
                   f"{days:.1f} days"))
    checks.append((f"paper return > {p.promotion.min_paper_return:.0%}", ret > p.promotion.min_paper_return,
                   f"{ret:+.2%} realised"))
    return checks


def cmd_live(args) -> int:
    from . import runner

    p = _params(args)
    checks = promotion_status(p)
    for label, ok, val in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label} — {val}")
    if not all(ok for _, ok, _ in checks):
        if not args.force_unproven:
            print("\n  Not going live: the desk hasn't proven itself yet. Keep paper trading.")
            return 2
        print("\n  --force-unproven: you are overriding the go-live gate. This is your call, not the desk's.")
    if args.max_notional is None:
        print("  live requires --max-notional (start with something you can lose, e.g. 50)")
        return 2
    phrase = "I understand this trades real money and can lose all of it"
    if os.environ.get("DESK_LIVE_CONFIRM") != phrase:
        print(f'  set DESK_LIVE_CONFIRM="{phrase}" in .env to arm live trading.')
        return 2
    runner.run(p, "live", poll_s=args.poll, host=args.host, port=args.port, dashboard=not args.no_dashboard,
               max_notional=args.max_notional, jev=_jev(p))
    return 0


def cmd_reset_halt(args) -> int:
    path = Path(f"state/{args.mode}_desk.json")
    if not path.exists():
        print("no desk state found")
        return 1
    d = json.loads(path.read_text())
    risk = d.get("risk", {})
    print(f"  halted: {risk.get('halted')} — {risk.get('halt_reason') or 'n/a'}")
    risk.update({"halted": False, "halt_reason": "", "peak_equity": 0.0})
    d["risk"] = risk
    path.write_text(json.dumps(d))
    print("  halt cleared; peak equity resets to current equity on the next bar.")
    return 0


def cmd_jev_test(args) -> int:
    from .jev import QUESTIONS, JevClient

    c = JevClient()
    if not c.enabled:
        print("JEV_API_KEY is not set (put it in .env)")
        return 1
    ans = c.ask("BTC perpetual, 4h bars. Candidate entry: LONG. Close just broke the prior 30-bar high by 0.4 ATR. "
                "Funding +8% annualised.", QUESTIONS)
    if ans is None:
        print(f"Jev call failed: {c.last_error}")
        return 1
    print(json.dumps(ans, indent=1))
    return 0


def main(argv=None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="python -m desk", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, history=False, server=False):
        sp.add_argument("--interval", default=None, help="bar size (default from thresholds.py: 4h)")
        sp.add_argument("--risk", type=float, default=None, help="risk per trade, e.g. 0.01 = 1%%")
        sp.add_argument("--jev-mode", choices=["off", "advisory", "gate"], default=None)
        if history:
            sp.add_argument("--source", choices=["binance", "hyperliquid"], default="binance")
            sp.add_argument("--start", default="2020-01")
            sp.add_argument("--end", default=None)
        if server:
            sp.add_argument("--host", default="127.0.0.1")
            sp.add_argument("--port", type=int, default=8787)

    sp = sub.add_parser("backtest")
    common(sp, history=True)
    sp.add_argument("--research", action="store_true", help="add sensitivity/ablation/sizing/cost/Jev sections")
    sp.add_argument("--name", default="backtest", help="report file name under reports/")
    sp.set_defaults(fn=cmd_backtest)

    sp = sub.add_parser("replay")
    common(sp, history=True, server=True)
    sp.add_argument("--speed", type=float, default=25.0, help="bars per second")
    sp.add_argument("--watch-from", default="2024-01", help="YYYY-MM to start drawing from (earlier bars run instantly)")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("step")
    common(sp)
    sp.add_argument("--ledger", default="ledger")
    sp.set_defaults(fn=cmd_step)

    for name, fn in (("paper", cmd_paper), ("live", cmd_live)):
        sp = sub.add_parser(name)
        common(sp, server=True)
        sp.add_argument("--poll", type=float, default=60.0 if name == "paper" else 15.0,
                        help="seconds between checks")
        sp.add_argument("--no-dashboard", action="store_true")
        if name == "paper":
            sp.add_argument("--ledger", default="ledger")
        if name == "live":
            sp.add_argument("--max-notional", type=float, default=None, help="hard USD cap per position")
            sp.add_argument("--force-unproven", action="store_true", help="override the go-live gate (don't)")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("reset-halt")
    sp.add_argument("--mode", choices=["paper", "live"], default="paper")
    sp.set_defaults(fn=cmd_reset_halt)

    sp = sub.add_parser("jev-test")
    sp.set_defaults(fn=cmd_jev_test)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
