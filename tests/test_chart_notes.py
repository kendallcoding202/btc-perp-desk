"""The agents' notes off the chart, and each card pointing at what its note is about. Each test fails on 3fd4a61.

The owner, on a laptop, read the agents' caption boxes on BTC's chart (SCAN, VET, CROWD, RISK, SIZE, FILLS) as noise:
they sat at fixed spots over the plot (BUBBLE_SPOTS), unrelated to what they mentioned, covering candles, and said
what the cards already said, cut to one line there. Now nothing sits over the plot at any width (the chief's line
stays beside the chart), a card's whole note wraps on the card, and hovering a card, tabbing to it or tapping it marks
on the chart the thing its note is about, only when the chart draws it: SCAN's 60-bar line with the price its note
gives (and the next close's, when the line steps at the candle still forming), VET's fast and slow averages, RISK's and
SIZE's stop and entry (a position only), FILLS' fills; CROWD, CHIEF and CRAWLER point at nothing. The mark is drawn on
a canvas of its own, clipped to the plot: the chart's own frame, its values, lines and tags, and the live marker are as
they were, and the gold live line shows on top of the mark, with no name on it nor on another line marked. A phone's
tap that brings the chart into view takes the page back to the card when the mark ends; switching accounts ends it. The spider's web is drawn under the chart's canvas,
fainter, so the candles and lines are always drawn over its silk. The page's own functions are run in node, drawing on
a canvas that records what it is asked to draw (tests/test_live_marker.py's), with the comic theme's day and night
tokens."""

from __future__ import annotations

import json
import re

from test_dashboard_phone_words import _media, _rule, _top
from test_live_marker import APP, DAY, NIGHT, ROOT, STYLE, THEME, _between, _draw_js, _function, _node, node

HTML = (ROOT / "dashboard" / "index.html").read_text()
FILTER = (ROOT / "desk" / "filter.py").read_text()

H1 = "nothing yet. waiting for a close above the 60-bar high $82,500."
L1 = "nothing yet. waiting for a close below the 60-bar low $78,000."


def _point_js() -> str:
    """drawChart's harness, plus the pointing code, a #point that is the same recording canvas (fit() clears it, so
    after drawPoint the ops are the point's alone), the agent cards, a page state, and timers that only record."""
    return (_draw_js() + _between(APP, "  const COLORS = {", "  // ---- pixel sprites")
            + _between(APP, "  const POINTS = {", "  function referent(") + _function(APP, "referent")
            + _between(APP, "  const point = { key", "  function drawPoint(") + _function(APP, "drawPoint")
            + _function(APP, "pointTag") + _function(APP, "pointAt") + _function(APP, "tapped") + _function(APP, "aim") + """
let state = null, coin = "BTC";
const innerHeight = 800, tr = [], timers = [], cleared = [];
const setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; }, clearTimeout = (t) => { if (t) cleared.push(t); };
Object.defineProperty(g, "globalCompositeOperation", { get() { return cur.gco || "source-over"; }, set(v) { cur.gco = v; } });
g.translate = (x, y) => { tr.push([x, y]); };
const _stroke = g.stroke, _fill = g.fill;
g.stroke = function () { _stroke.call(this); ops[ops.length - 1].gco = g.globalCompositeOperation; };
g.fill = function () { _fill.call(this); ops[ops.length - 1].gco = g.globalCompositeOperation; };
canvas.classList = { on: false, toggle(c, v) { this.on = !!v; } };
g.rect = function (x, y, w, h) { this._pts.push([x, y, w, h]); };
g.clip = function () { ops.push({ k: "clip", rect: this._pts[0] }); };
canvas.rect = { top: 100, bottom: 360, height: 260 }; canvas.getBoundingClientRect = () => canvas.rect;
canvas.scrolled = []; canvas.scrollIntoView = (o) => canvas.scrolled.push(o);
let scrollY = 2600; const scrolls = [], scrollTo = (o) => scrolls.push(o);
document.documentElement = { scrollHeight: 4000 };
const KEYS = ["chief", "scan", "vet", "crowd", "size", "fills", "risk", "crawler"];
const CARDS = KEYS.map((key) => ({ id: `card-${key}`, attrs: {}, hasAttribute(a) { return a in this.attrs; },
  removeAttribute(a) { delete this.attrs[a]; }, set tabIndex(v) { this.attrs.tabindex = v; } }));
document.querySelectorAll = () => CARDS;
const tabbable = () => CARDS.filter((c) => c.hasAttribute("tabindex")).map((c) => c.id.slice(5));
const agents = (scan) => KEYS.map((key) => ({ key, name: key.toUpperCase(), quote: key === "scan" ? scan : "a note" }));
const desk = (ch, scan, tok) => { TOK = tok; drawChart(ch); state = { header: { kind: "desk" }, agents: agents(scan), chart: ch }; };
const refs = (ch, scan, tok = TOKS.day) => {
  desk(ch, scan, tok);
  return Object.fromEntries(KEYS.map((k) => { const r = referent(k, state);
    return [k, r && r.map((x) => x.mark ? { mark: true, x: x.x, y: x.y, r: x.r } : { name: x.name, x: x.x, y: x.y, color: x.color,
      dash: (x.dash || []).join(","), series: !!x.xs, level: x.at != null })]; }));
};
const pos = (o = {}) => chartOf({ stop: 79000, entry: 81000, markers: [
  { ts: T0 + 2 * H * 10, price: 80200, kind: "entry", side: "long" }, { ts: T0 + 2 * H * 20 + 600000, price: 81000, kind: "exit", side: "long", pnl: 5 },
  { ts: T0 - 10 * H, price: 80000, kind: "entry", side: "long" }], ...o });
// the board's chart (decided: its newest candle) with one candle still forming past it, on the next close's line
const forming = (next, o = {}, lo = false) => { const ch = chartOf({ decided: N - 1, ...o }); ch.candles.push([T0 + 2 * H * N, 80000, 80100, 79900, 80050]);
  ch.ema_fast.push(null); ch.ema_slow.push(null); ch.don_lo.push(lo ? next : null); ch.don_hi.push(lo ? null : next); return ch; };
// what #point drew: its name boxes' middles, in the order drawn, and the names
const names = () => ({ boxes: ops.filter((x) => x.k === "fillRect" && x.h === 16 && x.w > 4).map((x) => x.y + 8), names: ops.filter((x) => x.k === "text").map((x) => x.t) });
""")


def _run(cases: str) -> dict:
    return _node(_point_js() + "const TOKS = " + json.dumps({"day": DAY, "night": NIGHT}) + ";\nconst out = {};\n" + cases
                 + "\nconsole.log(JSON.stringify(out));")


# ---------------------------------------------------------------- nothing over the chart

def test_no_caption_box_over_the_chart_and_the_chiefs_line_stays_beside_it():
    """The six boxes are gone at every width: no #bubbles over the plot, no code that placed them, no CSS for them. The
    chart's box holds its head, its canvas, the web's (under it) and the point's (empty until a card points). The
    chief's line is still the chief's quote, in the column beside the chart (narrow windows hide it, as before)."""
    wrap = _between(HTML, '<div class="chart-wrap">', '<aside class="stage-right">')
    assert re.findall(r'id="([\w-]+)"', wrap) == ["chart-title", "chart-sub", "chart", "web", "point"]
    assert "bubble" not in wrap
    right = _between(HTML, '<aside class="stage-right">', "</aside>")
    assert '<div class="bubble static" id="chief-bubble">' in right
    for gone in ("BUBBLE_SPOTS", "renderBubbles", "plotBubbles", '$("bubbles")'):
        assert gone not in APP, gone
    assert '$("chief-bubble").textContent = chief ? chief.quote : "";' in _function(APP, "renderAgents")
    for css in (STYLE, THEME):
        sels = re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        assert not [s for s in sels if re.search(r"\.bubbles?\b(?!\.static)", s)], css[:40]
    assert re.search(r"\.sprite-slot, \.bubble\.static \{ display: none; \}", _media(STYLE, "(max-width: 980px)"))
    assert "agent cards, bubbles" not in (ROOT / "README.md").read_text()


def test_a_cards_whole_note_wraps_on_the_card():
    """A desktop card cut its note to one line with an ellipsis (the boxes over the chart had the rest): it wraps now,
    at every width over a phone's, and nothing later cuts it back to a line; the quote carries no hover title for a
    cut it no longer has. A phone keeps its few-line cut (overflow hidden is that cut's)."""
    q = _rule(_top(STYLE), ".card .quote")
    for d in ("nowrap", "ellipsis"):
        assert d not in q, d
    assert "overflow: hidden" in q and "overflow-wrap: anywhere" in q
    for css in (_media(STYLE, "(max-width: 980px)"), THEME):
        for body in re.findall(r"\.card \.quote\s*\{([^}]*)\}", css):
            assert "nowrap" not in body and "ellipsis" not in body, body
    assert re.search(r"-webkit-line-clamp: 3;", _rule(_media(STYLE, "(max-width: 560px)"), ".card .quote"))
    agents = _function(APP, "renderAgents")
    assert '.querySelector(".quote").title' not in agents
    assert 'el.querySelector(".quote").textContent = `"${a.quote}"`;' in agents


# ---------------------------------------------------------------- what each card points at

@node
def test_each_card_points_only_at_what_its_note_names_and_the_chart_draws():
    """SCAN: the 60-bar line its note names (the high, or the low a short waits on), named with its price; nothing while
    warming up or with shorts disabled. VET: the fast and slow averages, while drawn. RISK and SIZE: the stop and the
    entry, with a position only, and an entry off the plot isn't pointed at; the stop is left unnamed (its own STOP tag
    on the axis names it), the entry is named. FILLS: each fill drawn (one before the chart's first candle isn't), an
    entry's mark where its pin hangs (the newest candle's pin box held clear of the axis, as entryPin draws it), an
    exit's on its dot. CROWD, CHIEF and CRAWLER: nothing, ever."""
    o = _run(f"""
out.flat = refs(chartOf(), {json.dumps(H1)}); out.yHi = lastGeom.Y(82500);
out.low = refs(chartOf(), {json.dumps(L1)}); out.yLo = lastGeom.Y(78000);
out.warm = refs(chartOf(), "warming up the averages.");
out.noshort = refs(chartOf(), "nothing yet. shorts disabled.");
out.pos = refs(pos(), {json.dumps(H1)});
out.y = {{ stop: lastGeom.Y(79000), entry: lastGeom.Y(81000), e10: lastGeom.Y(80200), x10: lastGeom.X(10), x20: lastGeom.X(20), x21: lastGeom.Y(81000),
  edge: lastGeom.w - lastGeom.padR - 4 }};
out.far = refs(chartOf({{ stop: 79000, entry: 120000 }}), {json.dumps(H1)});
const cold = chartOf(); ["ema_fast", "ema_slow", "don_hi"].forEach((k) => {{ cold[k] = cold[k].map(() => null); }});
out.cold = refs(cold, {json.dumps(H1)});
out.night = refs(pos(), {json.dumps(H1)}, TOKS.night);
out.newest = refs(pos({{ markers: [{{ ts: T0 + 2 * H * (N - 1), price: 80000, kind: "entry", side: "long" }}] }}), {json.dumps(H1)});
out.nx = {{ x: lastGeom.X(N - 1), edge: lastGeom.w - lastGeom.padR - 4 }};""")
    for case in ("flat", "low", "warm", "noshort", "pos", "far", "cold", "night"):
        for k in ("chief", "crowd", "crawler"):
            assert o[case][k] is None, (case, k)
    scan = o["flat"]["scan"]
    assert len(scan) == 1 and scan[0]["series"] and scan[0]["name"] == "60-bar high 82,500"
    assert abs(scan[0]["y"] - o["yHi"]) < 1e-6 and scan[0]["color"] == DAY["--channel"] and scan[0]["dash"] == "3,4"
    assert o["low"]["scan"][0]["name"] == "60-bar low 78,000" and abs(o["low"]["scan"][0]["y"] - o["yLo"]) < 1e-6
    assert o["warm"]["scan"] is None and o["noshort"]["scan"] is None and o["cold"]["scan"] is None
    assert [(x["name"], x["color"]) for x in o["flat"]["vet"]] == [("fast", DAY["--cyan"]), ("slow", DAY["--blue"])]
    assert o["cold"]["vet"] is None
    for k in ("risk", "size", "fills"):                                          # flat, no fills drawn: nothing
        assert o["flat"][k] is None, k
    y = o["y"]
    for k in ("risk", "size"):
        got = o["pos"][k]
        assert [(x["name"], x["dash"], x["level"]) for x in got] == [(None, "6,4", True), ("entry 81,000", "2,3", True)]
        assert abs(got[0]["y"] - y["stop"]) < 1e-6 and abs(got[1]["y"] - y["entry"]) < 1e-6
        assert got[0]["x"] == got[1]["x"] == y["edge"]                            # named at the plot's end, short of the axis
        assert got[0]["color"] == DAY["--orange"] and got[1]["color"] == DAY["--entry"]
    assert [(x["dash"], abs(x["y"] - y["stop"]) < 1e-6) for x in o["far"]["risk"]] == [("6,4", True)]   # the entry is off the plot: not it
    fills = o["pos"]["fills"]
    assert [f["mark"] for f in fills] == [True, True]                             # the one before the window: not drawn
    assert abs(fills[0]["x"] - y["x10"]) < 1e-6 and abs(fills[0]["y"] - (y["e10"] + 9)) < 1e-6 and fills[0]["r"] == 12   # the L pin
    assert abs(fills[1]["x"] - y["x20"]) < 1e-6 and abs(fills[1]["y"] - y["x21"]) < 1e-6 and fills[1]["r"] == 8           # the dot
    assert o["night"]["risk"][1]["color"] == NIGHT["--entry"]
    nx = o["nx"]
    assert nx["x"] > nx["edge"] and [f["x"] for f in o["newest"]["fills"]] == [nx["edge"]]   # the newest pin's box, held clear


@node
def test_scans_price_is_the_decided_bars_and_a_stepped_next_close_is_named_too():
    """SCAN's note gives the line at the bar the desk decided on (desk/filter.py: don_hi[i]); a candle still forming past
    it (BTC's formingBars, ETH's and SOL's Hyperliquid candle) is drawn on the next close's line, analysis.outlook[0],
    which steps when an old high ages out or the bar's wick went over it. SCAN's mark names the decided bar's price, the
    one its note gives, at that bar, and when the forming candle's line has stepped, also "next close" with that price
    at the line's end (the BUY LINE panel's words): the card and its mark never give two prices for one line unsaid.
    One line is drawn either way. A board drawn before chart.decided (or without one) names the line's end, as before.
    withLive marks chart.decided before it adds any forming candle."""
    o = _run(f"""
out.step = refs(forming(82100), {json.dumps(H1)});
out.g = {{ hi: lastGeom.Y(82500), next: lastGeom.Y(82100), xd: lastGeom.X(N - 1), xn: lastGeom.X(N) }};
out.same = refs(forming(82500), {json.dumps(H1)});
out.none = refs(forming(null), {json.dumps(H1)});
point.key = "scan"; desk(forming(82100), {json.dumps(H1)}, TOKS.night); drawPoint();
out.ops = ops.filter((x) => (x.k === "stroke" && x.width === 2.5) || x.k === "text").map((x) => x.k === "text" ? x.t : "line");
out.ys = [...new Set(ops.filter((x) => x.k === "fillRect" && x.h === 16).map((x) => x.y + 8))];""")
    g = o["g"]
    step = o["step"]["scan"]
    assert [(x["name"], x["series"]) for x in step] == [("60-bar high 82,500", True), ("next close 82,100", False)]
    assert abs(step[0]["x"] - (g["xd"] - 2)) < 1e-6 and abs(step[0]["y"] - g["hi"]) < 1e-6         # at the decided bar
    assert abs(step[1]["x"] - (g["xn"] - 2)) < 1e-6 and abs(step[1]["y"] - g["next"]) < 1e-6       # at the line's end
    for case in ("same", "none"):                                                   # not stepped, or not drawn past it
        got = o[case]["scan"]
        assert [x["name"] for x in got] == ["60-bar high 82,500"] and abs(got[0]["x"] - (g["xd"] - 2)) < 1e-6, case
    assert o["ops"] == ["line", "60-bar high 82,500", "next close 82,100"]          # one line, two names
    assert abs(o["ys"][0] - o["ys"][1]) >= 17                                       # stacked, not over each other
    wl = _function(APP, "withLive")
    mark = wl.index("s.chart.decided = s.chart.candles.length - 1;")
    assert mark < wl.index("formingBars(s, Date.now())") < wl.index("ch.candles.push(c);")


def test_scans_mark_follows_the_words_the_desk_writes():
    """SCAN's mark is the line its note names, read from the notes desk/filter.py writes: "a close above the 60-bar
    high $84,295" (or below the low; whole dollars, ',.0f'), or "breakout: 60-bar high taken out". A bull trap's
    "broke the 60-bar high 3 bars ago, now back below $X" names an older bar's line, not the one drawn at the bar the
    desk decided on: that wording isn't read."""
    assert "f\"a close {'above' if d > 0 else 'below'} the {s.breakout_bars}-bar {hi_lo} ${level:,.0f}\"" in FILTER
    assert 'f"breakout: {s.breakout_bars}-bar {hi_lo} taken out"' in FILTER
    assert 'hi_lo = "high" if d > 0 else "low"' in FILTER
    assert 'f"bull trap: broke the {s.breakout_bars}-bar high {i - j} bars ago, now back below ${lvl:,.0f}"' in FILTER
    rx = r'/(\d+)-bar (high|low) (?:\$([\d,]+(?:\.\d+)?)|taken out)/'
    assert rx + '.exec(a.quote || "")' in APP
    py = re.compile(rx[1:-1])
    assert py.search("nothing yet. waiting for a close above the 60-bar high $84,295.").groups() == ("60", "high", "84,295")
    assert py.search("breakout: 60-bar low taken out. that's a setup.").groups() == ("60", "low", None)
    assert not py.search("bull trap: broke the 60-bar high 3 bars ago, now back below $84,295. that's a setup.")


@node
def test_scans_name_says_the_price_as_its_note_does_and_a_note_about_another_line_points_at_nothing():
    """The name gives the note's own figure: a coin under $1,000 has its line at 113.39 and its note says $113 (the
    chart's axis would say 113.39), so the name says 113. A note that names no price ("taken out") is named with the
    line's. A note whose price isn't the line's at the decided bar (more than the half dollar its rounding can be off
    by, either way Python rounds a half) points at nothing, as does a bull trap's note."""
    o = _run(f"""
const small = (f, o = {{}}) => {{ const ch = chartOf(o); ch.candles = ch.candles.map(([t, ...p]) => [t, ...p.map((v) => v * f)]);
  ["ema_fast", "ema_slow", "don_hi", "don_lo"].forEach((k) => {{ ch[k] = ch[k].map((v) => v * f); }}); return ch; }};
out.sol = refs(small(113.39 / 82500), "nothing yet. waiting for a close above the 60-bar high $113.");
out.solLo = refs(small(113.39 / 82500), "nothing yet. waiting for a close below the 60-bar low $107.");   // 78,000 scaled: 107.20
out.taken = refs(chartOf(), "breakout: 60-bar high taken out. that's a setup.");
out.half = refs(chartOf({{ don_hi: Array(N).fill(82500.5) }}), {json.dumps(H1)});
out.off = refs(chartOf(), "nothing yet. waiting for a close above the 60-bar high $82,499.");
out.trap = refs(chartOf(), "bull trap: broke the 60-bar high 3 bars ago, now back below $82,500. that's a setup.");
out.axis = pxAxis(113.39);""")
    assert o["axis"] == "113.39"
    assert [x["name"] for x in o["sol"]["scan"]] == ["60-bar high 113"]
    assert [x["name"] for x in o["solLo"]["scan"]] == ["60-bar low 107"]
    assert [x["name"] for x in o["taken"]["scan"]] == ["60-bar high 82,500"]
    assert [x["name"] for x in o["half"]["scan"]] == ["60-bar high 82,500"]       # 82,500.5: Python's ',.0f' says 82,500
    assert o["off"]["scan"] is None and o["trap"]["scan"] is None


# ---------------------------------------------------------------- how it is drawn

@node
def test_the_mark_is_drawn_on_its_own_canvas_and_leaves_the_chart_and_live_marker_as_drawn():
    """Pointing never redraws or changes the chart's own canvas: its frame is the same with a card pointing. On #point,
    shifted to the chart's place and clipped to the plot before anything is drawn (never the price axis's column, its
    tags and the live one's beat, never the date axis): the plot washed pale, and last the live line's own pixels cut out
    of it all (a cut as wide as the line, so no candle shows through beside it), the line and its tag exactly as drawn;
    the thing redrawn 2.5 px wide in its own color and dashes over an 8 px halo in the pointing card's color; the entry
    named in a caption box at its end (the stop has its own tag on the axis). A fill on the newest candle at the live
    price is ringed inside the plot only. A card that points at nothing draws nothing (what was drawn fades out as it
    was); nor does a card on the VT basket's tab. The cards that point can be tabbed to, the others can't."""
    o = _run("""
const ch = pos(); ch.live = ch.candles[N - 1][4];
desk(ch, %s, TOKS.night);
const before = JSON.stringify(ops);
point.key = "risk"; drawPoint();
out.risk = { on: canvas.classList.on, ops: ops.slice(), tab: tabbable() };
drawChart(ch); out.same = before === JSON.stringify(ops);
out.g = { pw: lastGeom.w - lastGeom.padR, bot: lastGeom.h - 18, yLive: lastGeom.Y(ch.live), yStop: lastGeom.Y(79000), yEntry: lastGeom.Y(81000) };
point.key = "fills"; drawPoint(); out.fills = ops.slice(); out.saved = st.length;
const newFill = pos({ markers: [{ ts: T0 + 2 * H * (N - 1), price: ch.live, kind: "entry", side: "long" }] }); newFill.live = ch.live;
desk(newFill, %s, TOKS.night); point.key = "fills"; drawPoint(); out.newest = ops.slice(); out.npw = lastGeom.w - lastGeom.padR;
desk(ch, %s, TOKS.night);
point.key = "crowd"; ops.length = 0; ops.push({ k: "kept" }); drawPoint(); out.crowd = { on: canvas.classList.on, kept: ops.length === 1 };
state = { ...state, header: { kind: "basket" } }; point.key = "risk"; drawPoint(); out.basket = { on: canvas.classList.on, tab: tabbable() };
desk(chartOf(), %s, TOKS.day); point.key = null; drawPoint(); out.flatTab = tabbable();
out.tr = tr;""" % ((json.dumps(H1),) * 4))
    g, ops = o["g"], o["risk"]["ops"]
    assert o["same"] and o["risk"]["on"] and o["tr"][0] == [6, 40]                  # the chart's place in its box
    assert ops[0] == {"k": "clip", "rect": [0, 0, g["pw"], g["bot"]]}               # the plot, before anything is drawn
    assert o["saved"] == 0                                                         # and let go of after (save / restore)
    wash = [x for x in ops if x["k"] == "fillRect" and x["x"] == 0 and x["y"] == 0]
    assert wash == [{"k": "fillRect", "color": "rgba(26,32,48,0.6)", "x": 0, "y": 0, "w": g["pw"], "h": g["bot"]}]   # night's --panel
    live = [x for x in ops if x["k"] == "stroke" and x["gco"] == "destination-out"]
    assert [(x["pts"], x["width"]) for x in live] == [([[0, g["yLive"]], [g["pw"], g["yLive"]]], float(NIGHT["--live-width"]))]   # its own pixels
    wide = [(x["pts"][0][1], x["color"], x["dash"]) for x in ops if x["k"] == "stroke" and x["width"] == 2.5]
    assert wide == [(g["yStop"], NIGHT["--orange"], "6,4"), (g["yEntry"], NIGHT["--entry"], "2,3")]
    halo = [x["color"] for x in ops if x["k"] == "stroke" and x["width"] == 8]
    assert halo == ["rgba(213,138,0,0.3)"] * 2                                     # RISK's own --orange
    assert [x["t"] for x in ops if x["k"] == "text"] == ["entry 81,000"]            # the stop: its own STOP tag names it
    for x in ops:                                                                  # all of it inside the plot
        if x["k"] in ("fillRect", "strokeRect"):
            assert x["x"] >= 0 and x["x"] + x["w"] <= g["pw"] + 1e-6 and x["y"] + x["h"] <= g["bot"] + 1e-6, x
        if x["k"] == "stroke":
            assert all(p[0] <= g["pw"] + 1e-6 for p in x["pts"]), x
        if x["k"] == "text":
            assert x["x"] < g["pw"], x
    assert sorted(o["risk"]["tab"]) == ["fills", "risk", "scan", "size", "vet"]
    cuts = [x for x in o["fills"] if x["k"] == "fill" and x["gco"] == "destination-out"]
    rings = [x for x in o["fills"] if x["k"] == "stroke" and x["width"] == 2]
    assert [c["pts"][0][2] for c in cuts] == [12, 8] and [r["color"] for r in rings] == [NIGHT["--ice"]] * 2   # each fill unwashed, ringed
    assert not [x for x in o["fills"] if x["k"] == "text"]
    nw = o["newest"]                                                               # the newest candle's fill at the live price
    assert nw[0] == {"k": "clip", "rect": [0, 0, o["npw"], g["bot"]]}
    ring = [x["pts"][0] for x in nw if x["k"] == "stroke" and x["width"] == 2]
    assert len(ring) == 1 and ring[0][0] == o["npw"] - 4 and ring[0][0] + ring[0][2] > o["npw"]   # it reaches the axis: the clip stops it
    assert o["crowd"] == {"on": False, "kept": True}
    assert o["basket"] == {"on": False, "tab": []}
    assert sorted(o["flatTab"]) == ["scan", "vet"]                                 # flat, no fills: those three can't be tabbed to


@node
def test_the_live_line_shows_on_top_of_a_mark_and_no_name_sits_on_it():
    """The chart draws its gold live line over everything, and a mark on #point over it hid it: an entry or a 60-bar
    line at or near the price, just after an entry or with a breakout about to happen, is when it matters most. The
    live line's own pixels are cut out of the mark last, after its lines, rings and names, so the chart's line shows
    on top as drawn; and a name's box keeps 9 px clear of the live line: above its line as before, under it when the
    live line runs just above (the box would reach it), past the live line when there's no room under at the plot's
    foot. RISK with the entry just under, just over and at the live price, and under it at the plot's foot; SCAN with
    the 60-bar high just under the price; VET with both averages under it; FILLS with a fill on the live line."""
    o = _run("""
const run = (key, ch, scan = %s) => {
  point.key = key; desk(ch, scan, TOKS.day); drawPoint();
  const cut = ops[ops.length - 1], yl = lastGeom.Y(ch.live);
  return { yl, pw: lastGeom.w - lastGeom.padR, h: lastGeom.h, cut: { gco: cut.gco, k: cut.k, pts: cut.pts, width: cut.width },
    cuts: ops.filter((x) => x.gco === "destination-out" && x.k === "stroke").length,
    boxes: ops.filter((x) => x.k === "fillRect" && x.h === 16 && x.w > 4).map((x) => x.y + 8),
    lines: referent(key, state).filter((r) => r.name).map((r) => r.y) };
};
const live = (p, o) => { const ch = pos(o); ch.live = p; return ch; };
out.under = run("risk", live(81100));
out.over = run("size", live(80900));
out.at = run("risk", live(81000));
out.foot = run("risk", live(78100, { stop: null, entry: 78060 }));
const sc = chartOf(); sc.live = 82600; out.scan = run("scan", sc);
const vt = chartOf(); vt.live = vt.ema_fast[N - 1] + 60; out.vet = run("vet", vt);
const fl = pos(); fl.live = 81000; out.fills = run("fills", fl);""" % json.dumps(H1))
    lw = float(DAY["--live-width"])
    for case, r in o.items():
        yl = r["yl"]
        assert r["cut"] == {"gco": "destination-out", "k": "stroke", "pts": [[0, yl], [r["pw"], yl]], "width": lw}, case   # last
        assert r["cuts"] == 1, case
        assert all(abs(c - yl) >= 17 - 1e-6 for c in r["boxes"]), (case, yl, r["boxes"])   # the box 9 px clear of it
        assert all(abs(a - b) >= 17 - 1e-6 for a in r["boxes"] for b in r["boxes"] if a != b), case
    (b,), (e,) = o["under"]["boxes"], o["under"]["lines"]
    assert e > o["under"]["yl"] and b - 8 > e                                     # the entry just under: its name under it
    for case in ("over", "at"):
        (b,), (e,) = o[case]["boxes"], o[case]["lines"]
        assert e <= o[case]["yl"] and b + 8 < e, case                              # over or at it: above, as before
    f = o["foot"]
    (b,), (e,) = f["boxes"], f["lines"]
    assert e > f["yl"] and e + 19 > f["h"] - 18 and b + 8 < f["yl"]              # no room under: past the live line
    (b,), (e,) = o["scan"]["boxes"], o["scan"]["lines"]
    assert e > o["scan"]["yl"] and b - 8 > e                                      # the 60-bar high just under the price
    assert len(o["vet"]["boxes"]) == 2 and o["fills"]["boxes"] == []


@node
def test_two_names_close_together_go_on_their_outer_sides_and_none_covers_a_line():
    """SCAN's line can step at the candle still forming (an old high ages out of the 60 bars: about one run in ten),
    often by only a few pixels. Its two names were stacked above their lines, so on a phone the "next close" box sat
    on the decided bar's line, hiding the very plateau its note names. Now no name covers a line the card marks: two
    lines close together are named on their outer sides (the upper one's above it, the lower one's under it), and with
    the live line near, a name goes past it on its line's side, 9 px clear, never onto a marked line. For the 60-bar
    high and low, a step of 1 to 25 px either way, with no live line or one anywhere 40 px either side of the line.
    The same for VET's averages close together. Away from the plot's top and foot, where there's room, a name stays
    on its line's side of the live line."""
    o = _run(f"""
const px = 100 / (forming(82500), desk(forming(82500), {json.dumps(H1)}, TOKS.day), lastGeom.Y(82400) - lastGeom.Y(82500));   // $ a px
out.scan = []; out.vet = [];
const M1 = "nothing yet. waiting for a close above the 60-bar high $80,800.", mid = {{ don_hi: Array(N).fill(80800) }};
for (const [lo, D, note, o] of [[false, 82500, {json.dumps(H1)}, {{}}], [true, 78000, {json.dumps(L1)}, {{}}], [false, 80800, M1, mid]])
  for (const d of [-25, -20, -16, -12, -8, -5, -3, -1, 1, 3, 5, 8, 12, 16, 20, 25])   // the next close's line, px under D's
    for (const v of [null, ...Array.from({{ length: 21 }}, (_, k) => k * 4 - 40)]) {{    // the live line, px under D's
      const next = D - d * px, ch = forming(next, o, lo);
      if (v != null) ch.live = D - v * px;
      point.key = "scan"; desk(ch, note, TOKS.day); drawPoint();
      const Y = lastGeom.Y;
      const both = Y(next) <= lastGeom.h - 18;   // (a low's next close under the plot isn't named)
      out.scan.push({{ lo, d, v, lines: [Y(D), ...(both ? [Y(next)] : [])], keep: [[Y(D)], [Y(D), Y(next)]], yl: v == null ? null : Y(ch.live), h: lastGeom.h, ...names() }});
    }}
for (const d of [-21, -15, -10, -6, -3, -1, 1, 3, 6, 10, 15, 21])                      // the slow average, px under the fast
  for (const v of [null, ...Array.from({{ length: 11 }}, (_, k) => k * 8 - 40)]) {{
    const ch = chartOf(), f = ch.ema_fast[N - 1]; ch.ema_slow[N - 1] = f - d * px;
    if (v != null) ch.live = f - v * px;
    point.key = "vet"; desk(ch, {json.dumps(H1)}, TOKS.night); drawPoint();
    const Y = lastGeom.Y;
    out.vet.push({{ d, v, lines: [Y(f), Y(f - d * px)], keep: [[Y(f), Y(f - d * px)], [Y(f), Y(f - d * px)]], yl: v == null ? null : Y(ch.live), h: lastGeom.h, ...names() }});
  }}""")
    two = side = 0
    for case in o["scan"] + o["vet"]:
        boxes, lines, yl = case["boxes"], case["lines"], case["yl"]
        assert len(boxes) == len(case["names"]) == len(lines), case
        for c, keep in zip(boxes, case["keep"]):
            # its 16 px clear of the lines under it: SCAN's decided name of its line (the next close's runs only past
            # the decided bar), the next close's of both; VET's either of both averages
            assert all(abs(c - y) >= 10 for y in keep), case
            assert yl is None or abs(c - yl) >= 17 - 1e-6, case                  # 9 px clear of the live line
            assert 8 - 1e-6 <= c <= case["h"] - 26 + 1e-6, case                  # on the plot, over the date axis
        assert all(abs(a - b) >= 17 - 1e-6 for a in boxes for b in boxes if a is not b), case
        if yl is not None and all(50 < y < case["h"] - 60 and (y - yl) * (lines[0] - yl) > 0 for y in lines):
            assert all((c - yl) * (lines[0] - yl) > 0 for c in boxes), case        # on its line's side of the live
            side += 1
        (up, lo) = sorted(lines) if len(lines) == 2 else (0, 0)
        if yl is None and len(boxes) == 2 and lo - up < 22 and up - 11 >= 8 and lo + 11 <= case["h"] - 26:
            (bu, bl) = sorted(boxes)                                              # close together: on their outer sides
            assert bu < up and bl > lo, case
            two += 1
    assert two >= 20 and side >= 50, (two, side)


@node
def test_a_mark_belongs_to_the_tab_it_was_made_on():
    """A tap's mark (or a resting mouse's) was carried over a tab switch: tap BTC's SCAN, switch to ETH, and ETH's chart
    marked ETH's 60-bar line for the rest of the 4 s, though nothing on ETH was tapped. Switching accounts (show: a tab,
    or a #COIN typed or linked) ends the mark first, its timer with it, so neither it nor the tap's way back runs on the
    new tab. On the VT basket's tab #point is worked out too: off, and no card can be tabbed to."""
    o = _run(_function(APP, "show") + """
let missing = null, src = "";
const storeSet = () => {}, history = { replaceState() {} }, live = {}, tick = () => Promise.resolve(), pollCandle = () => {};
desk(pos(), %s, TOKS.day);
canvas.rect = { top: -1500, bottom: -1240, height: 260 }; canvas.scrollIntoView = (o) => { scrollY = 830; };
tapped("scan"); const t = point.timer;
out.before = { key: point.key, on: canvas.classList.on, ms: timers[t - 1][1] };
show("ETH");
out.after = { key: point.key, on: canvas.classList.on, timer: point.timer, cleared: cleared.includes(t), coin };""" % json.dumps(H1))
    assert o["before"] == {"key": "scan", "on": True, "ms": 4000}
    assert o["after"] == {"key": None, "on": False, "timer": 0, "cleared": True, "coin": "ETH"}
    assert _function(APP, "show").split("{", 1)[1].lstrip().startswith("pointAt(null);")
    assert "window.addEventListener(\"hashchange\"" in APP and "if (ACCOUNTS.includes(c) && c !== coin) show(c);" in APP
    assert "if (basket) { renderBasket(s); drawPoint(); }" in _function(APP, "render")


@node
def test_a_page_without_a_point_canvas_still_draws():
    """An older page's HTML served with this script (a CDN edge mixing builds while a deploy spreads) has no #point:
    drawPoint threw on every render, so nothing after the chart was drawn. Now it does nothing there."""
    o = _run("""
const bare = new Function("$", "state", "point", "referent", "fit", "lastGeom", "COLORS", "rgba", "css", "line", "stackTags", "pointTag",
  %s + "; return drawPoint;")((id) => id === "point" ? null : canvas, { header: { kind: "desk" }, chart: chartOf(), agents: [] }, { key: "vet" },
  referent, fit, lastGeom, COLORS, rgba, css, line, stackTags, pointTag);
try { bare(); out.ok = true; } catch (e) { out.ok = String(e); }""" % json.dumps(_function(APP, "drawPoint")))
    assert o["ok"] is True


@node
def test_hover_focus_and_tap_point_and_end_as_they_should():
    """A mouse over a card points until it leaves; keyboard focus (focus-visible) points until blur, a tap's focus
    doesn't; a tap points for 3 s, which neither a stray leave nor a blur cuts short. On a phone the cards are far
    below the chart: a tap on a card that points, with none of the chart on screen, scrolls it into view and points
    for 4 s, then takes the page back to where the reader was, unless they have scrolled it since (a phone's toolbar
    going or coming back moves the page a little: that isn't a scroll) or gone to another tab; no smooth scroll either
    way under reduced motion. One that points at nothing never scrolls. A touch never points by hovering, and
    the card never cancels a touch, so a swipe on it scrolls the page as before."""
    o = _run("""
desk(pos(), %s, TOKS.day);
const el = () => { const h = {}; return { h, vis: false, addEventListener(t, f) { h[t] = f; }, matches(s) { return s === ":focus-visible" && this.vis; } }; };
const log = [], step = (name) => log.push([name, point.key, point.timer ? timers[point.timer - 1][1] : 0]);
const e = el(); aim(e, "risk");
e.h.pointerenter({ pointerType: "mouse" }); step("mouse in");
e.h.pointerleave({ pointerType: "mouse" }); step("mouse out");
e.h.pointerdown({ pointerType: "touch" }); e.h.pointerenter({ pointerType: "touch" }); step("touch down");
e.h.focus(); step("a tap's focus");
e.h.click(); step("tap");
e.h.pointerleave({ pointerType: "mouse" }); e.h.blur(); step("leave, blur");
timers[point.timer - 1][0](); step("3 s on");
canvas.rect = { top: -1500, bottom: -1240, height: 260 };   // the chart's middle 1,500 - 130 + 400 px up: the page at 830
canvas.scrollIntoView = (o) => { canvas.scrolled.push(o); scrollY = 830; };
const back = [];
e.h.pointerdown({ pointerType: "touch" }); e.h.click(); step("tap, chart away");
timers[point.timer - 1][0](); back.push(scrolls.splice(0));
const c = el(); aim(c, "crowd"); scrollY = 2600; c.h.pointerdown({ pointerType: "touch" }); c.h.click(); step("crowd tap, chart away");
timers[point.timer - 1][0](); back.push(scrolls.splice(0));
scrollY = 2600; e.h.pointerdown({ pointerType: "touch" }); e.h.click(); scrollY = 1700;   // the reader scrolls on from the chart
timers[point.timer - 1][0](); back.push(scrolls.splice(0));
still.matches = true; scrollY = 2600; e.h.pointerdown({ pointerType: "touch" }); e.h.click();
timers[point.timer - 1][0](); back.push(scrolls.splice(0)); still.matches = false;
scrollY = 2600; e.h.pointerdown({ pointerType: "touch" }); e.h.click(); scrollY = 830 + 45;   // a phone's toolbar went
timers[point.timer - 1][0](); back.push(scrolls.splice(0));
scrollY = 2600; e.h.pointerdown({ pointerType: "touch" }); e.h.click(); coin = "ETH";          // the reader went to ETH
timers[point.timer - 1][0](); back.push(scrolls.splice(0)); coin = "BTC";
e.h.pointerdown({ pointerType: "mouse" }); e.h.click(); step("mouse click");
e.vis = true; e.h.focus(); step("tab to it"); e.h.blur(); step("tab away");
out.log = log; out.scrolled = canvas.scrolled; out.back = back; out.on = Object.keys(e.h).sort();""" % json.dumps(H1))
    assert o["log"] == [["mouse in", "risk", 0], ["mouse out", None, 0], ["touch down", None, 0], ["a tap's focus", None, 0],
                        ["tap", "risk", 3000], ["leave, blur", "risk", 3000], ["3 s on", None, 0],
                        ["tap, chart away", "risk", 4000], ["crowd tap, chart away", "crowd", 3000],
                        ["mouse click", None, 0], ["tab to it", "risk", 0], ["tab away", None, 0]]
    assert o["scrolled"] == [{"block": "center", "behavior": b} for b in ("smooth", "smooth", "auto", "smooth", "smooth")]
    assert o["back"] == [[{"top": 2600, "behavior": "smooth"}], [], [], [{"top": 2600, "behavior": "auto"}],   # back to the card
                         [{"top": 2600, "behavior": "smooth"}], []]                # the toolbar's 45 px: back; another tab: not
    assert o["on"] == ["blur", "click", "focus", "pointerdown", "pointerenter", "pointerleave"]   # no touchstart, no wheel
    aim = _function(APP, "aim") + _function(APP, "tapped")
    assert "preventDefault" not in aim and 'still.matches ? "auto" : "smooth"' in aim
    assert "aim(el, a.key);" in _function(APP, "renderAgents")                     # each card, once, as it is made
    render = _function(APP, "render")
    assert "drawChart(s.chart); drawWeb(); drawPoint(); }" in render                # in step with every redraw of the chart


def test_the_point_fades_except_under_reduced_motion_and_a_pointing_card_shows_it():
    """And a tap on a card gets no grey flash (iOS Safari's on anything clickable, and every card has a click listener):
    a card that points answers with its mark on the chart, one that points at nothing (CROWD, CHIEF, CRAWLER) does
    nothing it seems to. The README says how long a tap marks, a second longer when it scrolls to the chart."""
    assert "-webkit-tap-highlight-color: transparent;" in _rule(_top(STYLE), ".card")
    assert "-webkit-tap-highlight-color" not in THEME
    readme = (ROOT / "README.md").read_text()
    assert "On a phone a tap marks it for 3 s; when none of the chart is on screen, the tap brings the chart into view first, marks it a second longer, and then goes back to the card. A mark stays on its own tab: switching accounts ends it." in readme
    point = _rule(_top(STYLE), ".point")
    assert "position: absolute" in point and "pointer-events: none" in point and "opacity: 0" in point
    assert "transition: opacity .2s" in point and _rule(_top(STYLE), ".point.on").strip() == "opacity: 1;"
    assert ".point { transition: none; }" in _media(STYLE, "(prefers-reduced-motion: reduce)")
    assert re.search(r"\.card\[tabindex\]:focus-visible \{ outline: 2px solid var\(--card", STYLE)
    assert re.search(r"@media \(hover: hover\) \{ \.card\[tabindex\]:hover \{ outline: 2px solid var\(--card", STYLE)


# ---------------------------------------------------------------- the web

def test_the_web_is_drawn_under_the_chart_and_fainter():
    """The web's canvas was over the chart's, its silk across the candle bodies and the price lines: it is under it now
    (z-index -1 in the chart's box, which isolates it: over the stage's paper), so whatever the chart draws, candles,
    averages, the channel, the stop, the live line and the tags, is drawn over the silk. And fainter, in both editions:
    a pencil-thin silk, no glow. The spider switch still leaves the hub to the walking spider."""
    assert "isolation: isolate" in _rule(_top(STYLE), ".chart-wrap")
    assert "z-index: -1" in _rule(_top(STYLE), ".web")
    assert "z-index: 1" in _rule(_top(STYLE), ".point")
    assert HTML.index('id="chart"') < HTML.index('id="web"') < HTML.index('id="point"')
    for ed in (DAY, NIGHT):
        alpha = float(re.fullmatch(r"rgba\([^)]*,\s*([\d.]+)\)", ed["--web-silk"]).group(1))
        assert alpha <= 0.3 and ed["--web-glow"] == "0", ed["--web-silk"]
    web = _function(APP, "drawWeb")
    assert 'if (document.documentElement.dataset.spider === "on") return g.restore();' in web
    assert 'g.shadowBlur = +(css("--web-glow") || 6);' in web
