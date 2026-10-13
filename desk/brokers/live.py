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


class LiveBroker:
    is_live = True

    def __init__(self, p: Params, venue: str | None = None, state_path: str | Path = "state/live_position.json",
                 max_notional_usd: float | None = None, exchange=None, log=print):
        self.p = p
        self.log = log
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
        self.stop_order_id: str | None = None
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

    def equity(self, price: float) -> float:
        if self.venue in EQUITY_INCLUDES_UPNL or self.position is None:
            return self.cash
        return self.cash + self.position.unrealized(price)

    def sync(self, price: float | None = None) -> None:
        """Reconcile with the exchange. If the stop filled while we were away, record the trade."""
        side, qty, entry = self._exchange_position()
        if side is None and self.position is not None:
            # the exchange closed us — almost certainly the resting stop
            pos = self.position
            px = price or pos.stop
            gross = pos.side.sign * pos.qty * (px - pos.entry_price)
            fee = pos.qty * px * self.p.costs.taker_fee_bps / 1e4
            self.trades.append(Trade(pos.side, pos.qty, pos.entry_ts, pos.entry_price, int(time.time() * 1000), px,
                                     "stop (exchange)", gross, pos.fees_paid + fee, pos.funding_paid))
            self.position = None
            self.stop_order_id = None
        elif side is not None and self.position is None:
            # a position we don't know about: adopt it; the runner gives it a stop straight away
            now = int(time.time() * 1000)
            self.position = Position(side, qty, entry, now, stop=0.0, best_close=entry)
        elif side is not None and self.position is not None:
            self.position.qty = qty
        self.cash = self._balance()
        self._save_local()

    # ---- orders ----------------------------------------------------------
    def submit(self, order: Order) -> None:
        self.pending.append(order)
        self.orders_submitted += 1

    def execute_now(self, price: float, ts: int) -> None:
        pending, self.pending = self.pending, []
        for o in pending:
            if o.action == "open" and self.position is None:
                self._open(o, price, ts)
            elif o.action == "close" and self.position is not None:
                self._close(price, ts, o.reason)

    fill_pending = execute_now

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
        res = self.ex.create_order(self.symbol, "market", side, amount, price)
        if res.get("filled") is not None:
            qty = float(res["filled"]) * self.contract_size
        fill = float(res.get("average") or res.get("price") or price)
        fee = float((res.get("fee") or {}).get("cost") or qty * fill * self.p.costs.taker_fee_bps / 1e4)
        self.fees_total += fee
        self.position = Position(o.side, qty, fill, ts, stop=o.stop or 0.0, best_close=fill, fees_paid=fee,
                                 setup=o.reason)
        self.orders_filled += 1
        self.last_fill = {"action": "open", "side": o.side.value, "qty": qty, "price": fill, "fee": fee, "ts": ts,
                          "reason": o.reason}
        if o.stop:
            self._place_stop(o.stop)
        self._save_local()

    def _close(self, price: float, ts: int, reason: str) -> Trade | None:
        pos = self.position
        if pos is None:
            return None
        self._cancel_stop()
        side = "sell" if pos.side is Side.LONG else "buy"
        res = self.ex.create_order(self.symbol, "market", side, self._amount(pos.qty), price, {"reduceOnly": True})
        fill = float(res.get("average") or res.get("price") or price)
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

    def _place_stop(self, stop: float) -> None:
        pos = self.position
        side = "sell" if pos.side is Side.LONG else "buy"
        stop = float(self.ex.price_to_precision(self.symbol, stop))
        res = self.ex.create_order(self.symbol, "market", side, self._amount(pos.qty), stop,
                                   {"stopLossPrice": stop, "reduceOnly": True})
        self.stop_order_id = res.get("id")

    def _cancel_stop(self) -> None:
        if self.stop_order_id:
            try:
                self.ex.cancel_order(self.stop_order_id, self.symbol)
            except Exception:  # already filled or gone — the sync will tell us which
                pass
            self.stop_order_id = None

    def update_stop(self, stop: float) -> None:
        """Move the resting stop: place the new one first, then cancel the old one (never unprotected)."""
        if self.position is None:
            return
        old = self.stop_order_id
        self.position.stop = stop
        self._place_stop(stop)
        if old:
            try:
                self.ex.cancel_order(old, self.symbol)
            except Exception:
                pass
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
            "stop_order_id": self.stop_order_id,
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
        pd = d.get("position")
        if pd:
            self.position = Position(Side(pd["side"]), pd["qty"], pd["entry_price"], pd["entry_ts"], pd["stop"],
                                     pd["best_close"], pd.get("fees_paid", 0.0), pd.get("funding_paid", 0.0),
                                     setup=pd.get("setup", ""))
        self.trades = [Trade(Side(t["side"]), t["qty"], t["entry_ts"], t["entry_price"], t["exit_ts"],
                             t["exit_price"], t["reason"], t["gross_pnl"], t["fees"], t["funding"])
                       for t in d.get("trades", [])]
