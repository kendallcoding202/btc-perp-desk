"""Round 7: every time SHADOW and LIVE show you is Mountain Time (America/Denver: MDT until 2026-11-01, then MST; the
zone's rules, never a fixed offset), like "10:08 am MT": the SHADOW page (dashboard/shadow.js), SHADOW's and LIVE's
phone pushes and the text they're built from, the run's log, and the setup guide. Logic and every stored timestamp
stay UTC epoch ms, and paper's boards and logs are unchanged (still UTC). Each fails on the code before it (4c0d11c):
the page and the pushes said UTC. Also the SHADOW page's status: "no money in it" only once a run has read the balance
(before: after refused first runs too), and a board that holds only a never-worked run's log shows that log.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import mountain
from fake_coinbase import FakeCoinbase, make_ec_key
from test_coinbase_shadow import Shadow

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _ms(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


# the zone's own rules: MDT (UTC-6) until 2 am on Sunday 2026-11-01, MST (UTC-7) after; the weekly break (Friday
# 5-6 pm New York) is 3-4 pm in Denver all year
INSTANTS = {_ms(2026, 10, 15, 16, 8): "Oct 15, 10:08 am MT",
            _ms(2026, 11, 1, 7, 30): "Nov 1, 1:30 am MT",          # MDT, half an hour before the change
            _ms(2026, 11, 1, 9, 30): "Nov 1, 2:30 am MT",          # MST
            _ms(2026, 10, 16, 21, 0): "Oct 16, 3:00 pm MT",        # the weekly break, in summer (21:00 UTC)
            _ms(2026, 11, 6, 22, 0): "Nov 6, 3:00 pm MT",          # and in winter (22:00 UTC)
            _ms(2027, 1, 1, 6, 59): "Dec 31, 11:59 pm MT",
            _ms(2027, 3, 14, 9, 30): "Mar 14, 3:30 am MT"}         # MDT again (the second Sunday of March)


def test_mountain_time_follows_the_zone_not_an_offset():
    assert mountain.TZ is not None, "the tz database (zoneinfo America/Denver) isn't on this machine"
    for ms, want in INSTANTS.items():
        assert mountain.when(ms) == want, (ms, mountain.when(ms))
    assert mountain.clock(_ms(2026, 10, 15, 16, 8, 3), seconds=True, label=False) == "10:08:03 am"
    assert mountain.clock(_ms(2026, 10, 15, 18, 0)) == "12:00 pm MT" and mountain.clock(_ms(2026, 10, 15, 6)) == \
        "12:00 am MT"


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_shadow_page_writes_the_same_times(tmp_path):
    js = tmp_path / "t.js"
    js.write_text("const m = require(process.argv[2]); console.log(JSON.stringify(JSON.parse(process.argv[3]).map("
                  "(t) => m.when(t))));")
    got = json.loads(subprocess.run([NODE, str(js), str(ROOT / "dashboard" / "shadow.js"), json.dumps(list(INSTANTS))],
                                    capture_output=True, text=True, timeout=60, check=True).stdout)
    assert got == list(INSTANTS.values())
    page = (ROOT / "dashboard" / "shadow.js").read_text()
    code = "\n".join(ln.split("// ")[0] for ln in page.splitlines())          # the code, not its comments
    assert "toISOString" not in code and "UTC" not in code and 'timeZone: "America/Denver"' in code


@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        f.now_s += 8 * 60
        yield f


def _no_utc(texts):
    bad = [t for t in texts if re.search(r"\bUTC\b", t)]
    assert not bad, bad


def test_shadow_pushes_board_and_log_say_mountain_time(fake, tmp_path):
    sh = Shadow(fake, tmp_path, make_ec_key("mt7-view-not-real"))
    code, r = sh.run()
    assert code == 0, r.lines[-3:]
    connected = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: connected (read only)"]
    close = mountain.clock((sh.bars[-1].ts // 1000) * 1000 + 7200_000)
    assert connected and f"to the {close} close" in connected[0], connected
    sh.pushes.clear()
    sh.breakout()
    code, r = sh.run()
    entry = [b for t, b, *_ in sh.pushes if t.startswith("BTC SHADOW: would")]
    assert code == 0 and entry, [p[0] for p in sh.pushes]
    assert re.search(r"\b\d{1,2}:\d\d [ap]m MT · POST /api/v3/brokerage/orders", entry[0]), entry[0]
    _no_utc([b for _, b, *_ in sh.pushes] + [t for _, t, *_ in sh.pushes])
    board = sh.board()
    log = board["shadow"]["log"]
    assert log and re.match(r"\d{1,2}:\d\d:\d\d [ap]m ", log[0]), log[:2]
    _no_utc(log + [c["note"] for c in board["shadow"]["checks"]] + [w["text"] for w in board["shadow"]["would_send"]])
    acc = sh.file("shadow", "account.json")
    assert any(ln.startswith("--- run 2 ") and ln.endswith(" MT") for ln in acc["log"]), acc["log"][-3:]
    assert isinstance(sh.status()["updated"], int)               # stored: epoch ms, as before


def test_live_pushes_say_mountain_time(fake, tmp_path):
    sh = Shadow(fake, tmp_path, make_ec_key("mt7-view2-not-real"))
    from test_coinbase_shadow import _write_proof
    _write_proof(sh, fake)
    tk = fake.add_key(make_ec_key("mt7-trade-not-real", trade=True))
    sh.env["COINBASE_CDP_KEY"] = tk.file_json()
    code, r = sh.run("live")
    armed = [b for t, b, *_ in sh.pushes if t == "BTC LIVE: armed and connected"]
    assert code == 0 and armed, (r.lines[-3:], [p[0] for p in sh.pushes])
    assert re.search(r"to the \d{1,2}:\d\d [ap]m MT close", armed[0]), armed[0]
    _no_utc([b for _, b, *_ in sh.pushes])


def test_the_session_gate_says_mountain_time_on_coinbase_and_utc_on_paper():
    from desk import venue
    from desk.thresholds import COINBASE

    t = _ms(2026, 10, 16, 21, 30)                           # inside the weekly break (Friday, summer)
    assert venue.Rules(COINBASE).session_gate(t).note == "Coinbase closed at the fill (weekly break, reopens 22:00 UTC)"
    assert venue.Rules(COINBASE, clock=mountain.clock).session_gate(t).note == \
        "Coinbase closed at the fill (weekly break, reopens 4:00 pm MT)"


def test_the_guide_says_mountain_time():
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    assert "Fridays 3:00 to 4:00 pm MT" in guide and not re.search(r"\bUTC\b", guide)


# ---------------------------------------------------------------- the SHADOW page's status and a log-only board

SCRIPT = r"""
const m = require(process.argv[2]);
const now = Date.UTC(2026, 9, 15, 16, 10);
const base = { v: 1, mode: "SHADOW", coin: "BTC", updated: now - 60000, started_ts: 1, ok: false, problem: "permissions",
  note: "the key can trade or move money (SHADOW needs a View-only key)", key_view_only: false, checks_passed: 3,
  checks_total: 4, opened: false };
console.log(JSON.stringify({
  refused: m.statusHtml(Object.assign({}, base, { runs: 0, failed_runs: 0 }), now),
  worked: m.statusHtml(Object.assign({}, base, { ok: true, problem: null, note: null, runs: 3, failed_runs: 0 }), now),
  stub: m.boardHtml({ updated: null, header: { mode: "SHADOW" }, status: { ok: false, problem: "error",
    checked: Date.UTC(2026, 9, 15, 16, 8), why: "8:08:01 am refused: an error in the desk's own code (KeyError)" },
    shadow: { checks: [{ key: "key", label: "Key accepted (signed request)", ok: true, note: "" }],
      alerts: [], log: ["10:08:01 am error while reading the balance: KeyError: 'a field'"] } }, now),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_page_says_no_money_only_once_a_run_read_the_balance_and_shows_a_log_only_board(tmp_path):
    js = tmp_path / "t.js"
    js.write_text(SCRIPT)
    out = json.loads(subprocess.run([NODE, str(js), str(ROOT / "dashboard" / "shadow.js")], capture_output=True,
                                    text=True, timeout=60, check=True).stdout)
    assert "no money in it" not in out["refused"] and "the balance hasn't been read yet" in out["refused"], out
    assert "no money in it" in out["worked"]
    stub = out["stub"]
    assert "No SHADOW run has worked yet" in stub and "Oct 15, 10:08 am MT" in stub and "KeyError: 'a field'" in stub
    assert "REAL ACCOUNT" not in stub and "no money in the account" not in stub and "undefined" not in stub
    assert cr.UNWORKED_PUBLIC["key"] == "a secret isn't set right"
