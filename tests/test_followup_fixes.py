"""What the review of the Mountain Time, live card and forming candle follow-up found, each pinned here.

VT's monitor named the next day's close as the one it decided at; Coinbase's short-answer error came out as one end
in MT beside an unlabelled UTC one; the live runner's catch-up words had bare UTC times; the chart's day tick was an
hour off on the two days a year the clocks change; BTC's forming candle, as the run answered it, was drawn solid
(final and current) whenever the page had no newer price; and a 320 px phone scrolled sideways once the run's hour
had two digits, while a few one-line readouts lost their ends at 375 and 390 px. The page's own functions are run
in node, under a phone set to Tokyo and one set to Denver."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import board, mountain, paper, runner
from desk.feeds import coinbase as cb
from desk.thresholds import VOLTREND
from test_coinbase import served  # noqa: F401 (the fixture: Coinbase's recorded answers, served)

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
APP = (DASH / "app.js").read_text()
ACT = (DASH / "activity.js").read_text()
STYLE = (DASH / "style.css").read_text()
RUNNER = (ROOT / "desk" / "runner.py").read_text()
HOUR = 3600_000

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


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


MT_JS = ("const DAY_MS = 86400000;\n" + _between(APP, "  const deskMT = (() => {", "  window.deskMT = deskMT;")
         + "const MT = deskMT, TZ = deskMT.zone, hhmm = (ms) => MT.clock(ms);\n")


def _node(js: str, tz: str = "Asia/Tokyo"):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True,
                         env={**os.environ, "TZ": tz}).stdout.strip()
    return json.loads(out)


# ---------------------------------------------------------------- VT: the close it decided at

def _vt_board(tmp_path):
    from test_desk import DAY, FakeDailyFeed, _vt_market

    series, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in series}
    t0 = series["BTC"][0].ts
    for k in (60, 75):                                  # day one, then a run 15 days on: decisions at real closes
        clock["now"] = t0 + k * DAY + DAY // 2
        s = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    return s


@node
@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/Denver"])
def test_vts_monitor_names_the_close_it_decided_at_the_same_as_its_replay_label(tmp_path, tz):
    """decided_ts is the close itself (voltrend decides at ts + DAY_MS, the day's 00:00 UTC close), so it equals the
    board's last_close_ts. The monitor added a day to it: "the 10-15 close" in the REPLAY label above and "decided at
    the 10-16 close" under it, a close that hadn't happened. Both now name the close's own MT date."""
    s = _vt_board(tmp_path)
    bk = s["basket"]
    dts = [h["decided_ts"] for h in bk["holdings"] if h["decided_ts"] is not None]
    assert dts and max(dts) == bk["last_close_ts"]            # the board's semantics: the close, not the day after
    js = MT_JS + """
const DAY = 86400000, set = (el, t) => { el.textContent = t; }, md = (ms) => MT.md(ms), HHMM = (ms) => MT.clock(ms, { up: true });
const fix = (x, dp) => Number(x).toFixed(dp), esc = (x) => String(x), modeWord = () => "paper", stopRun = () => {};
const reduced = { matches: true }, visible = () => false, monOn = false, runs = {}, chain = () => null;
const node = () => ({ textContent: "", innerHTML: "", children: [], classList: { add() {}, remove() {} } });
const M = { rl: node(), cnt: node(), nodes: node(), read: node() };
const B = """ + json.dumps(s) + ";\n" + _function(ACT, "vtStrip") + (
        _function(ACT, "replayLabel") if "function replayLabel(" in ACT else "") + """
vtStrip(false);
console.log(JSON.stringify({ rl: (M.rl.innerHTML || M.rl.textContent).replace(/<[^>]+>/g, ""), read: M.read.textContent }));
"""
    out = _node(js, tz)
    want = datetime.fromtimestamp(bk["last_close_ts"] / 1000, tz=timezone.utc).astimezone(mountain.TZ).strftime("%m-%d")
    assert re.search(r"· the (\d\d-\d\d) close$", out["rl"])[1] == want
    assert re.match(r"decided at the (\d\d-\d\d) close · ", out["read"])[1] == want, out


def test_vts_decided_line_adds_no_day():
    line = [x for x in ACT.splitlines() if "decided at the ${" in x]
    assert line == ['      : `decided at the ${md(Math.max(...dts))} close · ${orders.length ? orders.join(" · ") : '
                    '"no order waiting"}`);']


# ---------------------------------------------------------------- Coinbase's short answer: both ends in MT

def test_a_short_answers_span_is_mt_at_both_ends(served, tmp_path, monkeypatch):  # noqa: F811
    """coinbase.py says "(2026-10-10 00:00 to 2026-10-11 02:00 UTC)": one label, at the end. Only the second time was
    converted, so the card read "(2026-10-10 00:00 to Oct 10, 8:00 pm MT)": a UTC time with no label beside an MT one.
    Both are converted now, on the board's copy only (the desk's words, and every ledger, stay as they were)."""
    s, feed = served(drop=[t for t in range(_ms("2026-10-10T00:00"), _ms("2026-10-11T04:00"), 2 * HOUR)])
    feed.product()
    with pytest.raises(cb.FeedError) as caught:
        feed.candles(start_ms=s.now - 1560 * feed.bar_ms, closed_only=False)
    said = str(caught.value)
    assert "(2026-10-10 00:00 to 2026-10-11 02:00 UTC)" in said                 # the feed's own words: unchanged
    shown = board.in_mountain_time(said, s.now)
    assert "(Oct 9, 6:00 pm MT to Oct 10, 8:00 pm MT)" in shown and "UTC" not in shown
    # through the step, as index.json's error row (the BTC card's "error:" line and the notice)
    from desk import __main__ as cli
    import argparse

    exc = caught.value
    monkeypatch.setattr(paper, "step", lambda *a, **k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(cli, "_notify_failed", lambda *a, **k: None, raising=False)
    args = argparse.Namespace(coins="BTC", ledger=str(tmp_path), interval=None, risk=None, jev_mode="off", coin=None,
                              flows=str(tmp_path / "none.json"))
    cli.cmd_step(args)
    row = json.loads((tmp_path / "index.json").read_text())["coins"][0]
    assert row["error"].startswith("FeedError: 14 2h candles missing") and "UTC" not in row["error"]
    assert "(Oct 9, 6:00 pm MT to Oct 10, 8:00 pm MT)" in row["error"]


def test_a_utc_span_on_either_side_of_the_switch():
    said = "missing (2026-10-31 20:00 to 2026-11-01 10:00 UTC): a short answer"
    assert board.in_mountain_time(said, 0) == "missing (Oct 31, 2:00 pm MT to Nov 1, 3:00 am MT): a short answer"
    assert board.in_mountain_time("the 2026-10-12 22:00 UTC candle", 0) == "the Oct 12, 4:00 pm MT candle"   # as before


# ---------------------------------------------------------------- the live runner's catch-up words

def test_the_live_runners_times_are_labelled_utc_so_the_board_shows_them_in_mt(tmp_path, monkeypatch):
    """runner._utc wrote "2026-10-12 22:00" with no zone: board.in_mountain_time converts only a time labelled UTC,
    so a LIVE board showed a bare UTC time on a page that says MT. Labelled now; the board shows it in MT."""
    from test_desk import P, series
    from test_live import _desk_events, _run_live

    assert runner._utc(_ms("2026-10-12T22:00")) == "2026-10-12 22:00 UTC"
    assert not re.search(r"_utc\([^\n]*\)\}\"\s*\n\s*f\" UTC", RUNNER)     # never "UTC UTC"
    bars, funding = series(seed=3, drift=0.002, n=700)
    _run_live(tmp_path, P.with_(hard={"max_data_age_bars": 1e9}), bars, funding, 240, [1] * 9 + [5])
    said = [e for e in _desk_events(tmp_path) if "caught up" in e.text]
    assert said and all(re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", e.text) for e in said)
    shown = [x["text"] for x in board.events(said)]
    assert all("UTC" not in t and re.search(r"[A-Z][a-z]{2} \d+, \d+:\d\d [ap]m MT", t) for t in shown), shown


# ---------------------------------------------------------------- the chart's day tick on a clock-change day

@node
@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/Denver"])
def test_mt_midnight_is_right_on_the_days_the_clocks_change(tz):
    """midnight() took the clock's time of day off: 1:00 am MST on Nov 1 is 2 h after midnight, not 1, and 4:00 am MDT
    on a March Sunday 3 h, not 4. Checked every 17 minutes through 2026 and 2027: 0:00:00 on the same MT date."""
    out = _node(MT_JS + """
const u = (...a) => Date.UTC(...a), iso = (ms) => new Date(MT.midnight(ms)).toISOString();
let bad = 0;
for (let t = u(2026, 0, 1); t < u(2028, 0, 1); t += 17 * 60000) {
  const m = MT.midnight(t), p = MT.parts(m);
  if (p.h || p.mi || p.s || MT.key(m) !== MT.key(t) || m > t) bad++;
}
console.log(JSON.stringify({ bad, fall: [iso(u(2026, 10, 1, 7, 59)), iso(u(2026, 10, 1, 8, 0)), iso(u(2026, 10, 1, 12))],
  spring: [iso(u(2027, 2, 14, 8, 59)), iso(u(2027, 2, 14, 9, 0)), iso(u(2027, 2, 14, 10))],
  plain: [iso(u(2026, 9, 16, 3, 3)), iso(u(2026, 11, 31, 23, 30))] }));""", tz)
    assert out == {"bad": 0, "fall": ["2026-11-01T06:00:00.000Z"] * 3, "spring": ["2027-03-14T07:00:00.000Z"] * 3,
                   "plain": ["2026-10-15T06:00:00.000Z", "2026-12-31T07:00:00.000Z"]}


@node
def test_the_short_clock_drops_only_whole_hours_minutes():
    out = _node(MT_JS + """console.log(JSON.stringify([MT.clock(Date.UTC(2026, 9, 16, 0), { short: true }),
  MT.clock(Date.UTC(2026, 9, 16, 0, 30), { short: true }), MT.clock(Date.UTC(2026, 10, 2, 0), { short: true }),
  MT.clock(Date.UTC(2026, 9, 16, 0))]));""")
    assert out == ["6 pm", "6:30 pm", "5 pm", "6:00 pm"]


# ---------------------------------------------------------------- BTC's forming candle with nothing newer on it

def _form() -> str:
    return (MT_JS + "const cb = { barMs: 7200000, bars: new Map() };\n"
            + _between(APP, "  const NF = new Map()", "\n") + "\n" + _between(APP, "  const pxAxis =", "\n") + "\n"
            + _function(APP, "cbSeen") + _function(APP, "formingBars") + _function(APP, "provNote")
            + _function(APP, "closeTag") + """
const H = 3600000, T0 = Date.parse("2026-10-16T02:00:00Z"), run = T0 + 63 * 60000;   // the run at 9:03 pm MT
const boardAt = () => ({ analysis: { outlook: [{ level: 84295 }], outlook_side: "long" }, chart: { bar_ms: 2 * H,
  candles: [[T0 - 4 * H, 81000, 82000, 80500, 81500], [T0 - 2 * H, 81500, 82100, 81400, 81900]], ema_fast: [1, 1],
  ema_slow: [1, 1], don_hi: [2, 2], don_lo: [0, 0], forming: [T0, 81900, 82050, 81850, 81995], forming_as_of: run } });
const draw = (now, live) => { const s = boardAt(); formingBars(s, now); if (live) s.chart.live = live; const c = s.chart;
  return { prov: c.prov, run: c.provRun, note: provNote(c), tag: closeTag(c) }; };
const out = {};
out.proxy = draw(run + 13 * 60000);                    // the socket down (Hyperliquid's price the proxy): no feed
out.closed_since = draw(T0 + 2 * H + 60000);           // its window closed since, nothing newer
out.live_not_yet = draw(run + 60000, 82174);           // Coinbase's price live, none in this window since the run yet
cbSeen(82174, run + 14 * 60000);
out.carried = draw(run + 14 * 60000, 82174);           // carried on to the live price: its close is the live one
const f = boardAt(); f.chart.forming = null; formingBars(f, run + 14 * 60000);   // the feed's window, no live price now
out.feed_stale = closeTag(f.chart);
cb.bars.clear();
const d = boardAt(); d.chart.forming = null; formingBars(d, run); out.desk = closeTag(d.chart);   // a closed candle newest
console.log(JSON.stringify(out));
""")


@node
@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/Denver"])
def test_the_runs_forming_candle_is_provisional_whenever_nothing_newer_carries_it(tz):
    """With the socket down (the proxy), or Coinbase closed since the run, the run's candle was drawn solid while its
    window was still open, its close tag the run's price under a title with the proxy's: final and current, it read.
    It is provisional now whenever it's drawn as the run answered it, the footer says whose it is and when, and its
    close tag is outlined in dashes and says RUN; filled only when its close is the live price."""
    o = _node(_form(), tz)
    note = (" · newest candle: provisional, the desk's as of 9:03 pm MT; the desk's own candle replaces it at the "
            "next run")
    assert o["proxy"] == {"prov": [2], "run": True, "note": note, "tag": {"text": "RUN 81,995", "held": True}}
    assert o["closed_since"]["prov"] == [2] and o["closed_since"]["tag"] == {"text": "RUN 81,995", "held": True}
    assert o["closed_since"]["note"] == note.replace("9:03 pm MT;", "9:03 pm MT, before its window closed;")
    assert o["live_not_yet"]["tag"] == {"text": "RUN 81,995", "held": True}       # the live price has its own tag
    assert o["carried"]["prov"] == [2] and not o["carried"]["run"]
    assert o["carried"]["tag"] == {"text": "82,174", "held": False}               # the live price: filled, as before
    assert o["feed_stale"] == {"text": "82,174", "held": True}    # the feed's last price, the feed not live: dashed
    assert o["desk"] == {"text": "81,900", "held": False}                         # a closed candle's close, as before


def test_the_close_tag_is_drawn_dashed_when_held():
    draw = _function(APP, "drawChart")
    assert "if (!merged) tag(g, w - padR, ty[1], ct.text, css(lastC >= last[1] ? \"--green\" : \"--red\"), !ct.held, ct.held);" in draw
    assert "if (dashed) g.setLineDash([2, 2]);" in _function(APP, "tag")


# ---------------------------------------------------------------- a phone's width

def test_a_320_px_phone_fits_the_header_whatever_the_runs_hour():
    """"12:30 am MT" made the header's row 303 px at least, wider than a 320 px phone's frame: every panel widened
    with it and the page scrolled sideways. Under 360 px the row is tighter, and the frame's one column is never
    wider than the frame."""
    assert "grid-template-columns: minmax(0, 1fr);   /* one column, never wider than the frame" in STYLE
    narrow = STYLE.split("@media (max-width: 360px) {", 1)[1].split("}\n}", 1)[0]
    assert ".clock .time b { font-size: 14px;" in narrow and ".clock { gap: 4px; }" in narrow
    assert ".mini-clock b { color: var(--text); font-size: 18px; white-space: nowrap; }" in STYLE   # one line on a desktop


def test_the_one_line_readouts_keep_their_ends():
    """The setup line's price, VT's rows, the tape's board lines and the status line, at 375 and 390 px."""
    assert "setup WAIT ·${when} close" in ACT and 'when = cl ? ` ${hr(cl[0] + B.chart.bar_ms)}` : ""' in ACT
    assert 'const hr = (ms) => MT.clock(ms, { short: true });' in ACT
    assert '"BELOW"}</span> at ${B.basket.last_close_ts ? `${hr(B.basket.last_close_ts)} ${TZ}` : "the last close"}`' in ACT
    assert "last run ${hhmm(B.updated)}`" in ACT and "board built ${hhmm(B.updated)}${kb}`" in ACT
    assert ': tight.matches ? `LAST RUN ${age} AGO`' in ACT and 'matchMedia("(max-width: 380px)")' in ACT
    assert '<span class="act-tz"> ${TZ}</span>' in _function(ACT, "replayLabel")       # 320 px: the zone goes, not the time
    assert "@media (max-width: 340px) { .act-sh .act-tz { display: none; } }" in (DASH / "activity.css").read_text()
