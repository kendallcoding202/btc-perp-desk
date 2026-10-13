"""BTC on Coinbase's US nano BTC perp (BIP-20DEC30-CDE): the venue's own rules (desk/venue.py) and the backtest under
them. Whole 0.01 BTC contracts, the 4x cap, the overnight margin check, the fee estimate, the sessions, the funding log.
Nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from desk import backtest as bt
from desk import flows
from desk import indicators as ind
from desk import thresholds as th
from desk import venue as venue_mod
from desk.brokers.sim import VenueSimBroker
from desk.models import Bar, MarketContext, Order, Side, Verdict
from desk.thresholds import COINBASE, DEFAULT, params_for
from test_desk import P, series

HOUR = 3600_000
DAY = 86400_000

def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def rules(**spec) -> venue_mod.Rules:
    return venue_mod.Rules(replace(COINBASE, **spec) if spec else COINBASE)


# ---------------------------------------------------------------- the settings

def test_btc_simulates_coinbase_and_the_other_accounts_are_as_they_were():
    btc, eth, sol = (params_for(c) for c in ("BTC", "ETH", "SOL"))
    assert btc.market.venue == "coinbase" and th.venue_for(btc) is COINBASE
    assert (btc.sizing.starting_equity, btc.sizing.max_leverage, btc.costs.taker_fee_bps) == (500.0, 4.0, 5.0)
    assert btc.sizing.risk_per_trade == 0.05 and btc.strategy == DEFAULT.strategy      # the strategy is untouched
    assert (COINBASE.product, COINBASE.contract_size, COINBASE.max_leverage) == ("BIP-20DEC30-CDE", 0.01, 4.0)
    assert COINBASE.fee_min_per_contract == 0.20 and COINBASE.max_leverage < 1 / COINBASE.overnight_long_margin
    for p in (eth, sol):
        assert p.market.venue == "hyperliquid" and th.venue_for(p) is None
        assert (p.sizing.max_leverage, p.costs.taker_fee_bps) == (5.0, 4.5)


# ---------------------------------------------------------------- whole contracts, the cap, margin

def test_the_size_is_cut_down_to_whole_contracts_and_the_4x_cap():
    r = rules()
    qty, gates = r.size(0.0349, 83_000.0, 81_000.0, 2_000.0, 1, 0.05)     # 3.49 contracts wanted
    assert qty == 0.03 and [g.verdict for g in gates] == [Verdict.PASS, Verdict.PASS]
    assert "3 x 0.01 BTC, cut down from 3.49" in gates[0].note
    qty, _ = r.size(0.05, 83_000.0, 82_000.0, 500.0, 1, 0.05)             # 5 wanted, 4x $500 = 2.4 contracts
    assert qty == 0.02
    assert r.size(0.0399999999, 83_000.0, 82_000.0, 5_000.0, 1, 0.05)[0] == 0.03   # never rounded up
    assert r.size(500.0, 83_000.0, 82_000.0, 1e9, 1, 0.05)[0] == 50.0               # at most 5,000 contracts


def test_one_contract_over_the_risk_budget_is_skipped_with_the_reason():
    """At $83,165 one contract is $832. With $500 and a $3,550 stop it would risk $35.50, 7.1%: over the 5% budget,
    so the desk's own size rounds down to 0 contracts and the entry is skipped, saying why."""
    r = rules()
    price, stop, equity = 83_165.0, 83_165.0 - 3_550.0, 500.0
    want = equity * 0.05 / (price - stop)                                  # the desk's risk-based size: 0.70 contracts
    qty, gates = r.size(want, price, stop, equity, 1, 0.05)
    assert qty == 0.0 and gates[-1].name == "contracts" and gates[-1].verdict is Verdict.DROP
    assert gates[-1].note == "one contract ($832) would risk 7.1% at this stop (budget 5%)"
    # an account too small for one contract at 4x says that instead
    qty, gates = r.size(0.002, 83_000.0, 82_990.0, 150.0, 1, 0.05)
    assert qty == 0.0 and gates[-1].note == "one contract ($830) is 5.5x the account (cap 4x)"


def test_overnight_margin_cuts_contracts_and_skips_when_none_fit():
    """Equity minus the loss at the stop must cover contracts x notional x the overnight long margin rate."""
    r = venue_mod.Rules(COINBASE, margin={"overnight_long": 0.5})        # say Coinbase raised it to 50%
    qty, gates = r.size(0.04, 50_000.0, 49_000.0, 1_000.0, 1, 0.05)     # 4 wanted: 4 x $500 x 0.5 = $1,000 > $960
    assert qty == 0.03 and gates[-1].name == "margin" and gates[-1].verdict is Verdict.PASS
    assert gates[-1].note.startswith("cut from 4 to 3 contracts")
    qty, gates = venue_mod.Rules(COINBASE, margin={"overnight_long": 0.99}).size(0.01, 50_000.0, 49_000.0, 500.0, 1, 0.05)
    assert qty == 0.0 and gates[-1].verdict is Verdict.DROP
    assert gates[-1].note == "1 contract need $495 overnight margin (99.0%); the stop would leave $490"
    # at today's rate (21%) and the 4x cap, a 5% risk entry always fits: 4 x 21% + 5% < 100%
    assert rules().size(0.024, 83_000.0, 81_300.0, 500.0, 1, 0.05)[0] == 0.02


def test_the_fee_is_the_higher_of_the_per_contract_minimum_and_the_rate():
    p = params_for("BTC")
    r = rules()
    assert r.fee(0.03, 10_000.0, 5e-4) == pytest.approx(0.60)       # 3 x $100 at 5 bps = $0.15 < 3 x $0.20
    assert r.fee(0.03, 83_000.0, 5e-4) == pytest.approx(1.245)      # 3 x $830 at 5 bps = $1.245 > $0.60
    b = VenueSimBroker(p, r)
    b.submit(Order("open", Side.LONG, 0.03, "breakout", stop=9_000.0))
    b.fill_pending(10_000.0, 0)
    entry = 10_000.0 * 1.0003
    assert b.fees_total == pytest.approx(0.60) and b.position.entry_price == pytest.approx(entry)
    b.submit(Order("close", Side.LONG, 0.03, "trend_flip"))
    b.fill_pending(11_000.0, 1)
    exit_ = 11_000.0 * 0.9997
    assert b.fees_total == pytest.approx(1.20)                      # $110 a contract: still the minimum
    assert b.cash == pytest.approx(500.0 + 0.03 * (exit_ - entry) - 1.20)
    assert b.trades[0].fees == pytest.approx(1.20)


# ---------------------------------------------------------------- sessions

def test_the_weekly_break_is_friday_5pm_new_york_whatever_the_season():
    """As Coinbase's own 5-minute candles show it: 21:00-22:00 UTC in daylight time, 22:00-23:00 in winter."""
    s = venue_mod.Sessions(COINBASE)
    for day, hour in (("2026-10-09", 21), ("2026-10-02", 21), ("2026-03-13", 21), ("2026-01-09", 22),
                      ("2025-12-12", 22), ("2026-03-06", 22), ("2026-10-30", 21), ("2026-11-06", 22)):
        t = _ms(f"{day}T{hour:02d}:00")
        assert s.closure(t)[:2] == (t, t + HOUR) and s.closure(t)[2] == "weekly break"
        assert s.closure(t - 1) is None and s.closure(t + HOUR) is None
    assert s.closure(_ms("2026-10-08T21:30")) is None and s.closure(_ms("2026-10-10T21:30")) is None
    assert venue_mod.new_york_utc_offset(datetime(2026, 3, 8).date()) == -4      # DST from the 2nd Sunday of March
    assert venue_mod.new_york_utc_offset(datetime(2026, 3, 7).date()) == -5
    assert venue_mod.new_york_utc_offset(datetime(2026, 11, 1).date()) == -5     # to the 1st Sunday of November


def test_maintenance_windows_close_the_session_and_whole_bars_inside_them_have_no_candle():
    w = (_ms("2026-10-24T10:00"), _ms("2026-10-24T19:00"))             # the one Coinbase listed on 2026-10-13
    s = venue_mod.Sessions(COINBASE, [w])
    assert s.closure(w[0])[2] == "maintenance" and s.closure(w[1]) is None
    assert s.closed_through(_ms("2026-10-24T10:00"), _ms("2026-10-24T12:00"))
    assert not s.closed_through(_ms("2026-10-24T18:00"), _ms("2026-10-24T20:00"))     # 19:00-20:00 trades
    assert s.closure(_ms("2025-10-25T15:00"))[2] == "maintenance"                       # a known one, seen in candles
    bars = [Bar(_ms("2026-10-24T08:00") + k * 2 * HOUR, 1, 1, 1, 1) for k in range(7)]
    kept = venue_mod.tradable(bars, s, 2 * HOUR)
    assert [datetime.fromtimestamp(b.ts / 1000, tz=timezone.utc).hour for b in kept] == [8, 18, 20]
    nxt = venue_mod.Sessions(COINBASE).next_closure(_ms("2026-10-13T20:22"))
    assert nxt[:2] == (_ms("2026-10-16T21:00"), _ms("2026-10-16T22:00"))


def test_no_entry_is_decided_into_a_closure_or_while_coinbase_says_it_is_closed():
    r = rules()
    fri = _ms("2026-01-09T22:00")                                         # winter: the break is 22:00-23:00 UTC
    g = r.session_gate(fri)
    assert g.verdict is Verdict.DROP and g.note == "Coinbase closed at the fill (weekly break, reopens 23:00 UTC)"
    assert len(g.note) < 70                                               # it fits a phone's gate row
    assert r.session_gate(fri + HOUR).verdict is Verdict.PASS
    g = r.session_gate(fri + HOUR, MarketContext(session_open=False))
    assert g.verdict is Verdict.DROP and g.note == "Coinbase reports its session closed"
    assert r.session_gate(fri + HOUR, MarketContext(session_open=True)).verdict is Verdict.PASS
    assert r.session_gate(fri + HOUR, MarketContext(session_open=None)).verdict is Verdict.PASS   # the schedule decides
    g = r.session_gate(fri + HOUR, MarketContext(session_open=False, session_note="no session details"))
    assert g.verdict is Verdict.DROP and g.note == "no session details"    # fails closed, saying why
    assert venue_mod.skip_note([g]) is g


def test_a_stop_gapped_through_while_closed_fills_at_the_reopen():
    """Coinbase has no candles while closed, so the stop is first checked on the bar that reopens: it opens below the
    stop and fills there, the reopen price, not at the stop."""
    b = VenueSimBroker(params_for("BTC"), rules())
    b.submit(Order("open", Side.LONG, 0.02, "breakout", stop=80_000.0))
    b.fill_pending(82_000.0, 0)
    t = b.check_stop_bar(Bar(_ms("2026-10-24T18:00"), 79_000.0, 79_500.0, 78_800.0, 79_200.0))
    assert t.reason == "stop" and t.exit_price == pytest.approx(79_000.0 * 0.9997)


# ---------------------------------------------------------------- funding from snapshots

def test_the_funding_log_charges_each_hour_the_nearest_rate_it_knows_and_stays_bounded():
    log = venue_mod.FundingLog([[_ms("2026-10-13T17:00"), 1.8e-5]], keep_days=8)
    assert log.add(_ms("2026-10-13T20:00"), 1.3e-5) and not log.add(None, 1e-5)
    log.add(_ms("2026-10-13T20:00"), 1.4e-5)                           # the same hour read again: updated, not added
    assert len(log.rows) == 2
    ev = log.events(_ms("2026-10-13T16:00"), _ms("2026-10-13T22:00"))
    assert [e.ts for e in ev] == [_ms(f"2026-10-13T{h}:00") for h in range(17, 23)]
    # 18:00 is nearer 17:00's snapshot; 19:00 is as near both, and goes to the later one
    assert [e.rate for e in ev] == [1.8e-5, 1.8e-5, 1.4e-5, 1.4e-5, 1.4e-5, 1.4e-5]
    bars = [Bar(_ms("2026-10-13T16:00"), 1, 1, 1, 1), Bar(_ms("2026-10-13T18:00"), 1, 1, 1, 1)]
    assert ind.funding_per_bar(bars, ev, 2 * HOUR) == pytest.approx([2 * 1.8e-5, 2 * 1.4e-5])   # (16, 18], (18, 20]
    for k in range(400):
        log.add(_ms("2026-10-14T00:00") + k * HOUR, 1e-5)
    log.trim(_ms("2026-10-14T00:00") + 399 * HOUR)
    # 8 days, plus the newest snapshot before them (the nearest rate for the hours just after the bound)
    assert len(log.rows) == 8 * 24 + 2 and log.rows[-1][0] == _ms("2026-10-14T00:00") + 399 * HOUR
    assert log.rows[0][0] == _ms("2026-10-14T00:00") + 206 * HOUR


def test_remember_keeps_margin_windows_and_the_funding_log_across_runs():
    t0 = _ms("2026-10-13T20:22")
    a = venue_mod.remember(None, {"margin": {"overnight_long": 0.21025, "overnight_short": None},
                                  "maintenance": [_ms("2026-10-24T10:00"), _ms("2026-10-24T19:00")],
                                  "funding_time": _ms("2026-10-13T20:00"), "funding_rate": 1.3e-5,
                                  "session_open": True}, t0, COINBASE)
    b = venue_mod.remember(json.loads(json.dumps(a)), None, t0 + HOUR, COINBASE)      # a run with no answer
    assert b["margin"] == {"overnight_long": 0.21025} and b["windows"] == a["windows"]
    assert b["funding_log"] == [[_ms("2026-10-13T20:00"), 1.3e-5]] and b["session"]["open"] is True
    c = venue_mod.remember(b, {"margin": {"overnight_long": 0.25}, "funding_time": _ms("2026-10-13T21:00"),
                               "funding_rate": -2e-6}, t0 + 2 * HOUR, COINBASE)
    assert c["margin"]["overnight_long"] == 0.25 and len(c["funding_log"]) == 2
    assert venue_mod.Rules(COINBASE, c["windows"], c["margin"]).margin["overnight_long"] == 0.25
    d = venue_mod.remember(c, None, _ms("2026-12-01T00:00"), COINBASE)                  # weeks later
    assert d["windows"] == a["windows"]        # its missing candles are still in the 130 days paper reads
    assert d["funding_log"] == [[_ms("2026-10-13T21:00"), -2e-6]]                        # the newest is kept
    e = venue_mod.remember(c, None, _ms("2027-06-01T00:00"), COINBASE)                  # 200 days on
    assert e["windows"] == [] and e["funding_log"] == [[_ms("2026-10-13T21:00"), -2e-6]]


def test_live_refuses_btc_on_coinbase_with_why():
    from desk.brokers.live import LiveBroker, LiveSetupError

    with pytest.raises(LiveSetupError, match="BTC is set up for COINBASE · BTC PERP .*live trading there isn't built"):
        LiveBroker(params_for("BTC"), exchange=object(), state_path="/nonexistent/x.json")


# a venue like Coinbase's for the tests' synthetic 4h series: no known closures (they're from 2025-26), 3x
TEST_VENUE = replace(COINBASE, known_closures=(), max_leverage=3.0)


def _coinbase_p(**sizing):
    return P.with_(market={"venue": "coinbase"}, hard={"max_data_age_bars": 1e9},
                   sizing={"starting_equity": 800.0, "risk_per_trade": 0.02, **sizing}, costs={"taker_fee_bps": 5.0})


def test_a_withdrawal_a_contract_position_needs_waits_at_the_venues_leverage(tmp_path):
    """Six contracts held at $20,000 ($1,200) on $600: taking $400 out would leave them at 6x the $200 left, over
    BTC's 4x, so it waits; $200 out leaves 3x and goes."""
    from desk.engine import Desk

    p = params_for("BTC")
    r = rules()
    b = VenueSimBroker(p, r)
    b.cash = 600.0
    desk = Desk(p, b, venue=r)
    b.submit(Order("open", Side.LONG, 0.06, "breakout", stop=19_000.0))
    b.fill_pending(20_000.0, 0)
    cash = b.cash
    assert cash == pytest.approx(600.0 - 1.20)                    # 6 x the $0.20 minimum: 5 bps would be $0.60
    out = flows.Flow("extra-2026-12-15-0", "2026-12-15", _ms("2026-12-15T00:00"), -400.0, "extra")
    assert flows.apply_desk(desk, out, out.ts, 20_000.0, 500.0, []) is None
    assert "at most 4x the account" in desk.events[-1].text and b.cash == cash
    out2 = flows.Flow("extra-2026-12-15-0", "2026-12-15", _ms("2026-12-15T00:00"), -200.0, "extra")
    rec = flows.apply_desk(desk, out2, out2.ts, 20_000.0, 500.0, [])
    assert rec is not None and b.cash == pytest.approx(cash - 200.0) and r.contracts(b.position.qty) == 6


# ---------------------------------------------------------------- the backtest report

def test_the_backtest_report_shows_the_coinbase_rules_the_fractional_comparison_and_the_gate(tmp_path, monkeypatch):
    monkeypatch.setitem(th.VENUES, "coinbase", TEST_VENUE)
    bars, funding = series(seed=3, drift=0.0006, n=2500, start=3_000.0)
    p = _coinbase_p(starting_equity=500.0)
    r = bt.run(bars, funding, p)
    s = bt.summarize(r)
    v = s["venue"]
    assert v["name"] == "coinbase" and v["spec"]["contract_size"] == 0.01 and v["contracts"]["min"] >= 1
    assert "max($0.20 x contracts, 5 bps of notional)" in v["fee_estimate"]
    md, s["comparison"] = bt.venue_comparison(bars, funding, p, s)
    assert list(s["comparison"]) == ["fractional_before", "fractional_same_costs", "contracts", "fee_minimum",
                                     "coinbase"]
    assert s["comparison"]["coinbase"]["final_equity"] == s["full"]["final_equity"]
    before = bt.summarize(bt.run(bars, funding, p.with_(market={"venue": "hyperliquid"}, sizing={"max_leverage": 5.0},
                                                         costs={"taker_fee_bps": 4.5})))
    assert s["comparison"]["fractional_before"]["final_equity"] == before["full"]["final_equity"]
    assert "venue" not in before                                                   # fractional: no venue section
    path = bt.write_report(s, p, out_dir=tmp_path, extra_md=md)
    text = path.read_text()
    assert text.startswith("# Backtest — BTC perp on Coinbase rules (BIP-20DEC30-CDE, whole 0.01 BTC contracts)")
    assert "**COINBASE · BTC PERP rules** (BIP-20DEC30-CDE, simulated)" in text and "an ESTIMATE" in text
    assert "## Coinbase rules vs fractional sizing" in text and "## Go-live gate" in text
    assert len(bt.promotion_check(s, p)) == 5
    saved = json.loads((tmp_path / "backtest.json").read_text())
    assert saved["venue"]["skipped"] == v["skipped"] and saved["comparison"]["coinbase"]["trades"] == s["full"]["trades"]


def test_the_pace_counts_the_monthly_deposits_as_paper_will_and_leaves_them_out_of_the_pace(monkeypatch):
    monkeypatch.setitem(th.VENUES, "coinbase", TEST_VENUE)
    bars, funding = series(seed=3, drift=0.0006, n=2500, start=3_000.0)
    p = _coinbase_p(starting_equity=500.0)
    start = datetime.fromtimestamp(bars[600].ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    md, out = bt.pace(bars, funding, p, oos_cagr=0.25, start=start, monthly=100.0)
    assert out["deposits"] > 6 and out["deposited"] == 100.0 * out["deposits"]
    assert out["cagr"] is not None and out["years_to_target"] and out["years_to_target_at_oos"]
    assert out["final_equity"] > 0 and "## Pace to $100,000 with $500 + $100 a month" in md
    # the deposits are money in: the pace is the units' value, so it can't be the balance over the start
    assert out["twr"] != pytest.approx(out["final_equity"] / 500.0 - 1)
    assert out["years_to_target_at_oos"] == pytest.approx(flows.years_to(100_000.0, 500.0, 0.25,
                                                                          [((k + 1) / 12, 100.0) for k in range(60)]))


def test_the_research_tables_mark_each_halt_and_count_what_whole_contracts_skip(monkeypatch):
    """The sensitivity table says when a variant hit the 40% halt (BTC on Coinbase: every 2 ATR stop, October 2020),
    and the position-size table how many trades each risk took and how many entries no whole contract fit."""
    from pathlib import Path

    from desk import research

    md = (Path(__file__).resolve().parents[1] / "reports" / "backtest.md").read_text()
    sens = md[md.index("## Sensitivity"):md.index("## Ablation")]
    rows = {r.split("|")[1].strip(): r.split("|")[2] for r in sens.splitlines() if r.startswith("| ") and "---" not in r
            and "breakout" not in r}
    assert all("(halted 2020-10-" in rows[nb] for nb in ("20", "30", "45", "60"))       # the 2 ATR column
    size = md[md.index("## Position size"):md.index("## Cost stress")]
    assert "| trades | entries skipped: no whole contract fits |" in size
    assert "| 2% | +213% | +18.4% | 12.1% | -4.2% | no | 94 | 59 |" in size
    monkeypatch.setitem(th.VENUES, "coinbase", TEST_VENUE)
    bars, funding = series(seed=3, drift=0.0006, n=1200, start=3_000.0)
    out = research.sizing(bars, funding, _coinbase_p(starting_equity=500.0))
    assert out.splitlines()[2].endswith("| trades | entries skipped: no whole contract fits |")
    plain = research.sizing(bars, funding, P)                                   # no venue: no skip column
    assert plain.splitlines()[2].endswith("| trades |")
