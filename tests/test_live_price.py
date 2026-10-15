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
    assert "k.product_id === cb.sub" in msg and "JSON.parse(raw)" in msg and "isFinite(x)" in msg


def test_freshness_throttle_and_backoff_numbers():
    """Fresh = a heartbeat within 5 s (they come every second) and a ticker message within 15 s (about two a second).
    At most one redraw a second from the ticker. Retries from 2 s doubling to 60 s with jitter, never two a second."""
    assert _const("CB_BEAT") == 5000 and _const("CB_QUIET") == 15000 and _const("CB_DEAD") == 10000
    assert _const("CB_PAINT") >= 1000
    assert _const("CB_BASE") == 2000 and _const("CB_MAX") == 60000 and _const("CB_GAP") >= 1000
    fresh = APP[APP.index("const cbFresh"):APP.index("function cbClosed")]
    assert "now - cb.beat < CB_BEAT" in fresh and "now - cb.at < CB_QUIET" in fresh and "cb.at > 0" in fresh
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
                 "no live price until a run finds it open", "is closed now by its schedule"):
        assert text in APP, text
    # the chart's live marker only with Coinbase's own price; no candle made up for it
    assert 'if (lp.source === "coinbase") s.chart.live = mid;' in APP
    for text in ('"Coinbase feed"', "FROM COINBASE'S OWN PRICES", "A PROXY FOR COINBASE'S", 'from ${o.feed ? "Coinbase"'):
        assert text in ACT, text
    assert "d.source" in ACT and "d.priceAt" in ACT
    now = APP[APP.index("const liveNow"):APP.index("const ticked")]
    assert "source:" in now and "priceAt:" in now and "closed:" in now
    assert "...liveNow(), mids: live.mids, at: live.at" in APP and "live: liveNow() });" in APP   # desk:tick and desk:board
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
