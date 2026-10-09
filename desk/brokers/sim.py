"""Simulated broker — used by the backtest and by paper trading.

Every fill pays the taker fee and an adverse slippage; every bar held pays (or
receives) funding. Stops are checked against each bar's high/low; if a bar opens
through the stop, the fill is at the open, not the stop (gaps hurt, as in real life).
"""

from __future__ import annotations

from ..models import Bar, Order, Position, Side, Trade
from ..thresholds import Params


class SimBroker:
    is_live = False

    def __init__(self, p: Params):
        self.p = p
        self.cash = p.sizing.starting_equity        # realised equity (fees and funding already taken out)
        self.position: Position | None = None
        self.pending: list[Order] = []
        self.trades: list[Trade] = []
        self.fees_total = 0.0
        self.funding_total = 0.0
        self.orders_submitted = 0
        self.orders_filled = 0
        self.last_fill: dict | None = None

    # ---- costs -------------------------------------------------------------
    @property
    def fee_rate(self) -> float:
        return self.p.costs.taker_fee_bps / 1e4

    @property
    def slip(self) -> float:
        return self.p.costs.slippage_bps / 1e4

    # ---- queries -----------------------------------------------------------
    def equity(self, price: float) -> float:
        return self.cash + (self.position.unrealized(price) if self.position else 0.0)

    # ---- orders ------------------------------------------------------------
    def submit(self, order: Order) -> None:
        self.pending.append(order)
        self.orders_submitted += 1

    def fill_pending(self, price: float, ts: int) -> None:
        """Execute everything queued at the last decision, at `price` (next bar open, or live mid)."""
        pending, self.pending = self.pending, []
        for o in pending:
            if o.action == "open" and self.position is None:
                self._open(o, price, ts)
            elif o.action == "close" and self.position is not None:
                self._close(price, ts, o.reason)

    execute_now = fill_pending

    def _open(self, o: Order, price: float, ts: int) -> None:
        fill = price * (1 + self.slip * o.side.sign)
        fee = o.qty * fill * self.fee_rate
        self.cash -= fee
        self.fees_total += fee
        self.position = Position(o.side, o.qty, fill, ts, stop=o.stop or 0.0, best_close=fill, fees_paid=fee,
                                 take_profit=o.meta.get("take_profit"), setup=o.reason)
        self.orders_filled += 1
        self.last_fill = {"action": "open", "side": o.side.value, "qty": o.qty, "price": fill, "fee": fee,
                          "ts": ts, "reason": o.reason}

    def _close(self, price: float, ts: int, reason: str) -> Trade:
        pos = self.position
        assert pos is not None
        fill = price * (1 - self.slip * pos.side.sign)
        fee = pos.qty * fill * self.fee_rate
        gross = pos.side.sign * pos.qty * (fill - pos.entry_price)
        self.cash += gross - fee
        self.fees_total += fee
        trade = Trade(pos.side, pos.qty, pos.entry_ts, pos.entry_price, ts, fill, reason, gross,
                      pos.fees_paid + fee, pos.funding_paid)
        self.trades.append(trade)
        self.position = None
        self.orders_filled += 1
        self.last_fill = {"action": "close", "side": pos.side.value, "qty": pos.qty, "price": fill, "fee": fee,
                          "ts": ts, "reason": reason, "net_pnl": trade.net_pnl}
        return trade

    def close_now(self, price: float, ts: int, reason: str) -> Trade | None:
        self.pending = []
        return self._close(price, ts, reason) if self.position else None

    def update_stop(self, stop: float) -> None:
        if self.position:
            self.position.stop = stop

    # ---- the passage of time ---------------------------------------------
    def check_stop_bar(self, bar: Bar) -> Trade | None:
        pos = self.position
        if pos is None or not pos.stop:
            return None
        if pos.side is Side.LONG and bar.low <= pos.stop:
            return self._close(min(bar.open, pos.stop), bar.ts, "stop")
        if pos.side is Side.SHORT and bar.high >= pos.stop:
            return self._close(max(bar.open, pos.stop), bar.ts, "stop")
        tp = pos.take_profit   # checked after the stop: if a bar touches both, assume the worse one came first
        if tp and pos.side is Side.LONG and bar.high >= tp:
            return self._close(max(bar.open, tp), bar.ts, "take_profit")
        if tp and pos.side is Side.SHORT and bar.low <= tp:
            return self._close(min(bar.open, tp), bar.ts, "take_profit")
        return None

    def check_stop_price(self, price: float, ts: int) -> Trade | None:
        pos = self.position
        if pos is None or not pos.stop:
            return None
        if (pos.side is Side.LONG and price <= pos.stop) or (pos.side is Side.SHORT and price >= pos.stop):
            return self._close(price, ts, "stop")
        return None

    def apply_funding(self, rate: float, price: float) -> None:
        """`rate` = sum of funding rates charged during the bar. Longs pay positive funding."""
        pos = self.position
        if pos is None or rate == 0.0:
            return
        paid = pos.side.sign * pos.qty * price * rate
        self.cash -= paid
        pos.funding_paid += paid
        self.funding_total += paid

    def sync(self, price: float | None = None) -> None:
        """Live brokers reconcile with the exchange here; the simulator is its own source of truth."""

    # ---- persistence (paper trading survives restarts) ---------------------
    def to_dict(self) -> dict:
        pos = self.position
        return {
            "cash": self.cash, "fees_total": self.fees_total, "funding_total": self.funding_total,
            "orders_submitted": self.orders_submitted, "orders_filled": self.orders_filled,
            "last_fill": self.last_fill,
            "position": None if pos is None else {
                "side": pos.side.value, "qty": pos.qty, "entry_price": pos.entry_price, "entry_ts": pos.entry_ts,
                "stop": pos.stop, "best_close": pos.best_close, "fees_paid": pos.fees_paid,
                "funding_paid": pos.funding_paid, "take_profit": pos.take_profit, "setup": pos.setup,
            },
            "trades": [t.as_dict() for t in self.trades],
            "pending": [{"action": o.action, "side": o.side.value, "qty": o.qty, "reason": o.reason, "stop": o.stop}
                        for o in self.pending],
        }

    def load_dict(self, d: dict) -> None:
        self.cash = d["cash"]
        self.fees_total = d.get("fees_total", 0.0)
        self.funding_total = d.get("funding_total", 0.0)
        self.orders_submitted = d.get("orders_submitted", 0)
        self.orders_filled = d.get("orders_filled", 0)
        self.last_fill = d.get("last_fill")
        pd = d.get("position")
        self.position = None if pd is None else Position(
            Side(pd["side"]), pd["qty"], pd["entry_price"], pd["entry_ts"], pd["stop"], pd["best_close"],
            pd.get("fees_paid", 0.0), pd.get("funding_paid", 0.0), pd.get("take_profit"), pd.get("setup", ""))
        self.trades = [Trade(Side(t["side"]), t["qty"], t["entry_ts"], t["entry_price"], t["exit_ts"],
                             t["exit_price"], t["reason"], t["gross_pnl"], t["fees"], t["funding"])
                       for t in d.get("trades", [])]
        self.pending = [Order(o["action"], Side(o["side"]), o["qty"], o["reason"], o.get("stop"))
                        for o in d.get("pending", [])]
