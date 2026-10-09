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
import os
import time
from pathlib import Path

from ..models import Order, Position, Side, Trade
from ..thresholds import Params

SYMBOLS = {
    "hyperliquid": "BTC/USDC:USDC",
    "binanceusdm": "BTC/USDT:USDT",
    "bybit": "BTC/USDT:USDT",
    "okx": "BTC/USDT:USDT",
    "krakenfutures": "BTC/USD:USD",
}


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
                 max_notional_usd: float | None = None, exchange=None):
        self.p = p
        self.venue = venue or p.market.venue
        self.symbol = SYMBOLS[self.venue]
        self.ex = exchange or make_exchange(self.venue)
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
        self.stop_order_id: str | None = None
        self.cash = self._balance()
        self._load_local()
        self.sync()

    # ---- exchange reads ------------------------------------------------
    def _balance(self) -> float:
        bal = self.ex.fetch_balance()
        quote = self.symbol.split(":")[-1]
        total = bal.get("total", {}).get(quote)
        if total is None:
            total = bal.get(quote, {}).get("total", 0.0)
        return float(total or 0.0)

    def _exchange_position(self) -> tuple[Side | None, float, float]:
        for pos in self.ex.fetch_positions([self.symbol]):
            qty = float(pos.get("contracts") or 0.0) * float(pos.get("contractSize") or 1.0)
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
            qty = min(qty, self.max_notional_usd / price)
        qty = float(self.ex.amount_to_precision(self.symbol, qty))
        if qty <= 0:
            return
        side = "buy" if o.side is Side.LONG else "sell"
        res = self.ex.create_order(self.symbol, "market", side, qty, price)
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
        res = self.ex.create_order(self.symbol, "market", side, pos.qty, price, {"reduceOnly": True})
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
        res = self.ex.create_order(self.symbol, "market", side, pos.qty, stop,
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
