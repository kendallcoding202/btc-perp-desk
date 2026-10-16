"""The comic lab-notebook theme's hooks in the dashboard (theme-comic.css, edition.js and small hooks in app.js,
spider.js and activity.js).

Other branches edit app.js and activity.js too. A merge that takes their side of a hunk can drop a hook without
breaking anything visible in a test of their own: the loss mark on a losing HOLD, the CLOSED chip, the +/- in the
price-check tape. These checks fail when that happens. They also pin what the hooks promise: with no theme loaded
the page is drawn as before (the same ok / bad classes, today's triangles and filled candles), and arrows on the
page mean money only."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
APP = (DASH / "app.js").read_text()
ACT = (DASH / "activity.js").read_text()
SPIDER = (DASH / "spider.js").read_text()
CSS = (DASH / "theme-comic.css").read_text()
HTML = (DASH / "index.html").read_text()


def test_index_loads_the_theme_after_the_page_styles_and_the_switch_last():
    assert HTML.index('href="style.css"') < HTML.index('href="activity.css"') < HTML.index('href="theme-comic.css"')
    assert HTML.index('src="activity.js"') < HTML.index('src="edition.js"')
    assert '<meta name="color-scheme" content="light dark">' in HTML
    metas = re.findall(r'<meta name="theme-color" content="(#[0-9a-f]{6})" media="\(prefers-color-scheme: (light|dark)\)">', HTML)
    assert [m[1] for m in metas] == ["light", "dark"]
    # the head applies a picked edition before the first paint, and survives a browser that refuses localStorage
    assert re.search(r"<script>try\{.*localStorage\.getItem\(\"desk-edition\"\).*\}catch\(x\)\{\}</script>", HTML)


def test_fonts_come_from_google_fonts_only_and_the_lettering_has_no_digits_or_signs():
    hosts = set(re.findall(r'href="https?://([^/"]+)', HTML))
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}, hosts
    subsets = dict(re.findall(r'family=(Bangers|Comic\+Neue)[^"&]*&display=swap&text=([^"]+)"', HTML))
    assert set(subsets) == {"Bangers", "Comic+Neue"}
    for family, text in subsets.items():
        plain = unquote(text)
        assert re.search(r"[A-Za-z]{52}", plain), family           # the letters are in it
        assert not re.search(r"[0-9$%+]", plain), family           # every number falls through to JetBrains Mono
        assert ("-" in plain) == (family == "Bangers"), family     # headings hold no negative number; labels can


def test_app_js_money_spans_get_their_classes_from_tone():
    assert APP.count("tone(t.hold_pct, 4)") >= 3                 # the BTC/ETH/SOL ticker, the VT ticker, bal-hold
    assert 'class="${tone(t.hold_pct, 4)}"' in APP
    assert '<span class="${tone(s.ticker.pnl)}">P&amp;L' in APP
    assert '$("bal-hold").className = tone(t.hold_pct, 4)' in APP
    assert '$("balance").className = `big ${tone(t.pnl)}`' in APP
    assert "tone(made)" in APP and "tone(net ? bank - put : bank - start)" in APP
    assert 'closedTag("CLOSED (COINBASE) · ")' in APP


def _tone():
    m = re.search(r"const tone = (\(x, dp = 2\) => .*?);\n", APP)
    assert m, "tone() is gone from app.js"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_tone_keeps_the_old_ok_bad_and_adds_the_direction():
    js = f"const tone = {_tone()};\nconsole.log(JSON.stringify([-1, -0.004, 0, 0.004, 1, null, NaN].map((x) => tone(x))));" \
         f"\nconsole.log(JSON.stringify([-0.00004, 0.00004, -0.0001].map((x) => tone(x, 4))));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout.splitlines()
    # ok / bad exactly as `x < 0 ? "bad" : "ok"` always gave (a loss that rounds to zero stays bad without the theme)
    assert json.loads(out[0]) == ["bad down", "bad flat", "ok flat", "ok flat", "ok up", "ok", "ok"]
    assert json.loads(out[1]) == ["bad flat", "ok flat", "bad down"]


def test_canvas_colors_and_shapes_come_from_tokens_with_todays_fallback():
    for hook in ('css("--channel") || rgba("--muted", .55)', 'css("--channel") || rgba("--muted", .35)',
                 'css("--entry") || "rgba(241,241,246,.6)"', 'css("--web-dot") || "#fff"',
                 'css("--web-silk") || css("--cyan") || "#2ee6ff"', 'css("--candle-up") === "hollow"',
                 'css("--entry-mark") === "pin"', 'css("--spider-body")'):
        assert hook in APP, hook
    # without the pin token the entries are today's triangles, in the gain / loss colors
    assert 'g.fillStyle = css(short ? "--red" : "--green");' in APP
    assert 'window.addEventListener("desk:theme"' in APP
    assert '"--spider-body"' in SPIDER and 'addEventListener("desk:theme"' in SPIDER


def test_the_theme_turns_the_shape_hooks_on():
    root = CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))]
    assert re.search(r"--candle-up:\s*hollow;", root)
    assert re.search(r"--entry-mark:\s*pin;", root)


def test_arrows_mean_money_only():
    # the scripts write no arrow glyph at all (the price-check tape says a move as + / −): the theme's CSS draws the
    # money marks, from tone()'s classes
    for name, text in (("app.js", APP), ("activity.js", ACT), ("spider.js", SPIDER)):
        assert "▲" not in text and "▼" not in text, name
    # the theme draws its up / down marks only on tone()'s classes, never on the LONG / SHORT pill
    for sel in re.findall(r"([^{}]*)::before\s*[,{]", CSS):
        if "clip-path" in CSS[CSS.index(sel):CSS.index("}", CSS.index(sel))]:
            assert ".hl" not in sel, sel
    assert not re.search(r"\.hl[^{]*::(before|after)", CSS)


def test_only_live_gets_the_red_badge():
    assert 'dataset.mode = L.lmode.dataset.mode = mode.toLowerCase()' in ACT
    live = re.search(r'\.act-pill\[data-mode="live"\]\s*\{([^}]*)\}', CSS)
    assert live and "--brand-red" in live.group(1)
    other = re.search(r"\.act-pill\.act-livemode\s*\{([^}]*)\}", CSS)
    assert other and "--brand-red" not in other.group(1)


def _block(start: str) -> dict:
    i = CSS.index(start)
    body = CSS[CSS.index("{", i + len(start) - 1) + 1:CSS.index("}", i)]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


def test_the_night_edition_is_the_same_tokens_twice():
    auto = _block(':root:not([data-edition="day"]) {')
    picked = _block(':root[data-edition="night"] {')
    assert auto and auto == picked
    day = _block(":root {")
    assert set(auto) <= set(day)        # night only re-tunes tokens the day edition has


def test_the_spider_never_sits_on_money():
    m = re.search(r'const MONEY = "([^"]+)"', SPIDER)
    assert m
    for sel in (".c-bank", "#balance", "#bal-hold", "#ticker", "#vt-ticker", ".card .value", ".act-monitor"):
        assert sel in m.group(1), sel
    assert "spot = offMoney(spot);" in SPIDER
