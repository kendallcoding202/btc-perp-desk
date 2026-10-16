"""The account cards in the tab row, and the ALL total, move with the same live price each account's own tab uses, so
a card shows the same number whichever tab is open. (The owner saw ETH at $147.03 on the ETH tab and $149.58 on its
card from the BTC tab: the card kept the last run's bank while the tab re-marked it.)

ETH and SOL: Hyperliquid's mid. BTC on Coinbase: Coinbase's own price while its feed is fresh, else Hyperliquid's as the
proxy, and the board's price while Coinbase is closed, also on a page opened straight on another tab. VT: its holdings
at Hyperliquid's mids, as before. index.json carries what that takes: the cash, the open position, and BTC's closures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import board

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.js").read_text()


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, k = 0, src.index("{", start)
    for j in range(k, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def _between(src: str, start: str, end: str) -> str:
    k = src.index(start)
    return src[k:src.index(end, k)]


# ---------------------------------------------------------------- index.json

def _state(pos=None, venue=None):
    s = {"updated": 1, "header": {"coin": "BTC" if venue else "ETH"}, "paper": {"changed": False, "new_events": []},
         "ticker": {"bank": 147.5, "pnl": -2.5, "pnl_pct": -0.0167, "position": "SHORT" if pos else "FLAT",
                    "position_detail": "", "record": "0W 1L", "price": 2425.0, "cash": 149.0, "pos": pos},
         "goal": {"start": 150.0, "target": 100_000.0, "progress": 0.0}}
    if venue:
        s["venue"] = venue
    return s


VENUE = {"name": "coinbase", "product": "BIP-20DEC30-CDE", "contracts": 1,
         "closed_now": {"why": "weekly break", "until": 1792188000000, "scheduled": True},
         "session": {"open": False, "read_ts": 1, "next_closure": {"start": 1792184400000, "end": 1792188000000,
                                                                  "why": "weekly break"}}}


def test_index_marks_carry_cash_position_and_coinbases_closures():
    pos = {"side": "short", "qty": 0.03621, "entry": 2358.29, "stop": 2441.86}
    assert board.index_marks(_state(pos)) == {"cash": 149.0, "pos": {"side": "short", "qty": 0.03621, "entry": 2358.29}}
    assert board.index_marks(_state()) == {"cash": 149.0, "pos": None}
    btc = board.index_marks(_state(venue=VENUE))
    assert btc == {"cash": 149.0, "pos": None, "product": "BIP-20DEC30-CDE", "closed_now": VENUE["closed_now"],
                   "next_closure": VENUE["session"]["next_closure"]}


def test_step_writes_them_into_each_desk_row(tmp_path, monkeypatch):
    import argparse

    from desk import __main__ as cli
    from desk import paper

    pos = {"side": "long", "qty": 0.01, "entry": 80554.0, "stop": 77970.0}
    monkeypatch.setattr(paper, "step", lambda p, d, jev=None, **kw: _state(pos, VENUE if p.market.symbol == "BTC" else None))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = argparse.Namespace(coins="BTC,ETH", ledger=str(tmp_path), interval=None, risk=None, jev_mode="off", coin=None,
                              flows=str(tmp_path / "none.json"))
    assert cli.cmd_step(args) == 0
    btc, eth = json.loads((tmp_path / "index.json").read_text())["coins"]
    assert btc["cash"] == eth["cash"] == 149.0 and btc["pos"] == eth["pos"] == {"side": "long", "qty": 0.01, "entry": 80554.0}
    assert btc["venue"] == "coinbase" and btc["product"] == "BIP-20DEC30-CDE"
    assert btc["closed_now"]["until"] == 1792188000000 and btc["next_closure"]["why"] == "weekly break"
    assert "product" not in eth and "closed_now" not in eth


# ---------------------------------------------------------------- the page

_RULES = (_between(APP, "const CB_BEAT", "  // quiet: the ticker") + _between(APP, "const cbFresh", "  const cbMode")
          + _function(APP, "rebank") + _function(APP, "withLive") + _function(APP, "remark")
          + _between(APP, "const rowVenue", "  // A desk account's card") + _function(APP, "cardMark"))

_STUB = """
const live = { mids: { BTC: "80000", ETH: "2400" }, at: 0, candle: null };
const cb = { ws: null, sub: "P", at: 0, beat: 0, price: 81230, venue: null };
const now0 = Date.now();
live.at = now0 - 1000;
const feedOn = () => { cb.ws = { readyState: 1 }; cb.at = Date.now() - 500; cb.beat = Date.now() - 500; };
const feedOff = () => { cb.ws = null; cb.at = 0; };
const ven = (o) => ({ name: "coinbase", product: "P", closed_now: null, session: { next_closure: null }, ...o });
// a board and its index row, from the same run, as desk/board.py and desk/__main__.py write them
const boardOf = (coin, cash, pos, price, venue) => ({ header: { coin }, venue, ticker: { cash, pos, price, bank: cash, pnl: 0,
  pnl_pct: 0, hold_pct: 0 }, goal: { start: 150, target: 100000, progress: 0 }, analysis: { stats: { price } },
  chart: { candles: [], ema_fast: [], ema_slow: [], don_hi: [], don_lo: [] }, equity: [] });
const rowOf = (b) => ({ coin: b.header.coin, kind: "desk", bank: b.ticker.bank, price: b.ticker.price, cash: b.ticker.cash,
  pos: b.ticker.pos && { side: b.ticker.pos.side, qty: b.ticker.pos.qty, entry: b.ticker.pos.entry },
  ...(b.venue ? { venue: "coinbase", product: b.venue.product, closed_now: b.venue.closed_now, next_closure: b.venue.session.next_closure } : {}) });
// the tab's number (withLive) and the card's (cardMark) for one account
const both = (b) => { const t = withLive(b), c = cardMark(rowOf(b)); return [+t.ticker.bank.toFixed(6), +c.bank.toFixed(6), t.ticker.price, c.price]; };
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_card_and_its_tab_show_the_same_bank():
    js = _RULES + _STUB + """
const eth = boardOf("ETH", 149.0, { side: "short", qty: 0.03621, entry: 2358.29, stop: 2441.86 }, 2425.35, undefined);
const btc = (o) => boardOf("BTC", 500.0, { side: "long", qty: 0.01, entry: 80554, stop: 77970 }, 81900, ven(o));
const out = {};
out.eth = both(eth);
feedOn(); cb.venue = null;                          // a page opened straight on the ETH tab: no BTC board drawn yet
out.btc_feed = both(btc({}));
feedOff();
out.btc_proxy = both(btc({}));
const shut = { closed_now: { why: "weekly break", until: now0 + 3600000, scheduled: true } };
out.btc_closed_run = both(btc(shut));
const sched = { session: { next_closure: { start: now0 - 60000, end: now0 + 3600000, why: "weekly break" } } };
feedOn();
out.btc_closed_schedule = both(btc(sched));
live.at = 0; live.mids = {}; feedOff();          // no allMids answer yet
out.no_live_price = both(eth);
console.log(JSON.stringify(out));"""
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    # ETH, short 0.03621 from 2358.29, at Hyperliquid's 2400: 149 - 0.03621 x 41.71, on its tab and on its card
    assert out["eth"][0] == out["eth"][1] == pytest.approx(149.0 - 0.03621 * (2400 - 2358.29)) and out["eth"][2:] == [2400, 2400]
    assert out["btc_feed"][0] == out["btc_feed"][1] == pytest.approx(500 + 0.01 * (81230 - 80554))     # Coinbase's own
    assert out["btc_feed"][2:] == [81230, 81230]
    assert out["btc_proxy"][0] == out["btc_proxy"][1] == pytest.approx(500 + 0.01 * (80000 - 80554))   # the proxy
    for k in ("btc_closed_run", "btc_closed_schedule"):      # closed: the board's bank and price, never Hyperliquid's
        assert out[k] == [500, 500, 81900, 81900], k
    assert out["no_live_price"] == [149, 149, 2425.35, 2425.35]


def test_every_card_and_the_all_total_use_it():
    coins = _function(APP, "renderCoins")
    assert "let b = open ? state.ticker.bank : basket ? c.bank : mk.bank;" in coins
    assert "const mk = basket ? null : cardMark(c);" in coins and "bank += b;" in coins
    assert "basketLive(c.cash, " in coins                                       # VT as before
    assert 'tone(made)' in coins and "tone(net ? bank - put : bank - start)" in coins   # the theme's hooks stay
    assert "remark(t.cash, p, mid)" in _function(APP, "withLive")
