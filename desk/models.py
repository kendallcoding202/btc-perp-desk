"""Plain data types shared by every part of the desk."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Bar:
    ts: int          # open time, epoch milliseconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class FundingEvent:
    ts: int          # epoch ms when funding was charged
    rate: float      # fraction per funding period; positive = longs pay shorts


@dataclass
class MarketContext:
    """Live-only facts the backtest cannot know. None = unknown (gate is skipped)."""
    mid: float | None = None
    spread_bps: float | None = None
    depth_usd: float | None = None          # min(bid, ask) resting notional within 10 bps
    open_interest_usd: float | None = None
    funding_rate_now: float | None = None   # current predicted rate, per funding period
    funding_period_hours: float = 8.0
    session_open: bool | None = None        # a venue with sessions (Coinbase): is it open right now


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class Verdict(str, Enum):
    PASS = "PASS"
    MARK = "MARK"    # passed, but close to the line
    DROP = "DROP"    # blocked
    SKIP = "SKIP"    # not applicable / no data (e.g. order book in a backtest)
    WAIT = "WAIT"    # no setup yet (no breakout this bar) — normal, most bars are WAIT


@dataclass
class GateResult:
    name: str
    verdict: Verdict
    value: float | None = None
    threshold: float | None = None
    op: str = ""          # "min" / "max" / ""
    owner: str = ""       # which agent owns this gate
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "verdict": self.verdict.value, "value": self.value,
            "threshold": self.threshold, "op": self.op, "owner": self.owner, "note": self.note,
        }


@dataclass
class Position:
    side: Side
    qty: float                 # BTC
    entry_price: float
    entry_ts: int
    stop: float
    best_close: float          # most favourable close since entry (for the trailing stop)
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    take_profit: float | None = None
    setup: str = ""            # which SCAN setup opened it ("breakout", "failed_breakout", ...)

    def notional(self, price: float) -> float:
        return self.qty * price

    def unrealized(self, price: float) -> float:
        return self.side.sign * self.qty * (price - self.entry_price)


@dataclass
class Trade:
    side: Side
    qty: float
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    reason: str
    gross_pnl: float
    fees: float
    funding: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding

    def as_dict(self) -> dict:
        return {
            "side": self.side.value, "qty": self.qty, "entry_ts": self.entry_ts,
            "entry_price": self.entry_price, "exit_ts": self.exit_ts, "exit_price": self.exit_price,
            "reason": self.reason, "gross_pnl": self.gross_pnl, "fees": self.fees,
            "funding": self.funding, "net_pnl": self.net_pnl,
        }


@dataclass
class Order:
    """An intent queued by the desk; the broker decides how and when it fills."""
    action: str                # "open" or "close"
    side: Side                 # side of the position being opened/closed
    qty: float
    reason: str
    stop: float | None = None  # protective stop to attach to an opening order
    meta: dict = field(default_factory=dict)
