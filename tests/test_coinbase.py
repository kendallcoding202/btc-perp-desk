"""BTC on Coinbase: its feed (desk/feeds/coinbase.py) on Coinbase's own recorded answers (tests/fixtures/coinbase/), the
BTC paper account on it, held equal to the backtest under Coinbase's rules, and the old Hyperliquid BTC ledger refused.
Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import backtest as bt
from desk import flows
from desk import paper
from desk import thresholds as th
from desk import venue as venue_mod
from desk.feeds import coinbase as cb
from desk.thresholds import DEFAULT, params_for
from test_desk import H4, FakeFeed, series
from test_venue import DAY, HOUR, TEST_VENUE, _coinbase_p, _ms

FIX = Path(__file__).parent / "fixtures" / "coinbase"


# ---------------------------------------------------------------- the feed, on Coinbase's recorded answers

def recorded() -> tuple[dict, int]:
    fx = {k: json.loads((FIX / f"{k}.json").read_text()) for k in ("product", "candles", "book", "ticker")}
    return fx, fx["product"]["meta"]["recorded_ms"]


class Replay:
    """CoinbaseFeed._get from tests/fixtures/coinbase (real answers, recorded 2026-10-13). `edit(path, params, answer)`
    may change an answer; an unrecorded request fails the test."""

    def __init__(self, edit=None):
        fx, self.now = recorded()
        self.calls = [c for v in fx.values() for c in v["calls"]]
        self.edit, self.seen = edit, []

    def __call__(self, feed, path, params=None, retries=3):
        for c in self.calls:
            if c["path"] == path and c["params"] == (params or {}):
                feed.requests += 1
                self.seen.append(path)
                out = copy.deepcopy(c["response"])
                return self.edit(path, params or {}, out) if self.edit else out
        raise AssertionError(f"no recorded answer for {path} {params}")


@pytest.fixture
def replay(monkeypatch):
    def install(edit=None):
        r = Replay(edit)
        monkeypatch.setattr(cb.CoinbaseFeed, "_get", lambda feed, path, params=None, retries=3: r(feed, path, params))
        return r, cb.CoinbaseFeed(clock=lambda: r.now / 1000)
    return install


def test_the_feed_reads_the_contract_funding_margin_and_session(replay):
    r, feed = replay()
    d = feed.product()
    assert (d["product_id"], d["contract_size"], d["funding_interval"]) == ("BIP-20DEC30-CDE", 0.01, "3600s")
    assert d["funding_rate"] == 1.3e-5 and d["funding_time"] == _ms("2026-10-13T20:00")
    assert d["margin"] == {"intraday_long": 0.100079, "intraday_short": 0.0999811, "overnight_long": 0.21025,
                           "overnight_short": 0.256625}
    assert d["session_open"] is True and d["maintenance"] == [_ms("2026-10-24T10:00"), _ms("2026-10-24T19:00")]
    assert (d["base_min_size"], d["base_max_size"], d["price_increment"]) == (1.0, 5000.0, 5.0)
    assert feed.log.rows == [[_ms("2026-10-13T20:00"), 1.3e-5]]
    ev = feed.funding(r.now - 3 * DAY)
    assert len(ev) == 72 and {e.rate for e in ev} == {1.3e-5} and all(e.ts % HOUR == 0 for e in ev)


def test_the_feed_pages_candles_to_the_1500_closed_bars_the_desk_needs(replay):
    r, feed = replay()
    feed.product()
    bars = feed.candles(start_ms=r.now - 1560 * feed.bar_ms, closed_only=False)
    assert r.seen.count("products/BIP-20DEC30-CDE/candles") == 6                 # 300 a page, newest page first
    ts = [b.ts for b in bars]
    assert ts == sorted(set(ts)) and all(t % (2 * HOUR) == 0 for t in ts)
    assert len(bars) == 1559 and bars[-1].ts + 2 * HOUR > r.now                   # the forming bar is last
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a != 2 * HOUR]
    assert gaps == [(_ms("2026-07-25T12:00"), _ms("2026-07-25T16:00"))]          # 14:00-17:00 maintenance: no candle
    closed = feed.candles(start_ms=r.now - 1560 * feed.bar_ms)
    assert closed == bars[:-1] and len(closed) >= 1500 and closed[-1].ts == _ms("2026-10-13T18:00")
    assert bars[-1].ts == _ms("2026-10-13T20:00") and bars[-1].open == 83240.0 and bars[-1].low <= bars[-1].close


def test_the_context_has_spread_depth_in_dollars_open_interest_and_the_session(replay):
    r, feed = replay()
    ctx = feed.context()
    book = json.loads((FIX / "book.json").read_text())["calls"][0]["response"]["pricebook"]
    mid = (83130 + 83140) / 2
    bid = sum(float(b["price"]) * float(b["size"]) * 0.01 for b in book["bids"] if float(b["price"]) >= mid * 0.999)
    ask = sum(float(a["price"]) * float(a["size"]) * 0.01 for a in book["asks"] if float(a["price"]) <= mid * 1.001)
    assert ctx.mid == mid and ctx.spread_bps == pytest.approx(10 / mid * 1e4)
    assert ctx.depth_usd == pytest.approx(min(bid, ask)) and ctx.depth_usd > DEFAULT.hard.min_depth_usd
    assert ctx.open_interest_usd == pytest.approx(166925 * 0.01 * mid)
    assert ctx.funding_rate_now == 1.3e-5 and ctx.funding_period_hours == 1.0 and ctx.session_open is True


@pytest.mark.parametrize("case", ["short", "empty", "malformed", "impossible", "contract", "book", "session"])
def test_a_short_malformed_or_changed_answer_raises_or_closes_the_session(replay, case):
    def edit(path, params, out):
        if path.endswith("/candles"):
            if case == "short" and params["end"] < r.now // 1000 - 200 * 7200:
                out["candles"] = out["candles"][:100]
            elif case == "empty" and params["end"] == r.now // 1000 - r.now // 1000 % 7200:
                out["candles"] = []
            elif case == "malformed":
                out["candles"][3].pop("close")
            elif case == "impossible":
                out["candles"][5]["high"] = "1"
        elif path == "products/BIP-20DEC30-CDE":
            if case == "contract":
                out["future_product_details"]["contract_size"] = "0.1"
            if case == "session":
                out["fcm_trading_session_details"]["is_session_open"] = False
        elif path == "product_book" and case == "book":
            out["pricebook"]["asks"] = []
        return out

    r, feed = replay(edit)
    if case == "session":
        assert feed.product()["session_open"] is False and feed.context().session_open is False
        return
    with pytest.raises(cb.FeedError):
        if case == "contract":
            feed.product()
        elif case == "book":
            feed.context()
        else:
            feed.product()
            feed.candles(start_ms=r.now - 1560 * feed.bar_ms, closed_only=False)


def test_a_failed_request_is_retried_then_raises(monkeypatch):
    tries = []

    def fail(req, timeout):
        tries.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(cb.urllib.request, "urlopen", fail)
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        cb.CoinbaseFeed().product()
    assert len(tries) == 4 and tries[0] == "https://api.coinbase.com/api/v3/brokerage/market/products/BIP-20DEC30-CDE"


# ---------------------------------------------------------------- the BTC paper account on Coinbase

def test_a_fresh_btc_ledger_opens_at_500_on_coinbase_from_its_real_answers(replay, tmp_path):
    r, feed = replay()
    s = paper.step(params_for("BTC"), tmp_path, feed=feed, log=lambda *a: None, now_ms=r.now)
    led = json.loads((tmp_path / "paper.json").read_text())
    assert led["meta"]["venue"] == "coinbase" and led["meta"]["product"] == "BIP-20DEC30-CDE"
    assert led["meta"]["start_equity"] == 500.0 and led["broker"]["cash"] == 500.0
    assert led["meta"]["last_bar_ts"] == _ms("2026-10-13T18:00")                 # trading starts at the next close
    v = led["venue"]
    assert v["funding_log"] == [[_ms("2026-10-13T20:00"), 1.3e-5]] and v["margin"]["overnight_long"] == 0.21025
    assert v["windows"] == [[_ms("2026-10-24T10:00"), _ms("2026-10-24T19:00")]] and v["session"]["open"] is True
    assert "opened on COINBASE · BTC PERP (BIP-20DEC30-CDE, 0.01 BTC contracts)" in led["desk"]["events"][0]["text"]
    assert s["header"]["title"] == "COINBASE · BTC PERP (0.01 BTC contracts)" and s["header"]["venue"] == "coinbase"
    assert s["agents"][-1]["detail"] == "coinbase · 2h" and len(r.seen) == 9          # product, 6 pages, book, ticker
    sv = s["venue"]
    assert (sv["contracts"], sv["margin_in_use"], sv["one_contract_usd"]) == (0, 0.0, pytest.approx(831.35))
    assert sv["funding"]["estimate"] and sv["funding"]["apr"] == pytest.approx(1.3e-5 * 24 * 365 * 100)
    assert sv["session"]["next_closure"]["why"] == "weekly break"
    gates = {g["name"]: g for g in s["analysis"]["gates"]}
    assert gates["session"]["verdict"] == "PASS" and gates["contracts"]["verdict"] in ("PASS", "DROP")
    assert "(est.)" in next(a for a in s["agents"] if a["key"] == "crowd")["detail"]
    assert s["paper"]["changed"]


def test_the_next_run_logs_a_new_funding_hour_and_is_saved_for_it(replay, tmp_path):
    r, feed = replay()
    paper.step(params_for("BTC"), tmp_path, feed=feed, log=lambda *a: None, now_ms=r.now)

    def later(path, params, out):          # 40 minutes on, the same bars: only the funding hour has moved
        if path == "products/BIP-20DEC30-CDE":
            out["future_product_details"].update(funding_rate="0.000021", funding_time="2026-10-13T21:00:00Z")
        return out

    r2, feed2 = replay(later)
    s = paper.step(params_for("BTC"), tmp_path, feed=feed2, log=lambda *a: None, now_ms=r.now)
    log = json.loads((tmp_path / "paper.json").read_text())["venue"]["funding_log"]
    assert log == [[_ms("2026-10-13T20:00"), 1.3e-5], [_ms("2026-10-13T21:00"), 2.1e-5]]
    assert s["paper"]["bars_processed_this_run"] == 0 and s["paper"]["changed"]
    s = paper.step(params_for("BTC"), tmp_path, feed=replay(later)[1], log=lambda *a: None, now_ms=r.now)
    assert not s["paper"]["changed"]                                             # nothing new: nothing to save


def _hl_btc_ledger(path: Path) -> dict:
    """The BTC ledger as Hyperliquid paper wrote it (no venue in its meta), as on the paper-ledger branch 2026-10-13."""
    d = {"meta": {"started_ts": _ms("2026-10-13T17:34"), "start_equity": 500.0, "last_bar_ts": _ms("2026-10-13T16:00"),
                  "runs": 3, "interval": "2h", "last_run_ts": _ms("2026-10-13T20:08")},
         "broker": {"cash": 500.0, "trades": [], "position": None, "pending": []},
         "desk": {"risk": {}, "equity_curve": [], "events": []}}
    path.mkdir(parents=True, exist_ok=True)
    (path / "paper.json").write_text(json.dumps(d))
    return d


def test_a_hyperliquid_btc_ledger_is_refused_loudly_never_converted(tmp_path):
    before = _hl_btc_ledger(tmp_path / "BTC")

    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError("refused before any request")

    with pytest.raises(paper.LedgerRefused, match="is a Hyperliquid paper account") as exc:
        paper.step(params_for("BTC"), tmp_path / "BTC", feed=NoNetwork(), log=lambda *a: None)
    msg = str(exc.value)
    assert "COINBASE · BTC PERP (BIP-20DEC30-CDE" in msg and "archive it by hand" in msg
    assert "ledger/archive/BTC-hyperliquid-500usd-from-2026-10-13.json" in msg
    assert json.loads((tmp_path / "BTC" / "paper.json").read_text()) == before          # untouched
    assert not (tmp_path / "BTC" / "board.json").exists()
    # and a Coinbase ledger is never taken for a Hyperliquid one either
    eth = tmp_path / "ETH"
    eth.mkdir()
    (eth / "paper.json").write_text(json.dumps({**before, "meta": {**before["meta"], "venue": "coinbase"}}))
    with pytest.raises(paper.LedgerRefused):
        paper.step(params_for("ETH"), eth, feed=NoNetwork(), log=lambda *a: None)


def test_step_contains_the_refusal_and_the_other_accounts_still_run(tmp_path, monkeypatch, capsys):
    from desk import __main__ as cli

    _hl_btc_ledger(tmp_path / "BTC")
    real = paper.step
    ran = []

    def fake(p, ledger_dir, jev=None, **kw):
        if p.market.symbol == "BTC":
            return real(p, ledger_dir, jev=jev, feed=object(), **kw)      # refused before the feed is touched
        ran.append(p.market.symbol)
        return {"paper": {"changed": False, "new_events": []}, "updated": 1, "header": {"coin": p.market.symbol},
                "ticker": {"bank": 150.0, "pnl": 0.0, "pnl_pct": 0.0, "position": "FLAT", "position_detail": "",
                           "record": "0W 0L", "price": 1.0}, "goal": {"start": 150.0, "target": 1e5, "progress": 0.0}}

    monkeypatch.setattr(paper, "step", fake)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="BTC,ETH,SOL", ledger=str(tmp_path), interval=None, risk=None, jev_mode="off",
                              coin=None, flows=str(tmp_path / "none.json"))
    assert cli.cmd_step(args) == 1
    assert ran == ["ETH", "SOL"]
    idx = json.loads((tmp_path / "index.json").read_text())["coins"]
    assert idx[0]["coin"] == "BTC" and idx[0]["error"].startswith("LedgerRefused: ")
    assert "archive it by hand" in idx[0]["error"] and len(idx[0]["error"]) > 200      # the whole reason, not cut
    assert "step failed: LedgerRefused" in capsys.readouterr().out


def test_the_refusal_is_pushed_to_the_phone_every_6_hours(monkeypatch):
    from desk import __main__ as cli

    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: sent.append(req) or type("R", (), {"read": lambda s: b""})())
    six = cli.PAUSED_PUSH_EVERY_MS
    assert cli._notify_refused([("BTC", "ledger/BTC/paper.json is a Hyperliquid paper account")],
                               now_ms=6 * six + 4 * 60_000)
    assert not cli._notify_refused([("BTC", "x")], now_ms=6 * six + 19 * 60_000)
    assert len(sent) == 1 and sent[0].headers["Title"] == "BTC paused: ledger from another venue"


# ---------------------------------------------------------------- the paper ledger == the backtest, on Coinbase rules

class FakeCoinbase(FakeFeed):
    """FakeFeed as Coinbase serves it: no candle while it's closed, and product() lists the next maintenance window
    (as Coinbase lists one at a time) with the margin rates."""

    def __init__(self, bars, funding, upto, windows, sessions, margin=0.21025):
        super().__init__([b for b in bars if not sessions.closed_through(b.ts, b.ts + H4)], funding, upto)
        self.windows, self.margin = windows, margin

    def product(self):
        now = self.all_bars[min(self.upto, len(self.all_bars) - 1)].ts
        nxt = min((w for w in self.windows if w[1] > now), default=None)
        return {"margin": {"overnight_long": self.margin, "overnight_short": 0.256625}, "maintenance": list(nxt) if nxt
                else None, "funding_time": None, "funding_rate": None, "session_open": True}


def _paper_vs_backtest(tmp_path, monkeypatch, bars, funding, p, windows, start=300, seed=1):
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    monkeypatch.setitem(th.VENUES, "coinbase", TEST_VENUE)
    sessions = venue_mod.Sessions(TEST_VENUE, windows)
    feed = FakeCoinbase(bars, funding, start, windows, sessions)
    served = feed.all_bars
    clock = lambda: served[feed.upto].ts + H4 // 2 if feed.upto < len(served) else served[-1].ts + 2 * H4
    paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
    rnd, upto = random.Random(seed), start
    while upto < len(served):
        upto = min(len(served), upto + rnd.choice([1, 1, 2, 5]))     # sometimes the cron misses runs
        feed.upto = upto
        paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock())
    led = json.loads((tmp_path / "paper.json").read_text())
    ref = bt.run(bars, funding, p, windows=windows, start_ms=served[start].ts)     # Binance-style: every bar
    return led, ref


def _same(led, ref):
    b, rb = led["broker"], ref.broker
    assert b["trades"] == [t.as_dict() for t in rb.trades] and len(rb.trades) > 3
    assert b["cash"] == rb.cash and b["fees_total"] == rb.fees_total and b["funding_total"] == rb.funding_total != 0
    assert [tuple(x) for x in led["desk"]["equity_curve"]] == ref.desk.equity_curve
    assert led["desk"]["venue"] == ref.desk.venue_memo
    paper_only = ("BOUGHT", "SOLD", "SHORTED", "COVERED")       # paper also says each fill as it happens
    events = [(e["ts"], e["agent"], e["text"]) for e in led["desk"]["events"][1:] if not e["text"].startswith(paper_only)]
    assert "opened" in led["desk"]["events"][0]["text"] and len(led["desk"]["events"]) < 200
    assert events == [(e.ts, e.agent, e.text) for e in ref.desk.events]


def test_btc_paper_on_coinbase_rules_equals_the_backtest(tmp_path, monkeypatch):
    """Stepping the BTC ledger as the cron does, on Coinbase-style candles (none while it's closed), must give exactly
    what the backtest gives on the Binance-style history with every bar (it drops those inside a closure): the same
    whole-contract trades, fees with the $0.20 minimum, entries skipped (too small for a contract, into a closure),
    cash, equity curve and events, through missed runs and catch-ups."""
    bars, funding = series(seed=3, drift=0.002, n=700, start=2_000.0)    # one contract $30-90, a $150 account
    p = _coinbase_p(starting_equity=150.0)
    plain = bt.run(bars, funding, p, start_ms=bars[300].ts)
    e = [t.entry_ts for t in plain.broker.trades]
    windows = [(e[1], e[1] + 2 * H4), (e[4] - H4, e[4] + 3 * H4)]         # closures over two entries' fills
    led, ref = _paper_vs_backtest(tmp_path, monkeypatch, bars, funding, p, windows)
    _same(led, ref)
    memo = ref.desk.venue_memo
    assert memo["skipped"].get("session", 0) >= 1 and memo["skipped"].get("contracts", 0) >= 1
    assert ref.dropped_bars == 2 + 4
    fees = [t.fees for t in ref.broker.trades]
    assert all(abs(t.qty * 100 - round(t.qty * 100)) < 1e-9 for t in ref.broker.trades)      # whole contracts
    assert any(f == pytest.approx(0.20 * round(t.qty * 100) * 2) for f, t in zip(fees, ref.broker.trades))   # the minimum
    said = " ".join(e["text"] for e in led["desk"]["events"])
    assert "SIZE: skipped — one contract" in said and "FILLS: skipped — Coinbase is closed at the fill" in said
    assert re.search(r"BOUGHT \d+ contracts? \(0\.\d\d BTC\) @ ", said)
    assert re.search(r"go \(breakout\): long \d+ contracts? \(0\.\d\d BTC\)", said)


def test_a_too_small_account_trades_whole_contracts_once_a_deposit_makes_room(tmp_path, monkeypatch):
    """$100 is too small for one 0.01 BTC contract at the stop's risk: entries are skipped (said once each). The
    monthly deposits make room, and it then trades whole contracts sized from the bigger account. A withdrawal a
    contract position needs waits, at the venue's 4x."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    monkeypatch.setitem(th.VENUES, "coinbase", TEST_VENUE)
    bars, funding = series(seed=3, drift=0.002, n=700, start=3_000.0)    # one contract $45-130: too big for $100
    p = _coinbase_p(starting_equity=100.0)
    d0 = datetime.fromtimestamp(bars[300].ts / 1000, tz=timezone.utc)
    plan = flows.schedule(flows.parse(json.dumps({"BTC": {
        "monthly": {"amount": 250, "day": 1, "first": f"{d0.year + (d0.month == 12)}-{d0.month % 12 + 1:02d}-01",
                    "count": 3}}}), ["BTC"]), "BTC")
    feed = FakeCoinbase(bars, funding, 300, [], venue_mod.Sessions(TEST_VENUE))
    clock = lambda: bars[feed.upto].ts + H4 // 2 if feed.upto < len(bars) else bars[-1].ts + 2 * H4
    first_dep = plan[0].ts
    for upto in range(300, 700, 3):
        feed.upto = upto
        paper.step(p, tmp_path, feed=feed, log=lambda *a: None, now_ms=clock(), flows=plan)
    led = json.loads((tmp_path / "paper.json").read_text())
    trades = led["broker"]["trades"]
    skips = [e for e in led["desk"]["events"] if e["text"].startswith("SIZE: skipped")]
    assert skips and skips[0]["ts"] < first_dep and len(led["flows"]) == 2      # Dec and Jan: the data ends before Feb
    assert trades and all(t["entry_ts"] > first_dep for t in trades)              # only once the money came in
    assert all(abs(t["qty"] * 100 - round(t["qty"] * 100)) < 1e-9 and t["qty"] >= 0.01 for t in trades)
    deps = [e["text"] for e in led["desk"]["events"] if e["text"].startswith("DEPOSIT")]
    assert len(deps) == 2 and all("money in, not profit" in d for d in deps)
