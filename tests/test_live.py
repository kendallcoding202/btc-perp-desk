"""Live mode against a fake exchange, in process. Nothing here talks to a real exchange or needs a key.

FakeHyperliquid answers the way ccxt's hyperliquid class does (ccxt 4.5: create_order returns the parsed
"filled" / "resting" status with no fee, fetch_positions gives contracts with contractSize None, trigger orders
come back from fetch_open_orders with triggerPrice and reduceOnly, fills from fetch_my_trades carry the order id
and the fee, funding from fetch_funding_history is signed USDC, 500 rows a call, for every coin on the account and
each labelled with the symbol asked for (ccxt's parse_income), its own coin only in info.delta.coin, sizes are
rounded half-up to the size step and a positive size that rounds to zero raises). It records every order, can fail any call on demand, and fills resting
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
    FUNDING_PAGE = 500                    # rows per userFunding answer

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
        self.liquidity: dict[str, float] = {}     # symbol -> the most one market (IOC) order finds inside its band

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
        filled = self._fill(symbol, side, min(sz, self.liquidity.get(symbol, sz)), px, oid,
                            bool(params.get("reduceOnly")))
        self.status[oid] = {"id": oid, "symbol": symbol, "status": "closed", "side": side, "amount": sz,
                            "filled": filled, "average": px, "fee": None}
        if self.fail_after.get(kind, 0) > 0:
            self.fail_after[kind] -= 1
            raise ExchangeDown(f"{kind}: read timed out")
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
        """ccxt 4.5's hyperliquid builds this from orderStatus, which carries no average fill price: 'average' is
        None even for a filled order."""
        self._maybe_fail("order")
        if id not in self.status:
            raise OrderNotFound(id)
        return {**self.status[id], "average": None}

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._maybe_fail("my_trades")
        return [dict(t) for t in self.fills if symbol in (None, t["symbol"]) and (since is None or t["timestamp"] >= since)]

    def fetch_funding_history(self, symbol=None, since=None, limit=None, params=None):
        """Hyperliquid's userFunding from `since`: every coin's rows, at most 500, oldest first. ccxt 4.5 then labels
        each one with the symbol asked for (parse_income's safe_market falls back to it), whatever its coin."""
        self._maybe_fail("funding")
        rows = [e for e in self.incomes if since is None or e["timestamp"] >= since][:self.FUNDING_PAGE]
        return [{**e, "symbol": symbol or e["symbol"]} for e in rows]

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

    def charge_funding(self, coin: str, rate: float, dt_ms: int = 1) -> None:
        """Hyperliquid's hourly funding on the open position: longs pay a positive rate (usdc < 0 = paid)."""
        symbol = f"{coin}/USDC:USDC"
        pos = self.positions.get(symbol)
        self.now += dt_ms
        if pos is None:
            return
        usdc = -(1 if pos["side"] == "long" else -1) * pos["qty"] * self.marks[symbol] * rate
        self.cash += usdc
        self.incomes.append({"info": {"time": self.now, "hash": "0x" + "0" * 64, "delta": {
                                 "type": "funding", "coin": coin, "usdc": str(usdc), "fundingRate": str(rate)}},
                             "symbol": symbol, "code": "USDC", "timestamp": self.now, "id": None, "amount": usdc,
                             "rate": rate})


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


# ---------------------------------------------------------------- bug 3: book what the exchange actually did

@pytest.mark.parametrize("fill_oid", ["the stop's own id", "a fresh id"])
def test_an_exchange_stop_is_booked_at_its_fill_and_fee_not_at_the_next_mid(tmp_path, fill_oid):
    """The audit's repro: long 0.002 BTC at 80,000, stop 78,000. The stop fills on the exchange (here through a gap,
    at 77,850) and the price has bounced to 81,500 by the next poll. That loss used to be booked as a +$2.85 win at
    81,500; it must be booked at the fill, with the fees and the funding the exchange charged."""
    ex = FakeHyperliquid({"BTC": 80_000.0}, fee_bps=3.5, slip_bps=0.0)   # a fee tier below the desk's 4.5 bps
    ex.stop_fill_under_new_oid = fill_oid == "a fresh id"
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.002, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.charge_funding("BTC", 0.0001)                                        # the exchange charges $0.016...
    b.apply_funding(0.0004, 80_000.0)                                       # ...the desk's estimate was $0.064
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    ex.move("BTC", 81_500.0)
    b.check_stop_price(81_500.0, ex.now)
    t = b.trades[-1]
    assert b.position is None and t.reason == "stop (exchange)"
    assert t.exit_price == pytest.approx(77_850.0) and t.exit_ts == ex.fills[-1]["timestamp"]
    assert t.fees == pytest.approx(0.002 * (80_000 + 77_850) * 3.5e-4)
    assert t.funding == pytest.approx(0.002 * 80_000 * 0.0001)
    assert t.net_pnl == pytest.approx(0.002 * (77_850 - 80_000) - t.fees - t.funding) and t.net_pnl < -4
    saved = json.loads(b.state_path.read_text())["trades"][-1]              # and that's what the state file keeps
    assert saved["exit_price"] == pytest.approx(77_850.0)


def test_a_stop_that_filled_while_the_desk_was_down_is_booked_at_its_fill_after_a_restart(tmp_path):
    ex = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0)
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.002, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 77_000.0, fill_at=77_400.0)                              # the process is down meanwhile
    b2 = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())            # restarts from its state file
    assert b2.position is None and b2.trades[-1].exit_price == pytest.approx(77_400.0)


@pytest.mark.parametrize("readable", ["after one 429", "on a later check", "never"])
def test_when_the_fills_cant_be_read_the_exit_falls_back_loudly(tmp_path, readable):
    """The fills read fails as the stop's exit is booked. One failure is retried with backoff. If they stay unreadable
    the exit is booked for now at the stop level with an alert, never at the next mid, and re-booked from the fills
    once they read: Hyperliquid's fetch_order has no average price to fall back on, so that used to be for good."""
    ex, said = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0), []
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps(), on_alert=said.append)
    cash0 = ex.cash
    b.submit(Order("open", Side.LONG, 0.002, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    ex.fail["my_trades"] = {"after one 429": 1, "on a later check": live_mod.STOP_ATTEMPTS + 1, "never": 10 ** 6}[readable]
    b.check_stop_price(81_500.0, ex.now)
    t = b.trades[-1]
    if readable == "after one 429":
        assert t.exit_price == pytest.approx(77_850.0) and t.reason == "stop (exchange)" and not said
        assert b.sleep == [1.0]
        return
    assert t.exit_price == pytest.approx(78_000.0) and "fill not found" in t.reason     # the stop level, not 81,500
    assert any("couldn't be read" in a for a in said)
    b2 = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps(), on_alert=said.append)   # kept across a restart
    b2.check_stop_price(81_500.0, ex.now + 15_000)
    t = b2.trades[-1]
    if readable == "never":                          # still unreadable: stays at the stop level, still being retried
        assert t.exit_price == pytest.approx(78_000.0) and b2.provisional
        return
    assert len(b2.trades) == 1 and t.reason == "stop (exchange)" and t.exit_price == pytest.approx(77_850.0)
    assert t.net_pnl == pytest.approx(ex.cash - cash0) and not b2.provisional and any("re-booked" in a for a in said)
    assert json.loads(b2.state_path.read_text())["trades"][-1]["exit_price"] == pytest.approx(77_850.0)


def test_an_exit_partly_made_by_hand_is_booked_from_every_closing_fill(tmp_path):
    """0.001 of a 0.004 BTC long is closed by hand on the exchange, then the stop takes the rest: the exit is the
    average of both fills, not the stop's price on the whole position."""
    ex = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0)
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.004, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 79_500.0)
    ex.create_order("BTC/USDC:USDC", "market", "sell", 0.001, 79_500.0, {"reduceOnly": True})
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    b.check_stop_price(77_900.0, ex.now)
    t = b.trades[-1]
    assert t.exit_price == pytest.approx((0.001 * 79_500 + 0.003 * 77_850) / 0.004)
    assert t.fees == pytest.approx(sum(f["fee"]["cost"] for f in ex.fills))


def test_a_desk_close_carries_the_exchanges_fees_and_funding(tmp_path):
    ex = FakeHyperliquid({"ETH": 2_600.0}, fee_bps=2.0)
    b = _broker("ETH", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.1, "breakout", stop=2_500.0))
    b.execute_now(2_600.0, T0)
    for _ in range(3):
        ex.charge_funding("ETH", -0.00002)                                  # shorts paying longs: we receive
        b.apply_funding(0.0001, 2_600.0)
    ex.move("ETH", 2_700.0)
    t = b.close_now(2_700.0, ex.now, "trend_flip")
    entry, exit_ = ex.fills[0], ex.fills[1]
    assert t.entry_price == pytest.approx(entry["price"]) and t.exit_price == pytest.approx(exit_["price"])
    assert t.fees == pytest.approx(entry["fee"]["cost"] + exit_["fee"]["cost"])
    assert t.funding == pytest.approx(-sum(e["amount"] for e in ex.incomes)) and t.funding < 0
    assert b.cash == pytest.approx(ex.fetch_balance()["total"]["USDC"])


# ---------------------------------------------------------------- bug 4: catching up missed bars like paper

from desk import paper, runner                      # noqa: E402
from desk.engine import Desk                         # noqa: E402
from desk.models import MarketContext                # noqa: E402
from test_desk import H4, FakeFeed, P, series        # noqa: E402


class LiveFeed:
    """Hyperliquid's public feed, in process: a growing prefix of `bars`; bar `upto` is still forming."""

    def __init__(self, bars, funding, upto):
        self.bars, self.all_funding, self.upto = bars, funding, upto
        self.bar_ms, self.requests, self.last_latency_ms = H4, 0, 1.0

    def candles(self, start_ms=0, end_ms=None, closed_only=True):
        return [b for b in self.bars[:self.upto] if b.ts >= start_ms]

    def funding(self, start_ms, end_ms=None):
        end = self.bars[self.upto - 1].ts + H4
        return [e for e in self.all_funding if start_ms <= e.ts <= end]

    def context(self):
        return MarketContext(mid=self.mid(), spread_bps=1.0, depth_usd=1e7)

    def mid(self):
        return self.bars[self.upto].open if self.upto < len(self.bars) else self.bars[-1].close


class StopLoop(Exception):
    pass


def _spy_decisions(monkeypatch):
    """Record every on_bar_close, live and paper: bar ts -> (position before, decision, blocked by, orders)."""
    rows = {"live": {}, "paper": {}, "live_qty": {}, "paper_qty": {}}
    real = Desk.on_bar_close

    def spy(self, f, i, ctx=None, now_ms=None):
        before = self.broker.position.side.value if self.broker.position else None
        orders = real(self, f, i, ctx=ctx, now_ms=now_ms)
        d = self.last_decision
        who = "live" if self.broker.is_live else "paper"
        rows[who][f.bars[i].ts] = (before, d.side.value if d.side else None,
                                   d.blocked_by.name if d.blocked_by else None, [o.action for o in orders])
        rows[who + "_qty"][f.bars[i].ts] = d.qty
        return orders

    monkeypatch.setattr(Desk, "on_bar_close", spy)
    return rows


def _run_paper(tmp_path, p, bars, funding, start, end, monkeypatch):
    """paper.step once per bar, the way the Actions cron runs it, from `start` until bar `end` has been processed."""
    monkeypatch.setattr(paper, "LOOKBACK_BARS", 10 ** 6)
    feed = FakeFeed(bars, funding, start)
    led = tmp_path / "paper"
    for upto in range(start, end + 2):
        feed.upto = upto
        paper.step(p, led, feed=feed, log=lambda *a: None, now_ms=bars[upto].ts + H4 // 2)


def _run_live(tmp_path, p, bars, funding, start, script, ex=None, desk_meta=None, kills=None, wake_ms=None):
    """runner.run against the fake exchange. The first poll is a minute after bar start-1 closes; then each entry of
    `script` sleeps through that many bar closes (1 = a normal poll each bar, 5 = the laptop slept through 5, 0 = the
    next 15 s check inside the same bar),
    with the exchange's price walking through each bar's low, high and close meanwhile (resting stops trigger).
    `wake_ms` {step number: ms}: that wake-up comes this long after the forming bar's open, not a minute after."""
    coin = p.market.symbol
    feed = LiveFeed(bars, funding, start)
    ex = ex or FakeHyperliquid({coin: bars[start - 1].close}, usdc=10_000.0)
    clock = {"now": bars[start].ts + 60_000}
    ex.now = clock["now"]
    logs, steps, wakes, n_step = [], list(script), dict(wake_ms or {}), {"k": 0}
    paths = runner.live_paths(tmp_path / "live", coin)
    broker = LiveBroker(p, exchange=ex, state_path=paths["position"], log=logs.append, sleep=Sleeps(),
                        clock=lambda: clock["now"])

    def sleep(_):
        if not steps:
            raise StopLoop
        n = steps.pop(0)
        step, n_step["k"] = n_step["k"], n_step["k"] + 1
        if n == 0:                      # the next 15 s check, inside the same bar
            clock["now"] = ex.now = clock["now"] + 15_000
            return
        for _ in range(n):
            bar = bars[feed.upto]
            for k, px in enumerate((bar.low, bar.high, bar.close)):
                ex.now = bar.ts + (k + 1) * 1000
                ex.move(coin, px, dt_ms=0)
            feed.upto += 1
        clock["now"] = ex.now = bars[feed.upto].ts + wakes.get(step, 60_000)

    if desk_meta is not None:
        paths["desk"].write_text(json.dumps({"meta": desk_meta}))
    for name, on in (kills or {}).items():
        paths["kills"].write_text(json.dumps({name: on}))
    try:
        runner.run(p, "live", state_dir=str(tmp_path / "live"), dashboard=False, log=logs.append, feed=feed,
                   broker=broker, clock=lambda: clock["now"], sleep=sleep)
    except StopLoop:
        pass
    return broker, ex, logs


def _ts(bars, *idx):
    return [bars[k].ts for k in idx]


def test_after_a_gap_live_decides_on_every_missed_bar_in_order_as_paper_does(tmp_path, monkeypatch):
    """The audit's repro: the live loop slept through several closes and then decided on the newest one only."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    rows = _spy_decisions(monkeypatch)
    _run_paper(tmp_path, p, bars, funding, 240, 253, monkeypatch)
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 240, [1] * 9 + [5])    # through 248, then 249-253
    assert list(rows["live"]) == _ts(bars, *range(240, 254))                        # every bar, in order, once
    # Flat going into the gap, both make the same decision on each missed bar, the entry at 251 included...
    for k in (249, 250, 251):
        assert rows["live"][bars[k].ts] == rows["paper"][bars[k].ts]
        assert rows["live_qty"][bars[k].ts] == pytest.approx(rows["paper_qty"][bars[k].ts])
    assert rows["paper"][bars[251].ts][3] == ["open"]
    # ...but that entry's open has passed: live doesn't take it at today's price
    assert [o for o in ex.orders if o["kind"] == "entry"] == [] and broker.position is None
    assert any("not taken" in e.text for e in _desk_events(tmp_path))
    # so from there live is flat where paper is long, and their decisions differ until paper is flat again
    for k in (252, 253):
        assert rows["paper"][bars[k].ts][0] == "long" and rows["live"][bars[k].ts][0] is None


def test_an_entry_from_the_newest_close_after_a_gap_is_taken_as_paper_takes_it(tmp_path, monkeypatch):
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    rows = _spy_decisions(monkeypatch)
    _run_paper(tmp_path, p, bars, funding, 240, 252, monkeypatch)
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 240, [1] * 9 + [3])    # through 248, then 249-251
    for k in (249, 250, 251):
        assert rows["live"][bars[k].ts] == rows["paper"][bars[k].ts]
    entries = [o for o in ex.orders if o["kind"] == "entry"]
    paper_qty = rows["paper_qty"][bars[251].ts]
    assert len(entries) == 1 and paper_qty - 1e-5 <= entries[0]["amount"] <= paper_qty     # the same ticket
    assert broker.position is not None and len(ex.fetch_open_orders("BTC/USDC:USDC")) == 1


def test_an_exit_decided_at_a_missed_bar_is_sent_and_live_is_flat_from_there_like_paper(tmp_path, monkeypatch):
    bars, funding = series(seed=1, drift=0.0003, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9}, strategy={"stop_atr": 12.0, "trail_atr": 12.0})
    rows = _spy_decisions(monkeypatch)
    _run_paper(tmp_path, p, bars, funding, 380, 428, monkeypatch)
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 380, [1] * 42 + [6])   # through 421, then 422-427
    assert rows["paper"][bars[424].ts][3] == ["close"]                              # trend flip at 424
    for k in range(422, 428):
        assert rows["live"][bars[k].ts] == rows["paper"][bars[k].ts]
    closes = [o for o in ex.orders if o["kind"] == "close"]
    assert len(closes) == 1 and broker.position is None and broker.trades[-1].reason == "trend_flip"
    assert ex.positions == {} and ex.fetch_open_orders() == []


def test_a_trail_moved_during_a_gap_that_a_later_missed_bar_went_through_gets_live_out(tmp_path, monkeypatch):
    """Long from the 382 close. Asleep after 430 with the exchange stop at 77,166; while away the desk's trail rose to
    78,827 and bar 448 went through it (low 77,727), above the exchange's stop. Paper is out at 448; so is live."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    rows = _spy_decisions(monkeypatch)
    _run_paper(tmp_path, p, bars, funding, 370, 450, monkeypatch)
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 370, [1] * 61 + [20])  # through 430, then 431-450
    assert rows["live"][bars[430].ts][0] == "long" and rows["paper"][bars[448].ts][0] is None
    assert broker.position is None and broker.trades[-1].reason == "stop"
    assert ex.positions == {} and ex.fetch_open_orders() == []
    for k in (448, 449, 450):                                                       # flat from 448 on, like paper
        assert rows["live"][bars[k].ts] == rows["paper"][bars[k].ts]


def test_a_gap_longer_than_the_history_window_refuses_loudly_and_sends_nothing(tmp_path, monkeypatch):
    """With a 300-bar history window (bars 351-649) and the last bar processed at 100, bars 101-350 can't be
    rebuilt: paper would skip them silently; live refuses."""
    monkeypatch.setattr(runner, "LOOKBACK_BARS", 300)
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    ex = FakeHyperliquid({"BTC": bars[649].close}, usdc=10_000.0)
    with pytest.raises(SystemExit, match="too long ago"):
        _run_live(tmp_path, p, bars, funding, 650, [1], ex=ex, desk_meta={"last_bar_ts": bars[100].ts,
                                                                           "started_ts": 1, "start_equity": 1e4})
    assert ex.orders == []


def _desk_events(tmp_path):
    from desk.engine import Event
    d = json.loads(runner.live_paths(tmp_path / "live", "BTC")["desk"].read_text())
    return [Event(**e) for e in d["events"]]


# ---------------------------------------------------------------- bug 5: one coin's live state per directory

def test_two_coins_live_side_by_side_never_touch_each_others_state(tmp_path, monkeypatch):
    """BTC trades (long from the 251 close); then an ETH desk runs on the same state directory with its own entry
    kill. Before, both shared state/live_position.json, live_desk.json and kills.json: ETH would have read BTC's
    position, desk and kill switches and written over them."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    btc = P.with_(hard={"max_data_age_bars": 1e9})
    eth = btc.with_(market={"symbol": "ETH"})
    b_btc, ex_btc, _ = _run_live(tmp_path, btc, bars, funding, 240, [1] * 14)
    assert b_btc.position is not None
    mine = runner.live_paths(tmp_path / "live", "BTC")
    before = {k: mine[k].read_bytes() for k in ("position", "desk")}
    b_eth, ex_eth, _ = _run_live(tmp_path, eth, bars, funding, 240, [1] * 14, kills={"entry_kill": True})
    assert {k: mine[k].read_bytes() for k in ("position", "desk")} == before   # not a byte of BTC's state changed
    assert not mine["kills"].exists()                                      # ETH's kill switch isn't BTC's
    assert b_eth.position is None and ex_eth.orders == []                  # ...and it held for ETH
    pos = json.loads((tmp_path / "live" / "ETH" / "live_position.json").read_text())
    assert pos["symbol"] == "ETH/USDC:USDC" and pos["position"] is None
    assert json.loads((tmp_path / "live" / "BTC" / "live_position.json").read_text())["position"]["side"] == "long"
    assert sorted(f.name for f in (tmp_path / "live").iterdir()) == ["BTC", "ETH"]   # nothing shared at the top


@pytest.mark.parametrize("name", ["live_position.json", "live_desk.json", "kills.json"])
def test_an_old_shared_state_file_makes_live_refuse_to_start(tmp_path, monkeypatch, name):
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / name).write_text("{}")
    bars, funding = series(seed=3, drift=0.002, n=700)
    ex = FakeHyperliquid({"BTC": bars[299].close})
    with pytest.raises(LiveSetupError, match=name):
        _run_live(tmp_path, P, bars, funding, 300, [1], ex=ex)
    assert ex.orders == [] and (tmp_path / "live" / name).read_text() == "{}"   # untouched, nothing traded


def test_a_second_live_process_for_the_same_coin_is_refused(tmp_path):
    held = runner._lock(runner.live_paths(tmp_path / "live", "BTC")["lock"])
    bars, funding = series(seed=3, drift=0.002, n=700)
    ex = FakeHyperliquid({"BTC": bars[299].close})
    with pytest.raises(LiveSetupError, match="another live process"):
        _run_live(tmp_path, P, bars, funding, 300, [1], ex=ex)
    _run_live(tmp_path, P.with_(market={"symbol": "SOL"}), bars, funding, 300, [1])    # another coin is fine
    held.close()
    _run_live(tmp_path, P, bars, funding, 300, [1], ex=ex)                              # and so is BTC, once free


def test_a_position_file_written_by_another_coins_desk_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # the default path below is relative: keep it out of the repo
    ex = FakeHyperliquid(PRICES)
    path = tmp_path / "ETH" / "live_position.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"symbol": "BTC/USDC:USDC", "position": None, "trades": []}))
    with pytest.raises(LiveSetupError, match="BTC/USDC:USDC"):
        LiveBroker(params_for("ETH"), exchange=ex, state_path=path, log=lambda *a: None)
    assert LiveBroker(params_for("BTC"), exchange=ex, log=lambda *a: None).state_path == \
        Path("state/BTC/live_position.json")


def test_reset_halt_for_live_clears_only_that_coins_desk(tmp_path, monkeypatch, capsys):
    import argparse

    from desk import __main__ as cli

    monkeypatch.chdir(tmp_path)
    halted = {"risk": {"halted": True, "halt_reason": "test", "peak_equity": 1.0}, "meta": {}}
    for coin in ("BTC", "ETH"):
        runner.live_paths("state", coin)["dir"].mkdir(parents=True, exist_ok=True)
        runner.live_paths("state", coin)["desk"].write_text(json.dumps(halted))
    assert cli.cmd_reset_halt(argparse.Namespace(mode="live", coin="ETH")) == 0
    assert json.loads(runner.live_paths("state", "ETH")["desk"].read_text())["risk"]["halted"] is False
    assert json.loads(runner.live_paths("state", "BTC")["desk"].read_text())["risk"]["halted"] is True


# ---------------------------------------------------------------- the go-live gate still holds

REPO = Path(__file__).resolve().parents[1]


def _gate(tmp_path, monkeypatch, *, report="pass", paper_days=20.0, paper_cash=160.0, confirmed=True, stub_run=True):
    """A working directory for `python -m desk live --coin BTC`: a backtest report, a paper ledger and the
    confirmation, each as asked. Connecting to an exchange fails the test."""
    import time as _time

    from desk import __main__ as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    if report is not None:
        rep = json.loads((REPO / "reports" / "backtest.json").read_text())
        if report == "fail":
            rep["full"]["max_drawdown"] = 0.50
        (tmp_path / "reports" / "backtest.json").write_text(json.dumps(rep))
    if paper_days is not None:
        (tmp_path / "ledger" / "BTC").mkdir(parents=True)
        (tmp_path / "ledger" / "BTC" / "paper.json").write_text(json.dumps({
            "meta": {"started_ts": _time.time() * 1000 - paper_days * 86400_000, "start_equity": 150.0},
            "broker": {"cash": paper_cash}}))
    if confirmed:     # stands in for DESK_LIVE_CONFIRM, which is never set here
        monkeypatch.setattr(cli, "_live_confirmed", lambda: True)
    started = []
    if stub_run:
        monkeypatch.setattr(runner, "run", lambda *a, **kw: started.append(kw))

    def no_exchange(venue):
        raise AssertionError("the gate must refuse before any exchange is created")

    monkeypatch.setattr(live_mod, "make_exchange", no_exchange)
    return cli, started


def _live_args(**kw):
    import argparse

    base = dict(coin="BTC", interval=None, risk=None, jev_mode="off", poll=15.0, host="127.0.0.1", port=8787,
                no_dashboard=True, max_notional=50.0, force_unproven=False)
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize("missing", ["backtest report", "backtest passes", "paper ledger", "14 days of paper",
                                     "paper is up", "--max-notional", "DESK_LIVE_CONFIRM"])
def test_live_refuses_to_start_without_each_gate_condition(tmp_path, monkeypatch, missing):
    monkeypatch.delenv("DESK_LIVE_CONFIRM", raising=False)
    cli, started = _gate(tmp_path, monkeypatch,
                         report={"backtest report": None, "backtest passes": "fail"}.get(missing, "pass"),
                         paper_days={"paper ledger": None, "14 days of paper": 13.5}.get(missing, 20.0),
                         paper_cash=150.0 if missing == "paper is up" else 160.0,
                         confirmed=missing != "DESK_LIVE_CONFIRM")
    args = _live_args(max_notional=None) if missing == "--max-notional" else _live_args()
    assert cli.cmd_live(args) == 2 and started == []


def test_live_starts_only_when_every_gate_condition_holds(tmp_path, monkeypatch):
    cli, started = _gate(tmp_path, monkeypatch)
    assert cli.cmd_live(_live_args()) == 0 and len(started) == 1 and started[0]["max_notional"] == 50.0


def test_live_refuses_on_old_shared_state_even_with_the_gate_passed(tmp_path, monkeypatch, capsys):
    """The real runner this time: it refuses before creating an exchange, and `live` exits 2."""
    cli, _ = _gate(tmp_path, monkeypatch, stub_run=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "live_position.json").write_text("{}")
    assert cli.cmd_live(_live_args()) == 2
    assert "Not going live: found live state from before it was kept per coin" in capsys.readouterr().out


# ---------------------------------------------------------------- review: a Hyperliquid sub-account is the account traded

MASTER, SUB = "0x" + "aa" * 20, "0x" + "bb" * 20
NOT_A_KEY = "0x" + "11" * 32          # a placeholder: no account anywhere is behind it


def test_a_sub_account_is_traded_through_vault_address_and_read_as_itself():
    """A Hyperliquid sub-account has no key: orders and cancels for it must carry vaultAddress = the sub-account,
    and reads must ask for it. Without that, orders land on the main account while the desk reads the sub-account
    (an unconfirmed stop, a phantom exit, positions piling up past the cap on the main account). The real ccxt
    class is used, with no network: only how it resolves the addresses from the desk's config is checked."""
    ccxt = pytest.importorskip("ccxt")
    ex = ccxt.hyperliquid(live_mod.exchange_config("hyperliquid", wallet=MASTER, key=NOT_A_KEY, sub_account=SUB))
    assert ex.handle_public_address("fetchPositions", {})[0] == SUB                  # every read: the sub-account
    for method in ("createOrder", "cancelOrders"):                                      # every order and cancel
        assert ex.handle_option_string_and_params_2({}, method, "vaultAddress", "subAccountAddress")[0] == SUB
    main = ccxt.hyperliquid(live_mod.exchange_config("hyperliquid", wallet=MASTER, key=NOT_A_KEY))
    assert main.handle_public_address("fetchPositions", {})[0] == MASTER
    assert main.handle_option_string_and_params_2({}, "createOrder", "vaultAddress", "subAccountAddress")[0] is None


def test_an_account_that_reads_zero_refuses_to_start(tmp_path):
    ex = FakeHyperliquid(PRICES, usdc=0.0)          # e.g. HL_WALLET_ADDRESS is the API wallet's own address
    with pytest.raises(LiveSetupError, match="HL_WALLET_ADDRESS"):
        _broker("BTC", ex, tmp_path)
    assert ex.orders == []


def test_a_missing_credential_refuses_to_start_instead_of_being_retried():
    with pytest.raises(LiveSetupError, match="HL_PRIVATE_KEY"):
        live_mod.exchange_config("hyperliquid", wallet=MASTER)
    with pytest.raises(LiveSetupError, match="EXCHANGE_API_KEY"):
        live_mod.exchange_config("bybit")


# ---------------------------------------------------------------- review: no ccxt builder fee, no referrer

def _offline_hyperliquid():
    """The real ccxt class with its transport recorded and nothing signed or sent anywhere."""
    ccxt = pytest.importorskip("ccxt")
    ex = ccxt.hyperliquid(live_mod.exchange_config("hyperliquid", wallet=MASTER, key=NOT_A_KEY))
    sent = []

    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    ex.fetch = no_network
    ex.sign_l1_action = lambda *a, **k: {"r": "0x0", "s": "0x0", "v": 27}
    ex.sign_user_signed_action = lambda *a, **k: {"r": "0x0", "s": "0x0", "v": 27}
    ex.privatePostExchange = lambda params=None, context=None: (sent.append(params["action"]["type"])
                                                                or {"status": "ok", "response": {"type": "default"}})
    ex.publicPostInfo = lambda params=None, context=None: "default"
    return ex, sent


def test_ccxt_signs_no_builder_fee_approval_or_referrer_and_attaches_no_builder_fee():
    """ccxt's hyperliquid class runs initialize_client() before every order and cancel. By default it approves a
    0.01% builder fee (then adds it to every order: +22% on the 4.5 bp taker fee the costs assume) and sets a
    referrer; with an API wallet the refused approval is re-sent before every order. make_exchange switches both off."""
    ex, sent = _offline_hyperliquid()
    ex.initialize_client()
    assert sorted(sent) == ["approveBuilderFee", "setReferrer"] and ex.options.get("approvedBuilderFee")  # the default
    ex, sent = _offline_hyperliquid()
    live_mod._no_builder_fee(ex)
    for _ in range(3):
        ex.initialize_client()
    assert sent == [] and not ex.options.get("approvedBuilderFee")     # no builder attached to orders, nothing signed


# ---------------------------------------------------------------- review: a trade's funding is its own coin's, all of it

def test_a_trade_carries_only_its_own_coins_funding_read_from_every_page(tmp_path):
    """Two coins on one account (the README's discouraged case, or a position held by hand). ccxt labels every
    funding row with the symbol asked for, so BTC's trade was charged ETH's funding too; and only the first 500 rows
    were read. A BTC long held 274 h (the BTC backtest's longest hold) next to an ETH long: 548 hourly rows."""
    ex = FakeHyperliquid({"BTC": 80_000.0, "ETH": 2_500.0}, usdc=5_000.0, slip_bps=0.0)
    eth = _broker("ETH", ex, tmp_path, cap=500.0, sleep=Sleeps())
    eth.submit(Order("open", Side.LONG, 0.1, "breakout", stop=2_300.0))
    eth.execute_now(2_500.0, ex.now)
    btc = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    btc.submit(Order("open", Side.LONG, 0.004, "breakout", stop=76_000.0))
    btc.execute_now(80_000.0, ex.now)
    for _ in range(274):
        ex.charge_funding("BTC", 0.0000125, dt_ms=3_600_000)
        ex.charge_funding("ETH", 0.0000125, dt_ms=0)            # the same hour's time as BTC's row
    assert len(ex.incomes) > ex.FUNDING_PAGE
    btc_paid = -sum(e["amount"] for e in ex.incomes if e["info"]["delta"]["coin"] == "BTC")
    t = btc.close_now(80_000.0, ex.now, "trend_flip")
    assert t.funding == pytest.approx(btc_paid) and btc_paid == pytest.approx(274 * 0.004 * 80_000 * 0.0000125)


# ---------------------------------------------------------------- review: a position closed in pieces, or grown

def _realised(ex, start_cash):
    return ex.cash - start_cash          # what the exchange says the trade made: closed P&L, every fee, funding


def test_a_part_closed_by_hand_with_a_check_in_between_is_still_booked_in_the_trade(tmp_path):
    """0.001 of a 0.004 BTC long is sold by hand; a 15 s check runs; then the stop takes the other 0.003. The check
    used to shrink the desk's size and book nothing, so the trade was 0.003 at the stop's price and the hand-closed
    part (its loss and fee) was in no trade at all."""
    ex, said = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0), []
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps(), on_alert=said.append)
    cash0 = ex.cash
    b.submit(Order("open", Side.LONG, 0.004, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 79_500.0)
    ex.create_order("BTC/USDC:USDC", "market", "sell", 0.001, 79_500.0, {"reduceOnly": True})   # by hand
    b.check_stop_price(79_500.0, ex.now)
    assert b.position.qty == pytest.approx(0.003) and any("closed outside the desk" in a for a in said)
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    b.check_stop_price(77_900.0, ex.now)
    assert len(b.trades) == 1 and b.position is None
    t = b.trades[0]
    assert t.qty == pytest.approx(0.004) and t.exit_price == pytest.approx((0.001 * 79_500 + 0.003 * 77_850) / 0.004)
    assert t.net_pnl == pytest.approx(_realised(ex, cash0))


def test_a_desk_close_that_only_partly_fills_is_booked_in_full_when_the_rest_goes(tmp_path):
    """The desk's reduce-only close finds 0.0025 of 0.004 inside its band; the stop takes the other 0.0015 later.
    Only the stop's 0.0015 used to be booked."""
    ex = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0)
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    cash0 = ex.cash
    b.submit(Order("open", Side.LONG, 0.004, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 79_000.0)
    ex.liquidity["BTC/USDC:USDC"] = 0.0025
    with pytest.raises(RuntimeError, match="left 0.0015"):
        b.close_now(79_000.0, ex.now, "trend_flip")
    del ex.liquidity["BTC/USDC:USDC"]
    assert [o["triggerPrice"] for o in ex.fetch_open_orders()] == [78_000.0]     # its stop stays in place
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    b.check_stop_price(77_900.0, ex.now)
    t = b.trades[-1]
    assert len(b.trades) == 1 and t.qty == pytest.approx(0.004)
    assert t.exit_price == pytest.approx((0.0025 * 79_000 + 0.0015 * 77_850) / 0.004)
    assert t.net_pnl == pytest.approx(_realised(ex, cash0))


def test_a_position_grown_on_the_exchange_gets_a_stop_for_all_of_it(tmp_path):
    """+0.002 BTC is added by hand to the desk's 0.004 long. The check used to copy the size but keep the 0.004 stop,
    so when it fired 0.002 stayed open with no stop, and the 0.004 the stop sold was never booked."""
    ex, said = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0), []
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps(), on_alert=said.append)
    cash0 = ex.cash
    b.submit(Order("open", Side.LONG, 0.004, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.move("BTC", 80_400.0)
    ex.create_order("BTC/USDC:USDC", "market", "buy", 0.002, 80_400.0)                       # by hand
    b.check_stop_price(80_400.0, ex.now)
    stops = ex.fetch_open_orders("BTC/USDC:USDC")
    assert [(o["triggerPrice"], o["amount"]) for o in stops] == [(78_000.0, 0.006)]           # resized, one stop
    assert b.position.qty == pytest.approx(0.006) and b.position.entry_price == pytest.approx(
        ex.positions["BTC/USDC:USDC"]["entry"]) and any("added to the position" in a for a in said)
    ex.move("BTC", 77_900.0, fill_at=77_850.0)
    assert "BTC/USDC:USDC" not in ex.positions                                                 # nothing left open
    b.check_stop_price(77_900.0, ex.now)
    t = b.trades[-1]
    assert len(b.trades) == 1 and t.qty == pytest.approx(0.006) and t.exit_price == pytest.approx(77_850.0)
    assert t.net_pnl == pytest.approx(_realised(ex, cash0))


# ---------------------------------------------------------------- review: a desk close that fails is sent again

def test_a_close_that_fails_once_is_sent_again_at_the_next_check_not_the_next_bar(tmp_path, monkeypatch):
    """The trend flip at 424 (seed 1): its close gets one 429. It used to be dropped: nothing re-sent it until the
    desk decided again at the next bar close (4 h later here, 2 h on the real desk), and a daily-loss flatten is
    decided only once a day. Paper can't fail a close: it is out at 425's open."""
    bars, funding = series(seed=1, drift=0.0003, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9}, strategy={"stop_atr": 12.0, "trail_atr": 12.0})
    ex = FakeHyperliquid({"BTC": bars[379].close}, usdc=10_000.0)
    ex.fail["close"] = 1
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 380, [1] * 45 + [0, 0], ex=ex)
    closes = [o for o in ex.orders if o["kind"] == "close"]
    assert closes[0]["ts"] == bars[424].ts + H4 + 60_000                   # sent at the 424 close, and refused
    assert len(closes) == 2 and closes[1]["ts"] - closes[0]["ts"] == 15_000  # sent again at the next check
    assert broker.position is None and broker.trades[-1].reason == "trend_flip" and ex.positions == {}
    assert broker.close_wanted is None and ex.fetch_open_orders() == []


def test_a_wanted_close_survives_a_restart(tmp_path):
    ex = FakeHyperliquid({"BTC": 80_000.0}, slip_bps=0.0)
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.004, "breakout", stop=78_000.0))
    b.execute_now(80_000.0, T0)
    ex.fail["close"] = 1
    b.submit(Order("close", Side.LONG, 0.004, "daily_loss"))
    with pytest.raises(ExchangeDown):
        b.execute_now(79_000.0, T0 + 60_000)
    b2 = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())          # the process restarts
    assert b2.close_wanted == "daily_loss"
    t = b2.retry_close(79_000.0, T0 + 120_000)
    assert t.reason == "daily_loss" and b2.position is None and ex.positions == {}


def test_a_close_the_exchange_filled_but_whose_answer_was_lost_is_booked_as_the_desks_close(tmp_path):
    """The trend-flip close fills, then its answer times out. The next check found the exchange flat and booked it as
    'stop (exchange)': the board and the record said a stop fired when the desk closed on a trend flip."""
    ex = FakeHyperliquid({"BTC": 82_000.0}, slip_bps=0.0)
    b = _broker("BTC", ex, tmp_path, cap=500.0, sleep=Sleeps())
    b.submit(Order("open", Side.LONG, 0.005, "breakout", stop=80_000.0))
    b.execute_now(82_000.0, T0)
    ex.move("BTC", 83_000.0)
    ex.fail_after["close"] = 1
    b.submit(Order("close", Side.LONG, 0.005, "trend_flip"))
    with pytest.raises(ExchangeDown, match="timed out"):
        b.execute_now(83_000.0, ex.now)
    b.check_stop_price(83_000.0, ex.now + 1)
    t = b.trades[-1]
    assert t.reason == "trend_flip" and t.exit_price == pytest.approx(83_000.0)
    assert len([o for o in ex.orders if o["kind"] == "close"]) == 1 and ex.fetch_open_orders() == []


# ---------------------------------------------------------------- review: a close long gone isn't fresh

@pytest.mark.parametrize("wake", ["a minute after the close", "3h59m after the close"])
def test_an_entry_from_a_close_that_isnt_fresh_is_not_taken(tmp_path, monkeypatch, wake):
    """Same gap as test_an_entry_from_the_newest_close_after_a_gap_is_taken_as_paper_takes_it, with the shipped
    stale-data gate (1.5 bars). Waking 3h59m after the 251 close, the newest close used to count as fresh: its entry
    went out ~4 h after the signal, at a price paper never saw, sized and stopped for the old close."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1.5})
    rows = _spy_decisions(monkeypatch)
    late = wake.startswith("3h59m")
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 240, [1] * 9 + [3],
                                 wake_ms={9: H4 - 60_000} if late else None)       # through 248, then 249-251
    assert rows["live"][bars[251].ts][3] == ["open"]                                # the desk still decides it
    entries = [o for o in ex.orders if o["kind"] == "entry"]
    if late:
        assert entries == [] and broker.position is None
        assert any("too late to act on" in e.text for e in _desk_events(tmp_path))
    else:
        assert len(entries) == 1 and entries[0]["ts"] - (bars[251].ts + H4) == 60_000


# ---------------------------------------------------------------- review: a restart keeps trying

def test_a_restart_that_hits_an_exchange_error_keeps_trying_and_puts_the_missing_stop_on(tmp_path, monkeypatch):
    """The process dies between an entry's fill and its stop. On restart one 429 (on the open-orders read the stop
    check makes) used to propagate out of LiveBroker() and runner.run before the retrying loop existed: the process
    exited and the position stayed on the exchange with no stop."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    paths = runner.live_paths(tmp_path / "live", "BTC")
    ex = FakeHyperliquid({"BTC": bars[299].close}, usdc=10_000.0)
    b = LiveBroker(P, exchange=ex, state_path=paths["position"], log=lambda *a: None, sleep=Sleeps())

    class Killed(BaseException):
        pass

    def die(*a, **k):
        raise Killed

    b._ensure_stop = die
    b.submit(Order("open", Side.LONG, 0.1, "breakout", stop=bars[299].close * 0.95))
    with pytest.raises(Killed):
        b.execute_now(bars[299].close, ex.now)
    assert ex.positions and ex.fetch_open_orders() == []            # open, with no stop
    ex.fail["open_orders"] = 1
    real = live_mod.LiveBroker
    monkeypatch.setattr(live_mod, "LiveBroker", lambda p, **kw: real(p, exchange=ex, sleep=Sleeps(), **kw))
    pushed, naps = [], []
    monkeypatch.setattr(runner, "_push", lambda title, body: pushed.append(body))

    def sleep(s):
        naps.append(s)
        if len(naps) > 1:                                            # the loop's first poll: enough
            raise StopLoop

    with pytest.raises(StopLoop):
        runner.run(P, "live", state_dir=str(tmp_path / "live"), dashboard=False, log=lambda *a: None,
                   feed=LiveFeed(bars, funding, 300), clock=lambda: bars[300].ts + 60_000, sleep=sleep)
    stops = ex.fetch_open_orders("BTC/USDC:USDC")
    assert len(stops) == 1 and stops[0]["amount"] == pytest.approx(0.1)
    assert naps[0] == 15.0 and json.loads(paths["kills"].read_text())["entry_kill"]
    assert "connecting to the exchange failed" in pushed[0] and any("no stop resting" in x for x in pushed[1:])


# ---------------------------------------------------------------- review: killed mid catch-up, restarted

@pytest.mark.parametrize("killed", ["while the flushed stop is retried", "after the flush, before the desk file"])
def test_a_restart_after_a_kill_mid_catch_up_replays_the_gap_as_paper_did(tmp_path, monkeypatch, killed):
    """Long from the 382 close, polled every bar through 430, asleep through 431-440, killed during the wake-up at 441
    (Ctrl-C while flush_stop retries, up to 7 s; or just before the desk file is written). The state file used to keep
    the gap's final trail (78,341) while the desk file still said 430, so the restart replayed 431 against it and
    stopped out on a bar paper held through (431's low 78,334.7). Paper holds the long through 444."""
    bars, funding = series(seed=3, drift=0.002, n=700)
    p = P.with_(hard={"max_data_age_bars": 1e9})
    rows = _spy_decisions(monkeypatch)
    _run_paper(tmp_path, p, bars, funding, 370, 444, monkeypatch)
    fired = {"done": False}

    def kill_once(at):
        def check(*a, **k):
            if not fired["done"] and at():
                fired["done"] = True
                raise KeyboardInterrupt("Ctrl-C")
        return check

    if killed == "while the flushed stop is retried":
        real_flush = LiveBroker.flush_stop
        check = kill_once(lambda: True)

        def flush(self):
            if self.clock() >= bars[441].ts and self.position is not None and self.stop_px is not None \
                    and abs(self.position.stop - self.stop_px) > 1:
                check()
            return real_flush(self)
        monkeypatch.setattr(LiveBroker, "flush_stop", flush)
    else:
        real_write, clock_at = runner._atomic_write, {"ts": 0}
        check = kill_once(lambda: clock_at["ts"] >= bars[440].ts)            # the write after the gap's catch-up

        def write(path, data):
            clock_at["ts"] = data.get("meta", {}).get("last_bar_ts", 0)
            if path.name == "live_desk.json":
                check()
            return real_write(path, data)
        monkeypatch.setattr(runner, "_atomic_write", write)
    ex = FakeHyperliquid({"BTC": bars[369].close}, usdc=10_000.0)
    with pytest.raises(KeyboardInterrupt):
        _run_live(tmp_path, p, bars, funding, 370, [1] * 61 + [10], ex=ex)     # through 430, then asleep 431-440
    assert fired["done"]
    broker, ex, logs = _run_live(tmp_path, p, bars, funding, 441, [1] * 4, ex=ex)   # restarted from the files
    assert broker.trades == [] and broker.position is not None                     # still long, like paper
    for k in range(431, 445):
        assert rows["live"][bars[k].ts] == rows["paper"][bars[k].ts]


# ---------------------------------------------------------------- review: one stop, not a pile of them

def test_stops_placed_while_their_read_back_failed_dont_pile_up(tmp_path):
    """During a trail move every open-orders read fails (4 tries), so each try places a stop it can't confirm. They
    used to stay: five reduce-only stops (80,000 and 4 x 81,000) for one 0.005 BTC long, never cleaned by later checks."""
    ex = FakeHyperliquid(PRICES)
    b = _long_btc(tmp_path, ex, sleep=Sleeps())
    ex.fail["open_orders"] = live_mod.STOP_ATTEMPTS
    b.update_stop(81_000.0)
    for k in range(3):
        b.check_stop_price(82_000.0, T0 + 60_000 * (k + 1))
    stops = ex.fetch_open_orders("BTC/USDC:USDC")
    assert [(o["triggerPrice"], o["amount"]) for o in stops] == [(81_000.0, 0.005)] and b.stop_order_id == stops[0]["id"]


def test_reset_halt_for_live_refuses_while_that_coins_live_process_runs(tmp_path, monkeypatch, capsys):
    """reset-halt used to print 'halt cleared' under a running live process, which wrote the halt straight back at
    its next check: the operator was told the desk could trade when it couldn't."""
    import argparse

    from desk import __main__ as cli

    monkeypatch.chdir(tmp_path)
    halted = {"risk": {"halted": True, "halt_reason": "test", "peak_equity": 1.0}, "meta": {}}
    paths = runner.live_paths("state", "BTC")
    paths["dir"].mkdir(parents=True)
    paths["desk"].write_text(json.dumps(halted))
    running = runner._lock(paths["lock"])                 # what `live --coin BTC` holds while it runs
    assert cli.cmd_reset_halt(argparse.Namespace(mode="live", coin="BTC")) == 2
    assert "Stop it first" in capsys.readouterr().out
    assert json.loads(paths["desk"].read_text())["risk"]["halted"] is True
    running.close()                                        # stopped: now the reset goes through
    assert cli.cmd_reset_halt(argparse.Namespace(mode="live", coin="BTC")) == 0
    assert json.loads(paths["desk"].read_text())["risk"]["halted"] is False
    runner._lock(paths["lock"]).close()                    # and reset-halt let go of the lock


# ---------------------------------------------------------------- review: a cap that can't buy one ticket

def test_a_cap_too_small_for_any_ticket_at_the_current_price_refuses_to_start(tmp_path, monkeypatch):
    """BTC at 83,000, 3 bps slippage, size step 0.00001: a $10.00-$10.75 cap passed the start check (it isn't below
    the $10 minimum) and then skipped every entry, 0.00012 BTC = $9.96 after the cut. The smallest that trades is
    $10.80 at this price (0.00013 BTC, with the 3 bps cushion)."""
    # `live` refuses at startup, with the market's price
    bars, funding = series(seed=3, drift=0.002, n=700)
    ex2 = FakeHyperliquid({"BTC": bars[299].close}, usdc=10_000.0)
    paths = runner.live_paths(tmp_path / "live", "BTC")
    small = LiveBroker(P, exchange=ex2, state_path=paths["position"], max_notional_usd=10.0, log=lambda *a: None)
    with pytest.raises(LiveSetupError, match="smallest ticket"):
        runner.run(P, "live", state_dir=str(tmp_path / "live"), dashboard=False, log=lambda *a: None,
                   feed=LiveFeed(bars, funding, 300), broker=small, clock=lambda: bars[300].ts + 60_000,
                   sleep=lambda s: None)
    assert ex2.orders == []
    # and the line is exactly where an entry starts going out
    p = params_for("BTC")
    ex = FakeHyperliquid({"BTC": 83_000.0}, usdc=1_000.0)
    need = _broker("BTC", ex, tmp_path).smallest_cap(83_000.0)
    assert need == pytest.approx(10.80)
    for cap in (10.0, 10.5, 10.75, need - 0.01):
        with pytest.raises(LiveSetupError, match="smallest ticket"):
            _broker("BTC", ex, tmp_path, cap=cap).check_cap(83_000.0)
    b = _broker("BTC", ex, tmp_path, cap=need)
    b.check_cap(83_000.0)
    b.submit(Order("open", Side.LONG, 0.01, "breakout", stop=80_000.0))
    b.execute_now(83_000.0, ex.now)
    assert [o["kind"] for o in ex.orders][:1] == ["entry"] and b.position is not None      # exactly enough: it trades
    assert ex.orders[0]["amount"] * 83_000.0 <= need and p.costs.slippage_bps == 3.0
