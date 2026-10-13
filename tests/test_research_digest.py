"""dashboard/research.json: the research lines the dashboard's desk log shows. Every line must be what the repo's
research files say: its quote verbatim from its source, every number in it found in that quote, each account's
lines about that account only, and no backtest return percentages (Sharpe, worst drop and the chance of no edge
only). The page is shown to other people."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIGEST = json.loads((ROOT / "dashboard/research.json").read_text())
ITEMS = DIGEST["items"]
TABS = {"all", "BTC", "ETH", "SOL", "VT"}
NUM = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _quotes(item) -> list[str]:
    q = item["quote"]
    return q if isinstance(q, list) else [q]


def test_every_item_is_well_formed():
    ids = [x["id"] for x in ITEMS]
    assert len(ids) == len(set(ids))
    for x in ITEMS:
        assert x["tab"] in TABS, x["id"]
        assert x["tag"] in {"RE-AUDIT", "HUNT", "BACKTEST", "CHECK"}, x["id"]
        assert x["text"] and _quotes(x) and all(_quotes(x)), x["id"]
        assert (ROOT / x["src"]).is_file(), x["src"]


def test_every_quote_is_verbatim_from_its_source():
    for x in ITEMS:
        src = _flat((ROOT / x["src"]).read_text())
        for q in _quotes(x):
            assert _flat(q) in src, f"{x['id']}: not in {x['src']}: {q!r}"


def test_every_number_shown_is_in_its_quote():
    for x in ITEMS:
        said = {n for q in _quotes(x) for n in NUM.findall(q)}
        if "voted" in x:
            said.add(str(x["voted"]))
        for n in NUM.findall(x["text"]):
            assert n in said, f"{x['id']}: {n} is not in its quote"


def test_the_checker_count_matches_all_results():
    """'11 judged by independent checkers' = the entries in ALL_RESULTS.json's status summary that got a vote."""
    summary = json.loads((ROOT / "research/edge-hunt-2026-10/ALL_RESULTS.json").read_text())["status_summary"]
    voted = sum(len(v) for k, v in summary.items() if isinstance(v, list))
    hunt = next(x for x in ITEMS if x["id"] == "hunt")
    assert hunt["voted"] == voted == 11
    assert f"{voted} judged by independent checkers" in hunt["text"]
    assert (ROOT / hunt["src2"]).name == "ALL_RESULTS.json"


def test_each_account_gets_its_own_backtest_and_check_and_no_one_elses():
    coins = {"BTC", "ETH", "SOL", "VT"}
    for tab in coins:
        mine = [x for x in ITEMS if x["tab"] == tab]
        assert {x["tag"] for x in mine} >= {"BACKTEST", "CHECK"}, tab
        for x in mine:
            assert tab in x["text"], x["id"]
            others = {c for c in coins - {tab} if re.search(rf"\b{c}\b", x["text"])}
            assert not others, f"{x['id']} mentions {others}"
    sol = " ".join(x["text"] for x in ITEMS if x["tab"] == "SOL")
    assert "no demonstrated edge" in sol
    assert "switched on after missing their own bar" in " ".join(x["text"] for x in ITEMS if x["tab"] == "ETH")
    assert "0.5–0.7" in " ".join(x["text"] for x in ITEMS if x["tab"] == "VT")


def test_no_return_percentages_or_promises():
    for x in ITEMS:
        assert not re.search(r"[+−-]\s?\d[\d,.]*%", x["text"]), f"{x['id']} shows a return: {x['text']}"
        assert not re.search(r"\b(guarantee|will make|profit)", x["text"], re.I), x["id"]
    for x in ITEMS:
        if x["tag"] == "BACKTEST":
            assert "Sharpe" in x["text"], x["id"]


def test_the_reaudit_schedule_is_reaudit_md_s():
    """The page computes the next re-audit and the new-data days from these; REAUDIT.md is the source (the report's
    'first use in April 2027' predates the January schedule)."""
    text = _flat((ROOT / "research/edge-hunt-2026-10/REAUDIT.md").read_text())
    s = next(x for x in ITEMS if x["id"] == "reaudit")["schedule"]
    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]
    months = ", ".join(names[m - 1] for m in s["months"][:-1]) + f" and {names[s['months'][-1] - 1]}"
    assert f"runs on the 3rd of {months}" in text and s["day"] == 3
    assert f"Everything from **{s['new_data_from']}** on is new" in text
    assert "At least **12 months** of new data" in text and s["need_days"] == 365
    assert "never changes anything" in text


def test_no_banned_words():
    banned = re.compile(r"\blearn|\btrain|self-improv|adaptive|neural|\bAI\b|thinking|optimi[sz]ing", re.I)
    for x in ITEMS:
        assert not banned.search(x["text"]), x["id"]
    assert not banned.search(DIGEST["about"])
