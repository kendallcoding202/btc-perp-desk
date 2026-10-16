"""BTC's live price on the dashboard: Coinbase's own, from its public market-data WebSocket, else Hyperliquid's as a
labelled proxy, else nothing while Coinbase is closed.

The page is shown to other people, so what it says about where a price comes from must be true: Coinbase's feed only
while it is fresh, the proxy named as one, the two never in one number, VT on Hyperliquid's BTC. And it must be a
good citizen on Coinbase's feed: one socket, only for the board's product, closed while hidden, retries backed off."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
APP = (DASH / "app.js").read_text()
ACT = (DASH / "activity.js").read_text()
CB_WS = "wss://advanced-trade-ws.coinbase.com"
HL = "https://api.hyperliquid.xyz/info"


def _const(name: str) -> int:
    m = re.search(rf"\b{name} = (\d+)", APP)
    assert m, name
    return int(m.group(1))


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, k = 0, src.index("{", start)
    for j in range(k, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def test_one_coinbase_socket_and_no_other_new_host():
    """The only hosts app.js talks to are Hyperliquid's API and Coinbase's public market-data feed (no key); one
    WebSocket, made in one place; activity.js and spider.js open none."""
    assert set(re.findall(r"(?:https?|wss?)://[^\s\"'`)]+", APP)) == {HL, CB_WS}
    assert f'const CB_WS = "{CB_WS}";' in APP
    assert APP.count("new WebSocket(") == 1 and "new WebSocket(CB_WS)" in APP
    assert "advanced-trade-ws-user" not in APP and "jwt" not in APP.lower()
    for name in ("activity.js", "spider.js"):
        assert "WebSocket" not in (DASH / name).read_text(), name


def test_it_subscribes_to_the_boards_product_only():
    """Heartbeats and the ticker, one channel per message, for the product the board names: never a hard-coded one."""
    on_open = APP[APP.index("ws.onopen"):APP.index("ws.onmessage")]
    assert on_open.count("ws.send(") == 2
    assert '{ type: "subscribe", channel: "heartbeats" }' in on_open
    assert '{ type: "subscribe", channel: "ticker", product_ids: [cb.sub] }' in on_open
    assert "cb.product = s.venue.product" in APP and "sub: cb.product" in APP
    for js in (APP, ACT):
        assert not re.search(r"BIP-|-CDE\b|BTC-USD", js)
    msg = _function(APP, "cbMessage")
    assert 'm.channel === "heartbeats"' in msg and 'm.channel !== "ticker"' in msg
    assert "k.product_id !== cb.sub" in msg and "JSON.parse(raw)" in msg and "isFinite(x)" in msg
    assert '/^\\d+(\\.\\d+)?$/.test(k.price)' in msg      # "0x13a00", " 70000 ", "8.1e4" aren't prices


def test_freshness_throttle_and_backoff_numbers():
    """Fresh = a heartbeat within 5 s (they come every second) and a ticker message on this connection within a minute
    (its price is the last trade, still the price in a quiet spell). At most one redraw a second from the ticker.
    Retries from 2 s doubling to 60 s with jitter, never two a second. The proxy stops being called live when the desk
    monitor says "no live price", and the monitor leaves Coinbase's freshness to app.js."""
    assert _const("CB_BEAT") == 5000 and _const("CB_QUIET") == 60000 and _const("CB_DEAD") == 10000
    act = lambda name: int(re.search(rf"\b{name} = (\d+)", ACT).group(1))
    assert _const("STALE") == act("NO_TICK") == 20000 and act("FEED_QUIET") > _const("CB_QUIET")
    assert _const("CB_PAINT") >= 1000
    assert _const("CB_BASE") == 2000 and _const("CB_MAX") == 60000 and _const("CB_GAP") >= 1000
    fresh = APP[APP.index("const cbFresh"):APP.index("function cbClosed")]
    assert "near(now, cb.beat) < CB_BEAT" in fresh and "near(now, cb.at) < CB_QUIET" in fresh and "cb.at > 0" in fresh
    assert "const near = (now, ...ts) => Math.min(...ts.map((t) => Math.abs(now - t)));" in APP   # either way: tests/test_dashboard_polish.py
    retry = _function(APP, "cbRetry")
    assert "Math.min(CB_MAX" in retry and "Math.random()" in retry
    assert "cb.last + CB_GAP" in _function(APP, "cbConnect")


def test_closed_while_hidden_and_when_not_needed():
    assert re.search(r'addEventListener\("visibilitychange", \(\) => \{ if \(document\.hidden\) cbClose\(\);', APP)
    wanted = APP[APP.index("const cbWanted"):APP.index("function cbSync")]
    assert "!document.hidden" in wanted and "!!cb.product" in wanted
    assert "ws.close(1000)" in _function(APP, "cbClose")


def test_vt_and_the_other_tabs_keep_hyperliquid():
    """live.mids is only ever Hyperliquid's allMids answer, so VT's BTC sleeve and ETH / SOL never see Coinbase's price;
    Hyperliquid's forming candle is still never drawn on the Coinbase chart."""
    assert re.findall(r"live\.mids\s*=[^=]", APP) == ["live.mids = "]
    assert 'live.mids = await hl({ type: "allMids" })' in APP
    assert not re.search(r"live\.mids\[[^\]]+\]\s*=[^=]", APP)
    assert "c && !s.venue && live.candleCoin" in APP
    lp = _function(APP, "livePrice")
    assert 'if (!v) return { source: "hyperliquid"' in lp      # no venue: today's Hyperliquid price


def test_labels_name_the_source():
    for text in ('dot("LIVE (COINBASE)")', '" (HYPERLIQUID PROXY)"', '" (HL proxy)"', '"Price (HL proxy)"', '"Price (Coinbase live)"',
                 "s ago from Coinbase`", "s ago from Hyperliquid`", '"CLOSED (COINBASE) · "',
                 "no live price until a run finds it open", "is closed now by its schedule", '`STALE (HYPERLIQUID${v ? " PROXY" : ""}) · `',
                 "its own feed does`"):
        assert text in APP, text
    # the chart's live marker only with the chart's own fresh price: Coinbase's on BTC's (never the proxy's),
    # Hyperliquid's on the others; no candle made up for it
    assert 'if (lp.source === "coinbase" || (!s.venue && !lp.stale)) s.chart.live = mid;' in APP
    for text in ('"Coinbase feed"', "FROM COINBASE'S OWN PRICES", "A PROXY FOR COINBASE'S", 'from ${o.feed ? "Coinbase"',
                 "AT THE LAST RUN'S PRICE${basket() ? \"S\" : \"\"} (NO LIVE PRICE YET)"):
        assert text in ACT, text
    assert "d.source" in ACT and "d.priceAt" in ACT
    now = APP[APP.index("const liveNow"):APP.index("const ticked")]
    assert "source:" in now and "priceAt:" in now and "closed:" in now
    assert "...tell(liveNow()), mids: live.mids, at: live.at" in APP and "live: tell(liveNow()) });" in APP   # desk:tick, desk:board
    assert "const lv = d.live;" in ACT                                                            # the board's first draw


def test_no_stale_claim_that_a_browser_cant_read_coinbase():
    files = [*DASH.glob("*.js"), DASH / "index.html", ROOT / "README.md", *(ROOT / ".cursor/rules").glob("*.mdc")]
    for f in files:
        assert "a browser can't read Coinbase" not in f.read_text(), f.name


def test_content_security_policy_if_any_allows_the_feed():
    """There is none today; one added later must let the page reach Coinbase's feed and Hyperliquid, and nothing new."""
    html = (DASH / "index.html").read_text()
    m = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html, re.I)
    if m is None:
        assert "content-security-policy" not in html.lower()
        return
    src = re.search(r"connect-src ([^;]+)", m.group(1))
    assert src and CB_WS in src.group(1).split() and "https://api.hyperliquid.xyz" in src.group(1)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_coinbase_times_are_cut_to_milliseconds():
    """Coinbase's timestamps carry nanoseconds; Safari's Date.parse takes at most milliseconds."""
    fn = re.search(r"const cbTime = \(s\) => \{.*?\n  \};", APP, re.S).group(0)
    js = fn + """
const out = [cbTime("2026-10-15T12:52:20.546094437Z"), cbTime("2026-10-15T12:52:20Z"), cbTime("2026-10-15T12:52:20.5Z"),
  cbTime("2026-10-15 12:52:21.114074054 +0000 UTC m=+33780.245145592"), cbTime(null), cbTime(17), cbTime("junk")];
console.log(JSON.stringify(out));"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[1792068740546,1792068740000,1792068740500,0,0,0,0]"


def _node(js: str) -> str:
    return subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout.strip()


def _between(src: str, start: str, end: str) -> str:
    k = src.index(start)
    return src[k:src.index(end, k)]


# the page's own rules, run in node: cbClosed, livePrice and cbFresh as app.js has them, around a stub feed
_STUB = """
const live = { mids: { BTC: "80000" }, at: 0 };
const cb = { ws: null, sub: "P", at: 0, beat: 0, price: 81230 };
const open = (now) => { cb.ws = { readyState: 1 }; cb.at = now - 500; cb.beat = now - 500; };
const v = (o) => ({ name: "coinbase", product: "P", closed_now: null, session: { next_closure: null }, ...o });
const S = (o) => ({ venue: v(o), header: { coin: "BTC" } });
const show = (r) => [r.source, r.closed ? r.closed.by : null, !!r.stale];
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_where_the_price_comes_from():
    """Closed as the last run found it (a scheduled closure: until its end, then until Coinbase's own feed is fresh
    again), closed by the schedule, Coinbase's own while fresh, else the proxy, stale once Hyperliquid is 20 s quiet
    (another coin's Hyperliquid price too: ETH's chart and ticker stop calling it live then)."""
    rules = _between(APP, "const CB_BEAT", "  // quiet: the ticker") + _between(APP, "const cbFresh", "  const cbWanted")
    js = rules + _STUB + """
const now = Date.now(), out = {};
const sched = { why: "weekly break", until: now - 60000, scheduled: true };
live.at = now - 1000;
out.unscheduled_feed_on = (open(now), show(livePrice(S({ closed_now: { why: "x", until: null, scheduled: false } }), now)));
out.scheduled_before_end = show(livePrice(S({ closed_now: { ...sched, until: now + 60000 } }), now));
out.scheduled_past_end_fresh = show(livePrice(S({ closed_now: sched }), now));
out.window_now = show(livePrice(S({ session: { next_closure: { start: now - 1000, end: now + 1000, why: "maintenance" } } }), now));
out.window_over_fresh = show(livePrice(S({ session: { next_closure: { start: now - 9000, end: now - 1000, why: "maintenance" } } }), now));
out.fresh = show(livePrice(S({}), now));
out.quiet_59s = (cb.at = now - 59000, show(livePrice(S({}), now)));
out.quiet_61s = (cb.at = now - 61000, show(livePrice(S({}), now)));
out.no_heartbeat = (open(now), cb.beat = now - 5100, show(livePrice(S({}), now)));
out.scheduled_past_end_not_fresh = show(livePrice(S({ closed_now: sched }), now));
out.proxy_stale = (live.at = now - 21000, show(livePrice(S({}), now)));
out.other_coin = show(livePrice({ venue: null, header: { coin: "ETH" } }, now));
console.log(JSON.stringify(out));"""
    import json
    out = json.loads(_node(js))
    assert out == {
        "unscheduled_feed_on": [None, "run", False], "scheduled_before_end": [None, "run", False],
        "scheduled_past_end_fresh": ["coinbase", None, False], "window_now": [None, "schedule", False],
        "window_over_fresh": ["coinbase", None, False], "fresh": ["coinbase", None, False],
        "quiet_59s": ["coinbase", None, False], "quiet_61s": ["hyperliquid", None, False],
        "no_heartbeat": ["hyperliquid", None, False], "scheduled_past_end_not_fresh": [None, "run", False],
        "proxy_stale": ["hyperliquid", None, True], "other_coin": ["hyperliquid", None, True]}


def test_a_quiet_ticker_reconnects_and_then_takes_an_update():
    """A minute with no ticker message (heartbeats still coming), outside a closure the board knows of: the connection
    is dropped and retried through the backoff, which isn't reset for it. After that, or a closure, a snapshot (the
    price from before) doesn't count until an update does."""
    watch = _function(APP, "cbWatch")
    assert "cbClosed(cb.venue, now)" in watch and "cb.at = 0; cb.quiet = true;" in watch
    assert "near(now, cb.at, cb.opened, cb.calm) > CB_QUIET) { cb.quiet = true; cbDrop(true); return; }" in watch
    assert "if (up > 60000 && !quiet) cb.tries = 0;" in _function(APP, "cbDrop")
    msg = _function(APP, "cbMessage")
    assert 'upd = upd || e.type === "update"' in msg
    assert "if (!upd && (cb.quiet || (cb.venue && cb.venue.closed_now))) return;" in msg
    assert msg.index("if (cbClosed(cb.venue, now)) { cb.quiet = true; return; }") < msg.index("cbPaint();")


def test_the_btc_card_follows_the_same_rules_on_every_tab():
    """On the other tabs the BTC card is priced by livePrice on its venue from index.json's row (the same run's
    closures), else the BTC board's as last shown: the board's price while Coinbase is closed. tests/test_live_cards.py
    runs it."""
    mark = _function(APP, "cardMark")
    assert "livePrice({ venue: rowVenue(c) || cb.venue, header: { coin: c.coin } })" in mark
    assert "price: price > 0 ? price : c.price" in mark
    assert "cb.venue = s.venue;" in APP and "cb.venue = rowVenue(cbRow);" in APP


def test_listeners_hear_a_change_of_source_at_once():
    """Any redraw that changes where the price comes from (the page shown again, a closure, the feed going quiet)
    fires desk:tick; a Coinbase tick only with a new Coinbase message; an allMids tick once per answer."""
    render = _function(APP, "render")
    assert "told.source !== n.source" in render and render.rstrip("}").rstrip().endswith("ticked();")
    assert 'state.livePx.source === "coinbase" && (!told || told.priceAt !== cb.at)) ticked();' in _function(APP, "cbPaint")
    poll = _function(APP, "pollMids")
    assert 'livePrice(base).source === "coinbase") cbPaint();' in poll and "if (!told || told.at !== live.at) ticked();" in poll


def test_the_tape_has_one_line_per_real_event():
    feed = _function(ACT, "tapeFeed")
    assert "tk.at === feedAt" in feed and "feedAt = tk.at" in feed and "performance.now()" in feed
    assert "const answer = !!d.at && d.at !== midsAt;" in ACT and "else if (answer) setTimeout(" in ACT


def test_a_price_redraw_leaves_the_boards_own_panels_alone():
    """A Coinbase redraw (render(true)) skips the agents, bubbles, P&L bars, rules and switches; panels it does redraw
    are rewritten only when their text changes."""
    render = _function(APP, "render")
    for part in ("if (!light) renderAgents(s.agents);", "if (!light) renderBubbles(s.agents);", "if (!light) drawPnlBars(",
                 "if (!light) { if (basket) renderRules(s.basket.rules); else renderThresholds(s.thresholds); renderKills(s.kills); }"):
        assert part in render, part
    assert "render(true);" in _function(APP, "cbPaint")
    assert "if (light) return;" in _function(APP, "renderAnalysis")
    for el in ("stats", "outlook", "gates", "events", "kill-state", "jevbox", "goal-note", "chart-sub"):
        assert f'put($("{el}"), ' in APP, el
    assert "put(chips, " in APP
    assert "toLocaleString" not in APP[APP.index("const usd ="):APP.index("const esc =")]   # formatters made once


def test_tab_row_taps_only_switch_on_a_card():
    """A tap on the ALL total or a gap in the row reloads nothing (body carries data-coin too)."""
    handler = _between(APP, '$("coins").addEventListener("click"', "});")
    assert 'closest(".coin[data-coin]")' in handler and '$("coins").contains(b)' in handler


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_live_tag_is_clear_of_the_other_tags():
    """The right edge's tags (the live price's, the newest close's, the stop's) are 17 px apart at least (tags are 16
    tall), in the order of their prices (a stop at the same price on its own side: below a long's price, above a
    short's), inside the canvas, none left out. The live tag stays at its price unless the tags past it have no room
    before the canvas's edge; the others are at theirs or pushed on past it. Checked over every few pixels. When the
    newest candle's close is the live price (BTC's forming candle carried on with Coinbase's feed, ETH's and SOL's
    from Hyperliquid), the live tag says it: no second tag. A price label under any tag is left out; "MT" moves left."""
    draw = _function(APP, "drawChart")
    assert "const merged = chart.live && chart.live === lastC;" in draw and "if (!merged) tag(g, w - padR, ty[1]" in draw
    assert "if (under(y)) continue;" in draw and "const tzW = under(h - 7) ? g.measureText(TZ).width + 6 : 0;" in draw
    assert "g.fillText(TZ, tzW ? w - padR - tzW + 2 : w - padR + 6, h - 4);" in draw
    js = _function(APP, "stackTags") + """
const out = { n: 0, bad: [], moved: 0, same: 0 }, top = 8, bot = 252;
for (let close = 12; close <= 248; close += 7) for (const stop of [null, ...Array.from({ length: 28 }, (_, k) => 12 + 8.5 * k)])
for (const side of ["long", "short"]) for (let live = 12; live <= 248; live += 3) {
  const merged = live === close, tags = [{ y: live, tie: 0 }, merged ? null : { y: close, tie: 0 },
    stop == null ? null : { y: stop, tie: side === "short" ? -1 : 1 }];
  const ty = stackTags(tags, 0, top, bot), shown = tags.map((t, k) => t && [t.y, t.tie, ty[k]]).filter(Boolean);
  out.n++; if (merged) out.same++;
  if (tags.some((t, k) => t && ty[k] == null)) out.bad.push(["left out", close, stop, live]);
  const s = shown.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  for (let k = 1; k < s.length; k++) if (s[k][2] - s[k - 1][2] < 17 - 1e-9) out.bad.push(["close", close, stop, live, ty]);
  if (shown.some((t) => t[2] < top || t[2] > bot)) out.bad.push(["outside", close, stop, live, ty]);
  const m = s.findIndex((t) => t === shown[0]), room = Math.min(live - top - 17 * m, bot - live - 17 * (s.length - 1 - m));
  if (ty[0] !== live) { out.moved++; if (room >= 0) out.bad.push(["moved", close, stop, live, ty]); }
  shown.forEach((t) => { if (Math.abs(t[2] - t[0]) > 17 * 2 + 1e-9) out.bad.push(["pushed far", close, stop, live, ty]); });   // at most past two tags
}
console.log(JSON.stringify({ n: out.n, moved: out.moved, same: out.same, bad: out.bad.slice(0, 5), nbad: out.bad.length }));"""
    r = __import__("json").loads(_node(js))
    assert r["n"] > 50000 and r["nbad"] == 0 and r["same"] > 100, r
    assert 0 < r["moved"] < r["n"] / 50, r      # moved only at the canvas's very edge, with tags past it


def test_readme_pins_no_test_count():
    """A number of tests in the README goes stale with the next test added."""
    line = next(x for x in (ROOT / "README.md").read_text().splitlines() if x.startswith("python3 -m pytest"))
    assert not re.search(r"\d+ tests", line), line
