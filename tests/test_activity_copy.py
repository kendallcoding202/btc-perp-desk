"""dashboard/activity.js (the desk monitor and the desk log): its copy and the facts it copies from the repo.

The page is shown to other people, so: no words that say the desk learns or adapts; the stale limits, the
schedule and the gate order it shows are the repo's own; it adds no network host and no animation loop; and the
monitor and live rows never print an order word."""

import math
import random
import re
from pathlib import Path

from desk import indicators as ind
from desk import jev as jev_mod
from desk.engine import Desk
from desk.brokers.sim import SimBroker
from desk.filter import ORDER, evaluate_entry
from desk.models import Bar, FundingEvent, MarketContext
from desk.thresholds import DEFAULT, INTERVAL_SECONDS, params_for

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
JS = (DASH / "activity.js").read_text()
BANNED = re.compile(r"\blearn|\btrain|self-improv|adaptive|neural|\bAI\b|thinking|optimi[sz]ing", re.I)


def _js_map(name: str) -> dict:
    m = re.search(rf"const {name} = \{{(.*?)\}};", JS, re.S)
    assert m, name
    return dict(re.findall(r"(\w+): \"(\w+)\"", m.group(1)))


def _function(name: str) -> str:
    """The source of `function name(...) { ... }` in activity.js, by brace matching."""
    start = JS.index(f"function {name}(")
    depth, k = 0, JS.index("{", start)
    for j in range(k, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[j], 0)
        if depth == 0:
            return JS[start:j + 1]
    raise AssertionError(name)


def test_no_banned_words_in_the_new_files():
    for name in ("activity.js", "activity.css", "research.json"):
        text = (DASH / name).read_text()
        hits = [m.group(0) for m in BANNED.finditer(text)]
        assert not hits, f"{name}: {hits}"


def test_stale_limits_are_the_spiders():
    spider = (DASH / "spider.js").read_text()
    pat = r"const STALE_STATIC = (\d+) \* 60000, STALE_LIVE = (\d+) \* 60000;"
    assert re.search(pat, spider).groups() == re.search(pat, JS).groups() == ("150", "5")


def test_schedule_is_the_workflows():
    wf = (ROOT / ".github/workflows/paper.yml").read_text()
    cron = re.search(r'cron: "([^"]+)"', wf).group(1)
    assert re.search(r'const CRON = "([^"]+)";', JS).group(1) == cron
    # the heartbeat's promise is only what the workflow says: a late run catches up; nothing about a backup run
    assert "catches up on every bar it\n# missed" in wf and "a late run catches up on every close it missed" in JS
    assert "backup" not in JS


def _bars(n=900, seed=3, start=30_000.0, ms=None):
    ms = ms or INTERVAL_SECONDS["2h"] * 1000
    rnd, px, t0, out = random.Random(seed), start, 1_700_000_000_000 // ms * ms, []
    for k in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0.0005, 0.012))
        out.append(Bar(t0 + k * ms, o, max(o, c) * 1.003, min(o, c) * 0.997, c, 1.0))
        px = c
    return out, [FundingEvent(t0 + k * 8 * 3600_000, 0.0001) for k in range(n // 4)]


def test_every_gate_the_desk_makes_has_its_phase_in_filter_order():
    phase, owner_phase = _js_map("PHASE"), _js_map("OWNER_PHASE")
    phases = re.search(r"const PHASES = \[(.*?)\];", JS).group(1).replace('"', "").replace(" ", "").split(",")
    assert [p.lower().replace("data", "crawler") for p in phases] == ORDER
    p = params_for("ETH")
    bars, funding = _bars()
    f = ind.compute(bars, funding, p)
    ctx = MarketContext(mid=bars[-1].close, spread_bps=1.0, depth_usd=1e7)
    desk = Desk(p, SimBroker(p))
    seen = []
    for i in (100, len(bars) - 1):                      # warming up, then every gate
        d = evaluate_entry(p, f, i, equity=150.0, kills=desk.kill_gates(bars[i].ts, 150.0), ctx=ctx)
        seen.append(d.gates)
    pos = Desk(p, SimBroker(p))                          # in a position: the "flat" gate
    pos.broker.position = type("P", (), {"side": type("S", (), {"value": "long"})()})()
    seen.append(pos.kill_gates(bars[-1].ts, 150.0))
    seen.append(jev_mod.gates_from_answers({"momentum_already_spent": {"noul": 0.5}, "follow_through": {"noul": 0.5},
                                            "regime": {"probabilities": {"trend": 0.5}}}, p))
    seen.append(jev_mod.gates_from_answers(None, p))
    for gates in seen:
        order = []
        for g in gates:
            ph = phase.get(g.name) or owner_phase.get(g.owner)
            assert ph, f"{g.name} (owner {g.owner}) has no phase in activity.js"
            order.append(phases.index(ph))
        assert order == sorted(order), [g.name for g in gates]
    full = [g.name for g in seen[1]]
    assert set(full) <= set(phase) and len(full) == 17


def test_the_coinbase_gates_have_their_phase_too():
    """BTC on Coinbase adds the session and funding-known gates (HARD) and whole contracts and margin (SIZE)."""
    from desk import venue as venue_mod
    from desk.thresholds import COINBASE

    phase, owner_phase = _js_map("PHASE"), _js_map("OWNER_PHASE")
    phases = re.search(r"const PHASES = \[(.*?)\];", JS).group(1).replace('"', "").replace(" ", "").split(",")
    p = params_for("BTC")
    bars, funding = _bars(start=60_000.0)
    f = ind.compute(bars, funding, p)
    rules = venue_mod.Rules(COINBASE)
    desk = Desk(p, SimBroker(p))
    names = set()
    for open_, age in ((True, 3_600_000), (False, 30 * 3_600_000)):
        ctx = MarketContext(mid=bars[-1].close, spread_bps=1.0, depth_usd=1e7, session_open=open_, funding_age_ms=age)
        d = evaluate_entry(p, f, len(bars) - 1, equity=500.0, kills=desk.kill_gates(bars[-1].ts, 500.0), ctx=ctx,
                           venue=rules)
        order = []
        for g in d.gates:
            ph = phase.get(g.name) or owner_phase.get(g.owner)
            assert ph, f"{g.name} (owner {g.owner}) has no phase in activity.js"
            order.append(phases.index(ph))
            names.add(g.name)
        assert order == sorted(order), [g.name for g in d.gates]
    assert {"session", "funding_known", "desk_size_usd"} <= names and "ticket_usd" not in names
    assert phase["session"] == phase["funding_known"] == "HARD"
    assert phase["desk_size_usd"] == phase["contracts"] == phase["margin"] == "SIZE"


def test_it_adds_no_host_no_loop_and_one_same_site_request():
    assert "requestAnimationFrame" not in JS
    assert not re.search(r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(", JS)
    assert re.findall(r"\bfetch\(([^,)]+)", JS) == ['"research.json"']
    assert set(re.findall(r"https?://[^\s\"'`)]+", JS)) == {"https://api.hyperliquid.xyz/info"}
    html = (DASH / "index.html").read_text()
    assert html.index('href="style.css"') < html.index('href="activity.css"')
    assert html.index('src="spider.js"') < html.index('src="activity.js"')


def test_monitor_and_live_rows_print_no_order_word():
    """BUY / SELL only in the desk log's replay of a real VT order; never in the monitor or the live rows."""
    for name in ("meter", "vtLive", "vtStrip", "restText", "playStrip", "buildStrip", "liveRows", "countdown", "status"):
        body = _function(name)
        assert not re.search(r"\b(BUY|SELL)\b(?! LINE)", body), name
        assert not BANNED.search(body), name


def test_the_board_is_read_only_here():
    """detail.board is app.js's own copy: activity.js never assigns into it."""
    assert not re.search(r"\bB\.[\w.\[\]]+\s*(=[^=]|\+=|-=)", JS)
    assert not re.search(r"\bB\.[\w.]+\.(push|splice|sort|reverse|pop|shift|unshift)\(", JS)
    assert "Object.assign(B" not in JS


def test_every_brake_the_engine_can_pull_is_named():
    """A KILLS gate that drops holds every new entry: the monitor names it (and shows no buy line). Each gate the engine's
    kill_gates can drop, and each note manual_kill carries, has its name and its "until" from the engine's own words."""
    m = re.search(r"const BRAKE = \{(.*?)\n  \};", JS, re.S)
    brake = {k: (what, until) for k, what, until in re.findall(r'(\w+): \["([^"]+)", "([^"]+)"\]', m.group(1))}
    p = params_for("BTC")
    desk = Desk(p, SimBroker(p))
    names = {g.name for g in desk.kill_gates(0, 150.0)}
    assert names - {"manual_kill"} <= set(brake), names
    assert {"flat_kill", "entry_kill"} <= set(brake)
    engine = (ROOT / "desk/engine.py").read_text()
    assert "until a human resets it" in engine and brake["drawdown_halt"][1] == "until a human resets it"
    assert "no entries until tomorrow UTC" in engine and brake["daily_loss"][1] == "until the next UTC day"
    assert "sitting out" in engine and brake["cooldown"][0] == "losing-streak cooldown"
    assert 'note="flat_kill" if k.flat_kill else ("entry_kill"' in engine
    # braked and flat: no distance to a line, in the monitor or the live rows
    assert "NO NEW ENTRIES" in _function("meter") and "o.brake" in _function("liveRows")


def test_it_says_only_what_the_page_can_know():
    """A board that stops changing may be a desk that didn't run, or a deploy or a fetch that failed: the page says "no
    new board", and says when it can't read one. The position's figure is the price move, before fees and funding,
    and the mode word comes from the board, never a hard-coded "paper"."""
    code = re.sub(r"(?m)^\s*//.*$", "", JS)            # what it prints, not the comments that explain why
    assert not re.search(r"no desk run", code, re.I)
    assert "NO NEW BOARD FOR" in JS and "CAN'T READ THE BOARD SINCE" in JS and "CAN'T LOAD THE BOARD" in JS
    assert "before exit fees" not in JS and JS.count("before fees and funding") == 2
    for name in ("meter", "liveRows", "vtStrip"):
        assert not re.search(r"\bpaper\b", _function(name).replace("paper experiment", "")), name
