"""ShadowCoinbase: SHADOW's exchange. It looks to LiveBroker exactly like CoinbaseExchange (desk/brokers/coinbase.py),
so SHADOW runs LIVE's own code, but it never sends an order:

  * money is the REAL account's: the balance (spot + futures + open and unsettled P&L, coinbase_orders.parse_balance)
    and the real deposits and withdrawals (the v2 USD wallet), read with a View-only key;
  * positions, open orders, fills and funding are a VIRTUAL book, kept in SHADOW's state;
  * create_order and cancel_order build the exact request LIVE would POST (coinbase_orders' builders, LIVE's own),
    record it as "would send", and play it out on the virtual book: a market order fills at Coinbase's real best ask
    (a buy) or bid (a sell) read at that moment, with the desk's fee estimate (max $0.20 a contract, 5 bps); a stop
    rests in the book as the stop-limit LIVE would rest; a cancel takes it off.

What SHADOW holds is the account LIVE would hold: the real balance plus the estimated P&L of the orders LIVE would have
sent (balance(): real + virtual realised P&L, fees and funding + the virtual position's open P&L at this run's mark).

advance() plays the resting virtual stops against Coinbase's 5-minute candles since the last run: a stop triggers when
a candle trades through its stop price; a sell stop-limit then fills at the stop, or at the candle's open if it opened
past the stop, but only at or above its limit (a buy, the mirror). One that gapped past its limit stays triggered and
unfilled, as on Coinbase, until a later candle comes back to its limit; LiveBroker closes such a position at market
(_cb_limit_passed). No candle while Coinbase is closed: nothing triggers then, and a gap fills at the reopen. Funding is
charged on the virtual position every funding hour, at the rate SHADOW's funding log has for it (an estimate: Coinbase
publishes no funding history). Coinbase has no reduce-only: the virtual book has none either (a stop filling with no
position would open one), and the same guards as CoinbaseExchange refuse every order that could do that.

The client SHADOW holds can't send anything anyway: CoinbaseREST raises OrdersLocked on any POST without an Armed token,
and only coinbase_live.arm() makes one. Even a bug in this overlay that called the real client would stop there.
"""

from __future__ import annotations

import math

from ..mountain import clock as _mt_clock
from ..thresholds import COINBASE
from . import coinbase_orders as co
from .coinbase import (V3, CancelFailed, OrderRefused, StaleClientOrderId, StopAlreadyFilled, StopThroughMarket,
                       _ccxt_side)
from .coinbase_rest import CoinbaseRejected

BOOK_V = 1
FIVE_MIN_S = 300
HOUR_MS = 3600_000
CANDLE_PAGE = 300                   # five-minute candles per request (Coinbase answers at most 350)
REPLAY_MAX_MS = 3 * 86400_000       # the furthest back advance() replays; a longer gap is alerted
WOULD_SEND_KEEP = 200
FILLS_KEEP = 500
FUNDING_KEEP = 2000


def _new_book() -> dict:
    return {"v": BOOK_V, "seq": 0, "position": None, "orders": {}, "cids": {}, "fills": [], "cash": 0.0,
            "fees": 0.0, "funding": [], "funding_until_ts": None, "checked_until_ts": None, "would_send": [],
            "realized_trades": 0}


def _utc(ms: int) -> str:
    """A would-send record's time, as shown (the page, the pushes): Mountain Time (desk/mountain.py)."""
    return _mt_clock(ms)


def _usd(x: float, dp: int = 2) -> str:
    return f"${x:,.{dp}f}"


def _contracts(n: int) -> str:
    return f"{n} contract{'s' if n != 1 else ''}"


class ShadowCoinbase:
    id = "coinbase"
    is_coinbase_adapter = True
    is_shadow = True
    reduce_only_stops = False
    ledger_ids_stable = True
    precisionMode = 4
    walletAddress = ""
    has = {"fetchLedger": True, "fetchMyTrades": True, "fetchOrder": True, "fetchOpenOrders": True,
           "fetchFundingHistory": True}        # the virtual book's own funding (an estimate, see advance())

    def __init__(self, real, book: dict | None = None, *, rules=None, fee_rate: float = 0.0005, funding_rate=None,
                 log=print):
        self.real = real
        self.book = book if book else _new_book()
        if self.book.get("v") != BOOK_V:
            raise ValueError("SHADOW's virtual book is from another version of the desk")
        self.rules = rules
        self.fee_rate = fee_rate
        self.funding_rate = funding_rate        # ts -> the hourly rate (SHADOW's funding log), or None
        self.log = log
        self.options: dict = {}
        self.on_alert = None
        real.on_alert = lambda text: self.on_alert(text) if self.on_alert is not None else None
        self.product = real.product
        self.coin = real.coin
        self.cs = real.venue.contract_size
        self.mark: float | None = None
        self.mark_ts: int | None = None
        self.foreign: dict = {"positions": [], "orders": []}
        self.real_orders: list[dict] = []           # the real account's open orders and positions (the board)
        self.real_positions: list[dict] = []
        self.unknown_entries: set[str] = set()      # ledger entries parse_flow refused this run
        self.sent_this_run: list[dict] = []

    # ---- passthroughs: the market, the clock, and the real money
    @property
    def markets(self):
        return self.real.markets

    def load_markets(self, reload: bool = False):
        return self.real.load_markets(reload)

    @property
    def details(self):
        return self.real.details

    @property
    def product_block(self):
        return self.real.product_block

    @property
    def balance_block(self):
        return self.real.balance_block

    def amount_to_precision(self, symbol, amount):
        return self.real.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol, price):
        return self.real.price_to_precision(symbol, price)

    def time_left(self) -> float:
        return self.real.time_left()

    def fits(self, seconds: float) -> bool:
        return self.real.fits(seconds)

    def milliseconds(self) -> int:
        return self.real.milliseconds()

    def session_open(self):
        return self.real.session_open()

    def halted(self) -> bool:
        return self.real.halted()

    def cancels_refused(self) -> bool:
        return self.real.cancels_refused()

    def protecting(self):
        return self.real.protecting()

    def client_order_id(self, kind: str, *parts) -> str:
        return self.real.client_order_id(kind, *parts)

    def fetch_ledger(self, code=None, since=None, limit=None, params=None):
        return self.real.fetch_ledger(code, since, limit, params)

    def parse_flow(self, tx: dict):
        from .live import UnknownLedgerEntry

        try:
            return co.parse_flow(tx)
        except UnknownLedgerEntry:
            if co.flow_status(tx) == "final":
                self.unknown_entries.add(str(tx.get("id")))
            raise

    flow_status = staticmethod(co.flow_status)

    def wallets(self):
        return self.real.wallets()

    def key_permissions(self):
        return self.real.key_permissions()

    def fee_tier(self):
        return self.real.fee_tier()

    def last_trade(self):
        return self.real.last_trade()

    def best_bid_ask(self):
        return self.real.best_bid_ask()

    def price_at(self, ts: int):
        return self.real.price_at(ts)

    # ---- the real account, read once a run (the board, and what blocks entries)
    def refresh_real(self) -> None:
        """The real account's positions and open orders, on every product. To SHADOW all of them are someone
        else's: LIVE starts only on a flat account with no orders, and never touches what isn't the desk's."""
        bip = self.real.fetch_positions()
        others = list(self.real.foreign["positions"])
        self.real_positions = [{"product_id": p["symbol"], "side": p["side"], "contracts": int(p["contracts"]),
                                "entry": p.get("entryPrice"), "upnl": p.get("unrealizedPnl")} for p in bip] + \
            [{"product_id": p["product_id"], "side": p["side"], "contracts": p["contracts"]} for p in others]
        mine = self.real.fetch_open_orders(self.product)
        self.real_orders = [{"product_id": o["symbol"], "id": o["id"], "side": o["side"], "type": o.get("type"),
                             "amount": o.get("amount"), "stop": o.get("triggerPrice"), "limit": o.get("price"),
                             "status": o.get("coinbase_status")} for o in mine] + \
            [dict(o) for o in self.real.foreign["orders"]]
        self.foreign = {"positions": [{"product_id": p["product_id"], "side": p["side"], "contracts": p["contracts"]}
                                      for p in self.real_positions],
                        "orders": [{"product_id": o["product_id"], "id": o["id"], "side": o["side"]}
                                   for o in self.real_orders]}

    def entry_block(self) -> str | None:
        if self.real.product_block:
            return self.real.product_block
        if self.real.balance_block:
            return self.real.balance_block
        f = self.foreign
        if f["positions"] or f["orders"]:
            what = [f"a {p['side']} {p['contracts']} on {p['product_id']}" for p in f["positions"]] + \
                   [f"order {o['id']} on {o['product_id']}" for o in f["orders"]]
            return (f"the real account holds {', '.join(what)}: LIVE starts only on a flat account and never trades "
                    f"beside what isn't the desk's, so SHADOW doesn't either")
        if self.real.session_open() is False:
            return "Coinbase's market is closed now"
        return None

    # ---- the mark and the money
    def set_mark(self, px: float | None, ts: int) -> None:
        if px:
            self.mark, self.mark_ts = float(px), int(ts)

    def upnl(self, px: float | None = None) -> float:
        pos, px = self.book["position"], px if px is not None else self.mark
        if pos is None or px is None:
            return 0.0
        return (1 if pos["side"] == "LONG" else -1) * pos["n"] * self.cs * (px - pos["entry"])

    @property
    def real_equity(self) -> float | None:
        b = self.real.balance
        return None if b is None else b["equity"]

    @property
    def usable(self):
        u = self.real.usable
        return None if u is None else u + self.book["cash"] + self.upnl()

    def fetch_balance(self, params=None) -> dict:
        """The account LIVE would hold: the real one plus the virtual book's P&L (see the module docstring)."""
        r = self.real.fetch_balance(params)
        real = r["total"]["USD"]
        eq = real + self.book["cash"] + self.upnl()
        return {"info": {"real": r["info"], "virtual_cash": self.book["cash"], "virtual_upnl": self.upnl()},
                "total": {"USD": eq}, "free": {"USD": eq}, "USD": {"total": eq, "free": eq}}

    # ---- the virtual book's reads, shaped as CoinbaseExchange answers them
    def fetch_positions(self, symbols=None, params=None) -> list[dict]:
        pos = self.book["position"]
        if pos is None:
            return []
        return [{"symbol": self.product, "side": pos["side"].lower(), "contracts": float(pos["n"]),
                 "contractSize": self.cs, "entryPrice": pos["entry"], "unrealizedPnl": self.upnl(), "info": pos}]

    def _order_view(self, o: dict) -> dict:
        status = {"OPEN": "open", "QUEUED": "open", "FILLED": "closed", "CANCELLED": "canceled"}[o["status"]]
        filled = o["filled"]
        return {"id": o["order_id"], "clientOrderId": o["client_order_id"], "symbol": self.product,
                "side": o["side"].lower(), "type": {"market": "market", "close": "market", "stop": "stop_limit"}[o["kind"]],
                "status": status, "coinbase_status": o["status"], "amount": float(o["n"]), "filled": float(filled),
                "remaining": float(max(0, o["n"] - filled)), "average": o["avg"] or None, "price": o.get("limit"),
                "triggerPrice": o.get("stop"), "stopDirection": o.get("direction"),
                "triggerStatus": o.get("trigger"), "reduceOnly": None,
                "fee": {"cost": o["fees"], "currency": "USD"}, "timestamp": o["created"],
                "lastTradeTimestamp": o.get("last_fill"), "rejectReason": None, "info": dict(o)}

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None) -> list[dict]:
        return [self._order_view(o) for o in self.book["orders"].values() if o["status"] in ("OPEN", "QUEUED")]

    def fetch_order(self, id, symbol=None, params=None) -> dict:
        o = self.book["orders"].get(str(id))
        if o is None:
            raise CoinbaseRejected("GET", f"{V3}/orders/historical/{id}", 404,
                                   {"error": "NOT_FOUND", "message": "order with this orderID was not found"})
        return self._order_view(o)

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None) -> list[dict]:
        out = []
        for f in self.book["fills"]:
            if since is not None and f["ts"] < since:
                continue
            out.append({"id": f["id"], "order": f["order"], "symbol": self.product, "side": f["side"],
                        "amount": float(f["n"]), "price": f["price"], "fee": {"cost": f["fee"], "currency": "USD"},
                        "timestamp": f["ts"], "takerOrMaker": f.get("liq"), "info": dict(f)})
        return sorted(out, key=lambda t: t["timestamp"])

    def fetch_funding_history(self, symbol=None, since=None, limit=None, params=None) -> list[dict]:
        """The virtual position's funding (ccxt's sign: negative = paid), at SHADOW's logged rates: an estimate."""
        return [{"timestamp": r["ts"], "amount": -r["paid"], "symbol": self.product, "info": {"id": r["id"]}}
                for r in self.book["funding"] if since is None or r["ts"] >= since]

    def find_order(self, client_order_id: str, since_ms: int):
        oid = self.book["cids"].get(client_order_id)
        return None if oid is None else self._order_view(self.book["orders"][oid])

    def opened_by(self, side: str, since_ms: int):
        opening = "buy" if side == "long" else "sell"
        fills = [f for f in self.book["fills"] if f["side"] == opening and f["ts"] >= since_ms]
        return None if not fills else self.fetch_order(fills[-1]["order"])

    def watchdog(self) -> list[tuple[str, str]]:
        """What would be wrong with LIVE's protection on the virtual book (read only)."""
        pos, orders = self.book["position"], self.fetch_open_orders()
        out = []
        if pos is None:
            for o in orders:
                out.append(("stray", f"virtual order {o['id']} rests with no virtual position"))
        elif not any(o.get("triggerPrice") for o in orders):
            out.append(("unprotected", "the virtual position has no virtual stop resting"))
        return out

    # ---- orders: the exact request LIVE would POST, played out on the virtual book
    def _now(self) -> int:
        return self.real.milliseconds()

    def _record(self, kind: str, path: str, body: dict, text: str, **sim) -> dict:
        rec = {"ts": self._now(), "kind": kind, "method": "POST", "path": path, "body": body, "text": text, **sim}
        self.book["would_send"] = (self.book["would_send"] + [rec])[-WOULD_SEND_KEEP:]
        self.sent_this_run.append(rec)
        self.log(f"SHADOW would send: {text}")
        return rec

    def _closing(self) -> str | None:
        pos = self.book["position"]
        return None if pos is None else ("sell" if pos["side"] == "LONG" else "buy")

    def _new_order(self, body: dict, kind: str, side: str, n: int, **extra) -> dict:
        self.book["seq"] += 1
        oid = f"shadow-{self.book['seq']:06d}"
        o = {"order_id": oid, "client_order_id": body["client_order_id"], "kind": kind, "side": side.upper(), "n": n,
             "status": "OPEN", "filled": 0, "avg": 0.0, "fees": 0.0, "created": self._now(), "last_fill": None,
             **extra}
        self.book["orders"][oid] = o
        self.book["cids"][body["client_order_id"]] = oid
        return o

    def _fee(self, n: int, px: float) -> float:
        qty = n * self.cs
        if self.rules is not None:
            return self.rules.fee(qty, px, self.fee_rate)
        return max(COINBASE.fee_min_per_contract * n, qty * px * self.fee_rate)

    def _fill(self, o: dict, n: int, px: float, ts: int) -> float:
        """n contracts of `o` at `px`: the position moves (with no reduce-only: through flat and out the other side
        if the order is bigger than it), the fee and any P&L go into the virtual cash. Returns the fee."""
        sign = 1 if o["side"] == "BUY" else -1
        fee = self._fee(n, px)
        b = self.book
        pos = b["position"]
        if pos is None:
            b["position"] = {"side": "LONG" if sign > 0 else "SHORT", "n": n, "entry": px, "ts": ts}
        elif (pos["side"] == "LONG") == (sign > 0):
            tot = pos["n"] + n
            pos["entry"] = (pos["entry"] * pos["n"] + px * n) / tot
            pos["n"] = tot
        else:
            psign = 1 if pos["side"] == "LONG" else -1
            closed = min(n, pos["n"])
            b["cash"] += psign * closed * self.cs * (px - pos["entry"])
            pos["n"] -= closed
            rest = n - closed
            b["position"] = None if pos["n"] == 0 else pos
            if closed:
                b["realized_trades"] = b.get("realized_trades", 0) + (1 if b["position"] is None else 0)
            if rest > 0:
                b["position"] = {"side": "LONG" if sign > 0 else "SHORT", "n": rest, "entry": px, "ts": ts}
        b["cash"] -= fee
        b["fees"] += fee
        b["seq"] += 1
        b["fills"] = (b["fills"] + [{"id": f"shadow-fill-{b['seq']:06d}", "order": o["order_id"],
                                     "side": o["side"].lower(), "n": n, "price": px, "fee": round(fee, 6), "ts": ts,
                                     "liq": "taker" if o["kind"] != "stop" else "maker"}])[-FILLS_KEEP:]
        done = o["filled"] + n
        o["avg"] = (o["avg"] * o["filled"] + px * n) / done
        o["filled"], o["fees"], o["last_fill"] = done, o["fees"] + fee, ts
        if done >= o["n"]:
            o["status"] = "FILLED"
        return fee

    def _same(self, cid: str):
        """Coinbase's idempotency: an order sent again with a client_order_id already used comes back as the first."""
        oid = self.book["cids"].get(cid)
        return None if oid is None else self.book["orders"][oid]

    def create_order(self, symbol, type, side, amount, price=None, params=None) -> dict:
        params = dict(params or {})
        self.real._check_symbol(symbol)
        side = _ccxt_side(side)
        cid = params.get("clientOrderId")
        if not cid:
            raise OrderRefused("an order needs its clientOrderId (that's what makes a retry safe)")
        n = float(amount)
        if abs(n - round(n)) > 1e-9 or n < 1:
            raise OrderRefused(f"{amount} isn't a whole number of contracts")
        n = int(round(n))
        if params.get("stopLossPrice") is not None:
            return self._stop(cid, side, n, float(params["stopLossPrice"]))
        if params.get("reduceOnly"):
            return self._close(cid, side, n, price)
        return self._entry(cid, side, n, price)

    def _market_px(self, side: str, ref) -> tuple[float, str]:
        try:
            bba = self.real.best_bid_ask()
        except Exception as exc:
            self.log(f"SHADOW: no best bid/ask for the simulated fill: {type(exc).__name__}")
            bba = None
        if bba is not None:
            return (bba[1], "Coinbase's best ask then") if side == "buy" else (bba[0], "Coinbase's best bid then")
        px = float(ref or self.mark or 0.0)
        if not px:
            raise OrderRefused("no price to simulate the fill at (no fresh book from Coinbase)")
        return px, "no fresh book from Coinbase: at the desk's reference price"

    def _entry(self, cid: str, side: str, n: int, ref) -> dict:
        same = self._same(cid)
        if same is not None:
            return self._order_view(same)
        why = self.entry_block()
        if why:
            raise OrderRefused(f"entry not sent: {why}")
        if self.book["position"] is not None or self.fetch_open_orders():
            raise OrderRefused("entry not sent: a position or an order already rests on the product (Coinbase has no "
                               "reduce-only, so the desk never stacks orders)")
        body = co.market(cid, side.upper(), n)
        px, where = self._market_px(side, ref)
        o = self._new_order(body, "market", side, n)
        fee = self._fill(o, n, px, self._now())
        self._record("entry", f"{V3}/orders", body,
                     f"{_utc(self._now())} · POST {V3}/orders · {side.upper()} {_contracts(n)} at market · simulated "
                     f"fill {_usd(px)} ({where}), fee est. {_usd(fee)}", fill=px, fee=round(fee, 6), contracts=n)
        return self._order_view(o)

    def _stop(self, cid: str, side: str, n: int, trigger: float) -> dict:
        same = self._same(cid)
        if same is not None:
            if same["status"] == "CANCELLED" and not same["filled"]:
                raise StaleClientOrderId(f"stop order {same['order_id']} came back CANCELLED: its client id was used "
                                         f"before")
            if same.get("stop") != co.round_price(trigger) or same["n"] != n or same["side"] != side.upper():
                raise StaleClientOrderId(f"order {same['order_id']} isn't the stop just sent (its client id was used "
                                         f"before)")
            return self._order_view(same)
        closing = self._closing()
        if closing is None:
            raise OrderRefused("stop not sent: the account holds no position on the product, and a stop with no "
                               "position would open one if it fired")
        if side != closing:
            raise OrderRefused(f"stop not sent: a {side} stop doesn't close a {self.book['position']['side'].lower()} "
                               f"position")
        resting = sum(o["n"] - o["filled"] for o in self.book["orders"].values()
                      if o["status"] in ("OPEN", "QUEUED") and o["side"] == closing.upper())
        if resting + n > self.book["position"]["n"]:
            raise OrderRefused(f"stop not sent: {resting:g} closing contracts already rest against the "
                               f"{self.book['position']['n']} held; {n} more would open a position if they all fired")
        stop, limit, direction = co.stop_limit_prices(closing.upper(), trigger)
        last = None
        try:
            got = self.real.last_trade()
            last = got[0] if got else None
        except Exception as exc:
            self.log(f"SHADOW: no last trade to check the stop against: {type(exc).__name__}")
        last = last or self.mark
        if last is not None and ((closing == "sell" and stop >= last) or (closing == "buy" and stop <= last)):
            raise StopThroughMarket(f"the price is already through the stop at {stop:,} (Coinbase would refuse it: "
                                    f"PREVIEW_STOP_PRICE_{'ABOVE' if closing == 'sell' else 'BELOW'}_LAST_TRADE_PRICE)")
        body = co.stop_limit(cid, closing.upper(), n, trigger)
        status = "QUEUED" if self.real.session_open() is False else "OPEN"
        o = self._new_order(body, "stop", closing, n, stop=float(stop), limit=float(limit), direction=direction,
                            trigger="STOP_PENDING")
        o["status"] = status
        self._record("stop", f"{V3}/orders", body,
                     f"{_utc(self._now())} · POST {V3}/orders · protective stop: {closing.upper()} {_contracts(n)}, "
                     f"stop {_usd(stop, 0)}, limit {_usd(limit, 0)} (stop-limit: Coinbase has no stop-market)",
                     stop=stop, limit=limit, contracts=n)
        return self._order_view(o)

    def _close(self, cid: str, side: str, n: int, ref) -> dict:
        same = self._same(cid)
        if same is not None:
            return self._order_view(same)
        closing = self._closing()
        if closing is None:
            raise OrderRefused("close not sent: the account holds no position on the product")
        if side != closing or n > self.book["position"]["n"]:
            raise OrderRefused(f"close not sent: {side} {n} doesn't close the "
                               f"{self.book['position']['side'].lower()} {self.book['position']['n']} held (it would "
                               f"open the other way)")
        body = co.close_position(cid, n)
        px, where = self._market_px(side, ref)
        entry = self.book["position"]["entry"]
        sign = 1 if self.book["position"]["side"] == "LONG" else -1
        o = self._new_order({**body, "side": side.upper()}, "close", side, n)
        fee = self._fill(o, n, px, self._now())
        self._record("close", f"{V3}/orders/close_position", body,
                     f"{_utc(self._now())} · POST {V3}/orders/close_position · close {_contracts(n)} at market · "
                     f"simulated fill {_usd(px)} ({where}), fee est. {_usd(fee)}, P&L est. "
                     f"{sign * n * self.cs * (px - entry) - fee:+,.2f}", fill=px, fee=round(fee, 6), contracts=n)
        return self._order_view(o)

    def cancel_order(self, id, symbol=None, params=None) -> dict:
        oid = str(id)
        body = co.batch_cancel([oid])
        o = self.book["orders"].get(oid)
        what = f"the stop at {_usd(o['stop'], 0)}" if o and o.get("stop") else f"order {oid}"
        self._record("cancel", f"{V3}/orders/batch_cancel", body,
                     f"{_utc(self._now())} · POST {V3}/orders/batch_cancel · cancel {what} (Coinbase has no "
                     f"reduce-only stops, so the old stop goes before a new one: never two at once)")
        if o is None:
            raise CancelFailed(f"order {oid} isn't known ({'UNKNOWN_CANCEL_ORDER'})")
        if o["status"] == "FILLED":
            raise StopAlreadyFilled(f"order {oid} had already filled")
        o["status"] = "CANCELLED"
        return {"id": oid, "status": "canceled"}

    # ---- between runs: the virtual stops against Coinbase's 5-minute candles, and funding
    def _candles(self, start_s: int, end_s: int) -> list[dict]:
        out: dict[int, dict] = {}
        lo = start_s - start_s % FIVE_MIN_S
        while lo < end_s:
            hi = min(end_s, lo + (CANDLE_PAGE - 1) * FIVE_MIN_S)
            rows = self.real.rest.get(f"{V3}/products/{self.product}/candles",
                                      {"granularity": "FIVE_MINUTE", "start": lo, "end": hi}).get("candles") or []
            for r in rows:
                try:
                    c = {"start": int(r["start"]), "open": float(r["open"]), "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"])}
                except (KeyError, TypeError, ValueError):
                    continue
                if lo <= c["start"] <= hi:
                    out[c["start"]] = c
            lo = hi + FIVE_MIN_S
        return [out[k] for k in sorted(out)]

    def _charge_funding(self, until_ms: int, px: float | None) -> None:
        b, pos = self.book, self.book["position"]
        t = b.get("funding_until_ts")
        if t is None:
            b["funding_until_ts"] = until_ms - until_ms % HOUR_MS
            return
        h = (t // HOUR_MS + 1) * HOUR_MS
        while h <= until_ms:
            if pos is not None and self.funding_rate is not None and px:
                rate = self.funding_rate(h)
                if rate is not None:
                    sign = 1 if pos["side"] == "LONG" else -1
                    paid = sign * pos["n"] * self.cs * px * rate
                    b["cash"] -= paid
                    b["funding"] = (b["funding"] + [{"id": f"f{h}", "ts": h, "paid": round(paid, 8)}])[-FUNDING_KEEP:]
            b["funding_until_ts"] = h
            h += HOUR_MS

    def _play(self, o: dict, c: dict) -> None:
        """One resting stop-limit against one 5-minute candle."""
        sell = o["side"] == "SELL"
        stop, limit = o["stop"], o["limit"]
        ts = (c["start"] + FIVE_MIN_S) * 1000 - 1
        if o.get("trigger") == "STOP_PENDING":
            if not (c["low"] <= stop if sell else c["high"] >= stop):
                return
            o["trigger"] = "STOP_TRIGGERED"
            first = min(stop, c["open"]) if sell else max(stop, c["open"])
            if (sell and first >= limit) or (not sell and first <= limit):
                self._fill(o, o["n"] - o["filled"], first, ts)
                return
        # triggered, its limit not met yet: a resting limit order at `limit`
        if (sell and c["high"] >= limit) or (not sell and c["low"] <= limit):
            self._fill(o, o["n"] - o["filled"], limit, ts)

    def advance(self, now_ms: int) -> list[tuple[str, str]]:
        """Play the time since the last run: every resting virtual stop against each closed 5-minute candle, in
        order, and the funding hours. Returns what happened, for the board, as (kind, text): 'stop_filled' (a simulated
        stop filled), 'stop_unfilled' (it triggered, its limit was passed: no fill), 'replay_gap'."""
        b = self.book
        start = b.get("checked_until_ts")
        said: list[tuple[str, str]] = []
        resting = [o for o in b["orders"].values() if o["status"] in ("OPEN", "QUEUED") and o["kind"] == "stop"]
        if start is None or (b["position"] is None and not resting):
            b["checked_until_ts"] = now_ms - now_ms % (FIVE_MIN_S * 1000)
            self._charge_funding(now_ms, self.mark)
            return said
        if now_ms - start > REPLAY_MAX_MS:
            said.append(("replay_gap", f"SHADOW last ran {(now_ms - start) / 3600_000:.0f} h ago: only the last "
                                       f"{REPLAY_MAX_MS // 3600_000} h of 5-minute candles are replayed against the "
                                       f"virtual stop"))
            start = now_ms - REPLAY_MAX_MS
        rows = self._candles(start // 1000, now_ms // 1000)
        last_px = None
        for c in rows:
            end_ms = (c["start"] + FIVE_MIN_S) * 1000
            if end_ms > now_ms or end_ms <= start:
                continue
            self._charge_funding(c["start"] * 1000, last_px or c["open"])
            for o in sorted((o for o in b["orders"].values() if o["status"] in ("OPEN", "QUEUED")
                             and o["kind"] == "stop" and o["created"] < end_ms), key=lambda o: o["created"]):
                if o["status"] == "QUEUED":
                    o["status"] = "OPEN"        # Coinbase places a queued stop-limit at the reopen
                before = o["filled"]
                self._play(o, c)
                if o["filled"] > before:
                    said.append(("stop_filled", f"the simulated stop at {_usd(o['stop'], 0)} filled at "
                                                f"{_usd(o['avg'])} ({_utc(int(c['start'] * 1000))} 5-minute "
                                                f"candle)"))
                elif o.get("trigger") == "STOP_TRIGGERED" and not o.get("said_unfilled"):
                    o["said_unfilled"] = True
                    said.append(("stop_unfilled", f"the simulated stop at {_usd(o['stop'], 0)} triggered but its "
                                                  f"limit {_usd(o['limit'], 0)} was passed: no simulated fill, as a "
                                                  f"stop-limit on Coinbase wouldn't fill there"))
            last_px = c["close"]
            b["checked_until_ts"] = end_ms
        self._charge_funding(now_ms, self.mark or last_px)
        return said

    # ---- what the board shows
    def summary(self) -> dict:
        b = self.book
        pos = b["position"]
        return {"position": None if pos is None else {"side": pos["side"], "contracts": pos["n"], "entry": pos["entry"],
                                                      "upnl": round(self.upnl(), 2)},
                "orders": [{k: o.get(k) for k in ("order_id", "side", "n", "stop", "limit", "status", "trigger")}
                           for o in b["orders"].values() if o["status"] in ("OPEN", "QUEUED")],
                "cash": round(b["cash"], 2), "fees": round(b["fees"], 2),
                "funding": round(sum(r["paid"] for r in b["funding"]), 2), "fills": b["fills"][-20:]}


def gc_book(book: dict, keep_ms: int, now_ms: int) -> None:
    """Old finished orders and their ids out of the book (a few hundred a year), keeping anything still resting."""
    keep = {oid: o for oid, o in book["orders"].items()
            if o["status"] in ("OPEN", "QUEUED") or now_ms - (o.get("last_fill") or o["created"]) < keep_ms}
    book["orders"] = keep
    book["cids"] = {c: oid for c, oid in book["cids"].items() if oid in keep}


def stop_risk(entry: float, stop: float, side_sign: int, n: int, equity: float, offset: float = co.STOP_LIMIT_OFFSET,
              cs: float = COINBASE.contract_size) -> dict:
    """What a stop-limit position risks, both ways (estimates): at the stop's trigger, and filled at its limit
    (offset past the trigger). Past the limit, nothing fills until the next run closes at market: no bound."""
    s, lim, _ = co.stop_limit_prices("SELL" if side_sign > 0 else "BUY", stop, offset)
    at_stop = abs(entry - s) * n * cs
    at_limit = abs(entry - lim) * n * cs
    return {"stop": s, "limit": lim, "at_stop": at_stop, "at_limit": at_limit,
            "at_stop_pct": at_stop / equity if equity > 0 else math.inf,
            "at_limit_pct": at_limit / equity if equity > 0 else math.inf}
