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

def _withlive_base() -> str:
    """withLive and what it reads: a board of either kind, Coinbase's feed and Hyperliquid's mids for a case to set."""
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
const mark = (s) => { const c = withLive(s).chart; return [c.live ?? null, !!c.canLive, c.candles[c.candles.length - 1][4], c.side]; };
""")


def _withlive() -> str:
    return _withlive_base() + """
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
live.mids = { ETH: "2441.5" }; live.at = now - 1000;
out.hl_fresh = livePrice({ header: { coin: "ETH" }, venue: null });
live.at = now - 20500;
out.hl_stale = livePrice({ header: { coin: "ETH" }, venue: null });
console.log(JSON.stringify({ out, now, cbAt: cb.at }));"""


@node
def test_the_marker_only_with_the_charts_own_fresh_live_price():
    """chart.live (the marker) is BTC's Coinbase price while the feed is fresh, and ETH's (or SOL's) Hyperliquid mid
    while it is under STALE (20 s) old; never the proxy's on BTC's chart, a stale price, or anything while Coinbase is
    closed. Each of these charts can have one (canLive: its room is kept while it has none). ETH's Hyperliquid price
    is called stale past 20 s, as the proxy's is. With Hyperliquid's forming candle on the chart, its close is the live
    price: one tag says both."""
    r = _node(_withlive())
    o = r["out"]
    assert o["btc_coinbase"][:3] == [81230, True, 81230]               # the forming candle carried on to it: merged
    for case in ("btc_proxy", "btc_proxy_stale", "btc_closed_at_run", "btc_closed_by_schedule", "eth_21s", "eth_no_price"):
        assert o[case][:2] == [None, True], case
    assert o["eth_fresh"][:2] == [2441.5, True] and o["eth_fresh"][2] == 2440       # no candle yet: a tag of its own
    assert o["eth_candle"][0] == 2441.5 and o["eth_candle"][2] == 2441.5               # Hyperliquid's candle closes at it
    assert o["eth_19s"][0] == 2441.5
    assert o["eth_fresh"][3] == "short"                                                # the stop's side, from the position
    assert o["hl_fresh"]["stale"] is False and o["hl_stale"]["stale"] is True             # ETH's ticker says STALE then
    live = _function(APP, "withLive")
    assert live.index('if (s.header.kind === "basket") return withLiveBasket(s);') < live.index("s.chart.live = mid")   # VT: none


@node
def test_a_stale_hyperliquid_candle_is_held_never_the_price_now():
    """ETH's (or SOL's) forming candle from Hyperliquid closes at its mid. While that mid is fresh it is the live price:
    the candle is drawn as any, its close folded into the LIVE tag. Once Hyperliquid has been quiet past STALE (20 s)
    the LIVE marker goes and that close is a price from before now: the candle is provisional (dashed, as BTC's held
    one is) and its close tag outlined in dashes, never the filled tag of a closed candle or of the price now; the
    footer says what its close is. It was left filled at the stale mid."""
    js = (_withlive_base() + _between(APP, "  const NF = new Map()", "\n") + "\n" + _between(APP, "  const pxAxis =", "\n")
          + "\n" + _function(APP, "closeTag") + _function(APP, "provNote") + """
const hhmm = () => "", TZ = "MT";
live.mids = { ETH: "2441.5" }; live.candleCoin = "ETH";
const run = (age) => {
  live.at = now - age; live.candle = [T, 2430, 2450, 2425, 2433];
  const c = withLive(board("ETH", null)).chart;
  return { live: c.live ?? null, prov: c.prov || [], n: c.candles.length, close: c.candles[c.candles.length - 1][4], tag: closeTag(c), note: provNote(c) };
};
console.log(JSON.stringify({ fresh: run(1000), stale: run(21000) }));""")
    r = _node(js)
    assert r["fresh"] == {"live": 2441.5, "prov": [], "n": 3, "close": 2441.5, "tag": {"text": "2,442", "held": False}, "note": ""}
    st = r["stale"]
    assert st["live"] is None and st["prov"] == [2] and st["close"] == 2441.5
    assert st["tag"] == {"text": "2,442", "held": True}                           # dashed, outlined; no RUN: not the run's
    assert st["note"] == " · newest candle: provisional, its close Hyperliquid's last price, not now's"
    o = _draw("""
const ch = chartOf({ prov: [N - 1], provHl: true, canLive: true });
out.f = frame(TOKS.night, ch);""")["f"]
    assert len(o["tags"]) == 1 and o["tags"][0]["dash"] and o["tags"][0]["fill"] == NIGHT["--ink"] and o["words"] == []


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
  const tz = ops.filter((o) => o.k === "text" && o.t === TZ).map((o) => ({ x: o.x, y: o.y, w: 0.6 * 10 * TZ.length }));
  const dates = ops.filter((o) => o.k === "text" && /^\\d\\d-\\d\\d$/.test(o.t)).map((o) => ({ x: o.x, w: 30 }));
  const pins = ops.filter((o) => o.k === "fillRect" && o.w === 11 && o.h === 12).map((o) => ({ x: o.x, y: o.y, w: o.w, h: o.h }));
  const order = ops.filter((o) => o.k === "stroke" && o.pts.length === 2 && o.pts[0][1] === o.pts[1][1] && o.pts[1][0] === w - padR).map((o) => o.color);
  return { tags, lines, curves, labels, words, dots, tz, dates, pins, order, padR, w, yLive: ch.live ? Y(ch.live) : null,
    yStop: ch.stop ? Y(ch.stop) : null, beats: beats.length };
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
    """With a live price: a solid line in --live (--live-width wide: 2 px by day, 1.5 at night), ending in one tag at
    the right edge on its price, filled
    --live, its dot and "LIVE" and the price in --on-live, wholly inside the canvas. No word LIVE anywhere else (it no
    longer floats above the wicks). The newest close's tag is folded into it when that close is the live price, and
    drawn on its own (filled, as a closed candle's) when it isn't."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf(); ch.live = ch.candles[N - 1][4];
out.merged = frame(TOKS.{edition}, ch);
const c2 = chartOf(); c2.live = c2.candles[N - 1][4] - 700;
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
        assert ll == [{"y": f["yLive"], "color": live, "width": float(tok["--live-width"]), "dash": ""}]   # solid: the price now
    assert len(o["merged"]["tags"]) == 1                                         # its close is the live price: one tag
    assert len(o["apart"]["tags"]) == 2 and not _overlap(*o["apart"]["tags"])
    close = next(t for t in o["apart"]["tags"] if "LIVE" not in t["text"])
    assert close["fill"] in (tok["--green"], tok["--red"]) and not close["dash"]


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_the_live_lines_color_is_no_other_lines(edition):
    """The live line's color is the only one of its kind on the chart: not the fast (--cyan) or slow (--blue)
    average, the channel, the entry or the stop, the candles or the grid, as drawn and as the theme defines them, and
    far from each by eye (CIE76 delta E 25 or more); readable on the panel (3:1 as a line), its lettering 4.5:1. Apart
    from the stop's orange for a red-green-blind (deuteranope) eye too: delta E 15 or more as such an eye sees them
    (the day edition's first gold was 13)."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf({{ stop: 79000, entry: 81000 }}); ch.live = ch.candles[N - 1][4];
out.f = frame(TOKS.{edition}, ch);""")["f"]
    live = tok["--live"]
    others = {x["color"] for x in o["lines"] if x["color"] != live} | set(o["curves"])
    assert live not in others and len([x for x in o["lines"] if x["color"] == live]) == 1
    for name in ("--cyan", "--blue", "--channel", "--entry", "--orange", "--green", "--red", "--line", "--dim", "--pen"):
        assert tok[name] in others or name in ("--pen", "--green", "--red", "--dim"), name   # each is a line on this chart
        assert _de76(live, tok[name]) >= 25, (name, tok[name], live, _de76(live, tok[name]))
    assert _contrast(live, tok["--panel"]) >= 3 and _contrast(tok["--on-live"], live) >= 4.5
    assert _de76(live, tok["--orange"], deutan=True) >= 15, _de76(live, tok["--orange"], deutan=True)


# a deuteranope's view of a linear RGB color (Machado, Oliveira and Fernandes 2009, severity 1)
_DEUTAN = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))


def _lab(h: str, deutan: bool = False):
    c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    if deutan:
        r, g, b = [min(1.0, max(0.0, m[0] * r + m[1] * g + m[2] * b)) for m in _DEUTAN]
    x, y, z = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _de76(a: str, b: str, deutan: bool = False) -> float:
    return sum((p - q) ** 2 for p, q in zip(_lab(a, deutan), _lab(b, deutan))) ** 0.5


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
    assert 'css("--live") || "#ffd84a", +(css("--live-width") || 1.5));' in draw
    assert 'line(g, chart.ema_fast.map(' in draw and 'css("--cyan"), 1.6);' in draw
    assert 'rgba("--cyan", .7)' not in APP


def test_the_day_line_is_drawn_wider_where_its_gold_is_lighter():
    """By day the gold is the lightest line on the paper (3.1:1 on --panel; the averages, the channel, the entry and
    the stop are 5:1 and more), so the live line is drawn 2 px wide there (--live-width), wider than the averages'
    1.6, and 1.5 at night, where it is 11:1 on the paper. A darker day gold would not do: one at 3.8:1 (#977f00)
    comes within delta E 10 of the stop's orange for a red-green-blind eye. Its tag's fill stays the same gold."""
    lines = ("--cyan", "--blue", "--channel", "--entry", "--orange")
    assert all(_contrast(DAY["--live"], DAY["--panel"]) < _contrast(DAY[n], DAY["--panel"]) for n in lines)
    assert float(DAY["--live-width"]) == 2 > 1.6 and float(NIGHT["--live-width"]) == 1.5
    assert _contrast(NIGHT["--live"], NIGHT["--panel"]) >= 11
    assert "--live-width" not in STYLE                                            # the base page: 1.5, app.js's fallback


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
    are clear of both, and so is the run's held RUN tag, dashed as before. "MT" is always there: under the price axis,
    or just left of it when a tag takes that corner, clear of the tags and of the dates."""
    o = _draw(f"""
const side = "{side}";
for (const [name, d] of [["at", 0], ["above", 40], ["below", -40], ["near", 150]]) {{
  const ch = chartOf({{ side }}); ch.live = ch.candles[N - 1][4] - 300; ch.stop = ch.live + d; ch.entry = side === "long" ? ch.live + 900 : ch.live - 900;
  out[name] = frame(TOKS.night, ch);
}}
for (const [name, ext] of [["low", -1], ["high", 1]]) {{   // the stop the chart's extreme, the price on it
  const ch = chartOf({{ side }}); ch.stop = ext < 0 ? 77000 : 84000; ch.live = ch.stop;
  out[name] = frame(TOKS.night, ch);
}}
// the run's own candle held (RUN, dashed) a few px from the live price, the stop between them
const ch = chartOf({{ side, prov: [N - 1] }}); ch.runIdx = N - 1; ch.live = ch.candles[N - 1][4] - 30; ch.stop = ch.live + 15;
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
            assert all(abs((y - 3) - t["y"]) >= 13 for t in tags), (name, text, y)
        assert len(f["tz"]) == 1, name                                            # MT, once, never left out
        z = f["tz"][0]
        assert all(abs((z["y"] - 3) - t["y"]) >= 13 or z["x"] + z["w"] <= t["x"] for t in tags), (name, z, tags)
        assert all(d["x"] + d["w"] <= z["x"] for d in f["dates"]), (name, z, f["dates"])
    if side == "long":                                                           # the stop's tag in the corner: MT left of it
        assert o["low"]["tz"][0]["x"] + o["low"]["tz"][0]["w"] <= o["low"]["w"] - o["low"]["padR"]
    held = next(t for t in o["held"]["tags"] if t["text"].startswith("RUN "))
    assert held["dash"] and len(o["held"]["tags"]) == 3                          # still dashed, still there, clear


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_a_live_price_equal_to_the_runs_held_close_keeps_its_run_tag(edition):
    """The run's own candle is the newest (a window the page saw nothing of since) and Coinbase's price comes to equal
    its close exactly (BTC's $5 tick makes it likely): the live price doesn't carry that candle on, so its dashed RUN
    tag stays, the LIVE tag stacked clear of it on the price. It was folded into one LIVE tag on the held candle. A
    candle the feed did carry on to the price (formingBars: runIdx -1) still has the one LIVE tag."""
    tok = DAY if edition == "day" else NIGHT
    o = _draw(f"""
const ch = chartOf({{ prov: [N - 1] }}); ch.runIdx = N - 1; ch.live = ch.candles[N - 1][4];
out.run = frame(TOKS.{edition}, ch);
const fed = chartOf({{ prov: [N - 1] }}); fed.runIdx = -1; fed.live = fed.candles[N - 1][4];
out.fed = frame(TOKS.{edition}, fed);""")
    tags = o["run"]["tags"]
    assert len(tags) == 2 and not _overlap(*tags), tags
    live = next(t for t in tags if "LIVE" in t["text"])
    run = next(t for t in tags if t["text"].startswith("RUN "))
    assert run["dash"] and run["fill"] == tok["--ink"] and run["text"].split()[-1] == live["text"].split()[-1]
    assert abs(live["y"] - o["run"]["yLive"]) < 1e-6 and live["fill"] == tok["--live"]
    assert len(o["fed"]["tags"]) == 1 and "LIVE" in o["fed"]["tags"][0]["text"]


@node
@pytest.mark.parametrize("side", ["long", "short"])
def test_an_entry_pin_on_the_newest_candle_stays_clear_of_the_tags(side):
    """ETH's newest candle (about 1.5 px a slot on a phone) with an entry on it at the live price and the stop 2
    above (a short) or below (a long): the pin's box stays left of the tags past the axis (it reached 3 px under the
    STOP tag), its point still on the candle and the price."""
    o = _draw(f"""
const side = "{side}", M = 140;
const cs = Array.from({{ length: M }}, (_, k) => {{ const c = 2400 + 40 * Math.sin(k / 6); return [T0 + 2 * H * k, c - 3, c + 8, c - 8, c + 1]; }});
const ch = {{ bar_ms: 2 * H, candles: cs, ema_fast: cs.map((c) => c[4] + 10), ema_slow: cs.map((c) => c[4] - 12), don_hi: cs.map(() => 2460),
  don_lo: cs.map(() => 2340), prov: [], side, live: cs[M - 1][4], entry: cs[M - 1][4], stop: cs[M - 1][4] + (side === "short" ? 2 : -2),
  markers: [{{ ts: cs[M - 1][0], price: cs[M - 1][4], kind: "entry", side }}] }};
TOK = {{ ...TOKS.night, "--entry-mark": "pin" }};
out.f = frame(TOK, ch);
out.f.x = lastGeom.X(M - 1); out.f.points = ops.filter((p) => p.k === "fill" && p.pts.length === 1 && p.pts[0][2] === 1.8).map((p) => p.pts[0][0]);""")["f"]
    assert len(o["pins"]) == 1 and len(o["tags"]) == 2
    pin, edge = o["pins"][0], min(t["x"] for t in o["tags"])
    assert edge - (o["w"] - o["padR"]) == 2                                       # the tags start 2 px past the axis
    assert pin["x"] + pin["w"] <= edge, (pin, edge)
    assert o["points"] == [o["x"]]                                                # its point on the newest candle


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_a_stop_at_the_live_price_shows_over_the_live_line(edition):
    """A stop at the live price: its dashed line is drawn again over the solid live one (it was hidden under it), so
    both read on the chart while its tag is stacked past the live tag; a stop away from the price is drawn once,
    under the live line as before."""
    o = _draw(f"""
const a = chartOf({{ side: "long" }}); a.live = a.candles[N - 1][4] - 300; a.stop = a.live;
out.at = frame(TOKS.{edition}, a);
const b = chartOf({{ side: "long" }}); b.live = b.candles[N - 1][4] - 300; b.stop = b.live - 900;
out.apart = frame(TOKS.{edition}, b);""")
    tok = DAY if edition == "day" else NIGHT
    live, stop = tok["--live"], tok["--orange"]
    at = o["at"]["order"]
    assert at.count(stop) == 2 and at.index(live) < len(at) - 1 - at[::-1].index(stop)   # the stop's dashes on top
    assert o["apart"]["order"].count(stop) == 1 and o["apart"]["order"].index(stop) < o["apart"]["order"].index(live)


@node
@pytest.mark.parametrize("edition", ["day", "night"])
def test_an_entry_at_the_live_price_shows_over_the_live_line(edition):
    """The price back at the entry: the entry's dotted line is drawn again over the solid live one (under it, it was
    hidden), in the live tag's ink, which reads on the gold in both editions (the night entry's cream doesn't); an
    entry away from the price is drawn once, in its own color, under the live line as before."""
    o = _draw(f"""
const a = chartOf({{ side: "long" }}); a.live = a.candles[N - 1][4] - 300; a.entry = a.live;
out.at = frame(TOKS.{edition}, a);
const b = chartOf({{ side: "long" }}); b.live = b.candles[N - 1][4] - 300; b.entry = b.live - 900;
out.apart = frame(TOKS.{edition}, b);""")
    tok = DAY if edition == "day" else NIGHT
    live, entry, ink = tok["--live"], tok["--entry"], tok["--on-live"]
    at = [x for x in o["at"]["lines"] if abs(x["y"] - o["at"]["yLive"]) < 2]
    assert [(x["color"], x["dash"]) for x in at] == [(entry, "2,3"), (live, ""), (ink, "2,3")]   # the last one on top
    assert _contrast(ink, live) >= 3
    if edition == "night":
        assert _contrast(entry, live) < 1.5                                            # why not in its own color
    apart = o["apart"]["order"]
    assert apart.count(entry) == 1 and ink not in apart and apart.index(entry) < apart.index(live)
    assert "beat.dot" not in APP and "const beat = { el: null, price: 0 }," in APP     # (its dot only ever passed in)


@node
@pytest.mark.parametrize("held", [False, True])
def test_the_live_tags_room_is_kept_so_the_candles_never_move_sideways(held):
    """A chart that can have a live price (canLive: BTC's on Coinbase, ETH's, SOL's) keeps the live tag's room in its
    price axis while it has none (the proxy, stale, closed, Hyperliquid quiet), so the plot is as wide with the marker
    as without it and nothing jumps when it comes and goes; a chart that can't have one is as before (64 px at least)."""
    o = _draw(f"""
const held = {"true" if held else "false"};
const mk = (o) => {{ const ch = chartOf(held ? {{ prov: [N - 1] }} : {{}}); if (held) ch.runIdx = N - 1; return Object.assign(ch, o); }};
out.live = frame(TOKS.night, mk({{ canLive: true, live: chartOf().candles[N - 1][4] }}));
out.none = frame(TOKS.night, mk({{ canLive: true }}));
out.cannot = frame(TOKS.night, mk({{}}));""")
    assert o["live"]["padR"] == o["none"]["padR"] > o["cannot"]["padR"] >= 64
    assert o["none"]["words"] == [] and all(t["text"].count("LIVE") == 0 for t in o["none"]["tags"])   # room only, no marker


@node
def test_a_price_tag_rounds_the_cent_as_the_title_does():
    """pxAxis (the tags and the axis) rounds a price under 1,000 as px (the title, the ticker) does: SOL at 104.345 is
    "104.35" on its LIVE tag and in its title, never 104.34 on one and 104.35 on the other."""
    js = (_between(APP, "  const NF = new Map()", "\n") + "\n" + _between(APP, "  const usd =", "\n") + "\n"
          + _between(APP, "  const px =", "\n") + "\n" + _between(APP, "  const pxAxis =", "\n") + """
const bad = [];
for (let c = 1; c < 100000; c += 7) { const x = c / 100 + 0.005; if ("$" + pxAxis(x) !== px(x)) bad.push(x); }
for (let c = 2001; c < 300000; c += 13) { const x = c / 2; if ("$" + pxAxis(x) !== px(x)) bad.push(x); }
console.log(JSON.stringify({ bad: bad.slice(0, 5), sol: pxAxis(104.345), title: px(104.345), big: pxAxis(81475.5), low: pxAxis(0.5) }));""")
    r = _node(js)
    assert r["bad"] == [] and r["sol"] == "104.35" == r["title"][1:]
    assert r["big"] == "81,476" and r["low"] == "0.50"


# ---------------------------------------------------------------- the legend

@node
def test_the_legend_says_what_the_marker_is_and_wraps_between_entries():
    """'live' with a small tag swatch (● on --live), last, while the marker is drawn; on a chart that can have one
    (canLive) but has none just now, the same entry kept unseen (visibility hidden), so the legend has as many lines
    and the chart under it doesn't move when the price comes and goes; none at all on a chart that can't have one.
    Every entry whole in a nowrap span, the dot after it glued with a no-break space, so a phone breaks only after a
    '·'."""
    js = _function(APP, "legend") + """
console.log(JSON.stringify([legend({ live: 82290, canLive: true, prov: [139] }, "<span>24h +2.10%</span>"),
  legend({ canLive: true, prov: [] }, "<span>24h</span>"), legend({ prov: [] }, "<span>24h</span>")]));"""
    with_live, kept, without = _node(js)
    entries = ('<span class="lg"><span>24h +2.10%</span></span>&nbsp;· <span class="lg">fast <i class="swatch fast"></i></span>'
               '&nbsp;· <span class="lg">slow <i class="swatch slow"></i></span>&nbsp;· <span class="lg">channel '
               '<i class="swatch channel"></i></span>&nbsp;· <span class="lg">stop <i class="swatch stop"></i></span>'
               '&nbsp;· <span class="lg">newest candle <i class="swatch prov"></i> provisional</span>')
    entry = '&nbsp;· <span class="lg">live <i class="swatch live"></i></span></span>'
    assert with_live == entries + '<span class="lg-live">' + entry
    assert kept.endswith('<span class="lg-live off">' + entry) and "provisional" not in kept
    assert "live" not in without and "provisional" not in without
    assert ".chart-head .lg { white-space: nowrap; }" in STYLE
    assert re.search(r"\.chart-head \.lg-live\.off \{ visibility: hidden; \}", STYLE)
    sw = re.search(r"\.swatch\.live \{([^}]*)\}", STYLE).group(1)
    assert "var(--live)" in sw and "var(--on-live)" in sw
    assert 'put($("chart-sub"), legend(s.chart, ' in _function(APP, "renderStage")


def test_the_readme_says_what_the_marker_is():
    readme = (ROOT / "README.md").read_text()
    assert "cyan `LIVE` line" not in readme
    assert "a solid gold line ending in one `● LIVE 82,290` tag on the price at the right edge" in readme


# ---------------------------------------------------------------- the beat

def _beat() -> str:
    return _between(APP, "  const beat = { el: null", "  function liveBeat(") + _function(APP, "liveBeat")


@node
@pytest.mark.parametrize("reduced", [False, True])
def test_the_dot_beats_once_per_new_price_and_never_under_reduced_motion(reduced):
    """liveBeat: one short animation (0.5 s) each time the live price drawn moves, none for a redraw with the same price
    (a Coinbase message with the book moved and no trade, an allMids answer with the same mid, a board or a resize),
    none once the marker is gone (an animation in flight is cancelled), the next price after it is back beating again,
    and none at all under prefers-reduced-motion. No loop: no interval or animation frame; the page's own redraws (a
    Coinbase message, an allMids answer) drive it, and cbPaint's redraw-free path for the same price doesn't call it.
    At its biggest (1.6 times) the dot stays a pixel clear of the tag's "LIVE" (at 2 times it touched the L)."""
    js = """
const anims = [], cancelled = [], el = { style: {}, setAttribute() {}, className: "",
  animate: (k, o) => { anims.push([k.map((x) => x.transform), o.duration]); return {}; },
  getAnimations: () => [{ cancel: () => cancelled.push(1) }] };
const canvas = { offsetLeft: 6, offsetTop: 40, animate() {}, after() {} };
const $ = () => canvas, document = { createElement: () => el };
const matchMedia = () => ({ matches: %s });
""" % ("true" if reduced else "false") + _beat() + """
liveBeat(82290.5, [10, 20]); liveBeat(82290.5, [10, 20]); liveBeat(82290.5, [10, 22]);   // the same price, redrawn
liveBeat(82290.7, [10, 20]);                                                             // it moved (a cent)
liveBeat(0, null);                                                                       // the marker gone: proxy, stale, closed
liveBeat(82290.7, [10, 25]);                                                             // back: a beat
console.log(JSON.stringify({ anims, cancelled: cancelled.length, left: el.style.left, top: el.style.top }));"""
    r = _node(js)
    if reduced:
        assert r["anims"] == []
    else:
        assert len(r["anims"]) == 3 and all(d == 500 for _, d in r["anims"])
        assert r["anims"][0][0] == ["scale(1)", "scale(1.6)", "scale(1)"] and r["left"] == "16px" and r["top"] == "65px"
        assert r["cancelled"] == 1
    tag = _function(APP, "liveTag")                                                # the dot at x + 7.5, r 2.5; LIVE at x + 13
    assert "g.arc(x + 7.5, y, 2.5, 0, Math.PI * 2)" in tag and 'g.fillText("LIVE", x + 13, y + 3.5)' in tag
    peak = max(float(m) for m in re.findall(r'transform: "scale\(([\d.]+)\)"', _function(APP, "liveBeat")))
    assert 7.5 + 2.5 * peak + 1 <= 13, peak
    for loop in ("setInterval", "requestAnimationFrame", "setTimeout", "infinite"):
        assert loop not in _function(APP, "liveBeat"), loop
    assert re.search(r"\.live-beat \{[^}]*opacity: 0;", STYLE)                    # nothing at rest
    reduce = STYLE[STYLE.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert ".live-beat { display: none; }" in reduce
    assert "liveBeat" not in _function(APP, "cbPaint")                             # the same Coinbase price: no beat
    assert "liveBeat(chart.live, liveTag(g, w - padR, ty[0], chart.live));" in _function(APP, "drawChart")


# ---------------------------------------------------------------- a quiet Hyperliquid

def test_a_quiet_hyperliquid_takes_live_off_everywhere_with_one_timer():
    """ETH, SOL and VT: one setTimeout to the moment Hyperliquid's last answer turns STALE (20 s), reset by each redraw,
    so when it goes quiet the chart's marker, the ticker's LIVE (STALE then, as the proxy's on BTC), the page's has-live
    glow and the footer all change together; nothing runs while prices keep coming. Each allMids ask counts the
    footer's age first (test_the_footer_counts_its_age_while_hyperliquid_hangs). BTC's Coinbase price and the proxy:
    cbWatch already redraws when either stops being fresh."""
    render = _function(APP, "render")
    assert "clearTimeout(liveDue);" in render
    assert ("if (!s.venue && live.at && !hlStale(Date.now())) liveDue = setTimeout(() => render(true), "
            "Math.max(0, live.at + STALE - Date.now()) + 50);") in render
    assert ": !!live.at && !hlStale(Date.now()));" in render                    # has-live: off once stale
    assert "const hlStale = (now) => !!live.at && near(now, live.at) > STALE;" in APP
    assert 'if (!v) return { source: "hyperliquid", price: hlMid, at: live.at, stale: hlStale(now) };' in _function(APP, "livePrice")
    assert ': lp.stale ? `STALE (HYPERLIQUID${v ? " PROXY" : ""}) · `' in _function(APP, "renderStage")
    assert 'hlStale(now) ? "STALE (HYPERLIQUID) · "' in _function(APP, "renderBasket")
    assert "!s.venue && hlStale(Date.now()) ? ` · stale price: Hyperliquid's last came ${secs(live.at)}s ago`" in _function(APP, "foot")
    assert "if (state && !state.venue) txt($(\"foot\"), foot(state));" in _function(APP, "pollMids")
    assert "if (cbMode(livePrice(on, now)) !== cb.mode) cbPaint(true);" in _function(APP, "cbWatch")


@node
def test_the_footer_says_stale_once_hyperliquid_is_quiet():
    """ETH's footer: "live price 3s ago from Hyperliquid" while it answers, "stale price: Hyperliquid's last came 25s
    ago" once it hasn't for over 20 s, never "live" then."""
    js = ("const DAY_MS = 86400000;\n" + _between(APP, "  const deskMT = (() => {", "  window.deskMT = deskMT;")
          + "const MT = deskMT, TZ = deskMT.zone, hhmm = (ms) => MT.clock(ms), missing = null;\n"
          + _between(APP, "  const STALE = 20000;", "  const CB_SKEW") + _between(APP, "  const near =", "  // quiet: the ticker")
          + _function(APP, "foot") + _function(APP, "provNote") + """
const live = { mids: { ETH: "2441.5" }, at: Date.now() - 3000 };
const s = { header: { coin: "ETH" }, updated: Date.now() - 60000, livePx: {}, chart: { prov: [] }, venue: null };
const a = foot(s); live.at = Date.now() - 25000; const b = foot(s);
console.log(JSON.stringify([a, b]));""")
    a, b = _node(js)
    assert " · live price 3s ago from Hyperliquid · " in a
    assert " · stale price: Hyperliquid's last came 25s ago · " in b and "live price" not in b


@node
def test_the_footer_counts_its_age_while_hyperliquid_hangs():
    """ETH's, SOL's and VT's footer is rewritten at each allMids ask (every 5 s), before the answer is awaited: with
    Hyperliquid answering nothing at all (a request that hangs) it counts 5, 10, 15 s and then says stale, where it
    stayed at "0s ago" until the 20 s timer. BTC's tab is left to cbWatch, once a second."""
    js = "async " + _function(APP, "pollMids") + """
const feet = [], state = { venue: null }, base = null, told = null, live = { mids: {}, at: 1 };
let n = 0;
const $ = () => ({}), txt = (el, t) => feet.push(t), foot = () => `age ${++n}`;
const hl = () => new Promise(() => {});                                  // asked, never answered
const render = () => feet.push("render"), ticked = () => {}, cbPaint = () => {}, livePrice = () => ({});
pollMids(); pollMids();
state.venue = { name: "coinbase" }; pollMids();
setTimeout(() => console.log(JSON.stringify(feet)), 20);"""
    assert _node(js) == ["age 1", "age 2"]
