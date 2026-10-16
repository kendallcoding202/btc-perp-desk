"""The agents' notes off the chart, and each card pointing at what its note is about. Each test fails on 3fd4a61.

The owner, on a laptop, read the agents' caption boxes on BTC's chart (SCAN, VET, CROWD, RISK, SIZE, FILLS) as noise:
they sat at fixed spots over the plot (BUBBLE_SPOTS), unrelated to what they mentioned, covering candles, and said
what the cards already said, cut to one line there. Now nothing sits over the plot at any width (the chief's line
stays beside the chart), a card's whole note wraps on the card, and hovering a card, tabbing to it or tapping it marks
on the chart the thing its note is about, only when the chart draws it: SCAN's 60-bar line with its price, VET's fast
and slow averages, RISK's and SIZE's stop and entry (a position only), FILLS' fills; CROWD, CHIEF and CRAWLER point at
nothing. The mark is drawn on a canvas of its own: the chart's own frame, its values, lines and tags, and the live
marker are as they were. The spider's web is drawn under the chart's canvas, fainter, so the candles and lines are
always drawn over its silk. The page's own functions are run in node, drawing on a canvas that records what it is
asked to draw (tests/test_live_marker.py's), with the comic theme's day and night tokens."""

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
let state = null;
const innerHeight = 800, tr = [], timers = [];
const setTimeout = (f, ms) => { timers.push([f, ms]); return timers.length; }, clearTimeout = () => {};
Object.defineProperty(g, "globalCompositeOperation", { get() { return cur.gco || "source-over"; }, set(v) { cur.gco = v; } });
g.translate = (x, y) => { tr.push([x, y]); };
const _stroke = g.stroke, _fill = g.fill;
g.stroke = function () { _stroke.call(this); ops[ops.length - 1].gco = g.globalCompositeOperation; };
g.fill = function () { _fill.call(this); ops[ops.length - 1].gco = g.globalCompositeOperation; };
canvas.classList = { on: false, toggle(c, v) { this.on = !!v; } };
canvas.rect = { top: 100, bottom: 360 }; canvas.getBoundingClientRect = () => canvas.rect;
canvas.scrolled = []; canvas.scrollIntoView = (o) => canvas.scrolled.push(o);
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
    return [k, r && r.map((x) => x.mark ? { mark: true, x: x.x, y: x.y, r: x.r } : { name: x.name, x: x.x, y: x.y, color: x.color, dash: x.dash.join(","), series: !!x.xs })]; }));
};
const pos = (o = {}) => chartOf({ stop: 79000, entry: 81000, markers: [
  { ts: T0 + 2 * H * 10, price: 80200, kind: "entry", side: "long" }, { ts: T0 + 2 * H * 20 + 600000, price: 81000, kind: "exit", side: "long", pnl: 5 },
  { ts: T0 - 10 * H, price: 80000, kind: "entry", side: "long" }], ...o });
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
    """SCAN: the 60-bar line its note names (the high, or the low a short waits on), named with its price at its right
    end; nothing while warming up or with shorts disabled. VET: the fast and slow averages, while drawn. RISK and SIZE:
    the stop and the entry, with a position only, and an entry off the plot isn't pointed at. FILLS: each fill drawn
    (one before the chart's first candle isn't), an entry's mark where its pin hangs, an exit's on its dot. CROWD,
    CHIEF and CRAWLER: nothing, ever."""
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
out.night = refs(pos(), {json.dumps(H1)}, TOKS.night);""")
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
        assert [(x["name"], x["dash"], x["series"]) for x in got] == [("stop 79,000", "6,4", False), ("entry 81,000", "2,3", False)]
        assert abs(got[0]["y"] - y["stop"]) < 1e-6 and abs(got[1]["y"] - y["entry"]) < 1e-6
        assert got[0]["x"] == got[1]["x"] == y["edge"]                            # named at the plot's end, short of the axis
        assert got[0]["color"] == DAY["--orange"] and got[1]["color"] == DAY["--entry"]
    assert [x["name"] for x in o["far"]["risk"]] == ["stop 79,000"]                 # the entry is off the plot: not it
    fills = o["pos"]["fills"]
    assert [f["mark"] for f in fills] == [True, True]                             # the one before the window: not drawn
    assert abs(fills[0]["x"] - y["x10"]) < 1e-6 and abs(fills[0]["y"] - (y["e10"] + 9)) < 1e-6 and fills[0]["r"] == 12   # the L pin
    assert abs(fills[1]["x"] - y["x20"]) < 1e-6 and abs(fills[1]["y"] - y["x21"]) < 1e-6 and fills[1]["r"] == 8           # the dot
    assert o["night"]["risk"][1]["color"] == NIGHT["--entry"]


def test_scans_mark_follows_the_words_the_desk_writes():
    """SCAN's mark is the line its note names, read from the note desk/filter.py writes ("a close above the 60-bar
    high $84,295", "... below the 60-bar low ..."): that wording is what the page reads."""
    assert "f\"a close {'above' if d > 0 else 'below'} the {s.breakout_bars}-bar {hi_lo} ${level:,.0f}\"" in FILTER
    assert 'hi_lo = "high" if d > 0 else "low"' in FILTER
    assert r'/(\d+)-bar (high|low)/.exec(a.quote || "")' in APP


# ---------------------------------------------------------------- how it is drawn

@node
def test_the_mark_is_drawn_on_its_own_canvas_and_leaves_the_chart_and_live_marker_as_drawn():
    """Pointing never redraws or changes the chart's own canvas: its frame is the same with a card pointing. On #point,
    shifted to the chart's place: the plot washed pale (never the price axis's column, never the date axis), but for a
    band along the live line, so the live line and its tag are exactly as drawn; the thing redrawn 2.5 px wide in its
    own color and dashes over an 8 px halo in the pointing card's color; each line named in a caption box at its end,
    inside the plot. A card that points at nothing draws nothing (what was drawn fades out as it was); nor does a card
    on the VT basket's tab. The cards that point can be tabbed to, the others can't."""
    o = _run("""
const ch = pos(); ch.live = ch.candles[N - 1][4];
desk(ch, %s, TOKS.night);
const before = JSON.stringify(ops);
point.key = "risk"; drawPoint();
out.risk = { on: canvas.classList.on, ops: ops.slice(), tab: tabbable() };
drawChart(ch); out.same = before === JSON.stringify(ops);
out.g = { pw: lastGeom.w - lastGeom.padR, bot: lastGeom.h - 18, yLive: lastGeom.Y(ch.live), yStop: lastGeom.Y(79000), yEntry: lastGeom.Y(81000) };
point.key = "fills"; drawPoint(); out.fills = ops.slice();
point.key = "crowd"; ops.length = 0; ops.push({ k: "kept" }); drawPoint(); out.crowd = { on: canvas.classList.on, kept: ops.length === 1 };
state = { ...state, header: { kind: "basket" } }; point.key = "risk"; drawPoint(); out.basket = { on: canvas.classList.on, tab: tabbable() };
desk(chartOf(), %s, TOKS.day); point.key = null; drawPoint(); out.flatTab = tabbable();
out.tr = tr;""" % (json.dumps(H1), json.dumps(H1)))
    g, ops = o["g"], o["risk"]["ops"]
    assert o["same"] and o["risk"]["on"] and o["tr"][0] == [6, 40]                  # the chart's place in its box
    wash = [x for x in ops if x["k"] == "fillRect" and x["x"] == 0 and x["y"] == 0]
    assert wash == [{"k": "fillRect", "color": "rgba(26,32,48,0.6)", "x": 0, "y": 0, "w": g["pw"], "h": g["bot"]}]   # night's --panel
    live = [x for x in ops if x["k"] == "stroke" and x["gco"] == "destination-out"]
    assert [(x["pts"], x["width"]) for x in live] == [([[0, g["yLive"]], [g["pw"], g["yLive"]]], 5)]                 # its band spared
    wide = [(x["pts"][0][1], x["color"], x["dash"]) for x in ops if x["k"] == "stroke" and x["width"] == 2.5]
    assert wide == [(g["yStop"], NIGHT["--orange"], "6,4"), (g["yEntry"], NIGHT["--entry"], "2,3")]
    halo = [x["color"] for x in ops if x["k"] == "stroke" and x["width"] == 8]
    assert halo == ["rgba(213,138,0,0.3)"] * 2                                     # RISK's own --orange
    assert [x["t"] for x in ops if x["k"] == "text"] == ["stop 79,000", "entry 81,000"]
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
    assert o["crowd"] == {"on": False, "kept": True}
    assert o["basket"] == {"on": False, "tab": []}
    assert sorted(o["flatTab"]) == ["scan", "vet"]                                 # flat, no fills: those three can't be tabbed to


@node
def test_hover_focus_and_tap_point_and_end_as_they_should():
    """A mouse over a card points until it leaves; keyboard focus (focus-visible) points until blur, a tap's focus
    doesn't; a tap points for 3 s, which neither a stray leave nor a blur cuts short. On a phone the cards are far
    below the chart: a tap on a card that points, with none of the chart on screen, scrolls it into view (no smooth
    scroll under reduced motion) and points for 4 s; one that points at nothing never scrolls. A touch never points by
    hovering, and the card never cancels a touch, so a swipe on it scrolls the page as before."""
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
canvas.rect = { top: -1500, bottom: -1240 };
e.h.pointerdown({ pointerType: "touch" }); e.h.click(); step("tap, chart away");
timers[point.timer - 1][0]();
const c = el(); aim(c, "crowd"); c.h.pointerdown({ pointerType: "touch" }); c.h.click(); step("crowd tap, chart away");
timers[point.timer - 1][0]();
e.h.pointerdown({ pointerType: "mouse" }); e.h.click(); step("mouse click");
e.vis = true; e.h.focus(); step("tab to it"); e.h.blur(); step("tab away");
out.log = log; out.scrolled = canvas.scrolled; out.on = Object.keys(e.h).sort();""" % json.dumps(H1))
    assert o["log"] == [["mouse in", "risk", 0], ["mouse out", None, 0], ["touch down", None, 0], ["a tap's focus", None, 0],
                        ["tap", "risk", 3000], ["leave, blur", "risk", 3000], ["3 s on", None, 0],
                        ["tap, chart away", "risk", 4000], ["crowd tap, chart away", "crowd", 3000],
                        ["mouse click", None, 0], ["tab to it", "risk", 0], ["tab away", None, 0]]
    assert o["scrolled"] == [{"block": "center", "behavior": "smooth"}]
    assert o["on"] == ["blur", "click", "focus", "pointerdown", "pointerenter", "pointerleave"]   # no touchstart, no wheel
    aim = _function(APP, "aim") + _function(APP, "tapped")
    assert "preventDefault" not in aim and 'behavior: still.matches ? "auto" : "smooth"' in aim
    assert "aim(el, a.key);" in _function(APP, "renderAgents")                     # each card, once, as it is made
    render = _function(APP, "render")
    assert "drawChart(s.chart); drawWeb(); drawPoint(); }" in render                # in step with every redraw of the chart


def test_the_point_fades_except_under_reduced_motion_and_a_pointing_card_shows_it():
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
