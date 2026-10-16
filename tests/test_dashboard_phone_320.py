"""Three readouts a 320 px phone cut off (each fails on 3fd4a61); the 1280 px desktop is as it was:

- the rules panel (thresholds.py): a gate's name ended in an ellipsis ("drawdown_ha…", "momentum_al…"). At 420 px and
  under it wraps in its own column, at its underscores (app.js puts a <wbr> after each), the value columns as they were;
- an account tab's line ("+0.00% · FLAT · 0W 0L · risk 5% · coinbase") was clamped to two lines and lost its end: at
  420 px and under it shows whole, and a row's tabs keep their lines at the top when one runs a line longer;
- the desk monitor: "STALE · NO NEW BOARD FOR 15H 58M" lost its end. activity.css meant a warning to wrap on a phone,
  but its rule asked for .act-warn on .act-st, and activity.js sets it on the line inside; and "REPLAY · LAST RUN
  2:22 PM MT" lost its time beside a long count ("13 pass · 1 wait · 4 drop"): the count goes under it at 420 px.

CSS rule assertions, and the page's own name split run in node."""

from __future__ import annotations

import json
import re
import subprocess

from test_dashboard_phone_words import ACTIVITY, APP, DASH, NODE, ROOT, STYLE, THEME, _media, _rule, _top, node

ACT_JS = (DASH / "activity.js").read_text()


def _decls(body: str) -> dict:
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if d.strip())}


def _after(css: str, first: str, then: str) -> bool:
    return css.index(f"@media {first} {{") < css.rindex(f"@media {then} {{")


# ---------------------------------------------------------------- the rules panel

# every gate name today's boards carry: desk/filter.py's, the brakes' and Coinbase's
NAMES = sorted(set(re.findall(r'_(?:hard|soft)\("([a-z_]+)"', (ROOT / "desk" / "filter.py").read_text())) | {
    "drawdown_halt", "daily_loss", "cooldown", "funding_known", "setup", "desk_size_usd", "contracts", "margin"})


def test_a_rules_name_wraps_in_its_column_on_a_narrow_phone_and_stays_one_line_on_a_desktop():
    narrow = _decls(_rule(_media(STYLE, "(max-width: 420px)"), ".thr-row .n"))
    assert narrow["white-space"] == "normal" and narrow["text-overflow"] == "clip"
    assert narrow["overflow-wrap"] == "anywhere"          # only a piece longer than the column: none today
    assert _after(STYLE, "(max-width: 560px)", "(max-width: 420px)")      # same specificity: the later one wins
    desk = _decls(_rule(_top(STYLE), ".thr-row .n"))
    assert (desk["white-space"], desk["overflow"], desk["text-overflow"]) == ("nowrap", "hidden", "ellipsis")
    # the value columns as they were, so the name's wrap never pushes them out
    assert "grid-template-columns: minmax(0, 1fr) 70px 70px 14px" in _rule(_media(STYLE, "(max-width: 560px)"), ".thr-row")
    assert "grid-template-columns: minmax(0, 1fr) 90px 90px 16px" in _rule(_top(STYLE), ".thr-row")
    for css in (THEME, ACTIVITY):
        assert not re.search(r"\.thr-row \.n\s*\{", css)


@node
def test_a_rules_name_breaks_at_its_underscores_and_reads_the_same():
    m = re.search(r'<span class="n">\$\{esc\(r\.name\)\.replace\((/_/g), "(_<wbr>)"\)\}</span>', APP)
    assert m, "the rules row's <wbr>s"
    out = subprocess.run([NODE, "-e", """
const re = eval(process.argv[1]), to = process.argv[2], ns = JSON.parse(process.argv[3]);
console.log(JSON.stringify(ns.map((n) => n.replace(re, to))));""", m.group(1), m.group(2), json.dumps(NAMES)],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    got = dict(zip(NAMES, json.loads(out)))
    assert len(NAMES) >= 20
    assert got["momentum_already_spent"] == "momentum_<wbr>already_<wbr>spent"    # before: "momentum_al…"
    assert got["drawdown_halt"] == "drawdown_<wbr>halt" and got["cooldown"] == "cooldown"
    for n, g in got.items():
        assert g.replace("<wbr>", "") == n                 # a <wbr> has no text: the name reads, copies and finds the same
        # each piece fits the name's column on a 320 px phone: about 85 px, 12 letters of 11.5 px mono
        assert max(len(p) for p in g.split("<wbr>")) <= 12, g


# ---------------------------------------------------------------- the account tabs

def test_an_account_tabs_line_shows_whole_on_a_narrow_phone():
    narrow = _media(STYLE, "(max-width: 420px)")
    sub = _decls(_rule(narrow, ".coin .c-sub"))
    assert sub["-webkit-line-clamp"] == "none" and sub["display"] == "block"   # before: two lines, "· risk 5% ·…"
    assert "align-content: start" in _rule(narrow, ".coin")      # a row's shorter tab keeps its lines at the top
    assert _after(STYLE, "(max-width: 560px)", "(max-width: 420px)")
    assert "-webkit-line-clamp: 2" in _rule(_media(STYLE, "(max-width: 560px)"), ".coin .c-sub")   # 421 to 560 px
    desk = _decls(_rule(_top(STYLE), ".coin .c-sub"))
    assert (desk["white-space"], desk["text-overflow"]) == ("nowrap", "ellipsis")
    assert ".coin .c-sub { white-space: normal; }" in _media(STYLE, "(min-width: 561px)")       # the desktop wraps
    # an account whose step failed keeps its own three-line error (app.js notice() says it in full)
    assert "-webkit-line-clamp: 3" in _rule(_top(STYLE), ".coin.err .c-sub")


# ---------------------------------------------------------------- the desk monitor

def test_the_monitors_warning_wraps_on_a_phone():
    # activity.js sets act-warn on the status line itself, the first span inside .act-st
    assert '<span class="act-st"><span data-k="st">' in ACT_JS
    assert 'M.st.classList.toggle("act-warn", old || lost);' in ACT_JS
    phone = _media(ACTIVITY, "(max-width: 560px)")
    assert ".act-st > span:first-child { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }" in phone
    # (0,3,1) over the one-line rule's (0,2,1); before, ".act-st.act-warn > span" matched nothing
    assert ".act-st > span.act-warn:first-child { white-space: normal; }" in phone
    assert ".act-st.act-warn > span" not in ACTIVITY
    for body in re.findall(r"\.act-st[^{]*\{([^}]*)\}", _top(ACTIVITY)):      # the desktop's line as it was
        assert "white-space" not in body and "overflow" not in body


def test_the_replay_labels_count_goes_under_it_on_a_narrow_phone():
    narrow = [b for b in re.findall(r"@media \(max-width: 420px\) \{(.*?)\n\}", ACTIVITY, re.S) if ".act-read" in b]
    assert len(narrow) == 1                                  # with the other readouts that wrap rather than lose an end
    sh = _decls(_rule(narrow[0], ".act-sh"))
    assert sh["flex-wrap"] == "wrap" and sh["white-space"] == "normal" and sh["row-gap"] == "0"
    assert _rule(narrow[0], ".act-sh > span:first-child").strip() == "text-overflow: clip;"
    desk = _top(ACTIVITY)
    assert "white-space: nowrap; overflow: hidden;" in _rule(desk, ".act-sh")     # one line from 421 px, as it was
    assert "text-overflow: ellipsis" in _rule(desk, ".act-sh > span:first-child")
    assert "@media (max-width: 340px) { .act-sh .act-tz { display: none; } }" in ACTIVITY   # 320 px: the zone still goes
    assert '<div class="act-sh"><span data-k="rl">REPLAY · LAST RUN</span><span class="act-cnt" data-k="cnt">' in ACT_JS
