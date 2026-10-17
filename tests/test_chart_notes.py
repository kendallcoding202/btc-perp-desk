"""The agents' notes off the chart, and each card pointing at what its note is about. Each test fails on 3fd4a61; the
ones on a mark's words and on a page mixing builds fail on 8ddd8ce too.

The owner, on a laptop, read the agents' caption boxes on BTC's chart (SCAN, VET, CROWD, RISK, SIZE, FILLS) as noise:
they sat at fixed spots over the plot (BUBBLE_SPOTS), unrelated to what they mentioned, covering candles, and said
what the cards already said, cut to one line there. Now no box sits over the plot at any width (the chief's line
stays beside the chart), a card's whole note wraps on the card, and hovering a card, tabbing to it or tapping it marks
on the chart the thing its note is about, only when the chart draws it: SCAN's 60-bar line at the price its note
gives, VET's fast and slow averages, RISK's and SIZE's stop and entry (a position only), FILLS' fill its note names;
CROWD, CHIEF and CRAWLER point at nothing. A mark is a highlight with no words: the plot washed pale but for it, the
line drawn wider over a halo in the card's color, a fill ringed. (8ddd8ce also named each line in a caption box on the
plot, and each round found a phone where a name sat over another line, past the live line or beside an older level;
the card's note names the thing and its price, the legend the averages, the axis's STOP tag the stop.) The mark is
drawn on a canvas of its own, clipped to the plot: the chart's own frame, its values, lines and tags, and the live
marker are as they were, and the gold live line shows on top of the mark. A phone's tap that brings the chart into
view takes the page back to the card when the mark ends; switching accounts ends it; this script with an older page's
HTML, and this HTML with an older script, still draw. The spider's web is drawn under the chart's canvas, fainter, so
the candles and lines are always drawn over its silk. The page's own functions are run in node, drawing on a canvas
that records what it is asked to draw (tests/test_live_marker.py's), with the comic theme's day and night tokens."""

from __future__ import annotations

import json
import re

from test_dashboard_phone_words import _media, _rule, _top
from test_live_marker import APP, DAY, NIGHT, ROOT, STYLE, THEME, _between, _draw_js, _function, _node, node

HTML = (ROOT / "dashboard" / "index.html").read_text()
ACTIVITY = (ROOT / "dashboard" / "activity.css").read_text()
FILTER = (ROOT / "desk" / "filter.py").read_text()

H1 = "nothing yet. waiting for a close above the 60-bar high $82,500."
L1 = "nothing yet. waiting for a close below the 60-bar low $78,000."


def _point_js() -> str:
    """drawChart's harness, plus the pointing code, a #point that is the same recording canvas (fit() clears it, so
    after drawPoint the ops are the point's alone), the agent cards, a page state, and timers that only record."""
    return (_draw_js() + _between(APP, "  const COLORS = {", "  // ---- pixel sprites")
            + _between(APP, "  const POINTS = {", "  function referent(") + _function(APP, "referent")
            + _between(APP, "  const point = { key", "  function drawPoint(") + _function(APP, "drawPoint")
            + _function(APP, "pointAt") + _function(APP, "tapped") + _function(APP, "aim") + """
let state = null, coin = "BTC";
const innerHeight = 800, tr = [], timers = [], cleared = [];
const setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; }, clearTimeout = (t) => { if (t) cleared.push(t); };
Object.defineProperty(g, "globalCompositeOperation", { get() { return cur.gco || "source-over"; }, set(v) { cur.gco = v; } });
g.translate = (x, y) => { tr.push([x, y]); };
g.strokeText = (t, x, y) => { ops.push({ k: "text", t: String(t), x, y }); };   // any word, filled or stroked
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
// FILLS' note names the broker's last fill (desk/board.py): by default pos()'s newest, its exit
const F1 = "sold 0.0100 BTC @ $81,000, fee $0.32, net +5.00.";
const agents = (scan, fills = F1) => KEYS.map((key) => ({ key, name: key.toUpperCase(), quote: key === "scan" ? scan : key === "fills" ? fills : "a note" }));
const desk = (ch, scan, tok, fills) => { TOK = tok; drawChart(ch); state = { header: { kind: "desk" }, agents: agents(scan, fills), chart: ch }; };
// what each card points at: a line's price (a series' at the bar it ends on, up to chart.decided) and where the chart
// draws it, its color and dashes; a fill's ring
const refs = (ch, scan, tok = TOKS.day, fills) => {
  desk(ch, scan, tok, fills);
  return Object.fromEntries(KEYS.map((k) => { const r = referent(k, state);
    return [k, r && r.map((x) => x.mark ? { mark: true, x: x.x, y: x.y, r: x.r } : { v: x.v ?? x.at, y: lastGeom.Y(x.v ?? x.at), color: x.color,
      dash: (x.dash || []).join(","), series: !!x.xs, level: x.at != null })]; }));
};
const pos = (o = {}) => chartOf({ stop: 79000, entry: 81000, markers: [
  { ts: T0 + 2 * H * 10, price: 80200, kind: "entry", side: "long" }, { ts: T0 + 2 * H * 20 + 600000, price: 81000, kind: "exit", side: "long", pnl: 5 },
  { ts: T0 - 10 * H, price: 80000, kind: "entry", side: "long" }], ...o });
// the board's chart (decided: its newest candle) with one candle still forming past it, on the next close's line
const forming = (next, o = {}, lo = false) => { const ch = chartOf({ decided: N - 1, ...o }); ch.candles.push([T0 + 2 * H * N, 80000, 80100, 79900, 80050]);
  ch.ema_fast.push(null); ch.ema_slow.push(null); ch.don_lo.push(lo ? next : null); ch.don_hi.push(lo ? null : next); return ch; };
// a coin under $1,000: BTC's chart scaled by f
const scaled = (f, o = {}) => { const ch = chartOf(o); ch.candles = ch.candles.map(([t, ...p]) => [t, ...p.map((v) => v * f)]);
  ["ema_fast", "ema_slow", "don_hi", "don_lo"].forEach((k) => { ch[k] = ch[k].map((v) => v * f); }); return ch; };
// what #point drew: the lines drawn wide (their points, color and dashes) and the words
const drawn = () => ({ wide: ops.filter((x) => x.k === "stroke" && x.width === 2.5).map((x) => ({ pts: x.pts, color: x.color, dash: x.dash })),
  words: ops.filter((x) => x.k === "text").map((x) => x.t) });
""")


def _run(cases: str) -> dict:
    return _node(_point_js() + "const TOKS = " + json.dumps({"day": DAY, "night": NIGHT}) + ";\nconst out = {};\n" + cases
                 + "\nconsole.log(JSON.stringify(out));")


# ---------------------------------------------------------------- nothing over the chart

def test_no_caption_box_over_the_chart_and_the_chiefs_line_stays_beside_it():
    """The six boxes are gone at every width: nothing fills #bubbles, no code placed them, no CSS for them. The
    chart's box holds its head, its canvas, the web's (under it), the point's (empty until a card points) and an empty,
    hidden #bubbles for an older script (below). The chief's line is still the chief's quote, in the column beside the
    chart (narrow windows hide it, as before), with its thick left edge (the bubbles' CSS took it along: a 2 px edge on
    6fdd716). The README says the notes are on the cards and the chart is clear of boxes, not that nothing sits over
    it: the spider walks over it."""
    wrap = _between(HTML, '<div class="chart-wrap">', '<aside class="stage-right">')
    assert re.findall(r'id="([\w-]+)"', wrap) == ["chart-title", "chart-sub", "chart", "web", "point", "bubbles"]
    assert re.findall(r"<[^>]*bubble[^>]*>", re.sub(r"<!--.*?-->", "", wrap, flags=re.S)) == ['<div class="bubbles" id="bubbles" hidden>']
    right = _between(HTML, '<aside class="stage-right">', "</aside>")
    assert '<div class="bubble static" id="chief-bubble">' in right
    for gone in ("BUBBLE_SPOTS", "renderBubbles", "plotBubbles", '$("bubbles")'):
        assert gone not in APP, gone
    assert '$("chief-bubble").textContent = chief ? chief.quote : "";' in _function(APP, "renderAgents")
    for css in (STYLE, THEME):
        sels = re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        assert not [s for s in sels if re.search(r"\.bubbles?\b(?!\.static)", s)], css[:40]
    assert re.search(r"\.sprite-slot, \.bubble\.static \{ display: none; \}", _media(STYLE, "(max-width: 980px)"))
    assert "border-left-width: 6px;" in _rule(_top(THEME), ".bubble.static")      # its thick left edge, as before
    readme = (ROOT / "README.md").read_text()
    assert "agent cards, bubbles" not in readme and "Nothing sits over the chart" not in readme
    assert "The agents' notes are on their cards and the chart is clear of boxes" in readme
    assert "no words on the chart (the note says what it is)" in readme and "no name sits on it" not in readme
    assert "dashboard/spider.js  the walking CRAWLER" in readme                     # (it walks over the chart)


# every element 3fd4a61's app.js looks up by id, $("…") (acct-err it makes itself, when an account has no board)
OLD_IDS = ("agents analysis-title bal-day bal-hold bal-mode bal-record balance bubbles chart chart-sub chart-title "
           "chief-bubble chief-sprite chips coins daydate equity events foot gates goal-bar goal-from goal-note goal-to "
           "holdings jevbox kill-hint kill-state mini-time mode outlook pnlbars scanned shadow-link stats tagline thr "
           "ticker time title tz vt-exp vt-head vt-next vt-note vt-rule vt-ticker web").split()


def test_an_older_script_with_this_page_finds_its_bubbles_and_shows_nothing():
    """The reverse of a page without #point (below): this HTML served with 3fd4a61's app.js, still cached while a deploy
    spreads. That script wrote the six boxes into #bubbles on every render ($("bubbles").innerHTML, then its style's
    right and transform): with no #bubbles it threw there, and nothing after the agent cards was drawn. The chart's box
    keeps it, empty and hidden, so the old script finds it and the boxes it writes never show: no CSS of this page
    styles .bubbles or #bubbles, nor gives [hidden] a display (3fd4a61's own gave .bubbles a place and z-index, and
    display: none under 980 px, never a display that would unhide it). Every other element that script looks up is
    here too."""
    wrap = _between(HTML, '<div class="chart-wrap">', '<aside class="stage-right">')
    assert '<div class="bubbles" id="bubbles" hidden></div>' in wrap
    for i in OLD_IDS:
        assert f'id="{i}"' in HTML, i
    for css in (STYLE, THEME, ACTIVITY):
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        assert not [s for s, _ in rules if re.search(r"[.#]bubbles\b", s)], css[:40]
        for sel, body in rules:
            if "[hidden]" in sel:
                assert re.fullmatch(r"\s*display: none;\s*", body), sel


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
    """SCAN: the 60-bar line its note names (the high, or the low a short waits on); nothing while warming up or with
    shorts disabled. VET: the fast and slow averages, while drawn. RISK and SIZE: the stop and the entry, with a
    position only, and an entry off the plot isn't pointed at. FILLS: the fill its note names (the newest drawn), an
    entry's mark where its pin hangs (the newest candle's pin box held clear of the axis, as entryPin draws it), an
    exit's on its dot. CROWD, CHIEF and CRAWLER: nothing, ever."""
    o = _run(f"""
out.flat = refs(chartOf(), {json.dumps(H1)}); out.yHi = lastGeom.Y(82500);
out.low = refs(chartOf(), {json.dumps(L1)}); out.yLo = lastGeom.Y(78000);
out.warm = refs(chartOf(), "warming up the averages.");
out.noshort = refs(chartOf(), "nothing yet. shorts disabled.");
out.pos = refs(pos(), {json.dumps(H1)});
out.y = {{ stop: lastGeom.Y(79000), entry: lastGeom.Y(81000), e10: lastGeom.Y(80200), x10: lastGeom.X(10), x20: lastGeom.X(20), x21: lastGeom.Y(81000) }};
out.far = refs(chartOf({{ stop: 79000, entry: 120000 }}), {json.dumps(H1)});
const cold = chartOf(); ["ema_fast", "ema_slow", "don_hi"].forEach((k) => {{ cold[k] = cold[k].map(() => null); }});
out.cold = refs(cold, {json.dumps(H1)});
out.night = refs(pos(), {json.dumps(H1)}, TOKS.night);
out.entry = refs(pos({{ markers: [{{ ts: T0 + 2 * H * 10, price: 80200, kind: "entry", side: "long" }}] }}), {json.dumps(H1)}, TOKS.day,
  "bought 0.0100 BTC @ $80,200, fee $0.32.");
out.newest = refs(pos({{ markers: [{{ ts: T0 + 2 * H * (N - 1), price: 80000, kind: "entry", side: "long" }}] }}), {json.dumps(H1)}, TOKS.day,
  "bought 0.0100 BTC @ $80,000, fee $0.32.");
out.nx = {{ x: lastGeom.X(N - 1), edge: lastGeom.w - lastGeom.padR - 4 }};""")
    for case in ("flat", "low", "warm", "noshort", "pos", "far", "cold", "night"):
        for k in ("chief", "crowd", "crawler"):
            assert o[case][k] is None, (case, k)
    scan = o["flat"]["scan"]
    assert len(scan) == 1 and scan[0]["series"] and scan[0]["v"] == 82500
    assert abs(scan[0]["y"] - o["yHi"]) < 1e-6 and scan[0]["color"] == DAY["--channel"] and scan[0]["dash"] == "3,4"
    assert o["low"]["scan"][0]["v"] == 78000 and abs(o["low"]["scan"][0]["y"] - o["yLo"]) < 1e-6
    assert o["warm"]["scan"] is None and o["noshort"]["scan"] is None and o["cold"]["scan"] is None
    assert [(x["series"], x["color"]) for x in o["flat"]["vet"]] == [(True, DAY["--cyan"]), (True, DAY["--blue"])]
    assert o["cold"]["vet"] is None
    for k in ("risk", "size", "fills"):                                          # flat, no fills drawn: nothing
        assert o["flat"][k] is None, k
    y = o["y"]
    for k in ("risk", "size"):
        got = o["pos"][k]
        assert [(x["v"], x["dash"], x["level"]) for x in got] == [(79000, "6,4", True), (81000, "2,3", True)]
        assert abs(got[0]["y"] - y["stop"]) < 1e-6 and abs(got[1]["y"] - y["entry"]) < 1e-6
        assert got[0]["color"] == DAY["--orange"] and got[1]["color"] == DAY["--entry"]
    assert [(x["dash"], abs(x["y"] - y["stop"]) < 1e-6) for x in o["far"]["risk"]] == [("6,4", True)]   # the entry is off the plot: not it
    (dot,) = o["pos"]["fills"]                                                    # its note's fill, the exit: its dot
    assert dot["mark"] and abs(dot["x"] - y["x20"]) < 1e-6 and abs(dot["y"] - y["x21"]) < 1e-6 and dot["r"] == 8
    (pin,) = o["entry"]["fills"]                                                  # an entry: where its L pin hangs
    assert abs(pin["x"] - y["x10"]) < 1e-6 and abs(pin["y"] - (y["e10"] + 9)) < 1e-6 and pin["r"] == 12
    assert o["night"]["risk"][1]["color"] == NIGHT["--entry"]
    nx = o["nx"]
    assert nx["x"] > nx["edge"] and [f["x"] for f in o["newest"]["fills"]] == [nx["edge"]]   # the newest pin's box, held clear


@node
def test_scans_mark_is_the_decided_bars_line_drawn_whole_and_once():
    """SCAN's note gives the line at the bar the desk decided on (desk/filter.py: don_hi[i]); a candle still forming past
    it (BTC's formingBars, ETH's and SOL's Hyperliquid candle) is drawn on the next close's line, analysis.outlook[0],
    which steps when an old high ages out or the bar's wick went over it. The mark is that one line, taken at the
    decided bar's price, and drawn whole as the chart draws it, its step at the forming candle with it: one stroke and
    no words (8ddd8ce also named the next close's price, a second name by the line). A note giving the next close's price
    isn't the decided bar's: nothing. A board drawn before chart.decided (or without one) takes the line's end, as
    before. withLive marks chart.decided before it adds any forming candle."""
    o = _run(f"""
out.step = refs(forming(82100), {json.dumps(H1)});
out.g = {{ hi: lastGeom.Y(82500), next: lastGeom.Y(82100) }};
out.nextNote = refs(forming(82100), "nothing yet. waiting for a close above the 60-bar high $82,100.");
out.undecided = refs(forming(82100, {{ decided: undefined }}), "nothing yet. waiting for a close above the 60-bar high $82,100.");
point.key = "scan"; desk(forming(82100), {json.dumps(H1)}, TOKS.night); drawPoint();
out.d = drawn(); out.n = N;""")
    g, n = o["g"], o["n"]
    (step,) = o["step"]["scan"]
    assert step["series"] and step["v"] == 82500 and abs(step["y"] - g["hi"]) < 1e-6       # at the decided bar
    (wide,) = o["d"]["wide"]                                                        # one line, drawn whole
    assert len(wide["pts"]) == n + 1 and abs(wide["pts"][n - 1][1] - g["hi"]) < 1e-6 and abs(wide["pts"][n][1] - g["next"]) < 1e-6
    assert wide["color"] == NIGHT["--channel"] and wide["dash"] == "3,4" and o["d"]["words"] == []
    assert o["nextNote"]["scan"] is None
    assert [x["v"] for x in o["undecided"]["scan"]] == [82100]
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
def test_scan_marks_its_line_only_at_the_price_its_note_gives():
    """The note's own figure is whole dollars: a coin under $1,000 has its line at 113.39 and its note says $113, and
    that's the line. A note that names no price ("taken out") is about the line as drawn. A note whose price isn't the
    line's at the decided bar (more than the half dollar its rounding can be off by, either way Python rounds a half)
    points at nothing, as does a bull trap's note."""
    o = _run(f"""
out.sol = refs(scaled(113.39 / 82500), "nothing yet. waiting for a close above the 60-bar high $113.");
out.solLo = refs(scaled(113.39 / 82500), "nothing yet. waiting for a close below the 60-bar low $107.");   // 78,000 scaled: 107.20
out.taken = refs(chartOf(), "breakout: 60-bar high taken out. that's a setup.");
out.half = refs(chartOf({{ don_hi: Array(N).fill(82500.5) }}), {json.dumps(H1)});
out.off = refs(chartOf(), "nothing yet. waiting for a close above the 60-bar high $82,499.");
out.solOff = refs(scaled(113.39 / 82500), "nothing yet. waiting for a close above the 60-bar high $112.");
out.trap = refs(chartOf(), "bull trap: broke the 60-bar high 3 bars ago, now back below $82,500. that's a setup.");""")
    assert [round(x["v"], 6) for x in o["sol"]["scan"]] == [113.39]
    assert [round(x["v"], 6) for x in o["solLo"]["scan"]] == [round(78000 * 113.39 / 82500, 6)]
    assert [x["v"] for x in o["taken"]["scan"]] == [82500]
    assert [x["v"] for x in o["half"]["scan"]] == [82500.5]                        # Python's ',.0f' says 82,500
    assert o["off"]["scan"] is None and o["solOff"]["scan"] is None and o["trap"]["scan"] is None


# ---------------------------------------------------------------- how it is drawn

@node
def test_the_mark_is_drawn_on_its_own_canvas_and_leaves_the_chart_and_live_marker_as_drawn():
    """Pointing never redraws or changes the chart's own canvas: its frame is the same with a card pointing. On #point,
    shifted to the chart's place and clipped to the plot before anything is drawn (never the price axis's column, its
    tags and the live one's beat, never the date axis): the plot washed pale, and last the live line's own pixels cut out
    of it all (a cut as wide as the line, so no candle shows through beside it), the line and its tag exactly as drawn;
    the thing redrawn 2.5 px wide in its own color and dashes over an 8 px halo in the pointing card's color; no words
    and no box (the stop's own tag on the axis and the card's note give the prices); FILLS' fill unwashed and ringed. A
    fill on the newest candle at the live price is ringed inside the plot only. A card that points at nothing draws
    nothing (what was drawn fades out as it was); nor does a card on the VT basket's tab. The cards that point can be
    tabbed to, the others can't."""
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
desk(newFill, %s, TOKS.night, `bought 0.0100 BTC @ $${Math.round(ch.live).toLocaleString("en-US")}, fee $0.32.`);
point.key = "fills"; drawPoint(); out.newest = ops.slice(); out.npw = lastGeom.w - lastGeom.padR;
desk(ch, %s, TOKS.night);
point.key = "crowd"; ops.length = 0; ops.push({ k: "kept" }); drawPoint(); out.crowd = { on: canvas.classList.on, kept: ops.length === 1 };
state = { ...state, header: { kind: "basket" } }; point.key = "risk"; drawPoint(); out.basket = { on: canvas.classList.on, tab: tabbable() };
desk(chartOf(), %s, TOKS.day); point.key = null; drawPoint(); out.flatTab = tabbable();
out.tr = tr;""" % ((json.dumps(H1),) * 4))
    g, ops = o["g"], o["risk"]["ops"]
    assert o["same"] and o["risk"]["on"] and o["tr"][0] == [6, 40]                  # the chart's place in its box
    assert ops[0] == {"k": "clip", "rect": [0, 0, g["pw"], g["bot"]]}               # the plot, before anything is drawn
    assert o["saved"] == 0                                                         # and let go of after (save / restore)
    wash = [x for x in ops if x["k"] == "fillRect"]
    assert wash == [{"k": "fillRect", "color": "rgba(26,32,48,0.6)", "x": 0, "y": 0, "w": g["pw"], "h": g["bot"]}]   # night's --panel
    live = [x for x in ops if x["k"] == "stroke" and x["gco"] == "destination-out"]
    assert [(x["pts"], x["width"]) for x in live] == [([[0, g["yLive"]], [g["pw"], g["yLive"]]], float(NIGHT["--live-width"]))]   # its own pixels
    wide = [(x["pts"][0][1], x["color"], x["dash"]) for x in ops if x["k"] == "stroke" and x["width"] == 2.5]
    assert wide == [(g["yStop"], NIGHT["--orange"], "6,4"), (g["yEntry"], NIGHT["--entry"], "2,3")]
    halo = [x["color"] for x in ops if x["k"] == "stroke" and x["width"] == 8]
    assert halo == ["rgba(213,138,0,0.3)"] * 2                                     # RISK's own --orange
    assert not [x for x in ops if x["k"] in ("text", "strokeRect")]                 # no words, no box
    for x in ops:                                                                  # all of it inside the plot
        if x["k"] == "stroke":
            assert all(p[0] <= g["pw"] + 1e-6 for p in x["pts"]), x
    assert sorted(o["risk"]["tab"]) == ["fills", "risk", "scan", "size", "vet"]
    cuts = [x for x in o["fills"] if x["k"] == "fill" and x["gco"] == "destination-out"]
    rings = [x for x in o["fills"] if x["k"] == "stroke" and x["width"] == 2]
    assert [c["pts"][0][2] for c in cuts] == [8] and [r["color"] for r in rings] == [NIGHT["--ice"]]   # the exit its note names
    assert not [x for x in o["fills"] if x["k"] in ("text", "strokeRect")]
    nw = o["newest"]                                                               # the newest candle's fill at the live price
    assert nw[0] == {"k": "clip", "rect": [0, 0, o["npw"], g["bot"]]}
    ring = [x["pts"][0] for x in nw if x["k"] == "stroke" and x["width"] == 2]
    assert len(ring) == 1 and ring[0][0] == o["npw"] - 4 and ring[0][0] + ring[0][2] > o["npw"]   # it reaches the axis: the clip stops it
    assert o["crowd"] == {"on": False, "kept": True}
    assert o["basket"] == {"on": False, "tab": []}
    assert sorted(o["flatTab"]) == ["scan", "vet"]                                 # flat, no fills: those three can't be tabbed to


@node
def test_the_live_line_shows_on_top_of_a_mark():
    """The chart draws its gold live line over everything, and a mark on #point over it hid it: an entry or a 60-bar
    line at or near the price, just after an entry or with a breakout about to happen, is when it matters most. The
    live line's own pixels are cut out of the mark last, after its lines and rings, so the chart's line shows on top as
    drawn, and nothing is written by it. RISK with the entry just under, just over and at the live price, and under it
    at the plot's foot; SCAN with the 60-bar high just under the price; VET with both averages under it; FILLS with a
    fill on the live line."""
    o = _run("""
const run = (key, ch, scan = %s) => {
  point.key = key; desk(ch, scan, TOKS.day); drawPoint();
  const cut = ops[ops.length - 1], yl = lastGeom.Y(ch.live);
  return { yl, pw: lastGeom.w - lastGeom.padR, cut: { gco: cut.gco, k: cut.k, pts: cut.pts, width: cut.width },
    cuts: ops.filter((x) => x.gco === "destination-out" && x.k === "stroke").length, wide: drawn().wide.length, words: drawn().words };
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
        assert r["cuts"] == 1 and r["words"] == [], case
    assert [o[c]["wide"] for c in ("under", "over", "at", "foot", "scan", "vet", "fills")] == [2, 2, 2, 1, 1, 2, 0]


@node
def test_no_card_writes_a_word_on_the_chart_and_each_marks_its_thing_as_the_chart_draws_it():
    """8ddd8ce named each line marked in a caption box on the plot ("60-bar high 84,295", "next close …", "fast",
    "slow", "entry …"), placed by a search for room; four rounds found a phone where one sat over another line, past
    the live line, or 50-80 px from its line beside an older level. A mark is a highlight now, and #point is asked to
    write nothing, for every card, on every chart here: flat, a position, a fill at the live price, a coin under $1,000,
    SCAN's line stepping by 1 to 25 px at the candle still forming with the live line anywhere 40 px around it, its
    older level just before the decided bar, VET's averages 3 to 21 px apart, BTC's real phone and laptop geometry,
    by day and by night. Each mark is the same as before: clipped to the plot, the one wash, each line it points at
    drawn wide in its own color and dashes over a halo in the card's color, exactly where the chart draws that line,
    each fill cut out of the wash and ringed, the live line cut out last; a card that points at nothing draws nothing;
    the cards that can be tabbed to are those that point; the chart's own frame is the same."""
    o = _run(f"""
const D = {{}}, cases = [], add = (name, ch, scan = {json.dumps(H1)}, fills, h = 260) => cases.push({{ name, ch, scan, fills, h }});
const px = 100 / (desk(forming(82500), {json.dumps(H1)}, TOKS.day), lastGeom.Y(82400) - lastGeom.Y(82500));   // $ a px
const live = (ch, p) => {{ ch.live = p; return ch; }};
add("flat", () => chartOf()); add("low", () => chartOf(), {json.dumps(L1)}); add("warm", () => chartOf(), "warming up the averages.");
add("pos", () => pos()); add("pos, live at the entry", () => live(pos(), 81000)); add("far entry", () => chartOf({{ stop: 79000, entry: 120000 }}));
add("entry fill", () => pos({{ markers: [{{ ts: T0 + 2 * H * 10, price: 80200, kind: "entry", side: "long" }}] }}), {json.dumps(H1)},
  "bought 0.0100 BTC @ $80,200, fee $0.32.");
add("fill at the live price", () => {{ const ch = pos({{ markers: [{{ ts: T0 + 2 * H * (N - 1), price: 80020, kind: "entry", side: "long" }}] }}); ch.live = 80020; return ch; }},
  {json.dumps(H1)}, "bought 0.0100 BTC @ $80,020, fee $0.32.");
add("foot", () => live(pos({{ stop: null, entry: 78060 }}), 78100));
add("sol", () => scaled(113.39 / 82500, {{ stop: 109, entry: 111 }}), "nothing yet. waiting for a close above the 60-bar high $113.");
add("older level", () => {{ const ch = chartOf({{ decided: N - 1 }}); ch.don_hi = ch.don_hi.map((v, k) => (k < N - 5 ? 82700 : 82500)); return ch; }});
for (const [lo, at, note] of [[false, 82500, {json.dumps(H1)}], [true, 78000, {json.dumps(L1)}]])
  for (const d of [-25, -12, -5, -1, 1, 5, 12, 25])                                  // the next close's line, px under the decided's
    for (const v of [null, -40, -20, -8, 0, 8, 20, 40])                               // the live line, px under it
      add(`step ${{lo ? "low" : "high"}} ${{d}} live ${{v}}`, () => {{ const ch = forming(at - d * px, {{}}, lo); if (v != null) ch.live = at - v * px; return ch; }}, note);
for (const d of [-21, -10, -3, 3, 10, 21])                                            // the slow average, px under the fast
  for (const v of [null, -40, -16, 0, 16, 40])
    add(`vet ${{d}} live ${{v}}`, () => {{ const ch = chartOf(), f = ch.ema_fast[N - 1]; ch.ema_slow[N - 1] = f - d * px; if (v != null) ch.live = f - v * px; return ch; }});
// BTC's real board, Coinbase's price just under the slow average: a 390-440 px phone's chart (260) and a laptop's (330)
const geo = (yf, ys, yl) => () => {{ const ch = chartOf(); TOK = TOKS.day; drawChart(ch);
  const Y = lastGeom.Y, at = (y) => 82500 - (y - Y(82500)) * 100 / (Y(82400) - Y(82500));
  ch.ema_fast[N - 1] = at(yf); ch.ema_slow[N - 1] = at(ys); ch.live = at(yl); return ch; }};
add("phone", geo(145.2, 165.8, 181.4)); add("laptop", geo(198, 214, 234), {json.dumps(H1)}, undefined, 330);
const fails = [], marked = {{}};
for (const tok of ["day", "night"])
  for (const c of cases) {{
    for (const key of KEYS) {{
      canvas.clientHeight = c.h; const ch = c.ch();
      point.key = key; desk(ch, c.scan, TOKS[tok], c.fills);
      const frame = JSON.stringify(ops); ops.length = 0;
      drawPoint();
      const got = ops.slice(), on = canvas.classList.on, ref = referent(key, state), tabs = tabbable();
      const fail = (why) => fails.push([tok, c.name, key, why]);
      if (got.some((x) => x.k === "text")) fail("words");
      if (JSON.stringify(tabs) !== JSON.stringify(KEYS.filter((k) => referent(k, state)))) fail("tab");
      if (!ref) {{ if (on || got.length) fail("drew for nothing"); }}
      else {{
        marked[key] = (marked[key] || 0) + 1;
        const {{ X, Y, w, h, padR }} = lastGeom, pw = w - padR, tint = COLORS[key];
        const lines = ref.filter((r) => !r.mark), marks = ref.filter((r) => r.mark);
        const pts = (l) => l.xs ? l.xs.flatMap((v, k) => (v == null ? [] : [[X(k), Y(v)]])) : [[0, Y(l.at)], [pw, Y(l.at)]];
        const want = [...lines.flatMap((l) => [{{ color: rgba(tint, .3), width: 8, dash: "", pts: pts(l) }}, {{ color: l.color, width: 2.5, dash: (l.dash || []).join(","), pts: pts(l) }}]),
          ...marks.flatMap((m) => [{{ color: rgba(tint, .3), width: 6, dash: "", pts: [[m.x, m.y, m.r + 2]] }}, {{ color: css(tint), width: 2, dash: "", pts: [[m.x, m.y, m.r]] }}])];
        const strokes = got.filter((x) => x.k === "stroke" && x.gco !== "destination-out").map(({{ color, width, dash, pts }}) => ({{ color, width, dash, pts }}));
        if (JSON.stringify(strokes) !== JSON.stringify(want)) fail("strokes");
        if (JSON.stringify(got[0]) !== JSON.stringify({{ k: "clip", rect: [0, 0, pw, h - 18] }})) fail("clip");
        const rects = got.filter((x) => x.k === "fillRect" || x.k === "strokeRect");
        if (JSON.stringify(rects) !== JSON.stringify([{{ k: "fillRect", color: rgba("--panel", .6), x: 0, y: 0, w: pw, h: h - 18 }}])) fail("wash");
        const holes = got.filter((x) => x.k === "fill");
        if (holes.length !== marks.length || holes.some((x, j) => x.gco !== "destination-out" || JSON.stringify(x.pts) !== JSON.stringify([[marks[j].x, marks[j].y, marks[j].r]]))) fail("holes");
        const cuts = got.filter((x) => x.k === "stroke" && x.gco === "destination-out"), last = got[got.length - 1];
        if (ch.live ? cuts.length !== 1 || last !== cuts[0] || JSON.stringify(last.pts) !== JSON.stringify([[0, Y(ch.live)], [pw, Y(ch.live)]]) : cuts.length) fail("live cut");
      }}
      drawChart(ch);
      if (JSON.stringify(ops) !== frame) fail("frame");
    }}
  }}
canvas.clientHeight = 260;
out.fails = fails.slice(0, 20); out.n = fails.length; out.marked = marked; out.cases = cases.length;""")
    assert o["n"] == 0, o["fails"]
    assert o["cases"] >= 150
    marked = o["marked"]
    assert not {"chief", "crowd", "crawler"} & set(marked)
    assert marked["scan"] >= 2 * 130 and marked["vet"] >= 2 * 140 and min(marked["risk"], marked["size"]) >= 2 * 6 and marked["fills"] >= 2 * 4, marked
    draw = _function(APP, "drawPoint")
    for gone in ("fillText", "strokeText", "measureText", "font", "stackTags", "want", "pointTag"):
        assert gone not in draw, gone
    assert "function pointTag(" not in APP and "next close" not in _between(APP, "  const POINTS = {", "  function referent(")


@node
def test_fills_points_at_the_one_fill_its_note_names():
    """FILLS' note is about the broker's last fill, "bought 0.0362 @ $2,425, fee $0.04, net -2.50." (desk/board.py: the
    exit of ETH's short, on the real board), but its mark rang every fill in the window, that short's entry too and any
    older trade's, and the fills of a chart whose note said "no fills yet". It rings the newest fill drawn now, when
    that is the note's price as the note gives it (whole dollars from $1,000, cents under it); "no fills yet", or a price
    that is an older fill's, or a cent off, points at nothing."""
    board = (ROOT / "desk" / "board.py").read_text()
    assert "@ {_usd(lf['price'])}, " in board and '"no fills yet. taker in, taker out."' in board
    assert "dp = 0 if abs(x) >= 1000 else 2" in board
    o = _run("""
const E1 = "bought 0.0100 BTC @ $80,200, fee $0.32.", none = "no fills yet. taker in, taker out.";
out.exit = refs(pos(), %s); out.x20 = lastGeom.X(20);
out.older = refs(pos(), %s, TOKS.day, E1);
out.none = refs(pos(), %s, TOKS.day, none);
const f = 113.39 / 82500, small = chartOf({ markers: [{ ts: T0 + 2 * H * 20, price: 81000 * f, kind: "exit", side: "long" }] });
small.candles = small.candles.map(([t, ...p]) => [t, ...p.map((v) => v * f)]);
["ema_fast", "ema_slow", "don_hi", "don_lo"].forEach((k) => { small[k] = small[k].map((v) => v * f); });
out.sol = refs(small, "nothing yet.", TOKS.day, "sold 1.2000 SOL @ $111.33, fee $0.06, net +1.00.");
out.solOff = refs(small, "nothing yet.", TOKS.day, "sold 1.2000 SOL @ $111.34, fee $0.06, net +1.00.");""" % ((json.dumps(H1),) * 3))
    (dot,) = o["exit"]["fills"]
    assert dot["r"] == 8 and abs(dot["x"] - o["x20"]) < 1e-6                     # the exit's dot, not the entry's pin
    assert o["older"]["fills"] is None and o["none"]["fills"] is None
    assert len(o["sol"]["fills"]) == 1 and o["solOff"]["fills"] is None


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
def test_a_page_without_a_point_canvas_still_draws_and_a_tap_there_stays_put():
    """An older page's HTML served with this script (a CDN edge mixing builds while a deploy spreads) has no #point:
    drawPoint threw on every render, so nothing after the chart was drawn. Now it does nothing there. And a tap on a
    card that points, with the chart off screen, scrolled such a page up to a chart that can't show the mark, and back
    4 s later: there the page stays put (the tap's 3 s run as on any page, drawing nothing). The same tap on this page
    brings the chart into view, as before."""
    src = json.dumps(_function(APP, "drawPoint") + _function(APP, "pointAt") + _function(APP, "tapped"))
    o = _run("""
desk(pos(), %s, TOKS.day);
const pt = { key: null, timer: 0 }, page = (id) => (id === "point" ? null : canvas);
const bare = new Function("$", "state", "point", "referent", "fit", "lastGeom", "COLORS", "rgba", "css", "line", "document",
  "innerHeight", "still", "scrollY", "coin", "scrollTo", "setTimeout", "clearTimeout", %s + "; return { drawPoint, tapped };")(
  page, state, pt, referent, fit, lastGeom, COLORS, rgba, css, line, document, innerHeight, still, scrollY, coin, scrollTo, setTimeout, clearTimeout);
pt.key = "vet"; ops.length = 0;
try { bare.drawPoint(); out.ok = ops.length === 0; } catch (e) { out.ok = String(e); }
canvas.rect = { top: -1500, bottom: -1240, height: 260 };
try { bare.tapped("scan"); out.tap = { key: pt.key, ms: timers[pt.timer - 1][1], scrolled: canvas.scrolled.length, drew: ops.length }; } catch (e) { out.tap = String(e); }
timers[pt.timer - 1][0](); out.back = scrolls.length;
tapped("scan"); out.here = { scrolled: canvas.scrolled.length, ms: timers[point.timer - 1][1] };""" % (json.dumps(H1), src))
    assert o["ok"] is True
    assert o["tap"] == {"key": "scan", "ms": 3000, "scrolled": 0, "drew": 0} and o["back"] == 0
    assert o["here"] == {"scrolled": 1, "ms": 4000}
    assert '!!$("point")' in _function(APP, "tapped")


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
