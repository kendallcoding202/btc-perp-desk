"""Live mode against a fake exchange, in process. Nothing here talks to a real exchange or needs a key.

FakeHyperliquid answers the way ccxt's hyperliquid class does (ccxt 4.5: create_order returns the parsed
"filled" / "resting" status with no fee, fetch_positions gives contracts with contractSize None, trigger orders
come back from fetch_open_orders with triggerPrice and reduceOnly, fills from fetch_my_trades carry the order id
and the fee, funding from fetch_funding_history is signed USDC, sizes are rounded half-up to the size step and a
positive size that rounds to zero raises). It records every order, can fail any call on demand, and fills resting
stops when the test moves the price through them.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from desk.brokers import live as live_mod
from desk.brokers.live import LiveBroker, LiveSetupError
from desk.models import Order, Side
from desk.thresholds import params_for

# szDecimals from Hyperliquid's public meta (POST /info {"type": "meta"}, read 2026-10-13): BTC 5, ETH 4, SOL 2.
SZ_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2}
T0 = 1_790_000_000_000


class ExchangeDown(Exception):
    """Stands in for ccxt.NetworkError / ExchangeError."""


class InvalidOrder(Exception):
    pass


class OrderNotFound(Exception):
    pass


class BadSymbol(Exception):
    pass


def _q(x, places: int, rounding=ROUND_HALF_UP) -> Decimal:
    return Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=rounding)


class FakeHyperliquid:
    id = "hyperliquid"
    precisionMode = 4                     # ccxt TICK_SIZE

    def __init__(self, marks: dict[str, float], usdc: float = 1_000.0, fee_bps: float = 4.5, slip_bps: float = 1.0,
                 coins=tuple(SZ_DECIMALS)):
        self.markets = {}
        for k, coin in enumerate(coins):
            d = SZ_DECIMALS.get(coin, 2)
            self.markets[f"{coin}/USDC:USDC"] = {
                "id": str(k), "symbol": f"{coin}/USDC:USDC", "base": coin, "quote": "USDC", "settle": "USDC",
                "type": "swap", "swap": True, "contract": True, "linear": True, "active": True, "contractSize": 1.0,
                "precision": {"amount": float(Decimal(1).scaleb(-d)), "price": None},
                "limits": {"amount": {"min": None, "max": None}, "cost": {"min": 10.0, "max": None},
                           "price": {"min": None, "max": None}, "leverage": {"min": None, "max": 20}},
                "info": {"name": coin, "szDecimals": d}}
        self.marks = {f"{c}/USDC:USDC": px for c, px in marks.items()}
        self.cash = usdc
        self.fee = fee_bps / 1e4
        self.slip = slip_bps / 1e4
        self.positions: dict[str, dict] = {}      # symbol -> {"side", "qty", "entry"}
        self.resting: dict[str, dict] = {}        # oid -> trigger order
        self.status: dict[str, dict] = {}         # oid -> order as fetch_order returns it
        self.fills: list[dict] = []               # ccxt trade structures, as fetch_my_trades returns them
        self.incomes: list[dict] = []             # ccxt funding history structures
        self.orders: list[dict] = []              # every create_order call, as sent
        self.cancels: list[str] = []
        self.fail: dict[str, int] = {}            # call kind -> how many of the next calls raise
        self.fail_after: dict[str, int] = {}      # ...raise after the exchange took the order (a timeout)
        self.stops_vanish = False                 # stop orders are acknowledged but never rest
        self.calls: list[str] = []
        self.now = T0
        self.oid = 100
        self.has = {"fetchMyTrades": True, "fetchFundingHistory": True, "fetchOpenOrders": True, "fetchOrder": True}
        self.stop_fill_under_new_oid = False      # True: a triggered stop's fill carries a fresh order id

    # ---- plumbing ---------------------------------------------------------
    def _maybe_fail(self, kind: str) -> None:
        self.calls.append(kind)
        if self.fail.get(kind, 0) > 0:
            self.fail[kind] -= 1
            raise ExchangeDown(f"{kind}: 429 Too Many Requests")

    def _next_oid(self) -> str:
        self.oid += 1
        return str(self.oid)

    def milliseconds(self) -> int:
        return self.now

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        if symbol not in self.markets:
            raise BadSymbol(f"hyperliquid does not have market symbol {symbol}")
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        d = self.market(symbol)["info"]["szDecimals"]
        out = _q(amount, d)
        if out == 0 and Decimal(str(amount)) > 0:
            raise InvalidOrder(f"hyperliquid amount of {symbol} must be greater than minimum amount precision")
        return format(out.normalize(), "f")

    def price_to_precision(self, symbol, price):
        d = self.market(symbol)["info"]["szDecimals"]
        digits = max(5, len(str(int(abs(float(price))))))
        sig = Decimal(str(price))
        exp = sig.adjusted() - digits + 1
        rounded = sig.quantize(Decimal(1).scaleb(exp), rounding=ROUND_HALF_UP)
        return format(_q(rounded, max(0, 6 - d)).normalize(), "f")

    # ---- account ------------------------------------------------------------
    def _upnl(self) -> float:
        return sum((1 if p["side"] == "long" else -1) * p["qty"] * (self.marks[s] - p["entry"])
                   for s, p in self.positions.items())

    def fetch_balance(self, params=None):
        self._maybe_fail("balance")
        v = self.cash + self._upnl()
        return {"info": {}, "USDC": {"free": v, "used": 0.0, "total": v}, "free": {"USDC": v}, "used": {"USDC": 0.0},
                "total": {"USDC": v}}

    def fetch_positions(self, symbols=None, params=None):
        self._maybe_fail("positions")
        out = []
        for s, p in self.positions.items():
            if symbols and s not in symbols:
                continue
            out.append({"info": {}, "symbol": s, "side": p["side"], "contracts": p["qty"], "contractSize": None,
                        "entryPrice": p["entry"], "unrealizedPnl": None, "marginMode": "cross"})
        return out

    def _fill(self, symbol, side, qty, px, oid, reduce_only):
        """Book a fill: position, cash (realised P&L and fee) and the fill record fetch_my_trades returns."""
        pos = self.positions.get(symbol)
        sign = 1 if side == "buy" else -1
        fee = qty * px * self.fee
        closed = 0.0
        if pos is None:
            if reduce_only:
                raise InvalidOrder("Reduce only order would increase position.")
            self.positions[symbol] = {"side": "long" if sign > 0 else "short", "qty": qty, "entry": px}
            direction = "Open Long" if sign > 0 else "Open Short"
        elif (pos["side"] == "long") == (sign > 0):
            if reduce_only:
                raise InvalidOrder("Reduce only order would increase position.")
            tot = pos["qty"] + qty
            pos["entry"] = (pos["entry"] * pos["qty"] + px * qty) / tot
            pos["qty"] = tot
            direction = "Open Long" if sign > 0 else "Open Short"
        else:
            qty = min(qty, pos["qty"]) if reduce_only else qty
            closed = (px - pos["entry"]) * qty * (1 if pos["side"] == "long" else -1)
            pos["qty"] = round(pos["qty"] - qty, 12)
            direction = "Close Long" if pos["side"] == "long" else "Close Short"
            if pos["qty"] <= 0:
                del self.positions[symbol]
        self.cash += closed - fee
        self.fills.append({
            "info": {"coin": symbol.split("/")[0], "dir": direction, "closedPnl": str(closed), "oid": oid,
                     "liquidationMarkPx": None, "crossed": True},
            "timestamp": self.now, "symbol": symbol, "id": f"t{len(self.fills) + 1}", "order": oid, "type": None,
            "side": side, "takerOrMaker": "taker", "price": px, "amount": qty, "cost": qty * px,
            "fee": {"cost": fee, "currency": "USDC", "rate": None}})
        return qty

    # ---- orders -------------------------------------------------------------
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        self.market(symbol)
        is_stop = "stopLossPrice" in params or "triggerPrice" in params
        kind = "stop" if is_stop else ("close" if params.get("reduceOnly") else "entry")
        self.orders.append({"symbol": symbol, "type": type, "side": side, "amount": amount, "price": price,
                            "params": params, "kind": kind, "ts": self.now})
        self._maybe_fail(kind)
        if type == "market" and price is None:
            raise InvalidOrder("hyperliquid market orders require price to calculate the max slippage price")
        sz = float(self.amount_to_precision(symbol, amount))
        oid = self._next_oid()
        if is_stop:
            trig = float(self.price_to_precision(symbol, params.get("stopLossPrice") or params["triggerPrice"]))
            self.resting[oid] = {"id": oid, "symbol": symbol, "side": side, "amount": sz, "triggerPrice": trig,
                                 "reduceOnly": bool(params.get("reduceOnly"))}
            self.status[oid] = {"id": oid, "symbol": symbol, "status": "open", "side": side, "amount": sz,
                                "filled": 0.0, "average": None, "triggerPrice": trig, "fee": None}
            if self.stops_vanish:
                del self.resting[oid]
                self.status[oid]["status"] = "rejected"
            if self.fail_after.get(kind, 0) > 0:
                self.fail_after[kind] -= 1
                raise ExchangeDown("stop: read timed out")
            return {"info": {"resting": {"oid": int(oid)}}, "id": oid, "symbol": symbol, "status": None,
                    "side": None, "amount": None, "filled": None, "average": None, "price": None, "fee": None,
                    "trades": []}
        mark = self.marks[symbol]
        px = mark * (1 + self.slip) if side == "buy" else mark * (1 - self.slip)
        if not params.get("reduceOnly") and sz * px < 10.0:
            raise InvalidOrder("Order must have minimum value of $10.")
        filled = self._fill(symbol, side, sz, px, oid, bool(params.get("reduceOnly")))
        self.status[oid] = {"id": oid, "symbol": symbol, "status": "closed", "side": side, "amount": sz,
                            "filled": filled, "average": px, "fee": None}
        return {"info": {"filled": {"totalSz": str(filled), "avgPx": str(px), "oid": int(oid)}}, "id": oid,
                "symbol": symbol, "status": None, "side": None, "amount": filled, "filled": filled, "average": px,
                "price": None, "fee": None, "trades": []}

    def cancel_order(self, id, symbol=None, params=None):
        self._maybe_fail("cancel")
        if id not in self.resting:
            raise OrderNotFound(f"hyperliquid order {id} not found")
        del self.resting[id]
        self.status[id]["status"] = "canceled"
        self.cancels.append(id)
        return {"id": id, "status": "canceled"}

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self._maybe_fail("open_orders")
        return [{"info": {"isTrigger": True, "orderType": "Stop Market", "triggerPx": str(o["triggerPrice"])},
                 "id": o["id"], "symbol": o["symbol"], "type": "market", "side": o["side"], "amount": o["amount"],
                 "remaining": o["amount"], "filled": 0.0, "status": "open", "reduceOnly": o["reduceOnly"],
                 "triggerPrice": o["triggerPrice"], "stopLossPrice": o["triggerPrice"], "takeProfitPrice": None,
                 "price": None, "average": None, "fee": None}
                for o in self.resting.values() if symbol in (None, o["symbol"])]

    def fetch_order(self, id, symbol=None, params=None):
        self._maybe_fail("order")
        if id not in self.status:
            raise OrderNotFound(id)
        return dict(self.status[id])

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._maybe_fail("my_trades")
        return [dict(t) for t in self.fills if symbol in (None, t["symbol"]) and (since is None or t["timestamp"] >= since)]

    def fetch_funding_history(self, symbol=None, since=None, limit=None, params=None):
        self._maybe_fail("funding")
        return [dict(e) for e in self.incomes if symbol in (None, e["symbol"]) and (since is None or e["timestamp"] >= since)]

    # ---- the passage of time ------------------------------------------------
    def move(self, coin: str, price: float, fill_at: float | None = None, dt_ms: int = 60_000) -> None:
        """The mark moves to `price`; a resting stop it crosses triggers and fills (at `fill_at`: a gap or slippage)."""
        symbol = f"{coin}/USDC:USDC"
        self.now += dt_ms
        self.marks[symbol] = price
        for oid, o in list(self.resting.items()):
            if o["symbol"] != symbol:
                continue
            hit = price <= o["triggerPrice"] if o["side"] == "sell" else price >= o["triggerPrice"]
            if not hit:
                continue
            del self.resting[oid]
            pos = self.positions.get(symbol)
            if o["reduceOnly"] and pos is None:
                self.status[oid].update(status="canceled")     # nothing left to reduce
                continue
            px = fill_at if fill_at is not None else price
            fill_oid = self._next_oid() if self.stop_fill_under_new_oid else oid
            filled = self._fill(symbol, o["side"], min(o["amount"], pos["qty"]), px, fill_oid, o["reduceOnly"])
            self.status[oid].update(status="closed", filled=filled, average=px)

    def charge_funding(self, coin: str, rate: float) -> None:
        """Hyperliquid's hourly funding on the open position: longs pay a positive rate (usdc < 0 = paid)."""
        symbol = f"{coin}/USDC:USDC"
        pos = self.positions.get(symbol)
        if pos is None:
            return
        usdc = -(1 if pos["side"] == "long" else -1) * pos["qty"] * self.marks[symbol] * rate
        self.cash += usdc
        self.now += 1
        self.incomes.append({"info": {}, "symbol": symbol, "code": "USDC", "timestamp": self.now, "id": None,
                             "amount": usdc, "rate": rate})


def _broker(coin, ex, tmp_path, cap=None, **kw):
    return LiveBroker(params_for(coin), exchange=ex, state_path=tmp_path / f"{coin}_position.json",
                      max_notional_usd=cap, log=lambda *a: None, **kw)


PRICES = {"BTC": 82_000.0, "ETH": 2_600.0, "SOL": 150.0}


# ---------------------------------------------------------------- bug 1: each coin trades its own market

@pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL"])
def test_each_coins_orders_go_to_its_own_market_under_the_cap(tmp_path, coin):
    """An ETH or SOL desk used to send its orders to BTC-PERP: ETH/SOL-sized quantities of BTC, 31-770x the cap,
    with the stop at the ETH/SOL price level. Every order must go to the coin's own perp, under the cap, on the size
    step, with the stop on that coin's price level."""
    px = PRICES[coin]
    ex = FakeHyperliquid(PRICES)
    b = _broker(coin, ex, tmp_path, cap=50.0)
    assert b.symbol == f"{coin}/USDC:USDC"
    b.submit(Order("open", Side.LONG, 100.0 / px, "breakout", stop=px * 0.97))   # a $100 ticket, capped to $50
    b.execute_now(px, T0)
    entry, stop = ex.orders[0], ex.orders[1]
    assert entry["symbol"] == stop["symbol"] == f"{coin}/USDC:USDC"
    notional = entry["amount"] * ex.fills[0]["price"]
    assert 10.0 <= notional <= 50.0
    d = SZ_DECIMALS[coin]
    assert entry["amount"] == round(entry["amount"], d) and stop["amount"] == entry["amount"]
    assert stop["params"]["reduceOnly"] and stop["side"] == "sell"
    assert stop["params"]["stopLossPrice"] == pytest.approx(px * 0.97, rel=1e-4)    # this coin's price level
    assert ex.positions[f"{coin}/USDC:USDC"]["qty"] == pytest.approx(b.position.qty)
    assert set(ex.positions) == {f"{coin}/USDC:USDC"}                                 # and nothing on BTC


def test_size_is_cut_down_to_the_step_and_tickets_under_the_venue_minimum_are_not_sent(tmp_path):
    ex = FakeHyperliquid(PRICES)
    eth = _broker("ETH", ex, tmp_path, cap=None)
    eth.submit(Order("open", Side.LONG, 0.0123456789, "breakout", stop=2_500.0))
    eth.execute_now(2_600.0, T0)
    assert ex.orders[0]["amount"] == pytest.approx(0.0123)        # cut down, not rounded up to 0.0124
    sol = _broker("SOL", ex, tmp_path, cap=None)
    sol.submit(Order("open", Side.LONG, 0.0659, "breakout", stop=140.0))   # $9.89 < Hyperliquid's $10 minimum
    sol.execute_now(150.0, T0)
    assert [o["symbol"] for o in ex.orders].count("SOL/USDC:USDC") == 0 and sol.position is None
    assert sol.skipped["notional"] < 10.0


def test_live_refuses_a_coin_or_venue_with_no_market_and_a_cap_below_the_minimum(tmp_path):
    ex = FakeHyperliquid(PRICES, coins=("BTC", "ETH"))          # this venue lists no SOL perp
    with pytest.raises(LiveSetupError, match="SOL"):
        _broker("SOL", ex, tmp_path)
    assert ex.orders == []
    with pytest.raises(LiveSetupError, match="venue"):
        LiveBroker(params_for("ETH"), venue="nowhere", exchange=ex, state_path=tmp_path / "x.json")
    with pytest.raises(LiveSetupError, match="minimum"):
        _broker("ETH", ex, tmp_path, cap=5.0)
    assert live_mod.market_symbol("hyperliquid", "eth") == "ETH/USDC:USDC"


# ---------------------------------------------------------------- bug 2: never a position without a stop

class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def _long_btc(tmp_path, ex, **kw):
    b = _broker("BTC", ex, tmp_path, cap=500.0, **kw)
    b.submit(Order("open", Side.LONG, 0.005, "breakout", stop=80_000.0))
    b.execute_now(82_000.0, T0)
    return b


def test_a_stop_that_fails_is_retried_with_backoff_until_it_rests_on_the_exchange(tmp_path):
    ex, naps = FakeHyperliquid(PRICES), Sleeps()
    ex.fail["stop"] = 2                                      # rate-limited twice, then it goes through
    b = _long_btc(tmp_path, ex, sleep=naps)
    stops = ex.fetch_open_orders("BTC/USDC:USDC")
    assert len(stops) == 1 and stops[0]["triggerPrice"] == 80_000.0 and stops[0]["reduceOnly"]
    assert b.position is not None and b.stop_order_id == stops[0]["id"]
    assert [o["kind"] for o in ex.orders] == ["entry", "stop", "stop", "stop"] and naps == [1.0, 2.0]


@pytest.mark.parametrize("how", ["every call fails", "accepted but never rests"])
def test_a_stop_that_never_rests_closes_the_position_and_alerts(tmp_path, how):
    ex, naps, said = FakeHyperliquid(PRICES), Sleeps(), []
    if how == "every call fails":
        ex.fail["stop"] = 99
    else:
        ex.stops_vanish = True
    b = _long_btc(tmp_path, ex, sleep=naps, on_alert=said.append)
    assert b.position is None and "BTC/USDC:USDC" not in ex.positions        # flat on the exchange too
    close = ex.orders[-1]
    assert close["kind"] == "close" and close["params"] == {"reduceOnly": True} and close["side"] == "sell"
    assert b.trades[-1].reason == "no_stop" and len(naps) == 3
    assert any("closing" in a for a in said)


def test_a_stop_the_exchange_took_despite_an_error_is_not_placed_twice(tmp_path):
    ex = FakeHyperliquid(PRICES)
    ex.fail_after["stop"] = 1                                # placed, but the answer timed out
    b = _long_btc(tmp_path, ex, sleep=Sleeps())
    assert len(ex.fetch_open_orders("BTC/USDC:USDC")) == 1 and b.position is not None


def test_every_sync_puts_back_a_stop_that_has_gone_missing(tmp_path):
    ex, said = FakeHyperliquid(PRICES), []
    b = _long_btc(tmp_path, ex, sleep=Sleeps(), on_alert=said.append)
    ex.resting.clear()                                       # cancelled on the exchange (by hand, or lost)
    b.check_stop_price(82_100.0, T0 + 60_000)
    stops = ex.fetch_open_orders("BTC/USDC:USDC")
    assert len(stops) == 1 and stops[0]["triggerPrice"] == 80_000.0
    assert any("no stop resting" in a for a in said)
    b.check_stop_price(82_200.0, T0 + 120_000)               # and a sync with the stop in place sends nothing
    assert len(ex.orders) == 3


def test_a_trail_move_that_fails_keeps_the_old_stop_and_is_retried_at_the_next_sync(tmp_path):
    ex = FakeHyperliquid(PRICES)
    b = _long_btc(tmp_path, ex, sleep=Sleeps())
    ex.fail["stop"] = live_mod.STOP_ATTEMPTS                 # every try of the move fails
    b.update_stop(81_000.0)
    assert b.position is not None                             # still protected by the old stop: not flattened
    assert [o["triggerPrice"] for o in ex.fetch_open_orders("BTC/USDC:USDC")] == [80_000.0]
    b.check_stop_price(82_000.0, T0 + 60_000)
    assert [o["triggerPrice"] for o in ex.fetch_open_orders("BTC/USDC:USDC")] == [81_000.0]


def test_a_close_leaves_no_stop_behind_to_close_the_next_position(tmp_path):
    ex = FakeHyperliquid(PRICES)
    b = _long_btc(tmp_path, ex, sleep=Sleeps())
    ex.fail["cancel"] = 1                                    # the cancel after the close fails once...
    b.close_now(82_500.0, T0 + 60_000, "flat_kill")
    assert b.position is None and "BTC/USDC:USDC" not in ex.positions
    b.check_stop_price(82_500.0, T0 + 120_000)               # ...and the next sync cancels it
    assert ex.fetch_open_orders("BTC/USDC:USDC") == []
