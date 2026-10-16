"""Every time the dashboard and the phone show is Mountain Time: America/Denver's own rules (MDT until 2026-11-01
02:00 local, then MST), never a fixed offset, labelled MT, whatever zone the phone itself is in. Stored timestamps stay
epoch ms, and the ledger keeps the desk's own words: only the board's copy of them, and what the page writes, change.

The page's half runs app.js's own helper (deskMT) in node, under a phone set to Tokyo and one set to New York."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import board, mountain, paper, voltrend
from desk import venue as venue_mod
from desk.thresholds import COINBASE, VOLTREND

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.js").read_text()
ACT = (ROOT / "dashboard/activity.js").read_text()
HTML = (ROOT / "dashboard/index.html").read_text()
DAY = 86400_000
HOUR = 3600_000


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- Python: boards and pushes

def test_mountain_time_follows_the_zone_across_the_switch():
    """MDT (UTC-6) until 2026-11-01 02:00 local, MST (UTC-7) after: the zone's rules, not an offset."""
    assert mountain.TZ is not None, "the tz database (zoneinfo America/Denver) isn't on this machine"
    assert mountain.when(_ms("2026-11-01T07:59")) == "Nov 1, 1:59 am MT"      # the last minute of MDT
    assert mountain.when(_ms("2026-11-01T08:00")) == "Nov 1, 1:00 am MT"      # the clocks go back
    assert mountain.when(_ms("2026-10-31T00:00")) == "Oct 30, 6:00 pm MT"     # 00:00 UTC in summer
    assert mountain.when(_ms("2026-11-02T00:00")) == "Nov 1, 5:00 pm MT"      # and in winter
    assert mountain.day(_ms("2026-11-01T06:30")) == "Nov 1" and mountain.day(_ms("2026-11-02T06:30")) == "Nov 1"


def test_the_desks_day_turns_at_6_pm_mt_in_summer_and_5_pm_in_winter():
    """The daily loss limit's day and VT's daily close are UTC's: shown as when they happen in Denver."""
    assert board.day_turns(_ms("2026-10-31T10:00")) == "6:00 pm MT"
    assert board.day_turns(_ms("2026-11-01T10:00")) == "5:00 pm MT"
    said = "down 10.4% today. flat, no entries until tomorrow UTC."          # desk/engine.py's words
    assert board.in_mountain_time(said, _ms("2026-11-01T10:00")) == \
        "down 10.4% today. flat, no entries until the desk's day turns at 5:00 pm MT."
    vt = "first decision at the next 00:00 UTC close."                       # desk/paper.py, VT's day one
    assert board.in_mountain_time(vt, _ms("2026-10-31T13:00")) == "first decision at the next 6:00 pm MT close."
    assert board.in_mountain_time(vt, _ms("2026-11-01T11:00")) == "first decision at the next 5:00 pm MT close."


def test_coinbases_weekly_break_is_3_to_4_pm_mt_all_year():
    """Friday 5-6 pm New York is 3-4 pm in Denver on both sides of the switch (the two zones change on the same day),
    though it's 21:00 UTC in October and 22:00 UTC in November: the gate's note and the board's closure say so."""
    s, r = venue_mod.Sessions(COINBASE), venue_mod.Rules(COINBASE)
    for fri in ("2026-10-30T21:30", "2026-11-06T22:30"):
        a, b, why = s.closure(_ms(fri))
        assert (mountain.clock(a), mountain.clock(b), why) == ("3:00 pm MT", "4:00 pm MT", "weekly break")
        note = r.session_gate(_ms(fri)).note                                   # the desk's words: UTC
        assert note.endswith("UTC)")
        shown = board.in_mountain_time(note, _ms(fri))
        assert shown == "Coinbase closed at the fill (weekly break, reopens 4:00 pm MT)" and len(shown) < 70


def test_a_dated_utc_time_is_rewritten_with_its_date():
    said = ("Coinbase never published the 2026-10-31 22:00 UTC candle (missing from every answer since "
            "2026-11-01 08:04 UTC); going on without it")
    assert board.in_mountain_time(said, 0) == ("Coinbase never published the Oct 31, 4:00 pm MT candle (missing from "
                                               "every answer since Nov 1, 1:04 am MT); going on without it")
    assert board.in_mountain_time("no times here", 0) == "no times here" and board.in_mountain_time(None, 0) is None
    assert paper._when(_ms("2026-11-02T03:00")) == "2026-11-02 03:00 UTC"     # what the ledger keeps
    assert board.in_mountain_time(paper._when(_ms("2026-11-02T03:00")), 0) == "Nov 1, 8:00 pm MT"


def test_the_board_and_the_push_say_mt_while_the_ledger_keeps_the_desks_words(tmp_path, monkeypatch):
    """VT's day one: the ledger's event says "00:00 UTC" as the desk said it; the board's events, its new_events and
    the phone's push say when that is in Denver, and the header's time and date are Denver's."""
    import urllib.request

    from desk import __main__ as cli
    from tests.test_desk import FakeDailyFeed, daily, hourly_funding

    series = {c: daily(seed=k + 21) for k, c in enumerate(VOLTREND.coins)}
    funding = {c: hourly_funding(b, k) for k, (c, b) in enumerate(series.items())}
    clock = {"now": series["BTC"][200].ts + 13 * HOUR + 5 * 60_000}
    feeds = {c: FakeDailyFeed(series[c], funding[c], clock) for c in VOLTREND.coins}
    st = paper.step_basket(VOLTREND, tmp_path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"])
    led = json.loads((tmp_path / "paper.json").read_text())
    opened = [e["text"] for e in led["broker"]["events"] if "first decision" in e["text"]]
    assert opened and "at the next 00:00 UTC close" in opened[0]
    turns = board.day_turns(clock["now"])
    for evs in (st["events"], st["paper"]["new_events"]):
        text = next(e["text"] for e in evs if "first decision" in e["text"])
        assert f"at the next {turns} close" in text and "UTC" not in text
    assert st["header"]["time"] == mountain.clock(clock["now"], label=False)
    assert st["header"]["date"] == mountain.day(clock["now"]).upper()
    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or __import__("io").BytesIO(b"ok"))
    cli._notify({**st, "paper": {"new_events": [{"ts": clock["now"], "agent": "chief", "text": opened[0]}]}})
    assert f"at the next {turns} close".encode() in sent[0].data and b"UTC" not in sent[0].data


def test_a_failed_steps_reason_on_the_page_is_in_mt(tmp_path, monkeypatch):
    """index.json's error row is shown on the page: a refused ledger's opening time reads in Denver's time."""
    import argparse

    from desk import __main__ as cli

    def refuse(p, ledger_dir, jev=None, **kw):
        raise paper.LedgerRefused("ledger/BTC/paper.json is a Hyperliquid paper account (opened 2026-11-01 08:30 UTC)")

    monkeypatch.setattr(paper, "step", refuse)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="BTC", ledger=str(tmp_path), interval=None, risk=None, jev_mode="off", coin=None,
                              flows=str(tmp_path / "none.json"))
    assert cli.cmd_step(args) == 1
    row = json.loads((tmp_path / "index.json").read_text())["coins"][0]
    assert "(opened Nov 1, 1:30 am MT)" in row["error"] and "UTC" not in row["error"]


# ---------------------------------------------------------------- the page

def _between(src: str, start: str, end: str) -> str:
    k = src.index(start)
    return src[k:src.index(end, k)]


def _page_mt(js: str, tz: str) -> dict:
    """app.js's deskMT, run in node on a machine set to `tz` (the phone's own zone)."""
    helper = "const DAY_MS = 86400000;\n" + _between(APP, "  const deskMT = (() => {", "  window.deskMT = deskMT;")
    out = subprocess.run(["node", "-e", helper + "\nconst MT = deskMT;\n" + js], capture_output=True, text=True,
                         check=True, env={**os.environ, "TZ": tz}).stdout
    return json.loads(out)


_PAGE = """
const ms = (s) => Date.parse(s + "Z");
console.log(JSON.stringify({
  zone: MT.zone, phone: new Date(ms("2026-11-01T08:00")).getHours(),
  last_mdt: MT.when(ms("2026-11-01T07:59")), first_mst: MT.when(ms("2026-11-01T08:00")),
  sec: MT.clock(ms("2026-11-01T08:00:09"), { sec: true }), up: MT.clock(ms("2026-10-17T00:00"), { up: true }),
  close_summer: MT.clock(MT.nextUtcDay(ms("2026-10-31T10:00")), { up: true }),
  close_winter: MT.clock(MT.nextUtcDay(ms("2026-11-01T10:00")), { up: true }),
  brk_oct: MT.span(ms("2026-10-30T21:00"), ms("2026-10-30T22:00"), true),
  brk_nov: MT.span(ms("2026-11-06T22:00"), ms("2026-11-06T23:00"), true),
  overnight: MT.span(ms("2026-10-24T03:00"), ms("2026-10-24T19:00"), true),
  across: MT.span(ms("2026-10-24T05:00"), ms("2026-10-24T09:00")),
  days: ["2026-10-31T23:00", "2026-11-01T05:59", "2026-11-01T06:00", "2026-11-02T06:59", "2026-11-02T07:00"].map((s) => MT.md(ms(s))),
  midnight_mdt: new Date(MT.midnight(ms("2026-10-31T09:00"))).toISOString(),
  midnight_mst: new Date(MT.midnight(ms("2026-11-02T09:00"))).toISOString(),
  wd: MT.wd(ms("2026-10-31T03:00")), ymd: MT.ymd(ms("2026-11-01T03:00")),
}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/New_York", "America/Denver", "UTC"])
def test_the_page_shows_mountain_time_whatever_the_phones_zone(tz):
    """The same words on a phone in Tokyo, New York, Denver or UTC: Denver's clock, its day boundaries (midnight is
    06:00 UTC in summer, 07:00 in winter), the 2026-11-01 switch, the weekly break at 3:00-4:00 pm MT."""
    out = _page_mt(_PAGE, tz)
    phone = out.pop("phone")
    assert phone == {"Asia/Tokyo": 17, "America/New_York": 3, "America/Denver": 1, "UTC": 8}[tz]   # the phone's own
    assert out == {
        "zone": "MT", "last_mdt": "Nov 1, 1:59 am", "first_mst": "Nov 1, 1:00 am", "sec": "1:00:09 am", "up": "6:00 PM",
        "close_summer": "6:00 PM", "close_winter": "5:00 PM",
        "brk_oct": "Fri 3:00–4:00 pm", "brk_nov": "Fri 3:00–4:00 pm",
        "overnight": "Fri 9:00 pm–Sat 1:00 pm", "across": "Fri 11:00 pm–Sat 3:00 am",
        "days": ["10-31", "10-31", "11-01", "11-01", "11-02"],
        "midnight_mdt": "2026-10-31T06:00:00.000Z", "midnight_mst": "2026-11-02T07:00:00.000Z",
        "wd": "Fri", "ymd": "2026-10-31"}


def test_the_page_labels_its_times_mt_and_reads_no_utc_clock():
    """No time on the page is cut from an ISO (UTC) string or labelled UTC: app.js and activity.js format every time
    with deskMT; activity.js's only ISO dates are the re-audit's calendar dates. The header's zone label is MT."""
    code = lambda src: re.sub(r"(?m)(^|\s)//\s.*$", "", src)        # the comments, which may say UTC
    assert "toISOString" not in code(APP).replace("new Date(MT.midnight", "")
    assert re.findall(r"toISOString\(\)[^;]*", code(ACT)) == ["toISOString().slice(0, 10)"]
    assert 'ymd = (ms) => new Date(ms).toISOString().slice(0, 10)' in ACT and ACT.count("ymd(") == 1   # the re-audit date
    for src in (APP, ACT):
        shown = re.findall(r"`[^`\n]*\bUTC\b[^`\n]*`|\"[^\"\n]*\bUTC\b[^\"\n]*\"", code(src))
        assert shown == ['"UTC"'], shown            # only the zone a browser without Denver's falls back to
    assert '<b id="time">--:--</b> <span id="tz">MT</span>' in HTML and "UTC" not in HTML
    assert 'timeZone: "America/Denver"' in APP and "window.deskMT = deskMT;" in APP
    assert "const MT = window.deskMT" in ACT
    # the countdown, as the owner asked for it: "NEXT 2H CLOSE 6:00 PM MT IN 0:42:10"
    assert "${HHMM(c.next)} ${TZ} IN ${clock(c.next - now)}" in ACT
