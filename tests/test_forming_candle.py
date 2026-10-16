"""BTC's chart draws the 2h candle that's still forming as its newest, on Coinbase's prices only.

The run already reads it (paper fills a pending order at its open and checks the stop against its range); the board
now carries it for the chart (chart.forming, chart.forming_as_of), after the ledger is saved: the ledger and every
decision are as they were. Between runs the page carries it on with the prices Coinbase's ticker sends it (never
Coinbase's candles channel, never Hyperliquid's candle on this chart), and draws a candle it built or widened from them
as provisional (dashed): the page sees no price from before it opened, so a high or a low since the run can be missing.
ETH and SOL keep Hyperliquid's forming candle, as before."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import board, paper
from desk.thresholds import params_for
from test_coinbase import replay  # noqa: F401 (the fixture: Coinbase's recorded answers)
from test_desk import H4, FakeFeed, P, series

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.js").read_text()
CSS = (ROOT / "dashboard/style.css").read_text()
FIX = Path(__file__).parent / "fixtures" / "coinbase"
TWO_H = 7200_000


# ---------------------------------------------------------------- the board

def _fixture_forming() -> list:
    rows = [r for c in json.loads((FIX / "candles.json").read_text())["calls"] for r in c["response"]["candles"]]
    r = max(rows, key=lambda r: int(r["start"]))       # 2026-10-13 20:00 UTC, recorded at 20:22: still forming
    return [int(r["start"]) * 1000] + [float(r[k]) for k in ("open", "high", "low", "close")]


def test_btcs_board_carries_coinbases_forming_candle_and_the_runs_time(replay, tmp_path):  # noqa: F811
    r, feed = replay()
    s = paper.step(params_for("BTC"), tmp_path, feed=feed, log=lambda *a: None, now_ms=r.now)
    ch = s["chart"]
    assert ch["forming"] == _fixture_forming() and ch["forming_as_of"] == r.now
    assert ch["forming"][0] == ch["candles"][-1][0] + ch["bar_ms"]          # the window after the newest close
    assert ch["forming"][0] <= r.now < ch["forming"][0] + ch["bar_ms"]
    saved = json.loads((tmp_path / "board.json").read_text())["chart"]
    assert saved["forming"] == ch["forming"] and saved["forming_as_of"] == r.now


def test_the_ledger_is_byte_identical_with_or_without_it(replay, tmp_path, monkeypatch):  # noqa: F811
    """Display only: the same answers make the same paper.json byte for byte, and the board differs by the two new
    fields alone."""
    out = {}
    for name in ("with", "without"):
        if name == "without":
            monkeypatch.setattr(board, "with_forming", lambda state, bar, as_of: state)
        r, feed = replay()
        d = tmp_path / name
        for _ in range(2):              # day one, then a run on the same answers
            paper.step(params_for("BTC"), d, feed=feed, log=lambda *a: None, now_ms=r.now)
        out[name] = ((d / "paper.json").read_bytes(), json.loads((d / "board.json").read_text()))
    assert out["with"][0] == out["without"][0]
    a, b = out["with"][1], out["without"][1]
    assert set(a["chart"]) - set(b["chart"]) == {"forming", "forming_as_of"}
    a["chart"].pop("forming"), a["chart"].pop("forming_as_of")
    assert a == b


def test_no_forming_candle_yet_adds_nothing_and_hyperliquid_boards_have_none(tmp_path, monkeypatch):
    """A window with no trade yet has no candle at Coinbase: nothing is added. ETH and SOL (Hyperliquid) never get
    one: their page draws Hyperliquid's forming candle, as before."""
    st = {"chart": {"candles": [[0, 1, 1, 1, 1]]}}
    assert board.with_forming(st, None, 5) == {"chart": {"candles": [[0, 1, 1, 1, 1]]}}
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    bars, funding = series(seed=3, n=400)
    feed = FakeFeed(bars, funding, 350)                 # bar 350 is forming
    s = paper.step(P, tmp_path, feed=feed, log=lambda *a: None, now_ms=bars[350].ts + H4 // 2)
    assert "forming" not in s["chart"] and "forming_as_of" not in s["chart"]


# ---------------------------------------------------------------- the page

def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, k = 0, src.index("{", start)
    for j in range(k, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def _between(src: str, start: str, end: str) -> str:
    k = src.index(start)
    return src[k:src.index(end, k)]


_PAGE = ("const DAY_MS = 86400000;\n" + _between(APP, "  const deskMT = (() => {", "  window.deskMT = deskMT;")
         + "const MT = deskMT, TZ = deskMT.zone, hhmm = (ms) => MT.clock(ms);\n"
         + "const cb = { barMs: 7200000, bars: new Map() };\n"
         + _function(APP, "cbSeen") + _function(APP, "formingBars") + _function(APP, "provNote") + """
const H = 3600000, T0 = Date.parse("2026-10-16T18:00:00Z");   // a 2h window's open (12:00 pm MT)
// a BTC board from the run at 18:20 UTC: closed candles to 16:00, the forming 18:00 one as Coinbase answered it
const boardAt = (forming, asOf) => ({ analysis: { outlook: [{ level: 85000 }], outlook_side: "long" }, chart: {
  bar_ms: 2 * H, candles: [[T0 - 4 * H, 1, 2, 0.5, 1.5], [T0 - 2 * H, 1.5, 2, 1, 80000]], ema_fast: [1, 1], ema_slow: [1, 1],
  don_hi: [2, 2], don_lo: [0, 0], ...(forming ? { forming, forming_as_of: asOf } : {}) } });
const draw = (b, now) => { const s = JSON.parse(JSON.stringify(b)); formingBars(s, now); const c = s.chart;
  return { n: c.candles.length, tail: c.candles.slice(2), prov: c.prov, feed: c.provFeed, run: c.provRun,
    lens: [c.ema_fast.length, c.ema_slow.length, c.don_hi.length, c.don_lo.length], don: c.don_hi.slice(2), note: provNote(c) }; };
const run = T0 + 20 * 60000, F = [T0, 80000, 80400, 79800, 80100], out = {};
out.as_answered = draw(boardAt(F, run), run + 60000);           // no price from the feed yet: the run's own, provisional
cbSeen(80050, run - 5 * 60000);                                 // a price from before the run (the page was open)
out.before_run = draw(boardAt(F, run), run + 60000);
cbSeen(80600, run + 3 * 60000); cbSeen(79700, run + 4 * 60000); cbSeen(80300, run + 5 * 60000);
out.carried = draw(boardAt(F, run), run + 6 * 60000);           // widened to 80600 / 79700, close 80300: provisional
cb.bars.clear();
out.window_closed = draw(boardAt(F, run), T0 + 2 * H + 60000);  // the run's own, its window closed since: provisional
cbSeen(80200, T0 + 2 * H + 10000); cbSeen(80900, T0 + 2 * H + 20000); cbSeen(80700, T0 + 2 * H + 30000);
out.rolled = draw(boardAt(F, run), T0 + 2 * H + 60000);         // the next window, from the feed's prices alone
cbSeen(81000, T0 + 6 * H + 1000);                               // a window after one the page saw nothing of
out.gap = draw(boardAt(F, run), T0 + 6 * H + 2000);
cb.bars.clear(); cbSeen(80100, T0 + 1000); cbSeen(80500, T0 + 2000);
out.no_forming = draw(boardAt(null, 0), T0 + 3000);             // Coinbase had no trade in it at the run
cb.bars.clear();
out.stale = draw(boardAt([T0 - 2 * H, 1, 2, 1, 1], run), run);  // a forming candle the closes have passed: ignored
cb.bars.clear(); for (let k = 0; k < 5; k++) cbSeen(80000 + k, T0 + k * 2 * H);
out.kept = [...cb.bars.keys()].map((t) => (t - T0) / H);       // the newest three windows
console.log(JSON.stringify(out));
""")


def _page(tz: str = "Asia/Tokyo") -> dict:
    import os

    out = subprocess.run(["node", "-e", _PAGE], capture_output=True, text=True, check=True,
                         env={**os.environ, "TZ": tz}).stdout
    return json.loads(out)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_page_carries_it_on_with_coinbases_prices_and_says_when_its_provisional():
    o = _page()
    T0 = 1792173600000          # 2026-10-16 18:00 UTC
    F = [T0, 80000, 80400, 79800, 80100]
    # the run's own, as it answered, its window still open: provisional too (the run saw part of it, its close is the
    # run's price), and said so: never drawn solid as if final and current
    assert o["as_answered"] == {"n": 3, "tail": [F], "prov": [2], "feed": False, "run": True, "lens": [3, 3, 3, 3],
                                "don": [85000], "note": " · newest candle: provisional, the desk's as of 12:20 pm MT; "
                                                        "the desk's own candle replaces it at the next run"}
    assert o["before_run"] == o["as_answered"]           # prices from before the run add nothing
    c = o["carried"]
    assert c["tail"] == [[T0, 80000, 80600, 79700, 80300]] and c["prov"] == [2] and c["feed"]
    assert c["note"] == (" · newest candle: provisional, from Coinbase's live feed; the desk's own candle replaces it at "
                         "the next run")
    w = o["window_closed"]
    assert w["tail"] == [F] and w["prov"] == [2] and w["run"] and not w["feed"]
    assert w["note"] == (" · newest candle: provisional, the desk's as of 12:20 pm MT, before its window closed; the "
                         "desk's own candle replaces it at the next run")
    r = o["rolled"]
    assert r["tail"] == [F, [T0 + 7200_000, 80200, 80900, 80200, 80700]] and r["prov"] == [2, 3]
    assert r["lens"] == [4, 4, 4, 4] and r["don"] == [85000, 85000]
    assert r["note"] == (" · newest 2 candles: provisional, from Coinbase's live feed and the desk's as of 12:20 pm MT, "
                         "before its window closed; the desk's own candles replace them at the next run")
    assert o["gap"]["tail"] == r["tail"]                 # 22:00 UTC never seen: 00:00 isn't drawn next to 20:00
    assert o["no_forming"]["tail"] == [[T0, 80100, 80500, 80100, 80500]] and o["no_forming"]["prov"] == [2]
    assert o["stale"]["n"] == 2 and o["stale"]["prov"] == []
    assert o["kept"] == [4, 6, 8]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_note_is_in_mountain_time_whatever_the_phones_zone():
    assert _page("America/Denver")["window_closed"]["note"] == _page("Asia/Tokyo")["window_closed"]["note"]


def test_only_the_tickers_prices_never_coinbases_candles_channel_or_hyperliquids_candle():
    """The socket subscribes to heartbeats and the ticker only. The ticker's prices are all that build it (cbSeen, from
    cbMessage after a price is taken as live). Hyperliquid's forming candle is drawn on a chart without a venue only."""
    sends = [line for line in APP.splitlines() if "ws.send(" in line]
    assert len(sends) == 2 and all('"heartbeats"' in s or '"ticker"' in s for s in sends) and "candles" not in "".join(sends)
    assert "cbSeen(p, ts && Math.abs(ts - now) <= CB_SKEW ? ts : now);" in _function(APP, "cbMessage")
    assert "cb.price = p" in _function(APP, "cbMessage").split("cbSeen(")[0]
    live = _function(APP, "withLive")
    assert "if (s.venue) formingBars(s, Date.now());" in live
    assert "if (c && !s.venue && live.candleCoin" in live          # Hyperliquid's candle: never on BTC's (venue) chart
    assert "live." not in _function(APP, "formingBars")


def test_a_provisional_candle_is_dashed_in_both_editions_and_said_in_the_legend_and_footer():
    draw = _function(APP, "drawChart")
    assert "if (prov.has(k)) { provCandle(" in draw
    prov = _function(APP, "provCandle")
    assert "g.setLineDash([2, 2])" in prov and "strokeRect(" in prov and 'css("--ink")' in prov
    assert ".swatch.prov" in CSS and "dashed" in CSS.split(".swatch.prov", 1)[1].split("\n", 1)[0]
    assert 'newest candle${s.chart.prov.length > 1 ? "s" : ""} <i class="swatch prov"></i> provisional' in APP
    assert "provNote(s.chart)" in _function(APP, "foot")
