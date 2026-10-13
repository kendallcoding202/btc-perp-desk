"""Deposits and withdrawals (desk/flows.py, deposits.json): money in and out, never profit or loss, never a brake trip.

The owner adds $100 to the BTC account every month for 5 years, and sometimes more. None of it may read as profit:
not on the board, the tabs, the ALL card, the phone, the commit message, the goal, the go-live gate or the brakes.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from desk import board as board_mod
from desk import flows
from desk import paper
from desk.brokers.sim import SimBroker
from desk.engine import Desk
from desk.flows import Flow, FlowsError
from desk.thresholds import ACCOUNTS, VOLTREND
from test_desk import DAY, H4, P, FakeDailyFeed, FakeFeed, _vt_market, series

REPO = Path(__file__).resolve().parents[1]


def _ms(ymd: str) -> int:
    return int(datetime.fromisoformat(ymd).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _nav_ok(recs: list[dict], start: float) -> None:
    """At every flow the NAV is the same just before and just after it: the flow bought or sold units at it."""
    units = start
    for r in recs:
        before = r["equity_before"] / units
        units += r["units"]
        assert (r["equity_before"] + r["amount"]) / units == pytest.approx(before, rel=1e-12) == r["nav"]


# ---------------------------------------------------------------- the file

def test_the_btc_plan_is_60_monthly_deposits_of_100_from_2026_11_to_2031_10():
    plans = flows.load(REPO / "deposits.json")
    btc = flows.schedule(plans, "BTC")
    assert len(btc) == 60 and {f.amount for f in btc} == {100.0} and {f.kind for f in btc} == {"plan"}
    assert (btc[0].id, btc[0].date) == ("monthly-2026-11", "2026-11-01")
    assert (btc[-1].id, btc[-1].date) == ("monthly-2031-10", "2031-10-01")
    days = [date.fromisoformat(f.date) for f in btc]
    assert all(d.day == 1 for d in days) and len({(d.year, d.month) for d in days}) == 60
    assert [f.id for f in btc] == [f"monthly-{d:%Y-%m}" for d in days]
    assert all(f.ts == _ms(f.date) and f.ts % DAY == 0 for f in btc)          # 00:00 UTC on the date
    assert all(a.ts < b.ts for a, b in zip(btc, btc[1:]))
    assert all(flows.schedule(plans, acc) == [] for acc in ("ETH", "SOL", "VT"))
    # nothing is due until 2026-11-01 00:00 UTC, the first deposit from that instant, all 60 by 2031-10-01
    assert flows.due(btc, [], _ms("2026-11-01") - 1) == []
    assert [f.id for f in flows.due(btc, [], _ms("2026-11-01"))] == ["monthly-2026-11"]
    assert len(flows.due(btc, [], _ms("2031-10-01"))) == 60


def test_extras_withdrawals_comments_and_a_changed_plan_amount_parse_in_date_order():
    text = json.dumps({"_how": "comments are any key starting with _", "ETH": {
        "_note": "two plans: $100 for 3 months, then $150",
        "monthly": [{"amount": 100, "day": 5, "first": "2027-01-05", "count": 3},
                    {"amount": 150, "day": 5, "first": "2027-04-05", "count": 2, "note": "raised"}],
        "extra": [{"date": "2027-02-05", "amount": 250, "note": "bonus"}, {"date": "2027-02-05", "amount": -50},
                  {"date": "2026-12-31", "amount": 20.5}]}})
    eth = flows.schedule(flows.parse(text, ACCOUNTS), "ETH")
    assert [(f.id, f.amount) for f in eth] == [
        ("extra-2026-12-31-0", 20.5), ("monthly-2027-01", 100.0), ("monthly-2027-02", 100.0),
        ("extra-2027-02-05-0", 250.0), ("extra-2027-02-05-1", -50.0), ("monthly-2027-03", 100.0),
        ("monthly-2027-04", 150.0), ("monthly-2027-05", 150.0)]
    assert eth[3].note == "bonus" and eth[6].note == "raised" and eth[1].note == "monthly plan"


BAD = {
    "not JSON": ('{"BTC": {"extra": []}', "not valid JSON"),
    "a key twice": ('{"BTC": {"extra": []}, "BTC": {}}', "appears twice"),
    "not an account": ('{"BTCC": {}}', "isn't an account"),
    "lower case": ('{"btc": {}}', "isn't an account"),
    "not an object": ('[]', "must be one object"),
    "NaN": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": NaN}]}}', "not a number"),
    "a string amount": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": "250"}]}}', "must be a number"),
    "a bool amount": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": true}]}}', "must be a number"),
    "zero": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": 0}]}}', "non-zero"),
    "fractions of a cent": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": 12.345}]}}', "2 decimals"),
    "a typo's zeros": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": 2000000}]}}', "typo guard"),
    "no such date": ('{"BTC": {"extra": [{"date": "2026-02-30", "amount": 5}]}}', "not a real date"),
    "a US date": ('{"BTC": {"extra": [{"date": "12/15/2026", "amount": 5}]}}', "YYYY-MM-DD"),
    "a misspelt key": ('{"BTC": {"extra": [{"date": "2026-12-15", "amount": 5, "notes": "x"}]}}', "unknown key"),
    "no amount": ('{"BTC": {"extra": [{"date": "2026-12-15"}]}}', "missing"),
    "extra not a list": ('{"BTC": {"extra": {"date": "2026-12-15", "amount": 5}}}', "must be a list"),
    "a negative plan": ('{"BTC": {"monthly": {"amount": -100, "day": 1, "first": "2026-11-01", "count": 6}}}',
                        "more than 0"),
    "day 31": ('{"BTC": {"monthly": {"amount": 100, "day": 31, "first": "2026-10-31", "count": 6}}}', "1 to 28"),
    "first off its day": ('{"BTC": {"monthly": {"amount": 100, "day": 1, "first": "2026-11-02", "count": 6}}}',
                          "must fall on"),
    "count 0": ('{"BTC": {"monthly": {"amount": 100, "day": 1, "first": "2026-11-01", "count": 0}}}', "1 to 600"),
    "no count": ('{"BTC": {"monthly": {"amount": 100, "day": 1, "first": "2026-11-01"}}}', "missing"),
    "plans overlap": ('{"BTC": {"monthly": [{"amount": 100, "day": 1, "first": "2026-11-01", "count": 6}, '
                      '{"amount": 150, "day": 1, "first": "2027-02-01", "count": 6}]}}', "must not overlap"),
    "weekly": ('{"BTC": {"weekly": {"amount": 25}}}', "unknown key"),
    "an entry hidden as a comment": ('{"BTC": {"_extra": [{"date": "2026-12-15", "amount": 5}]}}', "comment key"),
}


@pytest.mark.parametrize("case", list(BAD))
def test_a_malformed_file_fails_loudly_never_silently(case):
    text, says = BAD[case]
    with pytest.raises(FlowsError, match=says):
        flows.schedule(flows.parse(text, ACCOUNTS), "BTC")


def _cmd_step(tmp_path, monkeypatch, text):
    """cmd_step over every account with the steps stubbed: what each one was given, and the index it wrote."""
    from desk import __main__ as cli

    got = {}

    def state(acc, start):
        return {"paper": {"changed": False, "new_events": []}, "updated": 1, "header": {"coin": acc},
                "ticker": {"bank": start, "pnl": 0.0, "pnl_pct": 0.0, "position": "FLAT", "position_detail": "",
                           "record": "0W 0L", "price": 1.0, "cash": start}, "goal": {"start": start, "target": 1e5,
                                                                                    "progress": 0.0},
                "basket": {"holdings": []}}

    monkeypatch.setattr(paper, "step", lambda p, d, jev=None, flows=None, **kw: got.__setitem__(
        p.market.symbol, flows) or state(p.market.symbol, 150.0))
    monkeypatch.setattr(paper, "step_basket", lambda vt, d, flows=None, **kw: got.__setitem__("VT", flows) or
                        state("VT", 500.0))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(cli, "_notify_paused", lambda paused, path: got.__setitem__("paused", [c for c, _ in paused]))
    (tmp_path / "deposits.json").write_text(text)
    args = argparse.Namespace(coins="BTC,ETH,SOL,VT", ledger=str(tmp_path / "ledger"), interval=None, risk=None,
                              jev_mode="off", coin=None, flows=str(tmp_path / "deposits.json"))
    rc = cli.cmd_step(args)
    return rc, got, {c["coin"]: c for c in json.loads((tmp_path / "ledger" / "index.json").read_text())["coins"]}


PHONE_SLIPS = {     # what a phone keyboard does to the README's line, and the fix the message gives first
    "a comma after the last entry": ('"extra": [\n  {"date": "2026-12-15", "amount": 250},\n]',
                                     "remove the comma after the last entry"),
    "no comma between two entries": ('"extra": [\n  {"date": "2026-12-15", "amount": 250}\n  {"date": "2026-12-20", '
                                     '"amount": 5}\n]', "add a comma between two entries"),
    "the entry after the brackets": ('"extra": []\n  {"date": "2026-12-15", "amount": 250}',
                                     "put the entry inside the list's brackets"),
    "the entry after the open list's ]": ('"extra": [\n    ]\n  {"date": "2026-12-15", "amount": 250}',
                                          "put the entry inside the list's brackets"),
    "curly quotes": ('"extra": [{“date”: “2026-12-15”, “amount”: 250}]', "curly quotes"),
    "$250": ('"extra": [{"date": "2026-12-15", "amount": $250}]', "plain numbers"),
    "1,000": ('"extra": [{"date": "2026-12-15", "amount": 1,000}]', "plain numbers"),
    "- 50": ('"extra": [{"date": "2026-12-15", "amount": - 50}]', "plain numbers"),
}


@pytest.mark.parametrize("case", list(PHONE_SLIPS))
def test_a_phone_slip_says_how_to_fix_it_first(case):
    extra, says = PHONE_SLIPS[case]
    text = '{\n  "BTC": {\n    "monthly": {"amount": 100, "day": 1, "first": "2026-11-01", "count": 60},\n    ' + extra + '\n  }\n}'
    with pytest.raises(FlowsError) as exc:
        flows.parse(text, ACCOUNTS)
    msg = str(exc.value)
    assert msg.startswith("deposits.json is not valid JSON: ") and says in msg.split(" (line ")[0]


def test_an_extra_dated_more_than_a_year_ahead_is_taken_for_a_typo_unless_it_says_future():
    now = _ms("2026-10-13")
    typo = json.dumps({"BTC": {"extra": [{"date": "2062-12-15", "amount": 250}]}})
    with pytest.raises(FlowsError, match=r"more than a year from today \(2026-10-13\): a typo in the year\?"):
        flows.schedule(flows.parse(typo, ACCOUNTS, now_ms=now), "BTC")
    planned = json.dumps({"BTC": {"extra": [{"date": "2028-01-15", "amount": 250, "future": True},
                                            {"date": "2027-10-01", "amount": 50}]}})
    assert [f.id for f in flows.schedule(flows.parse(planned, ACCOUNTS, now_ms=now), "BTC")] == [
        "extra-2027-10-01-0", "extra-2028-01-15-0"]
    with pytest.raises(FlowsError, match="can only be true"):
        flows.schedule(flows.parse(json.dumps({"BTC": {"extra": [{"date": "2028-01-15", "amount": 5,
                                                                  "future": "yes"}]}}), ACCOUNTS, now_ms=now), "BTC")


def test_an_extra_put_above_a_recorded_one_on_the_same_day_says_to_move_it(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    plan = _desk_plan()
    _desk_run(tmp_path, plan, list(range(301, 600, 9)))
    above = [Flow("extra-2020-12-05-0", "2020-12-05", _ms("2020-12-05"), 75.0, "extra")] + [
        Flow(f"extra-2020-12-05-{int(f.id[-1]) + 1}", f.date, f.ts, f.amount, f.kind, f.note) if f.date == "2020-12-05"
        else f for f in plan]
    with pytest.raises(FlowsError, match=r"^BTC: if you added an entry dated 2020-12-05, move it below the one "
                                         r"already there"):
        _desk_run(tmp_path, sorted(above, key=lambda f: (f.ts, f.kind != "plan", f.id)), [600])
    edited = [Flow(f.id, f.date, f.ts, 333.0, f.kind, f.note) if f.id == "extra-2020-12-05-0" else f for f in plan]
    with pytest.raises(FlowsError, match=r"^BTC: put back what was recorded"):            # an edit, nothing added
        _desk_run(tmp_path, edited, [600])


def test_a_bad_entry_stops_only_its_account_and_a_bad_file_stops_every_account(tmp_path, monkeypatch):
    text = json.dumps({"BTC": {"extra": [{"date": "2026-12-15", "amount": "250"}]},
                       "ETH": {"extra": [{"date": "2026-12-15", "amount": 25}]}})
    rc, got, idx = _cmd_step(tmp_path, monkeypatch, text)
    assert rc == 1 and "BTC" not in got and "FlowsError" in idx["BTC"]["error"] and "BTC.extra[0]" in idx["BTC"]["error"]
    assert [f.id for f in got["ETH"]] == ["extra-2026-12-15-0"] and got["SOL"] is None and got["VT"] is None
    assert all("error" not in idx[c] for c in ("ETH", "SOL", "VT")) and got["paused"] == ["BTC"]   # and the phone told
    rc, got, idx = _cmd_step(tmp_path, monkeypatch, text[:-2])               # cut off: not JSON at all
    assert rc == 1 and all("not valid JSON" in idx[c]["error"] for c in ACCOUNTS) and got == {"paused": list(ACCOUNTS)}
    assert idx["BTC"]["error"].endswith("so no deposit or withdrawal is skipped")   # the whole message, its fix first
    rc, got, idx = _cmd_step(tmp_path, monkeypatch, (REPO / "deposits.json").read_text())   # the real file
    assert rc == 0 and len(got["BTC"]) == 60 and got["ETH"] is got["SOL"] is got["VT"] is None and "paused" not in got


# ---------------------------------------------------------------- paper: the desk accounts

def _desk_plan() -> list[Flow]:
    """For the synthetic 4h series (bars 300-699: 2020-11-02 to 2021-01-08): a monthly plan whose last deposit is
    after the data, an extra dated before the account opened, a withdrawal, and two extras on one day."""
    return flows.schedule(flows.parse(json.dumps({"BTC": {
        "monthly": {"amount": 1000, "day": 10, "first": "2020-11-10", "count": 3},
        "extra": [{"date": "2020-10-01", "amount": 500, "note": "before it opened"},
                  {"date": "2020-11-27", "amount": -2000, "note": "out"},
                  {"date": "2020-12-05", "amount": 300}, {"date": "2020-12-05", "amount": 200}]}}), ["BTC"]), "BTC")


def _desk_run(path, plan, runs, start=300, n=700, seed=3, drift=0.002):
    """paper.step on the synthetic series: open at bar `start`, then one run per entry of `runs` (the bar that's
    forming then); the clock is half way through that bar."""
    bars, funding = series(seed=seed, drift=drift, n=n)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    feed = FakeFeed(bars, funding, start)
    clock = lambda: bars[feed.upto].ts + H4 // 2 if feed.upto < len(bars) else bars[-1].ts + 2 * H4   # noqa: E731
    states = [paper.step(p, path, feed=feed, log=lambda *a: None, now_ms=clock(), flows=plan)]
    for u in runs:
        feed.upto = u
        states.append(paper.step(p, path, feed=feed, log=lambda *a: None, now_ms=clock(), flows=plan))
    return bars, json.loads((path / "paper.json").read_text()), states


def _strip_run(led: dict) -> dict:
    return {**led, "meta": {k: v for k, v in led["meta"].items() if k not in ("runs", "last_run_ts")}}


def test_each_flow_is_recorded_once_in_date_order_whatever_the_runs(tmp_path, monkeypatch):
    """Every bar with each run repeated, and random batches (missed runs): each flow once, in date order, with its id,
    kind and note. A run on time puts it in at its own time, on the forming bar; a run catching up missed bars puts it
    in at the open of the first one on or after its date's 00:00 UTC; one dated before the account opened goes in on
    day one. Repeating a run changes nothing. The plan's deposit after the data stays planned."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    plan = _desk_plan()
    every = [u for u in range(301, 700) for _ in (0, 1)]                 # each run twice: the second changes nothing
    rnd, ups, u = random.Random(7), [], 300
    while u < 699:
        u = min(699, u + rnd.choice([1, 2, 5, 13, 40]))
        ups.append(u)
    bars, a, sa = _desk_run(tmp_path / "every", plan, every)
    _, b, _ = _desk_run(tmp_path / "batches", plan, ups)
    want = [("extra-2020-10-01-0", 500.0, "extra", "before it opened"), ("monthly-2020-11", 1000.0, "plan", "monthly plan"),
            ("extra-2020-11-27-0", -2000.0, "extra", "out"), ("extra-2020-12-05-0", 300.0, "extra", ""),
            ("extra-2020-12-05-1", 200.0, "extra", ""), ("monthly-2020-12", 1000.0, "plan", "monthly plan")]
    for led in (a, b):
        assert [(r["id"], r["amount"], r["kind"], r["note"]) for r in led["flows"]] == want
        assert led["flows"][0]["ts"] == bars[300].ts + H4 // 2      # day one: the run that opened the account
        _nav_ok(led["flows"], 10_000.0)
    for r in a["flows"][1:]:                                         # on time: at the run, on the bar forming then
        first = next(x for x in bars if x.ts >= _ms(r["date"]))
        assert r["ts"] == first.ts + H4 // 2
    caught = 0
    for r in b["flows"][1:]:                                         # late: at the open of the bar it missed
        first = next(x for x in bars if x.ts >= _ms(r["date"]))
        assert r["ts"] in (first.ts, first.ts + H4 // 2)
        caught += r["ts"] == first.ts == _ms(r["date"])              # 4h bars open at 00:00 UTC
    assert caught >= 3
    said = [e["text"] for e in a["desk"]["events"] if e["agent"] == "bank"]
    assert len(said) == 6 and sum("money in, not profit" in t for t in said) == 5 and "money out, not a loss" in said[2]
    assert "(monthly plan, 2020-11-10)" in said[1]                     # the date: a desk board shows the time only
    assert sum(1 for s in sa if any(e["agent"] == "bank" for e in s["paper"]["new_events"])) == 5   # two on one run
    last = sa[-1]
    assert last["flows"]["planned"] == {"count": 1, "usd": 1000.0, "next": {"id": "monthly-2021-01",
                                                                           "date": "2021-01-10", "amount": 1000.0},
                                        "extras": []}
    # a run repeated (the cron firing twice in a bar) changes nothing but the run count
    bars, funding = series(seed=3, drift=0.002, n=700)
    p, feed, path = P.with_(hard={"max_data_age_bars": 1e9}), FakeFeed(bars, funding, 300), tmp_path / "again"
    for u in [300] + list(range(301, 520)):
        feed.upto = u
        for k in (0, 1):
            paper.step(p, path, feed=feed, log=lambda *a: None, now_ms=bars[u].ts + H4 // 2 + k * 60_000, flows=plan)
            led = json.loads((path / "paper.json").read_text())
            if k:
                assert _strip_run(led) == _strip_run(first)
            first = led
    assert len(led["flows"]) == 5


def test_a_deposit_recorded_while_a_position_is_open_leaves_the_performance_figure_where_it_was(tmp_path, monkeypatch):
    """The figure the board shows just before and just after a deposit is the same: its units are bought at the NAV
    the board marks (this run's price), not at the bar's open, which may be hours (VT: a day) old."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    runs = list(range(301, 700))
    bars, plain, ps = _desk_run(tmp_path / "plain", None, runs)
    t = next(t for t in plain["broker"]["trades"] if t["exit_ts"] - t["entry_ts"] >= 4 * H4)
    k = next(i for i, x in enumerate(bars) if x.ts > t["entry_ts"] + H4)        # a bar forming inside the trade
    plan = [Flow("extra-x-0", "2020-01-01", bars[k].ts, 25_000.0, "extra")]

    class Mid(FakeFeed):                     # the mid has moved away from the forming bar's open by the time of the run
        def context(self):
            c = super().context()
            return c.__class__(**{**c.__dict__, "mid": self.all_bars[self.upto].close})

    p = P.with_(hard={"max_data_age_bars": 1e9})
    for name, fl in (("no", None), ("yes", plan)):
        feed = Mid(bars, *series(seed=3, drift=0.002, n=700)[1:], 300)
        path = tmp_path / name
        for u in [300] + list(range(301, k + 1)):
            feed.upto = u
            st = paper.step(p, path, feed=feed, log=lambda *a: None, now_ms=bars[u].ts + H4 // 2, flows=fl)
        if fl is None:
            before = st["ticker"]["pnl_pct"]
        else:
            after, rec = st["ticker"]["pnl_pct"], json.loads((path / "paper.json").read_text())["flows"][0]
    assert rec["ts"] == bars[k].ts + H4 // 2 and abs(bars[k].close - bars[k].open) > 0     # the mid moved
    assert after == pytest.approx(before, abs=1e-12)
    assert st["ticker"]["bank"] == pytest.approx(rec["equity_before"] + 25_000.0)


def test_flows_never_change_the_performance_figure_and_pnl_leaves_them_out(tmp_path, monkeypatch):
    """Flows landing while the account is flat: its NAV path (the performance %) is the no-flow account's, bar for
    bar, because sizing is a share of equity and the brakes are scaled. The board's P&L is equity - start - net
    flows, its percentage the NAV's."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    runs = list(range(301, 700))
    bars, plain, ps = _desk_run(tmp_path / "plain", None, runs)
    held = [(t["entry_ts"], t["exit_ts"]) for t in plain["broker"]["trades"]]
    flat = [i for i in range(330, 690) if not any(a <= bars[i].ts <= b for a, b in held)]
    plan = [Flow(f"extra-x-{k}", "2020-01-01", bars[i].ts, amt, "extra") for k, (i, amt) in
            enumerate([(flat[0], 5000.0), (flat[len(flat) // 2], -7000.0), (flat[-2], 20000.0)])]
    _, led, states = _desk_run(tmp_path / "flows", plan, runs)
    assert len(led["flows"]) == 3 and len(plain["broker"]["trades"]) == len(led["broker"]["trades"]) >= 4

    def nav(curve, recs):
        return [eq / (10_000.0 + sum(r["units"] for r in recs if r["ts"] < ts)) for ts, eq in curve]

    assert nav(led["desk"]["equity_curve"], led["flows"]) == pytest.approx(nav(plain["desk"]["equity_curve"], []),
                                                                            rel=1e-12)
    _nav_ok(led["flows"], 10_000.0)
    s, net = states[-1], 5000.0 - 7000.0 + 20000.0
    t, fl = s["ticker"], s["flows"]
    assert fl["net"] == net and fl["deposited"] == 25_000.0 and fl["withdrawn"] == 7000.0 and t["net_flows"] == net
    assert t["pnl"] == pytest.approx(t["bank"] - 10_000.0 - net) and t["pnl_pct"] == pytest.approx(t["bank"] / fl["units"] - 1)
    assert t["pnl_pct"] == pytest.approx(ps[-1]["ticker"]["pnl_pct"], abs=1e-12)          # the same performance
    assert [m[0] for m in fl["marks"]] == [r["ts"] for r in led["flows"]] and s["goal"]["deposited"] == net


def test_nothing_due_means_byte_for_byte_the_ledger_and_board_without_flows(tmp_path, monkeypatch):
    """Until a flow is recorded (BTC's first deposit is 2026-11-01), ledgers and boards are exactly what they were
    before deposits existed: a plan still to come changes nothing, not even a key."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    future = [Flow("monthly-2031-01", "2031-01-01", _ms("2031-01-01"), 100.0, "plan", "monthly plan")]
    runs = list(range(301, 420, 3))
    _desk_run(tmp_path / "none", None, runs)
    _desk_run(tmp_path / "plan", future, runs)
    for name in ("paper.json", "board.json"):
        assert (tmp_path / "plan" / name).read_bytes() == (tmp_path / "none" / name).read_bytes()
    assert "flows" not in json.loads((tmp_path / "plan" / "paper.json").read_text())
    series_, funding = _vt_market()
    for d, fl in (("vt_none", None), ("vt_plan", future)):
        clock = {}
        feeds = {c: FakeDailyFeed(series_[c], funding[c], clock) for c in series_}
        for k in range(60, 130, 4):
            clock["now"] = series_["BTC"][0].ts + k * DAY + 4 * 60_000
            paper.step_basket(VOLTREND, tmp_path / d, feeds=feeds, log=lambda *a: None, now_ms=clock["now"], flows=fl)
    for name in ("paper.json", "board.json"):
        assert (tmp_path / "vt_plan" / name).read_bytes() == (tmp_path / "vt_none" / name).read_bytes()


def test_a_recorded_flow_cant_be_edited_or_removed_from_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    plan = _desk_plan()
    _desk_run(tmp_path, plan, list(range(301, 470, 7)))
    led = json.loads((tmp_path / "paper.json").read_text())
    assert [r["id"] for r in led["flows"]] == ["extra-2020-10-01-0", "monthly-2020-11", "extra-2020-11-27-0"]
    edited = [f if f.id != "extra-2020-11-27-0" else Flow(f.id, f.date, f.ts, -1500.0, f.kind, f.note) for f in plan]
    removed = [f for f in plan if f.id != "monthly-2020-11"]
    for bad, says in ((edited, "now says -1,500.00 on 2020-11-27"), (removed, "no longer lists it"),
                      ([], "no longer lists it")):
        with pytest.raises(FlowsError, match=says):
            _desk_run(tmp_path, bad, [480])
    assert json.loads((tmp_path / "paper.json").read_text()) == led          # nothing was written
    with pytest.raises(FlowsError, match="more than the account"):            # can't take out more than there is
        _desk_run(tmp_path / "big", [Flow("extra-x-0", "2020-01-01", 0, -1e6, "extra")], [])


def test_a_withdrawal_the_open_position_needs_waits_until_it_fits(tmp_path, monkeypatch):
    """The reviewer's case: $11,580.88 out of $12,272.97 while a long holds $6,116 (cash $10,888.78). Taking it would
    leave cash at -$692 and the position at 8.8x. Like an exchange, the desk keeps back what the position needs: the
    withdrawal waits (said once), and goes out at the first bar open where it fits, here once the long has closed."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    plan = [Flow("extra-x-0", "2020-01-01", 1606377600000, -11_580.88, "extra", "out")]
    bars, led, states = _desk_run(tmp_path, plan, list(range(301, 700)))
    assert min(s["ticker"]["cash"] for s in states) > 0
    rec = led["flows"][0]
    assert rec["ts"] > 1606377600000 + H4 and rec["equity_before"] == rec["cash_before"]   # later, and flat by then
    said = [e["text"] for e in led["desk"]["events"] if e["agent"] == "bank"]
    assert len(said) == 2 and said[0].startswith("WITHDRAWAL $11,580.88 waiting (extra: out, 2020-01-01): the open "
                                                 "position needs the money")
    assert said[1].startswith("WITHDRAWAL $11,580.88 recorded")
    _nav_ok(led["flows"], 10_000.0)
    assert flows.withdrawal_wait(-50.0, 1_000.0, 900.0, 0.0, 3.0) is None                # flat: anything up to it all
    assert flows.withdrawal_wait(-50.0, 1_000.0, 900.0, 2_000.0, 3.0) is None            # 2,000 / 950 = 2.1x: fits
    assert "at most $333.33" in flows.withdrawal_wait(-700.0, 1_000.0, 900.0, 2_000.0, 3.0)


# ---------------------------------------------------------------- the brakes

def _risk_desk(equity_path: list[float]):
    """A flat $10,000 desk whose cash walks `equity_path` one 4h close at a time (the brakes see each close)."""
    broker = SimBroker(P)
    desk = Desk(P, broker)
    ts = _ms("2026-11-02") + 2 * H4
    for eq in equity_path:
        broker.cash = eq
        desk._update_risk(ts, broker.equity(1.0), H4)
        ts += H4
    return desk, broker, ts


def _dd(desk, ts):
    g = next(g for g in desk.kill_gates(ts, desk.broker.equity(1.0)) if g.name == "drawdown_halt")
    return g.value


def test_a_deposit_during_a_35pct_drawdown_doesnt_hide_it_and_the_halt_comes_at_the_same_point():
    desk, broker, ts = _risk_desk([10_000.0, 8_000.0, 6_500.0])             # 35% below the peak
    naive, nb, _ = _risk_desk([10_000.0, 8_000.0, 6_500.0])
    rec = flows.apply_desk(desk, Flow("monthly-2026-11", "2026-11-01", ts, 5_000.0, "plan"), ts, 1.0, 10_000.0, [])
    nb.cash += 5_000.0                                                       # what a plain balance change would do
    for d in (desk, naive):
        d._update_risk(ts, d.broker.equity(1.0), H4)
    assert broker.cash == nb.cash == 11_500.0 and rec["nav"] == 0.65
    assert _dd(desk, ts) == pytest.approx(0.35) and _dd(naive, ts) == 0.0  # the plain change hid the drawdown
    nav = lambda: broker.cash / (10_000.0 + rec["units"])                   # noqa: E731
    broker.cash = 11_500.0 * 0.601 / 0.65                                   # NAV 0.601: 39.9% below the peak
    desk._update_risk(ts + H4, broker.cash, H4)
    assert not desk.risk.halted and nav() == pytest.approx(0.601)
    broker.cash = 11_500.0 * 0.599 / 0.65                                   # NAV 0.599: past 40%
    desk._update_risk(ts + 2 * H4, broker.cash, H4)
    assert desk.risk.halted and "40% below peak" in desk.risk.halt_reason   # where the no-deposit account halts too
    alone, ab, _ = _risk_desk([10_000.0, 8_000.0, 6_500.0, 6_010.0])
    assert not alone.risk.halted
    ab.cash = 5_990.0
    alone._update_risk(ts + 2 * H4, ab.cash, H4)
    assert alone.risk.halted


def test_a_withdrawal_never_trips_the_halt_or_the_daily_loss_lock():
    desk, broker, ts = _risk_desk([10_000.0, 7_000.0])                      # 30% below the peak
    naive, nb, _ = _risk_desk([10_000.0, 7_000.0])
    flows.apply_desk(desk, Flow("extra-2026-11-03-0", "2026-11-03", ts, -3_000.0, "extra"), ts, 1.0, 10_000.0, [])
    nb.cash -= 3_000.0
    for d in (desk, naive):
        d._update_risk(ts, d.broker.equity(1.0), H4)
    assert broker.cash == nb.cash == 4_000.0
    assert not desk.risk.halted and _dd(desk, ts) == pytest.approx(0.30)
    assert naive.risk.halted                                                 # 60% "down": the plain change halts it
    # the day's loss: -5% by midday, then $2,000 out; the plain change reads -25% and locks the day
    desk, broker, ts = _risk_desk([10_000.0, 9_700.0, 9_500.0])
    naive, nb, _ = _risk_desk([10_000.0, 9_700.0, 9_500.0])
    flows.apply_desk(desk, Flow("extra-2026-11-02-0", "2026-11-02", ts, -2_000.0, "extra"), ts, 1.0, 10_000.0, [])
    nb.cash -= 2_000.0
    for d in (desk, naive):
        d._update_risk(ts, d.broker.equity(1.0), H4)
    assert not desk.day_locked and naive.day_locked
    assert 1 - broker.cash / desk.risk.day_start_equity == pytest.approx(0.05)


# ---------------------------------------------------------------- the go-live gate

def _gate_ledger(tmp_path, monkeypatch, path):
    """`live --coin BTC` with everything passing but the paper record, which walks `path`: (cash before, flow) pairs,
    then the last cash."""
    from test_live import _gate, _live_args

    tmp_path.mkdir(parents=True, exist_ok=True)
    cli, started = _gate(tmp_path, monkeypatch)
    led = tmp_path / "ledger" / "BTC" / "paper.json"
    d = json.loads(led.read_text())
    recs = []
    for k, (cash, amount) in enumerate(path[:-1]):
        recs.append(flows.record(Flow(f"extra-x-{k}", "2026-11-01", k, amount, "extra"), k, cash, cash, 150.0, recs))
    d["broker"]["cash"], d["flows"] = path[-1], recs
    led.write_text(json.dumps(d))
    return cli, started, _live_args


def test_the_go_live_gate_is_up_overall_only_on_the_trading_never_on_deposits(tmp_path, monkeypatch, capsys):
    """Lost 6.7%, then $1,000 deposited: the cash says +661%, the trading says -6.5%. Refused."""
    cli, started, args = _gate_ledger(tmp_path / "a", monkeypatch, [(140.0, 1000.0), 1142.0])
    assert cli.cmd_live(args()) == 2 and started == []
    out = capsys.readouterr().out
    assert "FAIL  paper return > 0% — -6.50% realised, time-weighted" in out and "+1,000.00 net deposited" in out


def test_the_go_live_gate_isnt_failed_by_a_withdrawal(tmp_path, monkeypatch, capsys):
    """Made 10%, then $100 taken out: the cash says -56%, the trading says +11.7%. It passes."""
    cli, started, args = _gate_ledger(tmp_path / "b", monkeypatch, [(165.0, -100.0), 66.0])
    assert cli.cmd_live(args()) == 0 and len(started) == 1
    assert "PASS  paper return > 0% — +11.69% realised, time-weighted" in capsys.readouterr().out
    assert flows.realised_return(150.0, [], 160.0) == 160.0 / 150.0 - 1      # no flows: what the gate said before


def _gate_on(tmp_path, monkeypatch, capsys, plan):
    """The real gate (cmd_live) on a ledger the real paper.step made, 30 days old so only the return decides."""
    import time

    from test_live import _gate, _live_args

    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    _, led, _ = _desk_run(tmp_path / "run", plan, list(range(301, 600)), n=600, seed=10, drift=0.001)
    (tmp_path / "gate").mkdir(parents=True)
    cli, started = _gate(tmp_path / "gate", monkeypatch)
    led["meta"]["started_ts"] = time.time() * 1000 - 30 * 86400_000
    (tmp_path / "gate" / "ledger" / "BTC" / "paper.json").write_text(json.dumps(led))
    rc = cli.cmd_live(_live_args())
    return rc, next(x for x in capsys.readouterr().out.splitlines() if "paper return" in x), led


def test_the_go_live_gate_isnt_passed_by_a_deposit_made_inside_a_losing_trade(tmp_path, monkeypatch, capsys):
    """The reviewer's case: $30,000 lands while a long is down, and the trade then closes at a loss. Chaining the
    realised cash counted that loss against the bigger balance and passed a losing account (+0.54%)."""
    rc, line, _ = _gate_on(tmp_path / "a", monkeypatch, capsys, None)
    assert rc == 2 and "FAIL" in line
    rc, line, led = _gate_on(tmp_path / "b", monkeypatch, capsys, [Flow("extra-x-0", "2020-01-01", 1607400000000,
                                                                        30_000.0, "extra")])
    r = led["flows"][0]
    assert r["equity_before"] < r["cash_before"]                           # a position was open, and down
    nav = led["broker"]["cash"] / flows.units(10_000.0, led["flows"]) - 1
    assert rc == 2 and "FAIL" in line and f"{nav:+.2%} realised, time-weighted" in line and nav < 0


# ---------------------------------------------------------------- VT

def _vt_plan() -> list[Flow]:
    return flows.schedule(flows.parse(json.dumps({"VT": {
        "monthly": {"amount": 100, "day": 5, "first": "2024-02-05", "count": 4},
        "extra": [{"date": "2024-03-20", "amount": 250}, {"date": "2024-05-20", "amount": -150, "note": "out"}]}}),
        ["VT"]), "VT")


def _vt_run(path, plan, days, at=4 * 60_000):
    series_, funding = _vt_market()
    clock = {}
    feeds = {c: FakeDailyFeed(series_[c], funding[c], clock) for c in series_}
    t0 = series_["BTC"][0].ts
    states = []
    for k in days:
        clock["now"] = t0 + k * DAY + at
        states.append(paper.step_basket(VOLTREND, path, feeds=feeds, log=lambda *a: None, now_ms=clock["now"],
                                        flows=plan))
    return series_, json.loads((path / "paper.json").read_text()), states


def test_vt_flows_go_in_once_whatever_the_runs(tmp_path):
    """Daily runs at 00:04 (each twice) put each flow in at that run, after the day's fills at its open; runs that skip
    days put it in at the open of the day they missed, before its fills, or at the run if the day is still forming."""
    plan = _vt_plan()
    daily = [k for k in range(60, 262) for _ in (0, 1)]                      # every day at 00:04, each run twice
    rnd, gaps, k = random.Random(3), [60], 60
    while k < 261:
        k = min(261, k + rnd.choice([1, 2, 4, 9]))
        gaps.append(k)
    series_, a, sa = _vt_run(tmp_path / "daily", plan, daily)
    _, b, _ = _vt_run(tmp_path / "gaps", plan, gaps, at=DAY // 2)
    for led in (a, b):
        assert [(r["id"], r["amount"]) for r in led["flows"]] == [
            ("monthly-2024-02", 100.0), ("monthly-2024-03", 100.0), ("extra-2024-03-20-0", 250.0),
            ("monthly-2024-04", 100.0), ("monthly-2024-05", 100.0), ("extra-2024-05-20-0", -150.0)]
    # at the 00:04 run; a day whose forming bar is missing for a coin waits for the next run, which catches it up
    assert all(r["ts"] in (_ms(r["date"]) + 4 * 60_000, _ms(r["date"])) for r in a["flows"])
    assert sum(r["ts"] == _ms(r["date"]) + 4 * 60_000 for r in a["flows"]) >= 5
    assert all(r["ts"] in (_ms(r["date"]), _ms(r["date"]) + DAY // 2) for r in b["flows"])
    assert any(r["ts"] == _ms(r["date"]) for r in b["flows"])                        # some caught up at the open
    _nav_ok(a["flows"], 500.0)
    _nav_ok(b["flows"], 500.0)
    s = sa[-1]
    t, fl = s["ticker"], s["flows"]
    assert fl["net"] == 400.0 - 150.0 + 250.0 and t["pnl"] == pytest.approx(t["bank"] - 500.0 - fl["net"])
    assert t["pnl_pct"] == pytest.approx(fl["nav"] - 1) and s["goal"]["deposited"] == fl["net"]
    said = [e["text"] for e in a["broker"]["events"] if e["agent"] == "bank"]
    assert len(said) == 6 and "WITHDRAWAL $150.00 recorded (extra: out, 2024-05-20) — money out, not a loss" in said[-1]


def test_vt_a_deposit_recorded_mid_day_leaves_the_performance_figure_where_it_was(tmp_path):
    """A run at 12:00 UTC: the board marks the holdings at the forming day's price, so the deposit's units are bought
    there, not at the 00:00 open (the reviewer's case: +7.35% before, +6.03% after at the same marks)."""
    series_, _, _ = _vt_run(tmp_path / "x", None, [60])
    k = 96
    day = series_["BTC"][0].ts + k * DAY
    plan = [Flow("extra-x-0", "2020-01-01", day, 500.0, "extra")]
    _, _, s0 = _vt_run(tmp_path / "no", None, list(range(60, k)) + [k], at=DAY // 2)
    _, led, s1 = _vt_run(tmp_path / "yes", plan, list(range(60, k)) + [k], at=DAY // 2)
    assert any(h["qty"] for h in s1[-1]["basket"]["holdings"])                       # holdings open
    assert led["flows"][0]["ts"] == day + DAY // 2
    assert s1[-1]["ticker"]["pnl_pct"] == pytest.approx(s0[-1]["ticker"]["pnl_pct"], abs=1e-12)
    assert s1[-1]["ticker"]["bank"] == pytest.approx(s0[-1]["ticker"]["bank"] + 500.0)


# ---------------------------------------------------------------- the goal, the tabs, the phone, the commit message

def test_years_to_the_goal_count_the_deposits_still_planned():
    assert flows.years_to(1e5, 150.0, 0.28, []) == pytest.approx(math.log(1e5 / 150) / math.log(1.28))
    assert flows.years_to(1e5, 150.0, None, [(1.0, 100.0)]) is None
    assert flows.years_to(1e5, 2e5, 0.1, []) == 0.0
    plan = [(m / 12, 100.0) for m in range(1, 60)]
    got = flows.years_to(1e5, 250.0, 0.2817, plan)
    e, j, step = 250.0, 0, 1 / 1200                                           # the same, the slow way: 100 steps a month
    while e < 1e5:
        j += 1
        e *= 1.2817 ** step
        if j % 100 == 0 and 1 <= j // 100 <= 59:
            e += 100.0
    assert got == pytest.approx(j * step, abs=0.01) and got < math.log(1e5 / 250) / math.log(1.2817)
    assert flows.years_to(1e5, 50_000.0, 0.0, [(0.5, 60_000.0)]) == 0.5       # no growth: the deposit gets it there
    assert flows.years_to(1e5, 50_000.0, -0.5, [(1.0, 10.0)]) is None


def test_the_goal_and_the_tabs_treat_deposits_as_money_in(tmp_path, monkeypatch):
    from desk import __main__ as cli

    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    monkeypatch.chdir(REPO)                                                   # reports/: the backtest's pace
    bars, led, states = _desk_run(tmp_path / "BTC", _desk_plan(), list(range(301, 700, 2)))
    g, fl = states[-1]["goal"], states[-1]["flows"]
    assert g["plan_included"] and g["deposited"] == fl["net"] == 1000.0 and g["planned_left"] == 1
    assert g["planned_left_usd"] == 1000.0 and g["years_at_test_rate"] < g["years_at_test_rate_without_plan"]
    assert g["years_at_backtest_rate"] == g["years_at_test_rate"] and g["test_cagr"] == board_mod.report_pace("BTC")["cagr"]
    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or __import__("io").BytesIO(b""))
    deposit = next(s for s in states if any(e["agent"] == "bank" for e in s["paper"]["new_events"]))
    cli._notify(deposit)
    body = sent[0].data.decode()
    assert sent[0].headers["Title"] == "BTC desk: DEPOSIT" and "BANK: DEPOSIT $500.00 recorded" in body
    assert "money in, not profit" in body and "net deposited so far (not profit)" in body
    # the tabs and the ALL card get what they need to keep deposits out of the P&L
    monkeypatch.setattr(paper, "step", lambda p, d, jev=None, flows=None, **kw: states[-1])
    monkeypatch.delenv("NTFY_TOPIC")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="BTC", ledger=str(tmp_path / "out"), interval=None, risk=None, jev_mode="off",
                              coin=None, flows=str(REPO / "deposits.json"))
    assert cli.cmd_step(args) == 0
    row = json.loads((tmp_path / "out" / "index.json").read_text())["coins"][0]
    assert row["net_flows"] == 1000.0 and row["units"] == fl["units"] and row["pnl"] == pytest.approx(
        row["bank"] - row["start"] - 1000.0) and row["pnl_pct"] == pytest.approx(row["bank"] / row["units"] - 1)


def test_the_commit_message_says_what_was_deposited_and_is_unchanged_without(tmp_path):
    """The workflow's save step names each bank in the commit message: a deposit is marked as money in."""
    yml = (REPO / ".github" / "workflows" / "paper.yml").read_text()
    line = next(x.strip() for x in yml.splitlines() if x.strip().startswith("banks=$("))
    for acc, cash, recs in (("BTC", 250.0, [{"amount": 150.0}, {"amount": -50.0}]), ("ETH", 150.0, []),
                            ("SOL", 90.0, [{"amount": -60.0}])):
        (tmp_path / "ledger" / acc).mkdir(parents=True)
        d = {"meta": {}, "broker": {"cash": cash}, **({"flows": recs} if recs else {})}
        (tmp_path / "ledger" / acc / "paper.json").write_text(json.dumps(d))
    out = subprocess.run(["bash", "-c", line + '\necho "$banks"'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ("BTC $250.00 (+$100.00 deposited, not profit) ETH $150.00 "
                                  "SOL $90.00 ($60.00 withdrawn, not a loss)")
    shutil.rmtree(tmp_path / "ledger" / "BTC")
    shutil.rmtree(tmp_path / "ledger" / "SOL")
    out = subprocess.run(["bash", "-c", line + '\necho "$banks"'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ETH $150.00"                                # as before deposits existed
    assert re.search(r"git commit -qm \"ledger: \$\{banks\} at", yml)


def test_the_phone_title_is_a_trade_only_for_the_desks_own_trades_never_a_deposit_note(monkeypatch):
    """The owner's free-text note ("SOLD the old bike") is in the bank event: it must not make the push a high-priority
    TRADE. The percentage in the body says it's the time-weighted one."""
    from desk import __main__ as cli
    import urllib.request

    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or __import__("io").BytesIO(b""))

    def state(*events):
        return {"paper": {"new_events": [{"ts": 1, "agent": a, "text": t} for a, t in events]}, "header": {"coin": "BTC"},
                "ticker": {"bank": 600.0, "pnl_pct": 0.0123, "position": "FLAT", "price": 1.0},
                "flows": {"net": 100.0, "pnl": 1.0}}

    bank = "DEPOSIT $100.00 recorded (extra: SOLD the old bike, daily savings, 2026-12-15) — money in, not profit."
    cli._notify(state(("bank", bank)))
    cli._notify(state(("bank", bank), ("fills", "BOUGHT 0.001 BTC @ $80,000.00 · stop $78,000.00")))
    cli._notify(state(("bank", "WITHDRAWAL $50.00 waiting (extra, 2026-12-15): the open position needs the money")))
    titles = [r.headers["Title"] for r in sent]
    assert titles == ["BTC desk: DEPOSIT", "BTC desk: TRADE", "BTC desk: WITHDRAWAL waiting"]
    assert [r.headers["Priority"] for r in sent] == ["default", "high", "default"]
    assert "account $600.00 (+1.23% time-weighted)" in sent[0].data.decode()


def test_a_deposits_json_mistake_is_pushed_to_the_phone_once_after_the_edit_then_every_6_hours(tmp_path, monkeypatch):
    from desk import __main__ as cli
    import urllib.request

    sent = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or __import__("io").BytesIO(b""))
    path = tmp_path / "deposits.json"
    path.write_text("{")
    paused = [("BTC", "deposits.json: BTC.extra[0].amount is '250' (str); it must be a number of dollars")]
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert not cli._notify_paused(paused, path, now_ms=_ms("2026-12-15"))            # no topic: nothing
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    assert cli._notify_paused(paused, path, now_ms=_ms("2026-12-15") + 5 * 60_000)  # 00:05: the 6-hourly one
    assert not cli._notify_paused(paused, path, now_ms=_ms("2026-12-15") + 3600_000)  # 01:00, not just edited: quiet
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "deposits.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "edit"], cwd=tmp_path,
                   check=True, env={**__import__("os").environ, "GIT_COMMITTER_DATE": "2026-12-15T03:00:00Z"})
    edit = _ms("2026-12-15") + 3 * 3600_000                                          # the owner's edit, at 03:00
    assert cli._notify_paused(paused, path, now_ms=edit + 4 * 60_000)                # the run right after it
    assert not cli._notify_paused(paused, path, now_ms=edit + 34 * 60_000)           # the runs after that: quiet
    assert [r.headers["Title"] for r in sent] == ["BTC paused: deposits.json"] * 2
    assert "BTC.extra[0].amount" in sent[0].data.decode() and "catches up every missed candle" in sent[0].data.decode()
