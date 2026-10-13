"""Live broker — real orders through ccxt's unified API.

STATUS: written against ccxt's documented unified API (create_order with
reduceOnly / stopLossPrice, fetch_positions, fetch_balance) but NOT yet run
against a funded account. Before trusting it:

  1. run `python -m desk paper` for the promotion period,
  2. run `python -m desk live --max-notional 50` (a $50 cap) and watch every fill,
  3. only then raise the cap.

Design rules:
  * the protective stop lives ON THE EXCHANGE as a reduce-only stop-market order,
    so if this process dies the position is still protected;
  * the exchange is the source of truth for the position; the desk's local copy
    (entry, best close, stop) is reconciled on every sync;
  * any exception while placing an order leaves the desk in entry_kill.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from ..models import Order, Position, Side, Trade
from ..thresholds import Params

# ccxt's unified symbol for each coin's USD-margined perpetual, per venue. Hyperliquid's were checked against its
# public market list (ccxt.hyperliquid().load_markets(), no keys): BTC, ETH and SOL are "<COIN>/USDC:USDC", with
# size steps of 0.00001, 0.0001 and 0.01 and a $10 minimum order. The other venues follow ccxt's naming for linear
# perps and weren't reachable from here to check; LiveBroker refuses to start if the venue doesn't list the market.
SYMBOL_FORMATS = {
    "hyperliquid": "{coin}/USDC:USDC",
    "binanceusdm": "{coin}/USDT:USDT",
    "bybit": "{coin}/USDT:USDT",
    "okx": "{coin}/USDT:USDT",
    "krakenfutures": "{coin}/USD:USD",
}

TICK_SIZE = 4   # ccxt's precisionMode where precision["amount"] is the size step itself (not a count of decimals)


class LiveSetupError(RuntimeError):
    """Live trading refuses to start (no such market, a cap below the venue's minimum order, ...)."""


def market_symbol(venue: str, coin: str) -> str:
    """The perp this coin's desk trades on `venue`: the coin's own market, never another coin's."""
    if venue not in SYMBOL_FORMATS:
        raise LiveSetupError(f"no perp symbol known for venue {venue!r} (known: {', '.join(SYMBOL_FORMATS)})")
    return SYMBOL_FORMATS[venue].format(coin=coin.upper())


def make_exchange(venue: str):
    import ccxt  # imported lazily so backtest/paper never need it

    cls = getattr(ccxt, venue)
    if venue == "hyperliquid":
        cfg = {"walletAddress": os.environ["HL_WALLET_ADDRESS"], "privateKey": os.environ["HL_PRIVATE_KEY"]}
    else:
        cfg = {"apiKey": os.environ["EXCHANGE_API_KEY"], "secret": os.environ["EXCHANGE_API_SECRET"]}
        if os.environ.get("EXCHANGE_API_PASSWORD"):
            cfg["password"] = os.environ["EXCHANGE_API_PASSWORD"]
    cfg["enableRateLimit"] = True
    ex = cls(cfg)
    ex.load_markets()
    return ex


# Venues whose reported account value already includes unrealised PnL.
EQUITY_INCLUDES_UPNL = {"hyperliquid"}

# The protective stop: tries to get it resting on the exchange before giving up and closing the position,
# and the wait before each retry (doubling: 1 s, 2 s, 4 s).
STOP_ATTEMPTS = 4
STOP_BACKOFF_S = 1.0


def _trigger(o: dict) -> float | None:
    """The trigger price of a ccxt order structure (None for a plain limit/market order)."""
    for k in ("triggerPrice", "stopLossPrice", "stopPrice"):
        if o.get(k) is not None:
            return float(o[k])
    return None


class LiveBroker:
    is_live = True

    def __init__(self, p: Params, venue: str | None = None, state_path: str | Path = "state/live_position.json",
                 max_notional_usd: float | None = None, exchange=None, log=print, on_alert=None, sleep=time.sleep,
                 clock=None):
        self.p = p
        self.log = log
        self.on_alert = on_alert              # the runner puts alerts on the board and the phone
        self.sleep = sleep
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.venue = venue or p.market.venue
        self.coin = p.market.symbol.upper()
        self.symbol = market_symbol(self.venue, self.coin)
        self.ex = exchange or make_exchange(self.venue)
        self._load_market()
        if max_notional_usd is not None and self.min_cost and max_notional_usd < self.min_cost:
            raise LiveSetupError(f"--max-notional ${max_notional_usd:,.2f} is below {self.venue}'s minimum order of "
                                 f"${self.min_cost:,.2f} on {self.symbol}: every entry would be refused")
        self.state_path = Path(state_path)
        self.max_notional_usd = max_notional_usd
        self.position: Position | None = None
        self.pending: list[Order] = []
        self.trades: list[Trade] = []
        self.fees_total = 0.0
        self.funding_total = 0.0
        self.orders_submitted = 0
        self.orders_filled = 0
        self.last_fill: dict | None = None
        self.skipped: dict | None = None      # the last entry not sent: under the venue's minimum after rounding
        self.alerts: list[tuple[int, str]] = []
        self.stop_order_id: str | None = None
        self.stop_px: float | None = None     # trigger of that resting stop, as the venue rounded it
        self.last_price: float | None = None
        self.stops_to_clean = False           # a close left reduce-only stops behind that couldn't be cancelled yet
        self.cash = self._balance()
        self._load_local()
        self.sync()

    # ---- the market ------------------------------------------------------
    def _load_market(self) -> None:
        """Refuse to start unless the venue lists this coin's perp; keep its size step, contract size and minimums."""
        markets = getattr(self.ex, "markets", None) or self.ex.load_markets()
        m = markets.get(self.symbol)
        if m is None or not (m.get("swap") or m.get("contract")) or (m.get("base") or self.coin).upper() != self.coin:
            raise LiveSetupError(f"{self.venue} lists no {self.coin} perpetual ({self.symbol}); refusing to trade "
                                 f"{self.coin} on another market")
        if m.get("active") is False:
            raise LiveSetupError(f"{self.symbol} is not active on {self.venue}")
        self.market = m
        self.contract_size = float(m.get("contractSize") or 1.0)    # coins per contract (1 on Hyperliquid)
        step = (m.get("precision") or {}).get("amount")
        if step is not None and getattr(self.ex, "precisionMode", TICK_SIZE) != TICK_SIZE:
            step = 10.0 ** -float(step)                                 # precision given as a number of decimals
        self.amount_step = float(step) if step else None
        limits = m.get("limits") or {}
        self.min_cost = float((limits.get("cost") or {}).get("min") or 0.0)
        self.min_amount = float((limits.get("amount") or {}).get("min") or 0.0)

    def _amount(self, qty: float) -> float:
        """Coins -> the order amount the venue takes (contracts), cut DOWN to the size step, never rounded up past
        what the desk (or the --max-notional cap) allowed. 0 when that leaves nothing."""
        n = qty / self.contract_size
        if self.amount_step:
            n = math.floor(n / self.amount_step + 1e-9) * self.amount_step
        if n <= 0:
            return 0.0
        try:
            return float(self.ex.amount_to_precision(self.symbol, n))
        except Exception:   # ccxt raises when a positive amount rounds to zero
            return 0.0

    def _px(self, price: float) -> float:
        return float(self.ex.price_to_precision(self.symbol, price))

    def _closing_side(self, side: Side) -> str:
        return "sell" if side is Side.LONG else "buy"

    def alert(self, text: str) -> None:
        """Something a human must know about: logged, kept, and handed to the runner (board + phone)."""
        self.alerts = (self.alerts + [(self.clock(), text)])[-50:]
        self.log(f"ALERT {self.coin}: {text}")
        if self.on_alert is not None:
            try:
                self.on_alert(text)
            except Exception as exc:  # a failed notification must never stop the broker
                self.log(f"alert hook failed: {type(exc).__name__}: {exc}")

    # ---- exchange reads ------------------------------------------------
    def _balance(self) -> float:
        bal = self.ex.fetch_balance()
        quote = self.market.get("settle") or self.symbol.split(":")[-1]
        total = bal.get("total", {}).get(quote)
        if total is None:
            total = bal.get(quote, {}).get("total", 0.0)
        return float(total or 0.0)

    def _exchange_position(self) -> tuple[Side | None, float, float]:
        for pos in self.ex.fetch_positions([self.symbol]):
            if pos.get("symbol") not in (None, self.symbol):
                continue
            qty = float(pos.get("contracts") or 0.0) * float(pos.get("contractSize") or self.contract_size)
            if qty > 0:
                return Side(pos["side"]), qty, float(pos.get("entryPrice") or 0.0)
        return None, 0.0, 0.0

    def _resting_stops(self, side: str | None = None) -> list[dict]:
        """Reduce-only trigger orders resting on this market (on `side`, if given)."""
        out = []
        for o in self.ex.fetch_open_orders(self.symbol):
            if o.get("symbol") not in (None, self.symbol) or _trigger(o) is None or o.get("reduceOnly") is False:
                continue
            if side is None or o.get("side") in (None, side):
                out.append(o)
        return out

    def equity(self, price: float) -> float:
        if self.venue in EQUITY_INCLUDES_UPNL or self.position is None:
            return self.cash
        return self.cash + self.position.unrealized(price)

    def sync(self, price: float | None = None) -> None:
        """Reconcile with the exchange: the exchange's position is the truth. If the stop filled while we were away,
        record the trade. While a position is open, a stop must be resting on the exchange: if it's gone, put it back."""
        if price is not None:
            self.last_price = price
        side, qty, entry = self._exchange_position()
        if side is None and self.position is not None:
            # the exchange closed us — almost certainly the resting stop
            pos = self.position
            px = price or pos.stop
            gross = pos.side.sign * pos.qty * (px - pos.entry_price)
            fee = pos.qty * px * self.p.costs.taker_fee_bps / 1e4
            self.trades.append(Trade(pos.side, pos.qty, pos.entry_ts, pos.entry_price, self.clock(), px,
                                     "stop (exchange)", gross, pos.fees_paid + fee, pos.funding_paid))
            self.position = None
            self.stop_order_id = self.stop_px = None
            self.stops_to_clean = True
        elif side is not None and (self.position is None or side is not self.position.side):
            # a position we don't know about: adopt it. A stop already resting for it is kept; otherwise the
            # runner gives it one straight away.
            if self.position is not None:
                self.alert(f"the exchange holds a {side.value} {qty:g} {self.coin}, not the desk's "
                           f"{self.position.side.value}: adopting the exchange's position")
            self.position = Position(side, qty, entry, self.clock(), stop=0.0, best_close=entry)
            self.stop_order_id = self.stop_px = None
            stops = self._resting_stops(self._closing_side(side))
            if stops:
                self.position.stop = self.stop_px = _trigger(stops[0])
                self.stop_order_id = str(stops[0]["id"])
        elif side is not None:
            self.position.qty = qty
        if self.position is not None and self.position.stop:
            self._check_stop_resting()
        elif self.position is None and self.stops_to_clean:
            self._cancel_stops(keep=None)
        self.cash = self._balance()
        self._save_local()

    def _check_stop_resting(self) -> None:
        """Every sync while a position is open: the desk's stop must be resting on the exchange."""
        pos = self.position
        stops = self._resting_stops(self._closing_side(pos.side))
        want = self._px(pos.stop)
        at_stop = [o for o in stops if math.isclose(_trigger(o), want, rel_tol=1e-9)]
        if at_stop:
            ids = [str(o["id"]) for o in at_stop]
            if self.stop_order_id not in ids:
                self.stop_order_id = ids[0]
            self.stop_px = want
            return
        if stops:   # a stop rests, just not at the desk's level: a trail move that didn't go through. Try again.
            self.log(f"{self.coin}: resting stop at {_trigger(stops[0]):,.2f}, desk's stop {want:,.2f}: moving it")
            self._ensure_stop(pos.stop, self.last_price, self.clock(), keep_old=True)
            return
        self.alert(f"no stop resting on the exchange for the open {pos.side.value} {pos.qty:g} {self.coin}: "
                   f"placing it at {want:,.2f}")
        self._ensure_stop(pos.stop, self.last_price, self.clock())

    # ---- orders ----------------------------------------------------------
    def submit(self, order: Order) -> None:
        self.pending.append(order)
        self.orders_submitted += 1

    def execute_now(self, price: float, ts: int) -> None:
        self.last_price = price
        pending, self.pending = self.pending, []
        for o in pending:
            if o.action == "open" and self.position is None:
                self._open(o, price, ts)
            elif o.action == "close" and self.position is not None:
                self._close(price, ts, o.reason)

    fill_pending = execute_now

    @staticmethod
    def _accepted(res: dict | None, what: str) -> dict:
        if not res or res.get("status") == "rejected" or not res.get("id"):
            raise RuntimeError(f"{what} not accepted by the exchange: {res}")
        return res

    def _open(self, o: Order, price: float, ts: int) -> None:
        qty = o.qty
        if self.max_notional_usd is not None:
            # the cap holds after the fill's slippage too
            qty = min(qty, self.max_notional_usd / (price * (1 + self.p.costs.slippage_bps / 1e4)))
        amount = self._amount(qty)
        qty = amount * self.contract_size
        if qty <= 0 or qty * price * (1 - self.p.costs.slippage_bps / 1e4) < self.min_cost or amount < self.min_amount:
            self.skipped = {"ts": ts, "reason": o.reason, "qty": qty, "notional": qty * price,
                            "min_cost": self.min_cost, "step": self.amount_step}
            self.log(f"entry not sent: {qty:g} {self.coin} (${qty * price:,.2f}) after the size step "
                     f"{self.amount_step or 0:g} and the cap is under {self.venue}'s ${self.min_cost:,.2f} minimum")
            return
        side = "buy" if o.side is Side.LONG else "sell"
        res = self._accepted(self.ex.create_order(self.symbol, "market", side, amount, price), "entry order")
        if res.get("filled") is not None:
            qty = float(res["filled"]) * self.contract_size
        else:   # the venue didn't say how much filled: ask for the position
            ex_side, ex_qty, _ = self._exchange_position()
            qty = ex_qty if ex_side is o.side else 0.0
        if qty <= 0:
            self.alert(f"entry order {res['id']} for {amount:g} {self.coin} did not fill; no position")
            return
        fill = float(res.get("average") or res.get("price") or price)
        fee = float((res.get("fee") or {}).get("cost") or qty * fill * self.p.costs.taker_fee_bps / 1e4)
        self.fees_total += fee
        self.position = Position(o.side, qty, fill, ts, stop=o.stop or 0.0, best_close=fill, fees_paid=fee,
                                 setup=o.reason)
        self.orders_filled += 1
        self.last_fill = {"action": "open", "side": o.side.value, "qty": qty, "price": fill, "fee": fee, "ts": ts,
                          "reason": o.reason}
        self._save_local()
        if not o.stop:
            self.alert(f"entry {res['id']} came with no stop: closing it rather than holding it unprotected")
            self._close(price, ts, "no_stop")
            return
        self._ensure_stop(o.stop, price, ts)

    def _close(self, price: float | None, ts: int, reason: str) -> Trade | None:
        """Close at market (reduce-only). The close goes first and the stops are cancelled after it's done, so a
        close that fails leaves the stop in place."""
        pos = self.position
        if pos is None:
            return None
        ex_side, ex_qty, _ = self._exchange_position()
        if ex_side is None:     # already flat on the exchange (the stop got there first): book that instead
            n = len(self.trades)
            self.sync(price)
            return self.trades[-1] if len(self.trades) > n else None
        ref = price or self.last_price or pos.entry_price
        side = self._closing_side(pos.side)
        res = self._accepted(self.ex.create_order(self.symbol, "market", side, self._amount(ex_qty), ref,
                                                  {"reduceOnly": True}), f"close ({reason})")
        after_side, after_qty, _ = self._exchange_position()
        if after_side is not None:
            pos.qty = after_qty
            self._save_local()
            raise RuntimeError(f"close ({reason}) left {after_qty:g} {self.coin} open; its stop stays in place")
        self._cancel_stops(keep=None)
        fill = float(res.get("average") or res.get("price") or ref)
        fee = float((res.get("fee") or {}).get("cost") or pos.qty * fill * self.p.costs.taker_fee_bps / 1e4)
        gross = pos.side.sign * pos.qty * (fill - pos.entry_price)
        self.fees_total += fee
        t = Trade(pos.side, pos.qty, pos.entry_ts, pos.entry_price, ts, fill, reason, gross,
                  pos.fees_paid + fee, pos.funding_paid)
        self.trades.append(t)
        self.position = None
        self.orders_filled += 1
        self.last_fill = {"action": "close", "side": pos.side.value, "qty": pos.qty, "price": fill, "fee": fee,
                          "ts": ts, "reason": reason, "net_pnl": t.net_pnl}
        self.cash = self._balance()
        self._save_local()
        return t

    def close_now(self, price: float, ts: int, reason: str) -> Trade | None:
        self.pending = []
        return self._close(price, ts, reason)

    # ---- the protective stop ------------------------------------------------
    def _place_stop(self, stop: float) -> str:
        pos = self.position
        trig = self._px(stop)
        res = self._accepted(self.ex.create_order(self.symbol, "market", self._closing_side(pos.side),
                                                  self._amount(pos.qty), trig,
                                                  {"stopLossPrice": trig, "reduceOnly": True}), "stop order")
        return str(res["id"])

    def _find_stop(self, oid: str | None, stop: float) -> dict | None:
        """The resting stop with this id, or else one resting at this trigger (an order whose call failed after
        the exchange took it): never place a second one on top of it."""
        stops = self._resting_stops(self._closing_side(self.position.side))
        want = self._px(stop)
        for o in stops:
            if oid is not None and str(o["id"]) == oid:
                return o
        for o in stops:
            if math.isclose(_trigger(o), want, rel_tol=1e-9):
                return o
        return None

    def _ensure_stop(self, stop: float, price: float | None, ts: int, keep_old: bool = False) -> bool:
        """Get a stop at `stop` resting on the exchange, confirmed by reading it back from the open orders. Tries
        STOP_ATTEMPTS times with backoff. If it never shows up, the position is closed (reduce-only market) and an
        alert goes out: never a position without a stop. With `keep_old` (moving a trail), an older stop that is
        still resting keeps protecting the position instead, and the next sync tries the move again."""
        pos = self.position
        if pos is None:
            return True
        if price is not None and (price - stop) * pos.side.sign <= 0:
            # the market is already through the stop: that's the stop being hit
            self.alert(f"{self.coin} at {price:,.2f} is already through the stop {stop:,.2f}: closing at market")
            self._close(price, ts, "stop")
            return False
        err = "not on the exchange's open orders after placing it"
        for attempt in range(STOP_ATTEMPTS):
            if attempt:
                self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
            oid = None
            try:
                oid = self._place_stop(stop)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            try:
                found = self._find_stop(oid, stop)
            except Exception as exc:
                found, err = None, f"{type(exc).__name__}: {exc}"
            if found is not None:
                self.stop_order_id, self.stop_px = str(found["id"]), _trigger(found)
                self._cancel_stops(keep=self.stop_order_id)     # the old stop (a trail move), or a duplicate
                self._save_local()
                return True
            self.log(f"{self.coin}: stop at {stop:,.2f}, try {attempt + 1}/{STOP_ATTEMPTS}: {err}")
        if keep_old:
            try:
                old = self._resting_stops(self._closing_side(pos.side))
            except Exception:
                old = []
            if old:
                self.alert(f"could not move the stop to {stop:,.2f} after {STOP_ATTEMPTS} tries ({err}); the stop at "
                           f"{_trigger(old[0]):,.2f} still protects the position, trying again at the next sync")
                return False
        self.alert(f"could not get a stop resting on the exchange after {STOP_ATTEMPTS} tries ({err}): closing the "
                   f"{pos.side.value} {pos.qty:g} {self.coin} at market rather than hold it unprotected")
        self._close(price, ts, "no_stop")
        return False

    def _cancel_stops(self, keep: str | None) -> None:
        """Cancel every reduce-only stop on this market but `keep`. A stop left behind after a close would close the
        next position at the wrong level, so a cancel that fails is retried at the next sync."""
        try:
            stops = self._resting_stops()
        except Exception as exc:
            self.log(f"{self.coin}: could not list resting stops to cancel: {type(exc).__name__}: {exc}")
            if keep is None:
                self.stops_to_clean = True
            return
        failed = False
        for o in stops:
            if str(o["id"]) == keep:
                continue
            try:
                self.ex.cancel_order(o["id"], self.symbol)
            except Exception as exc:  # already filled or gone, or a blip: the next sync looks again
                failed = True
                self.log(f"{self.coin}: cancel of stop {o['id']} failed: {type(exc).__name__}: {exc}")
        if keep is None:
            self.stop_order_id = self.stop_px = None
            self.stops_to_clean = failed

    def update_stop(self, stop: float) -> None:
        """Move the resting stop: the new one is placed and confirmed first, then the old one is cancelled (never
        unprotected). If the new one won't go on, the old one stays and the next sync tries again."""
        if self.position is None:
            return
        self.position.stop = stop
        self._save_local()
        self._ensure_stop(stop, self.last_price, self.clock(), keep_old=True)
        if self.position is not None:
            self._save_local()

    def check_stop_price(self, price: float, ts: int):
        """The exchange holds the stop; just reconcile."""
        self.sync(price)

    def apply_funding(self, rate: float, price: float) -> None:
        """Funding is settled by the exchange; keep an estimate for the board."""
        if self.position:
            paid = self.position.side.sign * self.position.qty * price * rate
            self.position.funding_paid += paid
            self.funding_total += paid

    # ---- local mirror ------------------------------------------------------
    def _save_local(self) -> None:
        pos = self.position
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "stop_order_id": self.stop_order_id, "stop_px": self.stop_px, "stops_to_clean": self.stops_to_clean,
            "position": None if pos is None else {
                "side": pos.side.value, "qty": pos.qty, "entry_price": pos.entry_price, "entry_ts": pos.entry_ts,
                "stop": pos.stop, "best_close": pos.best_close, "fees_paid": pos.fees_paid,
                "funding_paid": pos.funding_paid, "setup": pos.setup},
            "trades": [t.as_dict() for t in self.trades[-500:]],
        }))

    def _load_local(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text())
        self.stop_order_id = d.get("stop_order_id")
        self.stop_px = d.get("stop_px")
        self.stops_to_clean = bool(d.get("stops_to_clean", False))
        pd = d.get("position")
        if pd:
            self.position = Position(Side(pd["side"]), pd["qty"], pd["entry_price"], pd["entry_ts"], pd["stop"],
                                     pd["best_close"], pd.get("fees_paid", 0.0), pd.get("funding_paid", 0.0),
                                     setup=pd.get("setup", ""))
        self.trades = [Trade(Side(t["side"]), t["qty"], t["entry_ts"], t["entry_price"], t["exit_ts"],
                             t["exit_price"], t["reason"], t["gross_pnl"], t["fees"], t["funding"])
                       for t in d.get("trades", [])]
