"""Small things on the phone dashboard that the live-price and comic-theme reviews found, each pinned here.

The BTC card says whose price it shows; a stale proxy is called stale in the footer too; the venue line follows the
closure the price on screen follows; the chart's right axis has room for its widest tag; a resize redraws the equity
curve and the P&L bars; a phone clock set back, or a Coinbase message stamped in the future, never freezes a price,
a tape line or a redraw. In the comic theme: risk's orange reads as orange, SIZE's dollars aren't a gain's green,
VT's unrealised P&L gets its up / down mark, the status bar takes a picked edition before the first paint, and the
walking spider fades while it crosses money. The page's own functions are run in node where they can be."""

import colorsys
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
APP = (DASH / "app.js").read_text()
ACT = (DASH / "activity.js").read_text()
SPIDER = (DASH / "spider.js").read_text()
STYLE = (DASH / "style.css").read_text()
THEME = (DASH / "theme-comic.css").read_text()
HTML = (DASH / "index.html").read_text()
EDITION = (DASH / "edition.js").read_text()

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, k = 0, src.index("{", start)
    for j in range(k, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def _line(src: str, start: str) -> str:
    k = src.index(start)
    return src[k:src.index("\n", k)]


def _block(src: str, start: str) -> str:
    """`const name = (...) => { ... }` from src, through its closing brace."""
    k = src.index(start)
    depth, j0 = 0, src.index("{", k)
    for j in range(j0, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[k:j + 1]
    raise AssertionError(start)


def _node(js: str):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout.strip()
    return json.loads(out)


# ---------------------------------------------------------------- the BTC card's source mark

@node
def test_the_btc_card_says_whose_price_it_shows():
    """cb: Coinbase's own; hl proxy: Hyperliquid's in its place (hl stale once that's 20 s old); closed: the run's
    price while Coinbase is closed; last run: no live price yet. The same livePrice answer the card's price comes
    from, so the word and the number never disagree."""
    js = _function(APP, "srcMark") + """
const out = [
  srcMark({ source: "coinbase", price: 81995, at: 1 }),
  srcMark({ source: "hyperliquid", price: 82046, at: 1, stale: false }),
  srcMark({ source: "hyperliquid", price: 82046, at: 1, stale: true }),
  srcMark({ source: null, price: NaN, at: 0, closed: { why: "weekly break", by: "schedule" } }),
  srcMark({ source: "hyperliquid", price: NaN, at: 0 }),
  srcMark(undefined),
].map((m) => m && [m[0], m[2], m[1]]);
console.log(JSON.stringify(out));"""
    out = _node(js)
    assert [m and m[:2] for m in out] == [["cb", "cb"], ["hl proxy", "proxy"], ["hl stale", "stale"], ["closed", "closed"],
                                          ["last run", "run"], None]
    assert "weekly break" in out[3][2] and "proxy for Coinbase's" in out[1][2]
    assert max(len(m[0]) for m in out if m) <= 8                         # a small word over the price: 320 px fits it
    coins = _function(APP, "renderCoins")
    assert 'srcMark(open ? state.livePx : mk.lp)' in coins and '<b class="c-src ${src[2]}">${src[0]}</b>' in coins
    assert re.search(r"\.coin \.c-name i\.c-px \{[^}]*flex-direction: column", STYLE)   # stacked over the price, not beside it
    assert re.search(r"\.coin \.c-src \{[^}]*font-size: 8px", STYLE)


def test_render_coins_no_longer_shadows_put():
    coins = _function(APP, "renderCoins")
    assert not re.search(r"\bconst put\b", coins) and "const putIn = start + net;" in coins
    assert APP.count("const put = (el, html)") == 1


# ---------------------------------------------------------------- the footer when the proxy is stale

@node
def test_the_footer_says_stale_when_the_proxy_is_stale():
    js = """
const MT = { when: () => "Oct 15, 8:00 pm" }, TZ = "MT", hhmm = () => "8:00 pm", missing = null;
const live = { at: 0 };
""" + _function(APP, "foot") + _function(APP, "provNote") + """
const now = Date.now(), s = (lp) => ({ updated: now - 60000, header: { coin: "BTC" }, venue: { name: "coinbase" }, livePx: lp, chart: {} });
live.at = now - 26000;
const stale = foot(s({ source: "hyperliquid", price: 82046, at: live.at, stale: true }));
live.at = now - 3000;
const fresh = foot(s({ source: "hyperliquid", price: 82046, at: live.at, stale: false }));
const behind = foot({ ...s({ source: "hyperliquid", price: 82046, at: live.at }), updated: now + 13 * 60000 });   // the phone's clock behind
console.log(JSON.stringify({ stale, fresh, behind }));"""
    out = _node(js)
    assert "desk updated Oct 15, 8:00 pm MT (0 min ago)" in out["behind"]
    assert "stale price: Hyperliquid's (a proxy for Coinbase's) last came 26s ago" in out["stale"]
    assert "live price" not in out["stale"]
    assert "live price 3s ago from Hyperliquid (a proxy" in out["fresh"]


# ---------------------------------------------------------------- the venue line and the closure on screen

@node
def test_the_venue_line_follows_the_closure_on_screen():
    """It said "next closure ..." through a closure that began after the run, and after one that had ended."""
    js = """
const esc = (s) => String(s), hhmm = (t) => "T" + t, TZ = "MT", MT = { span: (a, b) => `${a}-${b}` };
""" + _function(APP, "closureNote") + """
const wk = { start: 1000, end: 2000, why: "weekly break" }, v = (o) => ({ session: { next_closure: wk }, closed_now: null, ...o });
const out = {
  before: closureNote(v({}), { source: "hyperliquid" }, 500),
  during: closureNote(v({}), { source: null, closed: { why: "weekly break", until: 2000, by: "schedule" } }, 1500),
  after: closureNote(v({}), { source: "coinbase" }, 2500),
  run_found_then_open: closureNote(v({ closed_now: { why: "weekly break", until: 2000, scheduled: true } }), { source: "coinbase" }, 2500),
  run_found_still: closureNote(v({ closed_now: { why: "weekly break", until: 2000, scheduled: true } }),
    { source: null, closed: { why: "weekly break", until: 2000, by: "run" } }, 1500),
  unscheduled: closureNote(v({ closed_now: { why: "Coinbase reports its session closed", until: null, scheduled: false } }),
    { source: null, closed: { why: "x", by: "run" } }, 500),
};
console.log(JSON.stringify(out));"""
    out = _node(js)
    assert out["before"] == " · next closure weekly break 1000-2000 MT"
    assert out["during"] == " · <b>CLOSED NOW: weekly break until T2000 MT</b>"
    assert out["after"] == ""
    assert out["run_found_then_open"] == " · <b>CLOSED AT THE LAST RUN: weekly break until T2000 MT</b>"
    assert out["run_found_still"] == " · <b>CLOSED NOW: weekly break until T2000 MT</b>"
    assert out["unscheduled"] == (" · <b>CLOSED NOW: Coinbase reports its session closed</b>"
                                  " · next scheduled closure weekly break 1000-2000 MT")
    assert "closureNote(v, s.livePx, Date.now())" in _function(APP, "renderAnalysis")


# ---------------------------------------------------------------- the chart's right axis

@node
def test_the_right_axis_has_room_for_its_widest_tag():
    """'STOP 77,970' was cut off at the canvas's edge on a 375 / 390 px phone: the axis was a fixed 64 px. Measured
    with JetBrains Mono's advance (0.6 em, 6 px at 10 px), every tag now ends at least 2 px inside the edge."""
    nf = _line(APP, "const NF = new Map()")
    js = nf + "\n" + _line(APP, "const pxAxis =") + "\n" + _function(APP, "rightPad") + """
const g = { font: "", measureText: (t) => ({ width: 6 * t.length }) };
const out = [];
for (const [close, stop, live, lo, hi] of [[78500, 77970, null, 76000, 80000], [104250, 101975.5, 104300, 99000, 106000],
                                         [2425.5, 2441.86, null, 2300, 2500], [82015, null, 82020, 80000, 84000]]) {
  const padR = rightPad(g, { stop, live }, pxAxis(close), lo, hi), w = 358;
  const ends = [pxAxis(close), stop ? `STOP ${pxAxis(stop)}` : "", live ? pxAxis(live) : ""].map((t) => w - padR + 2 + 6 * t.length + 8);
  out.push({ padR, room: w - Math.max(...ends), old: w - 64 + 2 + Math.max(...[pxAxis(close), stop ? `STOP ${pxAxis(stop)}` : ""].map((t) => 6 * t.length + 8)) - w });
}
console.log(JSON.stringify(out));"""
    out = _node(js)
    assert all(o["room"] >= 2 and o["padR"] >= 64 for o in out), out
    assert out[0]["old"] > 0 and out[1]["old"] > 0          # the old 64 px ran 'STOP 77,970' past the edge
    assert out[3]["padR"] == 64                               # no stop: as wide as before
    assert "ct = closeTag(chart), padR = rightPad(g, chart, ct.text, lo, hi)" in _function(APP, "drawChart")


# ---------------------------------------------------------------- resize

def test_a_resize_redraws_the_equity_curve_and_the_pnl_bars():
    """addEventListener("resize", render) passed the event in as `light`: a price-only redraw, which leaves the equity
    curve and the P&L bars at their old size (stretched) until the next board. Now a full render, once a frame."""
    # the listener line as main has it (the coinbase-live branch adds its code right above it: a clean merge); render
    # itself takes anything but true or false (the event) as a full redraw, scheduled once a frame
    assert 'window.addEventListener("resize", render);' in APP
    render = _function(APP, "render")
    head = render.split("\n")[1]
    assert head.strip() == ("if (light !== true && light !== false) { if (!resized) resized = requestAnimationFrame(() => "
                            "{ resized = 0; render(); }); return; }")
    assert "if (!light) drawPnlBars(" in render and "if (!light || s.ticker.bank !== drawn.bank)" in render


# ---------------------------------------------------------------- a phone clock set back

@node
def test_a_clock_set_back_freezes_nothing_in_app_js():
    """Times stored before the phone's clock was set back an hour are an hour 'ahead': they aren't fresh for it (the
    next heartbeat, a second away, is), and no redraw or retry waits for the clock to catch up."""
    near = _line(APP, "const near =")
    paint = _function(APP, "cbPaint")
    js = f"""
const CB_BEAT = 5000, CB_QUIET = 60000, CB_PAINT = 1000, CB_GAP = 1000;
{near}
const cb = {{ ws: {{ readyState: 1 }}, sub: "P", at: 0, beat: 0, due: 0, painted: 0 }};
{_line(APP, "const cbFresh =")}
{APP[APP.index("const cbFresh =") + len(_line(APP, "const cbFresh =")):].split(chr(10), 2)[1]}
let delay = null; const setTimeout = (f, d) => {{ delay = d; return 1; }}, clearTimeout = () => {{}};
{paint}
const now = Date.now(), H = 3600000, out = {{}};
cb.beat = now + H; cb.at = now + H; out.ahead_fresh = cbFresh("P", now);
cb.beat = now - 500; cb.at = now - 400; out.just_now_fresh = cbFresh("P", now);
cb.beat = now - 6000; out.old_beat_fresh = cbFresh("P", now);
cb.painted = now + H; cbPaint(); out.paint_delay = delay;
out.dead_after_setback = near(now, now + H, now + H) > 10000;
out.quiet_with_new_message = near(now, now - 300, now + H, 0) > CB_QUIET;
console.log(JSON.stringify(out));"""
    out = _node(js)
    assert out == {"ahead_fresh": False, "just_now_fresh": True, "old_beat_fresh": False, "paint_delay": 1000,
                   "dead_after_setback": True, "quiet_with_new_message": False}
    assert "Math.min(CB_GAP, cb.last + CB_GAP - Date.now())" in _function(APP, "cbConnect")
    assert "near(now, live.at) > STALE" in _function(APP, "livePrice")


@node
def test_a_clock_set_back_still_puts_new_prices_on_the_tape():
    """activity.js took a new allMids answer for one only if its time was later than the last (d.at > midsAt), and a
    Coinbase line only if tk.at > feedAt: after the clock was set back, no line until it caught up. Now a new message
    is one with another time, and the 5 s between Coinbase lines are on performance.now()."""
    block = ACT[ACT.index("let feedLine"):]
    block = block[:block.index(_function(ACT, "tapeFeed")) + len(_function(ACT, "tapeFeed"))]
    js = """
let perf = 10000; const performance = { now: () => perf };
const lines = [], tapeAdd = (t) => lines.push(t), hms = () => "", mid$ = (x) => String(x), moved = () => "";
const B = { header: { coin: "BTC" } }; let tk = null;
""" + block + """
const T = 1792100000000, H = 3600000;
tk = { at: T, price: 1 }; tapeFeed(T);
perf += 6000; tk = { at: T - H + 6000, price: 2 }; tapeFeed(T - H + 6000);      // the clock was set back an hour
perf += 1000; tapeFeed(T - H + 7000);                                           // no new message: no line
perf += 6000; tk = { at: T - H + 13000, price: 3 }; tapeFeed(T - H + 13000);
const answerAt = (d, midsAt) => { """ + _line(ACT, "const answer = ") + """ return answer; };
console.log(JSON.stringify({ lines: lines.length, back: answerAt({ at: T - H }, T), same: answerAt({ at: T }, T), none: answerAt({ at: 0 }, T) }));"""
    out = _node(js)
    assert out == {"lines": 3, "back": True, "same": False, "none": False}
    assert "const since = (t, now) => Math.abs(now - t);" in ACT
    for use in ("since(o.at, now)", "since(midsAt, now)"):
        assert use in ACT, use
    assert not re.search(r"now - (midsAt|o\.at)\b", ACT)


# ---------------------------------------------------------------- Coinbase's stamps

@node
def test_a_message_stamped_in_the_future_cannot_freeze_the_price():
    """A ticker message stamped in 2099 (a clock off at either end) used to set the newest-seen stamp, and every message
    after it was dropped as older: the price froze until the feed was dropped as quiet. Now the newest is the last to
    arrive and its time is its arrival; the stamp only places the price in its candle window, when it's near."""
    js = """
const CB_SKEW = 300000; let now = 1792100000000; Date.now = () => now;
const cb = { sub: "P", quiet: false, venue: null, price: NaN, at: 0, beat: 0 };
const seen = [], cbSeen = (p, t) => seen.push([p, t]), cbPaint = () => {}, cbClosed = () => null;
""" + _block(APP, "const cbTime =") + ";\n" + _function(APP, "cbMessage") + """
const msg = (price, ts) => JSON.stringify({ channel: "ticker", timestamp: ts, events: [{ type: "update", tickers: [{ product_id: "P", price }] }] });
const iso = (t) => new Date(t).toISOString().replace("Z", "123456Z");
cbMessage(msg("80000", "2099-01-01T00:00:00.000000000Z"));
const a = { price: cb.price, at: cb.at };
now += 1000; cbMessage(msg("80100", iso(now - 200)));
const b = { price: cb.price, at: cb.at };
now += 1000; cbMessage(msg("80050", iso(now - 120000)));      // two minutes behind: still Coinbase's newest message
console.log(JSON.stringify({ a, b, c: { price: cb.price, at: cb.at }, seen, now }));"""
    out = _node(js)
    t0 = 1792100000000
    assert out["a"] == {"price": 80000, "at": t0}
    assert out["b"] == {"price": 80100, "at": t0 + 1000}                 # not dropped as "older" than 2099
    assert out["c"] == {"price": 80050, "at": t0 + 2000}
    assert out["seen"][0] == [80000, t0]                                  # 2099: placed by its arrival
    assert out["seen"][1] == [80100, t0 + 1000 - 200]                     # near: by its stamp
    assert out["seen"][2] == [80050, t0 + 2000 - 120000]
    msg = _function(APP, "cbMessage")
    assert "cb.ts" not in APP and "ts < cb" not in msg and "cb.at = now;" in msg


# ---------------------------------------------------------------- the tape's heading

def test_the_tapes_heading_names_coinbases_feed_where_it_has_lines():
    assert '<b data-k="tapeh">' in ACT
    assert "set(L.tapeh, `TAPE · this page's own requests${venue() ? \" and Coinbase's feed\" : \"\"}, as they land · times ${TZ}`);" in ACT


# ---------------------------------------------------------------- the comic theme

def _hex(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _lum(h: str) -> float:
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = map(f, _hex(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _lab(h: str) -> tuple[float, float, float]:
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = map(f, _hex(h))
    x, y, z = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b, \
        (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    t = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116  # noqa: E731
    return 116 * t(y) - 16, 500 * (t(x) - t(y)), 200 * (t(y) - t(z))


def _mix(a: str, b: str, wa: float) -> str:
    ca, cb = _hex(a), _hex(b)
    return "#" + "".join(f"{round((wa * x + (1 - wa) * y) * 255):02x}" for x, y in zip(ca, cb))


def _tokens(start: str) -> dict:
    i = THEME.index(start)
    body = THEME[THEME.index("{", i + len(start) - 1) + 1:THEME.index("}", i)]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-f]{6})\b", body))


DAY, NIGHT = _tokens(":root {"), _tokens(':root[data-edition="night"] {')


def test_the_day_risk_color_reads_as_orange_not_a_dark_red():
    o = DAY["--orange"]
    hue = colorsys.rgb_to_hls(*_hex(o))[0] * 360
    assert 22 <= hue <= 40, (o, hue)                                     # the old rust #7e2400 was 17: a dark red
    for bg in ("--panel", "--panel-2", "--caption"):
        assert _contrast(o, DAY[bg]) >= 4.5, bg
    assert abs(hue - colorsys.rgb_to_hls(*_hex(DAY["--red"]))[0] * 360) > 60   # nowhere near the loss magenta


def test_sizes_dollars_are_not_a_gains_green():
    """SIZE's value is dollars (the trade it would size) that are neither a gain nor a loss. In its card's olive (day) or
    lime (night) it read as a gain's green; now it's ink with a hint of that color: no chroma to speak of, and far
    from the green in both editions."""
    m = re.search(r"#card-size \.value \{ color: var\(--text\); color: color-mix\(in srgb, var\(--lime\) (\d+)%, var\(--text\)\); \}", THEME)
    assert m, "SIZE's value tone"
    w = int(m.group(1)) / 100
    d = lambda a, b: sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b))) ** 0.5  # noqa: E731
    chroma = lambda h: (_lab(h)[1] ** 2 + _lab(h)[2] ** 2) ** 0.5  # noqa: E731
    for ed in (DAY, NIGHT):
        size = _mix(ed["--lime"], ed["--text"], w)
        assert chroma(ed["--lime"]) >= 50 and chroma(size) <= 22, size       # a color before; an ink now
        assert d(size, ed["--green"]) >= 40, (size, ed["--green"])       # (the olive was 30 from it)
        assert _contrast(size, ed["--panel"]) >= 7
    assert d(DAY["--lime"], DAY["--green"]) < 35                          # the olive it was: a gain's green at a glance


def test_vt_unrealised_cells_take_the_up_down_flat_hook():
    assert 'class="h-pnl ${tone(h.unrealised)}"' in APP and "`h-pnl ${tone(t.unrealised)}`" in APP
    tri = THEME[THEME.index("/* ---- money:"):THEME.index("/* ---- agent cards")]
    assert ".h-row .h-pnl).up::before" in tri and ".h-row .h-pnl).down::before" in tri
    assert ".stats .h-pnl:is(.up, .down) > b::before" in tri and ".stats .h-pnl.down > b::before" in tri
    assert ".h-row .h-pnl).flat, .stats .h-pnl.flat { color: var(--text); }" in tri


@node
def test_the_status_bar_takes_the_picked_edition_before_the_first_paint():
    """The head's script set html[data-edition] but left both theme-colors on their media queries until edition.js ran
    at the end of the body: day picked on a dark phone showed the night bar for a moment. Now the head sets both, and
    edition.js still knows each one's own color for auto."""
    head = re.search(r"<script>(try\{.*?localStorage\.getItem\(\"desk-edition\"\).*?\}catch\(x\)\{\})</script>", HTML).group(1)
    metas = re.findall(r'<meta name="theme-color" content="(#[0-9a-f]{6})" media="(\([^"]+\))">', HTML)
    js = f"""
const metas = {json.dumps([{"content": c, "media": m} for c, m in metas])}.map((m) => ({{ ...m, dataset: {{}},
  setAttribute(k, v) {{ if (k === "data-own") this.dataset.own = v; }} }}));
const root = {{ dataset: {{}} }}, handlers = [];
let saved = null, darkDevice = true;
const localStorage = {{ getItem: () => saved, setItem: (k, v) => {{ saved = v; }}, removeItem: () => {{ saved = null; }} }};
const buttons = ["day", "night", "auto"].map((v) => ({{ dataset: {{ editionPick: v }}, setAttribute() {{}}, addEventListener(e, f) {{ handlers.push([v, f]); }} }}));
const document = {{ documentElement: root, querySelectorAll: (q) => q.includes("theme-color") ? metas : buttons }};
const matchMedia = () => ({{ matches: darkDevice, addEventListener() {{}} }});
const dispatchEvent = () => {{}}; class CustomEvent {{ constructor() {{}} }}
const out = {{}};
function load(pick) {{
  saved = pick; root.dataset = {{}}; metas.forEach((m, k) => {{ m.content = {json.dumps([c for c, _ in metas])}[k]; m.dataset = {{}}; }});
  {head}
  const head = metas.map((m) => m.content);
  {EDITION}
  return {{ head, edition: root.dataset.edition || null, after: metas.map((m) => m.content) }};
}}
out.day_on_dark = load("day");
handlers.filter(([v]) => v === "auto").pop()[1]();
out.auto_after = metas.map((m) => m.content);
out.night = load("night");
out.none = load(null);
console.log(JSON.stringify(out));"""
    out = _node(js)
    day, night = metas[0][0], metas[1][0]
    assert out["day_on_dark"] == {"head": [day, day], "edition": "day", "after": [day, day]}
    assert out["auto_after"] == [day, night]                              # auto: each back to its own
    assert out["night"]["head"] == [night, night]
    assert out["none"] == {"head": [day, night], "edition": None, "after": [day, night]}


def test_the_walking_spider_fades_while_it_crosses_money():
    """The perch rule kept its stops off money; on the way it could cover a figure. Now a walk, a drop or a climb
    reads where the money is once as it starts, and each frame compares numbers only, fading the sprite over it."""
    for fn in ("walk", "lower", "climb"):
        body = _function(SPIDER, fn)
        assert "fade: true" in body and "passing();" in body, fn
    assert "const passing = () => { pass = money(); };" in SPIDER
    paint = _function(SPIDER, "paint")
    assert "!clear({ x: x + S / 2 + fx, y: y + S / 2 + fy }, pass, false, 2)" in paint
    assert 'body.classList.toggle("over-money", over)' in paint
    assert "money()" not in paint and "getBoundingClientRect" not in paint and "getClientRects" not in paint
    assert re.search(r"\.spider\.over-money \{ opacity: \.1\d?; transition-duration: 0s; \}", STYLE)
