"""The chart's live price, unmistakable at a glance: one "● LIVE 82,290" tag on the price itself at the right edge, a
solid line in a gold no other line on the chart has, its dot beating once for each new price.

The owner, on a phone at night, read BTC's live price as missing: it was a thin dashed line in the fast average's
cyan, its word LIVE floated above the newest wicks (on the end of the fast average), the tag at the price didn't say
LIVE, and ETH's and SOL's charts had no marker at all. Now every chart with its own fresh live price draws it the same
way (BTC with Coinbase's, ETH and SOL with Hyperliquid's while it is under 20 s old), and none without: BTC on the
Hyperliquid proxy, stale, Coinbase closed, or the feed down keep today's tags. VT's tab has no price chart. The page's
own functions are run in node, drawing on a canvas that records what it is asked to draw, with the comic theme's day
and night tokens."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
APP = (DASH / "app.js").read_text()
STYLE = (DASH / "style.css").read_text()
THEME = (DASH / "theme-comic.css").read_text()

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


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


def _tokens(start: str) -> dict:
    """A theme block's tokens, var(--x) resolved against the day edition's."""
    i = THEME.index(start)
    body = THEME[THEME.index("{", i + len(start) - 1) + 1:THEME.index("}", i)]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


DAY = _tokens(":root {")
NIGHT = {**DAY, **_tokens(':root[data-edition="night"] {')}
for _ed in (DAY, NIGHT):
    for _k, _v in list(_ed.items()):
        m = re.fullmatch(r"var\((--[\w-]+)\)", _v.strip())
        if m:
            _ed[_k] = _ed[m.group(1)]


# ---------------------------------------------------------------- only the chart's own fresh price

def _withlive() -> str:
    return (_between(APP, "const CB_BEAT", "  // quiet: the ticker") + _between(APP, "const cbFresh", "  const cbWanted")
            + _function(APP, "formingBars") + _function(APP, "remark") + _function(APP, "rebank")
            + _function(APP, "basketLive") + _function(APP, "withLiveBasket") + _function(APP, "withLive") + """
const live = { mids: {}, at: 0, candle: null };
const cb = { ws: null, sub: "P", at: 0, beat: 0, price: 81230, bars: new Map(), barMs: 7200000 };
const H = 3600000, now = Date.now(), T = Math.floor(now / (2 * H)) * 2 * H;
const board = (coin, venue) => ({ header: { coin }, venue, analysis: { stats: {}, outlook: [] },
  ticker: { price: 1, cash: 500, pos: { side: "short", qty: 0.01, entry: 2600 } }, goal: { start: 500, target: 100000 },
  chart: { bar_ms: 2 * H, candles: [[T - 4 * H, 1, 2, 1, 2], [T - 2 * H, 2, 3, 2, 2440]], ema_fast: [1, 1], ema_slow: [1, 1],
    don_hi: [3, 3], don_lo: [1, 1], markers: [] } });
const venue = (o) => ({ name: "coinbase", product: "P", closed_now: null, session: { next_closure: null }, ...o });
const mark = (s) => { const c = withLive(s).chart; return [c.live ?? null, c.liveAt ?? null, c.candles[c.candles.length - 1][4], c.side]; };
const out = {};
cb.ws = { readyState: 1 }; cb.at = now - 400; cb.beat = now - 400; cb.bars.set(T, { o: 81000, h: 81300, l: 80900, c: 81230, at: cb.at });
live.mids = { BTC: "81250", ETH: "2441.5" }; live.at = now - 1000;
out.btc_coinbase = mark(board("BTC", venue({})));
cb.ws = null;
out.btc_proxy = mark(board("BTC", venue({})));
live.at = now - 21000;
out.btc_proxy_stale = mark(board("BTC", venue({})));
live.at = now - 1000;
out.btc_closed_at_run = mark(board("BTC", venue({ closed_now: { why: "weekly break", until: null, scheduled: false } })));
cb.ws = { readyState: 1 };
out.btc_closed_by_schedule = mark(board("BTC", venue({ session: { next_closure: { start: now - 1000, end: now + H, why: "maintenance" } } })));
out.eth_fresh = mark(board("ETH", null));
live.candle = [T, 2430, 2450, 2425, 2433]; live.candleCoin = "ETH";
out.eth_candle = mark(board("ETH", null));
live.candle = null;
live.at = now - 19500;
out.eth_19s = mark(board("ETH", null));
live.at = now - 20500;
out.eth_21s = mark(board("ETH", null));
live.mids = {}; live.at = 0;
out.eth_no_price = mark(board("ETH", null));
console.log(JSON.stringify({ out, now, cbAt: cb.at }));""")


@node
def test_the_marker_only_with_the_charts_own_fresh_live_price():
    """chart.live (the marker) is BTC's Coinbase price while the feed is fresh, and ETH's (or SOL's) Hyperliquid mid
    while it is under STALE (20 s) old; never the proxy's on BTC's chart, a stale price, or anything while Coinbase is
    closed. liveAt is that price's time (its dot beats for a new one). With Hyperliquid's forming candle on the chart,
    its close is the live price: one tag says both."""
    r = _node(_withlive())
    o, now = r["out"], r["now"]
    assert o["btc_coinbase"][:3] == [81230, r["cbAt"], 81230]          # the forming candle carried on to it: merged
    for case in ("btc_proxy", "btc_proxy_stale", "btc_closed_at_run", "btc_closed_by_schedule", "eth_21s", "eth_no_price"):
        assert o[case][:2] == [None, None], case
    assert o["eth_fresh"][:2] == [2441.5, now - 1000] and o["eth_fresh"][2] == 2440   # no candle yet: a tag of its own
    assert o["eth_candle"][0] == 2441.5 and o["eth_candle"][2] == 2441.5               # Hyperliquid's candle closes at it
    assert o["eth_19s"][0] == 2441.5
    assert o["eth_fresh"][3] == "short"                                                # the stop's side, from the position
    live = _function(APP, "withLive")
    assert live.index('if (s.header.kind === "basket") return withLiveBasket(s);') < live.index("s.chart.live = mid")   # VT: none


# ---------------------------------------------------------------- the chart as drawn

_CANVAS = """
const ops = [], st = [];
let cur = { fillStyle: "#000", strokeStyle: "#000", font: "10px sans-serif", lineWidth: 1, dash: [], textAlign: "start", globalAlpha: 1 };
const g = {
  get fillStyle() { return cur.fillStyle; }, set fillStyle(v) { cur.fillStyle = v; },
  get strokeStyle() { return cur.strokeStyle; }, set strokeStyle(v) { cur.strokeStyle = v; },
  get font() { return cur.font; }, set font(v) { cur.font = v; },
  get lineWidth() { return cur.lineWidth; }, set lineWidth(v) { cur.lineWidth = v; },
  get textAlign() { return cur.textAlign; }, set textAlign(v) { cur.textAlign = v; },
  get globalAlpha() { return cur.globalAlpha; }, set globalAlpha(v) { cur.globalAlpha = v; },
  save() { st.push({ ...cur, dash: cur.dash.slice() }); }, restore() { cur = st.pop(); },
  setTransform() {}, clearRect() { ops.length = 0; }, beginPath() { this._pts = []; }, closePath() {},
  moveTo(x, y) { this._pts.push([x, y]); }, lineTo(x, y) { this._pts.push([x, y]); }, arc(x, y, r) { this._pts.push([x, y, r]); },
  ellipse() {}, setLineDash(d) { cur.dash = d.slice(); }, getLineDash() { return cur.dash; },
  stroke() { ops.push({ k: "stroke", color: cur.strokeStyle, width: cur.lineWidth, dash: cur.dash.join(","), pts: this._pts }); },
  fill() { ops.push({ k: "fill", color: cur.fillStyle, pts: this._pts }); },
  fillRect(x, y, w, h) { ops.push({ k: "fillRect", color: cur.fillStyle, x, y, w, h }); },
  strokeRect(x, y, w, h) { ops.push({ k: "strokeRect", color: cur.strokeStyle, dash: cur.dash.length > 0, x, y, w, h }); },
  fillText(t, x, y) { ops.push({ k: "text", t: String(t), x, y, color: cur.fillStyle, font: cur.font }); },
  measureText(t) { return { width: 0.6 * parseFloat(/(\\d+(\\.\\d+)?)px/.exec(cur.font)[1]) * String(t).length }; },
};
const canvas = { clientWidth: 280, clientHeight: 260, width: 0, height: 0, offsetLeft: 6, offsetTop: 40, getContext: () => g };
const window = { devicePixelRatio: 2 }, beats = [];
const $ = () => canvas, matchMedia = () => ({ matches: false });
canvas.animate = () => {}; canvas.after = (el) => {};
const document = { createElement: () => ({ style: {}, setAttribute() {}, animate: (k, o) => beats.push(o.duration), getAnimations: () => [] }) };
let lastGeom = null, TOK = {};
const css = (n) => TOK[n] || "";
"""


def _draw_js() -> str:
    return ("const DAY_MS = 86400000;\n" + _between(APP, "  const deskMT = (() => {", "  window.deskMT = deskMT;")
            + "const MT = deskMT, TZ = deskMT.zone;\n" + _between(APP, "  const NF = new Map()", "\n") + "\n"
            + _between(APP, "  const pxAxis =", "\n") + "\n" + _CANVAS
            + _function(APP, "fit") + _function(APP, "rgba") + _function(APP, "line") + _function(APP, "drawChart")
            + _function(APP, "stackTags") + _block(APP, "const liveW =") + ";\n" + _function(APP, "liveTag")
            + _between(APP, "  const beat = { el: null", "  function liveBeat(") + _function(APP, "liveBeat")
            + _function(APP, "closeTag") + _function(APP, "rightPad") + _function(APP, "provCandle") + _function(APP, "nearest")
            + _function(APP, "entryPin") + _function(APP, "tag") + """
const H = 3600000, T0 = Date.parse("2026-10-14T00:00:00Z"), N = 60;
const chartOf = (o = {}) => {
  const candles = Array.from({ length: N }, (_, k) => { const c = 80000 + 1500 * Math.sin(k / 6); return [T0 + 2 * H * k, c - 100, c + 300, c - 300, c + 50]; });
  return { bar_ms: 2 * H, candles, ema_fast: candles.map((c) => c[4] + 400), ema_slow: candles.map((c) => c[4] - 500),
    don_hi: candles.map(() => 82500), don_lo: candles.map(() => 78000), markers: [], stop: null, entry: null, prov: [], ...o };
};
const frame = (tok, ch) => {
  TOK = tok; drawChart(ch);
  const h = 260, padR = lastGeom.padR, w = 280, Y = lastGeom.Y;
  const tags = ops.filter((o) => o.k === "strokeRect" && o.h === 16 && o.x >= w - padR).map((r) => ({ y: r.y + 8, x: r.x, w: r.w, color: r.color, dash: r.dash,
    text: ops.filter((t) => t.k === "text" && t.x >= r.x && t.x <= r.x + r.w && t.y >= r.y && t.y <= r.y + 16).sort((a, b) => a.x - b.x).map((t) => t.t).join(" "),
    fill: (ops.find((f) => f.k === "fillRect" && f.x === r.x && f.y === r.y) || {}).color }));
  const lines = ops.filter((o) => o.k === "stroke" && o.pts.length === 2 && o.pts[0][1] === o.pts[1][1] && o.pts[0][0] === 0 && o.pts[1][0] === w - padR)
    .map((o) => ({ y: o.pts[0][1], color: o.color, width: o.width, dash: o.dash }));
  const curves = ops.filter((o) => o.k === "stroke" && o.pts.length > 2).map((o) => o.color);
  const labels = ops.filter((o) => o.k === "text" && !/bold/.test(o.font) && o.x === w - padR + 6).map((o) => [o.t, o.y]);
  const words = ops.filter((o) => o.k === "text" && o.t === "LIVE").map((o) => ({ color: o.color, font: o.font }));
  const dots = ops.filter((o) => o.k === "fill" && o.pts.length === 1 && o.pts[0][2] === 2.5).map((o) => o.color);
  return { tags, lines, curves, labels, words, dots, padR, w, yLive: ch.live ? Y(ch.live) : null, yStop: ch.stop ? Y(ch.stop) : null,
    beats: beats.length };
};
""")


def _draw(cases: str) -> dict:
    return _node(_draw_js() + "const TOKS = " + json.dumps({"day": DAY, "night": NIGHT}) + ";\nconst out = {};\n" + cases
                 + "\nconsole.log(JSON.stringify(out));")


def _overlap(a: dict, b: dict) -> bool:
    return abs(a["y"] - b["y"]) < 16 and a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_one_live_tag_on_the_price_in_a_color_no_other_line_has(edition):
    """With a live price: a solid 1.5 px line in --live, ending in one tag at the right edge on its price, filled
    --live, its dot and "LIVE" and the price in --on-live, wholly inside the canvas. No word LIVE anywhere else (it no
    longer floats above the wicks). The newest close's tag is folded into it when that close is the live price, and
    drawn on its own (filled, as a closed candle's) when it isn't."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf(); ch.live = ch.candles[N - 1][4]; ch.liveAt = 1;
out.merged = frame(TOKS.{edition}, ch);
const c2 = chartOf(); c2.live = c2.candles[N - 1][4] - 700; c2.liveAt = 2;
out.apart = frame(TOKS.{edition}, c2);""")
    live, on = tok["--live"], tok["--on-live"]
    for case in ("merged", "apart"):
        f = o[case]
        lt = [t for t in f["tags"] if "LIVE" in t["text"]]
        assert len(lt) == 1, (case, f["tags"])
        t = lt[0]
        assert t["text"].startswith("LIVE ") and t["fill"] == live and t["color"] == live and not t["dash"]
        assert abs(t["y"] - f["yLive"]) < 1e-6                                  # on its price: the line ends in it
        assert t["x"] + t["w"] <= f["w"] - 2                                     # never cut off at the canvas's edge
        assert f["words"] == [{"color": on, "font": "bold 9px JetBrains Mono, monospace"}]
        assert f["dots"].count(on) == 1
        ll = [x for x in f["lines"] if x["color"] == live]
        assert ll == [{"y": f["yLive"], "color": live, "width": 1.5, "dash": ""}]   # solid: the price now
    assert len(o["merged"]["tags"]) == 1                                         # its close is the live price: one tag
    assert len(o["apart"]["tags"]) == 2 and not _overlap(*o["apart"]["tags"])
    close = next(t for t in o["apart"]["tags"] if "LIVE" not in t["text"])
    assert close["fill"] in (tok["--green"], tok["--red"]) and not close["dash"]


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_the_live_lines_color_is_no_other_lines(edition):
    """The live line's color is the only one of its kind on the chart: not the fast (--cyan) or slow (--blue)
    average, the channel, the entry or the stop, the candles or the grid, as drawn and as the theme defines them, and
    far from each by eye (CIE76 delta E 25 or more); readable on the panel (3:1 as a line), its lettering 4.5:1."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf({{ stop: 79000, entry: 81000 }}); ch.live = ch.candles[N - 1][4]; ch.liveAt = 1;
out.f = frame(TOKS.{edition}, ch);""")["f"]
    live = tok["--live"]
    others = {x["color"] for x in o["lines"] if x["color"] != live} | set(o["curves"])
    assert live not in others and len([x for x in o["lines"] if x["color"] == live]) == 1
    for name in ("--cyan", "--blue", "--channel", "--entry", "--orange", "--green", "--red", "--line", "--dim", "--pen"):
        assert tok[name] in others or name in ("--pen", "--green", "--red", "--dim"), name   # each is a line on this chart
        assert _de76(live, tok[name]) >= 25, (name, tok[name], live, _de76(live, tok[name]))
    assert _contrast(live, tok["--panel"]) >= 3 and _contrast(tok["--on-live"], live) >= 4.5


def _lab(h: str):
    c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    x, y, z = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _de76(a: str, b: str) -> float:
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b))) ** 0.5


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        r, g, b_ = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_live_is_a_token_of_its_own_in_both_editions():
    """--live and --on-live in the day edition and the night one (both night blocks, the same); the base page has
    them too. The fast average stays --cyan; the chart reads --live with a fallback, as its other hooks do."""
    for ed in (DAY, NIGHT):
        assert re.fullmatch(r"#[0-9a-f]{6}", ed["--live"]) and re.fullmatch(r"#[0-9a-f]{6}", ed["--on-live"])
    assert DAY["--live"] != NIGHT["--live"]
    assert _tokens(':root:not([data-edition="day"]) {') == _tokens(':root[data-edition="night"] {')
    assert re.search(r"--live:\s*#[0-9a-f]{6};", STYLE) and re.search(r"--on-live:\s*#[0-9a-f]{6};", STYLE)
    draw = _function(APP, "drawChart")
    assert 'css("--live") || "#ffd84a", 1.5);' in draw and 'line(g, chart.ema_fast.map(' in draw and 'css("--cyan"), 1.6);' in draw
    assert 'rgba("--cyan", .7)' not in APP


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_no_marker_without_a_live_price_and_the_held_tag_stays_dashed(edition):
    """No chart.live (BTC on the proxy, stale, Coinbase closed, the feed down; ETH with Hyperliquid quiet): no line or
    fill in --live, no LIVE, no beat; the run's provisional candle keeps its outlined, dashed RUN tag."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf({{ prov: [N - 1] }}); ch.runIdx = N - 1;
out.f = frame(TOKS.{edition}, ch);""")["f"]
    assert o["words"] == [] and all(x["color"] != tok["--live"] for x in o["lines"]) and o["beats"] == 0
    assert len(o["tags"]) == 1 and o["tags"][0]["text"].startswith("RUN ") and o["tags"][0]["dash"]
    assert o["tags"][0]["fill"] == tok["--ink"]                                   # outlined, not filled: not now's price


# ---------------------------------------------------------------- collisions

@node
@pytest.mark.parametrize("side", ["long", "short"])
def test_a_stop_at_the_live_price_and_tags_near_it_never_overlap(side):
    """A stop exactly at the live price (and just either side of it, and at the chart's low or high where it is the
    extreme): the LIVE tag stays on its line and the STOP tag goes on the stop's side of it (below a long's price,
    above a short's), clear of it, inside the canvas. The newest close's tag (not the live price) and the axis labels
    are clear of both, and so is the run's held RUN tag, dashed as before; "MT" is left out only when a tag takes
    its corner."""
    o = _draw(f"""
const side = "{side}";
for (const [name, d] of [["at", 0], ["above", 40], ["below", -40], ["near", 150]]) {{
  const ch = chartOf({{ side }}); ch.live = ch.candles[N - 1][4] - 300; ch.stop = ch.live + d; ch.entry = side === "long" ? ch.live + 900 : ch.live - 900; ch.liveAt = 1;
  out[name] = frame(TOKS.night, ch);
}}
for (const [name, ext] of [["low", -1], ["high", 1]]) {{   // the stop the chart's extreme, the price on it
  const ch = chartOf({{ side }}); ch.stop = ext < 0 ? 77000 : 84000; ch.live = ch.stop; ch.liveAt = 1;
  out[name] = frame(TOKS.night, ch);
}}
// the run's own candle held (RUN, dashed) a few px from the live price, the stop between them
const ch = chartOf({{ side, prov: [N - 1] }}); ch.runIdx = N - 1; ch.live = ch.candles[N - 1][4] - 30; ch.stop = ch.live + 15; ch.liveAt = 1;
out.held = frame(TOKS.day, ch);""")
    for name, f in o.items():
        tags = f["tags"]
        live = next(t for t in tags if "LIVE" in t["text"])
        stop = next(t for t in tags if t["text"].startswith("STOP"))
        assert all(not _overlap(a, b) for i, a in enumerate(tags) for b in tags[i + 1:]), (name, tags)
        assert all(8 <= t["y"] <= 252 for t in tags), name
        assert abs(live["y"] - f["yLive"]) < 1e-6 or name == "high", (name, live, f["yLive"])   # on its line
        assert abs(live["y"] - f["yLive"]) <= 8, name                             # never off its line
        if name == "at" or name in ("low", "high"):
            assert (stop["y"] > live["y"]) == (side == "long"), (name, side, stop, live)
        for text, y in f["labels"]:
            assert all(abs((y - 3) - t["y"]) >= 13 or text == "MT" for t in tags), (name, text, y)
    assert not any(t == "MT" for t, _ in o["low"]["labels"]) if side == "long" else True
    held = next(t for t in o["held"]["tags"] if t["text"].startswith("RUN "))
    assert held["dash"] and len(o["held"]["tags"]) == 3                          # still dashed, still there, clear


# ---------------------------------------------------------------- the legend

@node
def test_the_legend_says_what_the_marker_is_and_wraps_between_entries():
    """'live' with a small tag swatch (● on --live) while the marker is drawn, never without it; every entry
    whole in a nowrap span, the dot after it glued with a no-break space, so a phone breaks only after a '·'."""
    js = _function(APP, "legend") + """
const ch = { live: 82290, prov: [139] };
console.log(JSON.stringify([legend(ch, "<span>24h +2.10%</span>"), legend({ prov: [] }, "<span>24h</span>")]));"""
    with_live, without = _node(js)
    assert with_live == ('<span class="lg"><span>24h +2.10%</span></span>&nbsp;· <span class="lg">fast <i class="swatch fast"></i></span>'
                         '&nbsp;· <span class="lg">slow <i class="swatch slow"></i></span>&nbsp;· <span class="lg">channel '
                         '<i class="swatch channel"></i></span>&nbsp;· <span class="lg">stop <i class="swatch stop"></i></span>'
                         '&nbsp;· <span class="lg">live <i class="swatch live"></i></span>&nbsp;· <span class="lg">'
                         'newest candle <i class="swatch prov"></i> provisional</span>')
    assert "live" not in without and "provisional" not in without
    assert ".chart-head .lg { white-space: nowrap; }" in STYLE
    sw = re.search(r"\.swatch\.live \{([^}]*)\}", STYLE).group(1)
    assert "var(--live)" in sw and "var(--on-live)" in sw
    assert 'put($("chart-sub"), legend(s.chart, ' in _function(APP, "renderStage")


# ---------------------------------------------------------------- the beat

def _beat() -> str:
    return _between(APP, "  const beat = { el: null", "  function liveBeat(") + _function(APP, "liveBeat")


@node
@pytest.mark.parametrize("reduced", [False, True])
def test_the_dot_beats_once_per_new_price_and_never_under_reduced_motion(reduced):
    """liveBeat: one short animation (0.5 s) for each new price's time, none for a redraw with the same price, none
    once the marker is gone (an animation in flight is cancelled), and none at all under prefers-reduced-motion. No
    loop: no interval or animation frame; the page's own redraws (a Coinbase message, an allMids answer) drive it."""
    js = """
const anims = [], cancelled = [], el = { style: {}, setAttribute() {}, className: "",
  animate: (k, o) => { anims.push([k.map((x) => x.transform), o.duration]); return {}; },
  getAnimations: () => [{ cancel: () => cancelled.push(1) }] };
const canvas = { offsetLeft: 6, offsetTop: 40, animate() {}, after() {} };
const $ = () => canvas, document = { createElement: () => el };
const matchMedia = () => ({ matches: %s });
""" % ("true" if reduced else "false") + _beat() + """
liveBeat(1000, [10, 20]); liveBeat(1000, [10, 20]); liveBeat(1000);   // a redraw for another reason: the same price
liveBeat(2000);                                                        // a new price (cbPaint's no-redraw path)
liveBeat(0, null);                                                     // the marker gone: proxy, stale, closed
liveBeat(3000, [10, 25]);
console.log(JSON.stringify({ anims, cancelled: cancelled.length, left: el.style.left, top: el.style.top }));"""
    r = _node(js)
    if reduced:
        assert r["anims"] == []
    else:
        assert len(r["anims"]) == 3 and all(d == 500 for _, d in r["anims"])
        assert r["anims"][0][0] == ["scale(1)", "scale(2)", "scale(1)"] and r["left"] == "16px" and r["top"] == "65px"
        assert r["cancelled"] == 1
    for loop in ("setInterval", "requestAnimationFrame", "setTimeout", "infinite"):
        assert loop not in _function(APP, "liveBeat"), loop
    assert re.search(r"\.live-beat \{[^}]*opacity: 0;", STYLE)                    # nothing at rest
    reduce = STYLE[STYLE.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert ".live-beat { display: none; }" in reduce
    paint = _function(APP, "cbPaint")
    assert "liveBeat(cb.at);" in paint                                             # a Coinbase message, the same price


def test_a_quiet_hyperliquid_takes_the_marker_off_with_one_timer():
    """ETH and SOL: one setTimeout to the moment the shown mid turns STALE, reset by each redraw, so the marker goes
    when Hyperliquid goes quiet and nothing runs while prices keep coming. BTC's Coinbase price: cbWatch already
    redraws when it stops being fresh."""
    render = _function(APP, "render")
    assert "clearTimeout(liveDue);" in render
    assert ("if (!basket && !s.venue && s.chart.live) liveDue = setTimeout(() => render(true), "
            "Math.max(0, s.chart.liveAt + STALE - Date.now()) + 50);") in render
    assert "if (cbMode(livePrice(on, now)) !== cb.mode) cbPaint(true);" in _function(APP, "cbWatch")
