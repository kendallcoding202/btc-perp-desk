"""The SHADOW page in plain words, and two phone layout slips on the main board. Each fails on e94d763:

- the SHADOW page showed Coinbase's codes as they came: "margin window FCM_MARGIN_WINDOW_TYPE_OVERNIGHT", "intraday
  margin INTRADAY_MARGIN_SETTING_STANDARD", a stop "SELL 1 ... (STOP_PENDING)", an order "stop_limit", a sweep
  "PENDING", fee rates "0.0005". Now each reads in words (dashboard/shadow.js: plain, coded); a code Coinbase adds later
  reads in lower case without its type's prefix; the code itself stays in a title; and all of it stays escaped (the
  board is decrypted data). SHADOW's refusal over a key on another portfolio named it "the CONSUMER portfolio"; the
  checklist said the fee tier as "taker 0.0005" and a failed read as the desk's error name ("Coinbase answered
  CoinbaseUnavailable"); a stop queued for the reopen said "STOP_PENDING"; the USDC and balance warnings were grey;
- an agent card's detail line was cut off with an ellipsis on a phone ("funding +12.5% apr (est.) · oi $0.14B",
  "fee est. max($0.20/contract, 5bps) + slip 3bps"): it wraps there now, a number with the word before it ("oi
  $0.14B" never split), and the desktop's one line is as it was;
- BTC's title wraps on a phone, and the ring after it fell onto a line of its own: it rides the title's last line now
  (and, on a phone held sideways, 561 to 600 px, the title a size down keeps it beside the ring); VT's smaller title
  sits level with the ring as the others do.

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
# What the fields the page shows can hold, from the same spec (sha256 e1edecbc..., as the fixture above): components.
# schemas coinbase.retail.rest.proxy.fcm.* (GET cfm/intraday/current_margin_window and margin_setting, GET cfm/sweeps),
# then what the desk itself writes: sides (coinbase_orders.parse_position and parse_order lower Coinbase's; the
# simulated book's are upper), a real order's type (parse_order), a simulated stop's status and trigger
# (coinbase_shadow.summary: open or queued orders only), and the portfolio an older board's check named
SHOWN = {
    "FcmMarginWindowType": ["FCM_MARGIN_WINDOW_TYPE_UNSPECIFIED", "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                            "FCM_MARGIN_WINDOW_TYPE_WEEKEND", "FCM_MARGIN_WINDOW_TYPE_INTRADAY",
                            "FCM_MARGIN_WINDOW_TYPE_TRANSITION"],
    "MarginWindowType": ["MARGIN_WINDOW_TYPE_UNSPECIFIED", "MARGIN_WINDOW_TYPE_OVERNIGHT", "MARGIN_WINDOW_TYPE_WEEKEND",
                         "MARGIN_WINDOW_TYPE_INTRADAY", "MARGIN_WINDOW_TYPE_TRANSITION"],
    "IntradayMarginSetting": ["INTRADAY_MARGIN_SETTING_UNSPECIFIED", "INTRADAY_MARGIN_SETTING_STANDARD",
                              "INTRADAY_MARGIN_SETTING_INTRADAY"],
    "FCMSweepStatus": ["UNKNOWN_FCM_SWEEP_STATUS", "PENDING", "PROCESSING"],
    "sides": ["LONG", "SHORT", "BUY", "SELL"],
    "order types": ["stop_limit"],
    "simulated stops": ["OPEN", "QUEUED", "STOP_PENDING", "STOP_TRIGGERED"],
    "PortfolioType": SPEC["enums"]["PortfolioType"],
}


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
    assert (words["LONG"], words["SHORT"]) == ("long", "short")
    assert (words["BUY"], words["SELL"]) == ("buy", "sell")
    assert (words["PENDING"], words["PROCESSING"], words["UNKNOWN_FCM_SWEEP_STATUS"]) == ("pending", "processing",
                                                                                         "unknown")
    assert (words["STOP_PENDING"], words["STOP_TRIGGERED"]) == ("waiting to trigger", "triggered")
    assert (words["OPEN"], words["QUEUED"]) == ("open", "queued for the reopen")
    assert words["stop_limit"] == "stop-limit"
    assert words["INTX"] == "International (INTX)" and words["CONSUMER"] == "Consumer"
    assert words["UNDEFINED"] == "unnamed"


@node
def test_every_code_the_spec_lists_reads_in_words_without_its_underscores():
    """Any field the page might show one day: the fallback says every value of every enum the spec has."""
    got = _js("""
const m = require(process.argv[1]), codes = JSON.parse(process.argv[2]);
console.log(JSON.stringify(codes.map((c) => [c, m.plain(c)])));""", json.dumps(sorted({c for v in SPEC["enums"].values()
                                                                                       for c in v})))
    assert len(got) > 100
    for code, said in got:
        assert said and "_" not in said and said == said.strip(), (code, said)


@node
def test_the_page_and_the_desk_name_a_portfolio_the_same_way():
    from desk.coinbase_run import PORTFOLIO_WORDS
    got = _js("""
const m = require(process.argv[1]), codes = JSON.parse(process.argv[2]);
console.log(JSON.stringify(Object.fromEntries(codes.map((c) => [c, m.plain(c)]))));""", json.dumps(sorted(PORTFOLIO_WORDS)))
    assert got == PORTFOLIO_WORDS


@node
def test_a_code_coinbase_adds_later_still_reads_and_words_that_arent_codes_stay():
    got = _js("""
const m = require(process.argv[1]);
console.log(JSON.stringify(["FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK", "MARGIN_WINDOW_TYPE_HOLIDAY", "SOMETHING_NEW",
  "INTRADAY_MARGIN_SETTING_REDUCED", "stop_limit", "trailing_stop", "buy", "Advanced 1", "<$10k", "", null, undefined]
  .map((c) => [m.plain(c), m.coded(c)])));""")
    assert got[0] == ["lunch break", '<data value="FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK" '
                                     'title="FCM_MARGIN_WINDOW_TYPE_LUNCH_BREAK">lunch break</data>']
    assert [g[0] for g in got[1:6]] == ["holiday", "something new", "reduced", "stop-limit", "trailing stop"]
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
                         "fee_tier": {"tier": "<u>tier</u>", "taker": "<x>", "maker": 0.0002},
                         "usdc": "<img src=u onerror=alert(4)>", "balance_block": "</b></div><script>alert(5)</script>"},
             "shadow": {"checks": [{"key": "portfolio", "label": "Key is on the Default portfolio", "ok": False,
                                    "note": "<img src=y onerror=alert(2)>"},
                                   {"key": "fills", "label": "Fills read", "ok": False,
                                    "note": '<img src=w onerror=alert(3)>: CoinbaseRejected'}],
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
    assert "&lt;img src=u onerror=alert(4)&gt;" in html and "&lt;/b&gt;&lt;/div&gt;&lt;script&gt;alert(5)" in html
    assert '&lt;img src=w onerror=alert(3)&gt;: <data value="CoinbaseRejected" title="CoinbaseRejected">' in html
    # the codes among them: in words, the code in the title
    assert '<data value="FCM_MARGIN_WINDOW_TYPE_OVERNIGHT" title="FCM_MARGIN_WINDOW_TYPE_OVERNIGHT">overnight</data>' in html
    assert '<data value="SHORT" title="SHORT">short</data> 1 contract(s)' in html
    assert '<data value="BUY" title="BUY">buy</data> 1 stop $84,000 limit $88,200' in html
    assert len(re.findall(r'title="([^"]*)"', html)) == len(re.findall(r'title="[A-Za-z0-9_]+"', html))


def _board(**account) -> dict:
    """The least board boardHtml draws in full, with these account fields."""
    return {"updated": 1_792_134_908_791, "ticker": {"position": "FLAT"}, "status": {"ok": True}, "agents": [],
            "analysis": {"gates": []}, "events": [], "account": account,
            "shadow": {"checks": [], "book": {}, "would_send": [], "notes": [], "log": []}}


def _seen(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<details[\s\S]*?</details>", " ", html)))


@node
def test_a_failed_reads_note_says_what_stopped_it_in_words():
    """The desk's checks end a failed read's note in its error's name ("Fills read: CoinbaseRejected")."""
    got = _js("""
const m = require(process.argv[1]);
console.log(JSON.stringify(["CoinbaseUnavailable", "candles: DeadlineExceeded", "the USD wallet: CoinbaseDataError",
  "CoinbaseRejected", "candles: FeedError", "CoinbaseAuthError", "SomeNewError", "UNDEFINED",
  "Advanced 1, taker 5bps, maker 2bps", "total USD $500.00 (spot $500.00 + futures $0.00)", ""].map((n) => m.noted(n))));""")
    assert got[0] == '<data value="CoinbaseUnavailable" title="CoinbaseUnavailable">Coinbase didn\'t answer</data>'
    assert got[1] == 'candles: <data value="DeadlineExceeded" title="DeadlineExceeded">Coinbase didn\'t answer</data>'
    assert got[2] == ('the USD wallet: <data value="CoinbaseDataError" title="CoinbaseDataError">an answer the desk '
                      "can't read</data>")
    assert [re.sub(r"<[^>]+>", "", g) for g in got[3:6]] == ["Coinbase refused the read",
                                                             "candles: an answer the desk can't read",
                                                             "Coinbase refused the key"]
    assert got[6] == "SomeNewError"                      # a name the page doesn't know stays as it is, never blank
    assert got[7] == '<data value="UNDEFINED" title="UNDEFINED">unnamed</data>'
    assert got[8:10] == ["Advanced 1, taker 5bps, maker 2bps", "total USD $500.00 (spot $500.00 + futures $0.00)"]
    assert got[10] == "—"


@node
def test_a_queued_stop_an_order_of_another_type_and_the_two_warnings():
    board = _board(usdc=12.5, balance_block="spot + futures + open P&L is $3.20 off available margin + holds",
                   orders=[{"product_id": "BIP-20DEC30-CDE", "side": "buy", "type": "other"},
                           {"product_id": "BIP-20DEC30-CDE", "side": "sell", "type": "stop_limit"}])
    board["shadow"]["book"] = {"orders": [
        {"side": "SELL", "n": 2, "stop": 80000, "limit": 76000, "status": "QUEUED", "trigger": "STOP_PENDING"},
        {"side": "SELL", "n": 1, "stop": 79000, "limit": 75000, "status": "OPEN", "trigger": "STOP_PENDING"},
        {"side": "SELL", "n": 1, "stop": 79000, "limit": 75000, "status": "OPEN", "trigger": "STOP_TRIGGERED"}]}
    html = _js("""
const m = require(process.argv[1]), b = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(m.boardHtml(b, b.updated + 60000)));""", stdin=json.dumps(board))
    seen = _seen(html)
    # a stop placed while the market is shut waits for the reopen (coinbase_shadow: status QUEUED, its trigger pending)
    assert "sell 2 stop $80,000 limit $76,000 ( queued for the reopen )" in seen, seen
    assert "sell 1 stop $79,000 limit $75,000 ( waiting to trigger )" in seen and "( triggered )" in seen
    assert "BIP-20DEC30-CDE buy (an order type the desk doesn't know)" in seen             # before: "buy other"
    assert "BIP-20DEC30-CDE sell stop-limit" in seen
    # the warnings: the row red (.shadow-page .bad), never a span in the value, which #board .stats span greys
    assert '<div class="bad"><span>USDC in the portfolio</span><b>12.50 USDC</b></div>' in html
    assert ('<div class="bad"><span>balance check</span><b>spot + futures + open P&amp;L is $3.20 off available '
            'margin + holds</b></div>') in html
    assert '<span class="bad">' not in html
    assert re.search(r"#board \.stats span \{ color: var\(--muted\)", STYLE)
    assert ".shadow-page .bad { color: var(--red); }" in STYLE
    ok = _js("""
const m = require(process.argv[1]), b = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(m.boardHtml(b, b.updated + 60000)));""", stdin=json.dumps(_board()))
    assert '<div class=""><span>balance check</span><b>adds up</b></div>' in ok and "USDC" not in ok


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
    fees = next(c["note"] for c in board["shadow"]["checks"] if c["key"] == "fees")
    assert fees == "Advanced 1, taker 5bps, maker 2bps"                           # the checklist says it the same way
    assert "0.0005" not in seen and "0.0002" not in seen
    assert not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", seen), re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", seen)
    assert 'title="INTRADAY_MARGIN_SETTING_STANDARD"' in out["html"]


@pytest.mark.parametrize("kind,said,noted", [("CONSUMER", "Consumer", "Consumer"),
                                             ("INTX", "International (INTX)", "International (INTX)"),
                                             ("UNDEFINED", "unnamed", "unnamed"), (None, "unnamed", "not said")])
def test_a_key_on_another_portfolio_is_named_in_words_and_still_refused(fake, tmp_path, kind, said, noted):  # noqa: F811
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
    assert note == noted


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


# every detail line today's boards and the fixtures carry (desk/board.py composes them)
DETAILS = ["stop —", "coinbase · 2h", "risk 2.0% · ≤5x", "breakout.60 · ETH", "atr 1.42% · eff.60",
           "bar 1,500 · 2h · paper", "taker 4.5bps + slip 3.0bps", "funding +7.6% apr · oi $2.69B",
           "risk 5.0% · ≤4x · 0.01 BTC contracts", "funding +12.5% apr (est.) · oi $0.14B",
           "fee est. max($0.20/contract, 5bps) + slip 3bps"]


@node
def test_a_cards_number_keeps_the_word_before_it_when_the_line_wraps():
    m = re.search(r'\.querySelector\("\.detail"\)\.textContent = \(a\.detail \|\| ""\)\.replace\((/.+/g), "\$1\\u00a0"\);', APP)
    assert m, "the detail line's no-break spaces"
    out = subprocess.run([NODE, "-e", """
const re = eval(process.argv[1]), ds = JSON.parse(process.argv[2]);
console.log(JSON.stringify(ds.map((d) => d.replace(re, "$1\\u00a0"))));""", m.group(1), json.dumps(DETAILS)],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    got = dict(zip(DETAILS, json.loads(out)))
    nb = "\u00a0"
    assert got["funding +7.6% apr · oi $2.69B"] == f"funding{nb}+7.6% apr · oi{nb}$2.69B"     # never "oi" / "$2.69B"
    assert got["funding +12.5% apr (est.) · oi $0.14B"] == f"funding{nb}+12.5% apr (est.) · oi{nb}$0.14B"
    assert got["taker 4.5bps + slip 3.0bps"] == f"taker{nb}4.5bps + slip{nb}3.0bps"
    assert got["fee est. max($0.20/contract, 5bps) + slip 3bps"] == f"fee est. max($0.20/contract, 5bps) + slip{nb}3bps"
    assert got["risk 5.0% · ≤4x · 0.01 BTC contracts"] == f"risk{nb}5.0% · ≤4x · 0.01 BTC contracts"   # · still breaks
    assert got["stop —"] == "stop —" and got["coinbase · 2h"] == "coinbase · 2h"
    for d, g in got.items():
        assert g.replace(nb, " ") == d                     # only spaces change; the title keeps the line as it came
        assert max(len(w) for w in g.split(" ")) <= 25, g   # no piece longer than the card a 320 px phone gives it


def test_vts_smaller_title_sits_level_with_the_ring_and_a_sideways_phone_keeps_btcs_ring_beside_it():
    assert _rule(_media(THEME, "(max-width: 420px)"), ".kind-basket .brand h1").strip() == "font-size: 19px;"
    assert "vertical-align: -6px" in _rule(_media(THEME, "(max-width: 420px)"), ".kind-basket .ring")
    assert "vertical-align: -5px" in _rule(_media(STYLE, "(max-width: 560px)"), ".ring")
    side = _media(STYLE, "(min-width: 561px) and (max-width: 600px)")
    assert _rule(side, ".brand h1").strip() == "font-size: 25px;"
    assert "font-size: 28px" in _rule(_top(STYLE), ".brand h1")               # the desktop's title as it was


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
