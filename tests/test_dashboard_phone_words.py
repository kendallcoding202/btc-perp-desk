"""The SHADOW page in plain words, and two phone layout slips on the main board. Each fails on e94d763:

- the SHADOW page showed Coinbase's codes as they came: "margin window FCM_MARGIN_WINDOW_TYPE_OVERNIGHT", "intraday
  margin INTRADAY_MARGIN_SETTING_STANDARD", a stop "SELL 1 ... (STOP_PENDING)", an order "stop_limit", a sweep
  "PENDING", fee rates "0.0005". Now each reads in words (dashboard/shadow.js: plain, coded); a code Coinbase adds later
  reads in lower case without its type's prefix; the code itself stays in a title; and all of it stays escaped (the
  board is decrypted data). SHADOW's refusal over a key on another portfolio named it "the CONSUMER portfolio";
- an agent card's detail line was cut off with an ellipsis on a phone ("funding +12.5% apr (est.) · oi $0.14B",
  "fee est. max($0.20/contract, 5bps) + slip 3bps"): it wraps there now, and the desktop's one line is as it was;
- BTC's title wraps on a phone, and the ring after it fell onto a line of its own: it rides the title's last line now.

The page's own functions run in node; the SHADOW run against the local fake Coinbase (tests/fake_coinbase.py)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fake_coinbase import make_ec_key
from test_coinbase_shadow import Shadow, fake  # noqa: F401  (the fake Coinbase fixture)

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
SHADOW_JS = DASH / "shadow.js"
STYLE = (DASH / "style.css").read_text()
THEME = (DASH / "theme-comic.css").read_text()
ACTIVITY = (DASH / "activity.css").read_text()
APP = (DASH / "app.js").read_text()
NODE = shutil.which("node")
node = pytest.mark.skipif(NODE is None, reason="node isn't installed")

SPEC = json.loads((ROOT / "tests" / "fixtures" / "coinbase_spec" / "enums.json").read_text())
# What the board's account and book fields can hold, from the same spec (sha256 e1edecbc..., as the fixture above):
# components.schemas coinbase.retail.rest.proxy.fcm.* (the balance summary's measures, GET cfm/intraday/
# current_margin_window and margin_setting, GET cfm/sweeps, GET cfm/positions) and the order's side
FCM = {
    "FcmMarginWindowType": ["FCM_MARGIN_WINDOW_TYPE_UNSPECIFIED", "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                            "FCM_MARGIN_WINDOW_TYPE_WEEKEND", "FCM_MARGIN_WINDOW_TYPE_INTRADAY",
                            "FCM_MARGIN_WINDOW_TYPE_TRANSITION"],
    "MarginWindowType": ["MARGIN_WINDOW_TYPE_UNSPECIFIED", "MARGIN_WINDOW_TYPE_OVERNIGHT", "MARGIN_WINDOW_TYPE_WEEKEND",
                         "MARGIN_WINDOW_TYPE_INTRADAY", "MARGIN_WINDOW_TYPE_TRANSITION"],
    "IntradayMarginSetting": ["INTRADAY_MARGIN_SETTING_UNSPECIFIED", "INTRADAY_MARGIN_SETTING_STANDARD",
                              "INTRADAY_MARGIN_SETTING_INTRADAY"],
    "MarginLevelType": ["MARGIN_LEVEL_TYPE_UNSPECIFIED", "MARGIN_LEVEL_TYPE_BASE", "MARGIN_LEVEL_TYPE_WARNING",
                        "MARGIN_LEVEL_TYPE_DANGER", "MARGIN_LEVEL_TYPE_LIQUIDATION"],
    "FCMSweepStatus": ["UNKNOWN_FCM_SWEEP_STATUS", "PENDING", "PROCESSING"],
    "FCMPositionSide": ["UNKNOWN", "LONG", "SHORT"],
    "FuturesPositionSide": ["FUTURES_POSITION_SIDE_UNSPECIFIED", "FUTURES_POSITION_SIDE_LONG",
                            "FUTURES_POSITION_SIDE_SHORT"],
    "OrderSide": ["BUY", "SELL"],
}
SHOWN = {**FCM, **{k: SPEC["enums"][k] for k in ("OrderExecutionStatus", "StopTriggerStatus", "PortfolioType")}}


def _js(script: str, *args, stdin: str | None = None):
    out = subprocess.run([NODE, "-e", script, str(SHADOW_JS), *args], input=stdin, capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return json.loads(out)


def _media(css: str, query: str) -> str:
    """Every `@media <query> { ... }` block's body in css, one after the other."""
    out, k = [], css.find(f"@media {query} {{")
    while k >= 0:
        depth, j0 = 0, css.index("{", k)
        for j in range(j0, len(css)):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            if depth == 0:
                out.append(css[j0 + 1:j])
                break
        k = css.find(f"@media {query} {{", k + 1)
    assert out, query
    return "\n".join(out)


def _top(css: str) -> str:
    """css without its @media blocks: the desktop rules."""
    out, k = css, 0
    while "@media" in out[k:]:
        k = out.index("@media", k)
        depth, j0 = 0, out.index("{", k)
        for j in range(j0, len(out)):
            depth += {"{": 1, "}": -1}.get(out[j], 0)
            if depth == 0:
                out = out[:k] + out[j + 1:]
                break
    return out


def _rule(css: str, selector: str) -> str:
    m = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


# ---------------------------------------------------------------- the SHADOW page: codes in words

@node
def test_every_code_the_shadow_page_can_show_reads_in_words():
    got = _js("""
const m = require(process.argv[1]), codes = JSON.parse(process.argv[2]);
const out = {}; for (const [k, v] of Object.entries(codes)) out[k] = v.map((c) => [c, m.plain(c)]);
console.log(JSON.stringify(out));""", json.dumps(SHOWN))
    for kind, pairs in got.items():
        for code, said in pairs:
            assert said and "_" not in said and said != code, (kind, code, said)     # never blank, never the code
    words = {code: said for pairs in got.values() for code, said in pairs}
    assert {words[c] for c in ("FCM_MARGIN_WINDOW_TYPE_OVERNIGHT", "MARGIN_WINDOW_TYPE_OVERNIGHT")} == {"overnight"}
    assert words["FCM_MARGIN_WINDOW_TYPE_INTRADAY"] == words["MARGIN_WINDOW_TYPE_INTRADAY"] == "intraday"
    assert words["FCM_MARGIN_WINDOW_TYPE_WEEKEND"] == "weekend" and words["MARGIN_WINDOW_TYPE_TRANSITION"] == "transition"
    assert words["INTRADAY_MARGIN_SETTING_STANDARD"] == "standard"
    assert words["INTRADAY_MARGIN_SETTING_INTRADAY"] == "intraday"
    assert words["FCM_MARGIN_WINDOW_TYPE_UNSPECIFIED"] == words["INTRADAY_MARGIN_SETTING_UNSPECIFIED"] == "not said"
    assert (words["LONG"], words["SHORT"], words["FUTURES_POSITION_SIDE_SHORT"]) == ("long", "short", "short")
    assert (words["BUY"], words["SELL"]) == ("buy", "sell")
    assert (words["PENDING"], words["PROCESSING"], words["UNKNOWN_FCM_SWEEP_STATUS"]) == ("pending", "processing",
                                                                                         "unknown")
    assert (words["STOP_PENDING"], words["STOP_TRIGGERED"]) == ("waiting to trigger", "triggered")
    assert (words["OPEN"], words["QUEUED"], words["CANCELLED"]) == ("open", "queued for the reopen", "canceled")
    assert words["INTX"] == "International (INTX)" and words["CONSUMER"] == "Consumer"


@node
def test_a_code_coinbase_adds_later_still_reads_and_words_that_arent_codes_stay():
    got = _js("""
const m = require(process.argv[1]);
console.log(JSON.stringify(["FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK", "MARGIN_WINDOW_TYPE_HOLIDAY", "SOMETHING_NEW",
  "FCM_SWEEP_STATUS_ON_HOLD", "stop_limit", "trailing_stop", "buy", "Advanced 1", "<$10k", "", null, undefined]
  .map((c) => [m.plain(c), m.coded(c)])));""")
    assert got[0] == ["lunch break", '<data value="FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK" '
                                     'title="FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK">lunch break</data>']
    assert [g[0] for g in got[1:6]] == ["holiday", "something new", "on hold", "stop-limit", "trailing stop"]
    assert got[6] == ["buy", "buy"] and got[7] == ["Advanced 1", "Advanced 1"]      # no title when nothing changed
    assert got[8] == ["<$10k", "&lt;$10k"]
    assert [g[1] for g in got[9:]] == ["—", "—", "—"]                                 # missing: the dash, never blank


@node
def test_a_hostile_value_stays_escaped_and_never_reaches_a_title():
    board = {"updated": 1_792_134_908_791, "ticker": {"position": "LONG", "position_detail": "1 contract @ 82,015"},
             "status": {"ok": True}, "agents": [], "analysis": {"gates": []}, "events": [],
             "account": {"margin_window": "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                         "intraday_setting": '"><script>alert(1)</script>',
                         "positions": [{"product_id": "<b>X</b>", "side": 'LONG" onmouseover="alert(1)', "contracts": 1}],
                         "orders": [{"product_id": "BIP-20DEC30-CDE", "side": "SELL", "type": "<img src=x onerror=alert(1)>"}],
                         "sweeps": [{"status": "<svg onload=alert(1)>", "amount": "<i>9</i>"}],
                         "fee_tier": {"tier": "<u>tier</u>", "taker": "<x>", "maker": 0.0002}},
             "shadow": {"checks": [{"key": "portfolio", "label": "Key is on the Default portfolio", "ok": False,
                                    "note": "<img src=y onerror=alert(2)>"}],
                        "book": {"position": {"side": "SHORT", "contracts": 1, "entry": 82015, "upnl": 0},
                                 "orders": [{"side": "BUY", "n": 1, "stop": 84000, "limit": 88200, "status": "OPEN",
                                             "trigger": 'STOP_PENDING"><img src=z>'}]},
                        "would_send": [], "notes": [], "log": []}}
    html = _js("""
const m = require(process.argv[1]), b = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(m.boardHtml(b, b.updated + 60000)));""", stdin=json.dumps(board))
    for bad in ("<script", "<img", "<svg", "<b>X", "<i>9", "<u>", "<x>", '" onmouseover'):
        assert bad not in html, bad
    assert "&lt;img src=x onerror=alert(1)&gt;" in html and "&lt;svg onload=alert(1)&gt;" in html
    assert "LONG&quot; onmouseover=&quot;alert(1)" in html and "&quot;&gt;&lt;script&gt;" in html
    # the codes among them: in words, the code in the title
    assert '<data value="FCM_MARGIN_WINDOW_TYPE_OVERNIGHT" title="FCM_MARGIN_WINDOW_TYPE_OVERNIGHT">overnight</data>' in html
    assert '<data value="SHORT" title="SHORT">short</data> 1 contract(s)' in html
    assert '<data value="BUY" title="BUY">buy</data> 1 stop $84,000 limit $88,200' in html
    assert len(re.findall(r'title="([^"]*)"', html)) == len(re.findall(r'title="[A-Za-z0-9_]+"', html))


@node
def test_the_page_says_a_real_runs_margin_window_and_setting_in_words(fake, tmp_path):  # noqa: F811
    """A SHADOW run against the fake Coinbase, whose answers carry the codes; the board as the page renders it."""
    sh = Shadow(fake, tmp_path)
    code, r = sh.run()
    assert code == 0, r.lines[-5:]
    board = sh.board()
    assert board["account"]["margin_window"] == "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT"
    assert board["account"]["intraday_setting"] == "INTRADAY_MARGIN_SETTING_STANDARD"
    out = _js("""
const m = require(process.argv[1]), b = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const html = m.boardHtml(b, b.updated + 60000);
const seen = html.replace(/<details[\\s\\S]*?<\\/details>/g, " ").replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ");
console.log(JSON.stringify({ html, seen }));""", stdin=json.dumps(board))
    seen = out["seen"]
    assert "margin window overnight" in seen and "intraday margin standard" in seen, seen
    assert "taker 5bps · maker 2bps" in seen                                      # before: taker 0.0005
    assert not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", seen), re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", seen)
    assert 'title="INTRADAY_MARGIN_SETTING_STANDARD"' in out["html"]


@pytest.mark.parametrize("kind,said", [("CONSUMER", "Consumer"), ("INTX", "International (INTX)")])
def test_a_key_on_another_portfolio_is_named_in_words_and_still_refused(fake, tmp_path, kind, said):  # noqa: F811
    fk = make_ec_key()
    fk.portfolio_type = kind
    sh = Shadow(fake, tmp_path, fk)
    code, r = sh.run()
    assert code == 2 and r.problem == "permissions" and sh.status()["note"] == "the key isn't on the Default portfolio"
    assert not any("/cfm/balance_summary" in q["path"] for q in fake.requests)    # refused before any money is read
    bodies = [b for _, b, *_ in sh.pushes]
    assert any(f"this key is on the {said} portfolio, not Default" in b for b in bodies), bodies
    assert not any(f"the {kind} portfolio" in b for b in bodies)                  # before: "the CONSUMER portfolio"
    note = next(c["note"] for c in r.checks.ordered() if c["key"] == "portfolio")
    assert note == said


# ---------------------------------------------------------------- the main board on a phone

def test_a_cards_detail_line_wraps_on_a_phone_and_stays_one_line_on_a_desktop():
    desk = _rule(_top(STYLE), ".card .detail")
    for d in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):      # the desktop's look
        assert d in desk, d
    two = _rule(_media(STYLE, "(max-width: 980px)"), ".card .detail")                 # two cards a row: phones
    for d in ("white-space: normal", "text-overflow: clip", "overflow-wrap: anywhere"):
        assert d in two, d
    # nothing later puts the cut back: the phone block and the comic theme only place and letter it
    for css in (_media(STYLE, "(max-width: 560px)"), THEME, ACTIVITY):
        for body in re.findall(r"\.card \.detail\s*\{([^}]*)\}", css):
            assert "nowrap" not in body and "ellipsis" not in body, body
    assert '.querySelector(".detail").title = a.detail || "";' in APP              # the desktop's whole line, on hover
    # a 320 px phone's card: SIZE's quote runs to five lines, and the theme's four-line cut took its end
    assert _rule(_media(THEME, "(max-width: 420px)"), ".card .quote").strip() == "-webkit-line-clamp: 6;"


def test_btcs_wrapped_title_keeps_the_ring_on_its_last_line_on_a_phone():
    phone = _media(STYLE, "(max-width: 560px)")
    assert "display: block" in _rule(phone, ".brand") and "white-space: nowrap" in _rule(phone, ".brand")
    # the title's words wrap; the break between its last word and the ring is the brand's, and the brand's doesn't
    assert re.search(r"\.brand h1 \{ display: inline; white-space: normal;", phone)
    assert "display: inline-block" in _rule(phone, ".ring")
    desk = _top(STYLE)
    assert "display: flex" in _rule(desk, ".brand") and "flex-wrap: wrap" in _rule(desk, ".brand")   # desktop as it was
    assert "display: inline" not in _rule(desk, ".brand h1")
    for css in (THEME, ACTIVITY):                       # the theme only letters the title: nothing undoes the rule
        for body in re.findall(r"\.brand(?: h1)?\s*\{([^}]*)\}", css) + re.findall(r"(?:^|\n)\s*\.ring\s*\{([^}]*)\}", css):
            assert not re.search(r"(^|[;\s])(display|white-space)\s*:", body), body
