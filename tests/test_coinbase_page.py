"""The dashboard's SHADOW page (dashboard/shadow.js) opens the board SHADOW encrypts (desk/statebox.py) with the
browser's own WebCrypto, and renders it without "undefined" or "NaN". Run under Node's WebCrypto (the same API);
skipped where Node isn't installed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import statebox

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PW = "test-page-password-" + "p" * 20

SCRIPT = r"""
const fs = require("fs");
const m = require(process.argv[2]);
(async () => {
  const env = JSON.parse(fs.readFileSync(process.argv[3]));
  const out = {};
  const board = await m.decrypt(env, process.argv[4], globalThis.crypto.subtle);
  out.board = board;
  try { await m.decrypt(env, "wrong-" + process.argv[4], globalThis.crypto.subtle); out.wrong = "opened"; }
  catch (e) { out.wrong = "refused"; }
  out.html = m.boardHtml(board, board.updated + 60000);
  out.status = m.statusHtml(JSON.parse(fs.readFileSync(process.argv[5])), board.updated + 60000);
  out.stale = m.statusHtml(JSON.parse(fs.readFileSync(process.argv[5])), board.updated + 3 * 3600000);
  out.noted = m.statusHtml(Object.assign(JSON.parse(fs.readFileSync(process.argv[5])),
    { note: "BTC/kills.json on the coinbase-state branch can't be read: read as entry_kill on, no new entries" }),
    board.updated + 60000);
  out.none = m.statusHtml(null, Date.now());
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_page_opens_the_encrypted_board_with_webcrypto_and_renders_it(tmp_path):
    board = {"updated": 1_791_907_680_000, "header": {"mode": "SHADOW", "tagline": "SHADOW · YOUR REAL COINBASE ACCOUNT"},
             "ticker": {"position": "LONG", "position_detail": "1 contract @ 86,794", "record": "0W 0L", "fees": 0.43,
                        "funding": 0.0},
             "agents": [{"key": "chief", "value": "HOLD LONG"}], "analysis": {"gates": []}, "events": [
                 {"ts": 1_791_907_680_000, "agent": "fills", "text": "WOULD BUY 1 contract (0.01 BTC) at market"}],
             "account": {"spot_usd": 1000.0, "futures_usd": 0.0, "positions": [], "orders": []},
             "status": {"ok": True},
             "shadow": {"real_equity": 1000.0, "simulated_equity": 999.5, "simulated_pnl": -0.5, "checks": [
                 {"key": "key", "label": "Key accepted (signed request)", "ok": True, "note": ""}],
                 "would_send": [{"ts": 1_791_907_680_000, "text": "16:08 UTC · POST /api/v3/brokerage/orders · BUY",
                                 "body": {"side": "BUY"}}], "book": {"position": None, "orders": []}, "notes": ["n"],
                 "log": ["a line"]}}
    env = tmp_path / "board.enc.json"
    env.write_text(statebox.seal_json(board, PW, statebox.board_aad(), compress=False))   # the desk's 600,000 rounds
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"v": 1, "mode": "SHADOW", "coin": "BTC", "updated": board["updated"],
                                  "started_ts": 1, "runs": 3, "failed_runs": 0, "ok": True, "problem": None,
                                  "key_view_only": True, "decision": "HOLD LONG (simulated)",
                                  "would_send_last_24h": 2, "alerts_open": 0, "checks_passed": 10,
                                  "checks_total": 10, "opened": True}))
    js = tmp_path / "t.js"
    js.write_text(SCRIPT)
    res = subprocess.run([NODE, str(js), str(ROOT / "dashboard" / "shadow.js"), str(env), PW, str(status)],
                         capture_output=True, text=True, timeout=120)
    out = json.loads(res.stdout)
    assert out["board"] == board and out["wrong"] == "refused"
    for text in (out["html"], out["status"]):
        assert "undefined" not in text and "NaN" not in text
    assert "REAL ACCOUNT (COINBASE)" in out["html"] and "SHADOW (EST.)" in out["html"] and "NONE SENT" in out["html"]
    assert "View only" in out["status"] and "OK" in out["status"] and "NOT UPDATED" in out["stale"]
    assert "No SHADOW status yet" in out["none"]
    # a run that's OK can still have something to say (an unreadable kills.json): the status shows it
    assert "OK" in out["noted"] and "kills.json on the coinbase-state branch can't be read" in out["noted"]


def test_the_main_board_links_the_shadow_page_only_once_it_has_a_status_and_never_as_a_tab():
    app = (ROOT / "dashboard" / "app.js").read_text()
    html = (ROOT / "dashboard" / "index.html").read_text()
    assert 'id="shadow-link" href="shadow.html" hidden' in html
    assert 'const ACCOUNTS = ["BTC", "ETH", "SOL", "VT"];' in app            # the tabs (and the ALL total) are as before
    assert "boards/BTC-SHADOW.status.json" in app and "el.hidden = !st;" in app
    assert "`updated ${age} min ago` + (st.note ?" in app             # the note shows on an OK run too
    page = (ROOT / "dashboard" / "shadow.js").read_text()
    assert "sessionStorage" in page and 'BOARD_AAD = "btc-perp-desk:board-shadow:BTC"' in page
    assert "fetch(" in page and "method" not in page.split("async function fetchJson")[1].split("}")[0]  # GETs only
