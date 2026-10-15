"""CoinbaseExchange: Coinbase's US futures account (Coinbase Financial Markets) shaped like the ccxt calls LiveBroker
already makes, so LiveBroker (desk/brokers/live.py) drives it with the code it has. BTC only, on one product:
BIP-20DEC30-CDE, the nano BTC perp. It never rolls to another product.

Every call goes through CoinbaseREST (coinbase_rest.py): reads to the real account, orders only through a client
coinbase_live.arm() unlocked. Units: LiveBroker's quantities are BTC; Coinbase's sizes are contracts of 0.01 BTC;
this adapter speaks contracts to LiveBroker (market contractSize 0.01) and to Coinbase.

What this adapter adds because Coinbase isn't Hyperliquid:
  * The product is the desk's alone (LIVE starts only on a flat account with no orders, coinbase_live.arm). An open
    order on BIP-20DEC30-CDE is the desk's when it carries a desk client_order_id (a version-5 UUID,
    coinbase_orders.is_desk_cid) and was placed through the Advanced Trade API: as protection and for cancelling. Any
    other order (one placed in the Coinbase app or by hand, on any product) and any position on another product is
    someone else's: never touched, it blocks entries (entry_block) until it's gone, and on BIP its closing contracts
    count against the position in the guard below. Coinbase lists only RETAIL_ADVANCED orders unless asked
    (order_placement_source, "By default, returns RETAIL_ADVANCED placement source"): the open orders are read for
    RETAIL_SIMPLE (the app) too, and for any source the desk's own orders turn out to carry.
  * No reduce-only on Coinbase. Before every stop or close, the position and the open orders are read again, in the
    same step; a stop that would make the resting closing-side contracts more than the position holds is refused here
    (it would open a position if it fired), and so is a close bigger than the position, and an entry while a position or
    a resting order exists.
  * The account's value is total USD (spot + futures) + open P&L + P&L realised today but not settled yet + money in
    transit between the two accounts, cross-checked against available margin + holds + transit
    (coinbase_orders.parse_balance). When they disagree the balance is still read (so the stops are still managed)
    but entries are blocked; money that can't be margin yet (an ACH hold) is left out of what entries are sized on
    (usable).
  * Deposits and withdrawals: the v2 USD wallet's transactions (coinbase_orders.parse_flow), with stable ids.
  * A changed product (contract size, order sizes, venue, type, no funding rate) refuses to start, except with a
    position or an order resting: then entries are blocked and the stop and exits are still managed (the product is
    read leniently so the run goes on).
  * The session: open only when is_session_open says so and session_state is FCM_TRADING_SESSION_STATE_OPEN (the
    spec's FcmTradingSessionState also has PRE_OPEN, PRE_OPEN_NO_CANCEL, CLOSE, HALTED, UNDEFINED). HALTED: nothing
    is sent. PRE_OPEN_NO_CANCEL: no cancel.
  * A cancel is a request (pending_cancel, "not yet completed"): a cancelled order is gone only once Coinbase reads it
    CANCELLED (or CANCEL_QUEUED while closed); one whose cancel is in flight still rests and could still fill.
"""

from __future__ import annotations

import contextlib
import copy
import math

from ..feeds.coinbase import CoinbaseFeed, FeedError
from ..thresholds import COINBASE
from . import coinbase_orders as co
from .coinbase_rest import (OTHER_SOURCES, CoinbaseDataError, CoinbaseError, CoinbaseRejected, CoinbaseUnavailable,
                            CoinbaseUncertain)
from .live import LiveSetupError

V3 = "/api/v3/brokerage"
READBACK_TRIES = 5
READBACK_WAIT_S = 1.0
CANCEL_READS = 10                       # a cancel is read back up to this many times (1 s apart) until it's done
FILL_PAGES = 10
LEDGER_WINDOW_MS = 14 * 86400_000       # deposits and withdrawals are read back this far
PRICE_FRESH_MS = 60_000                 # a price older than this (a cached answer, a quiet book) isn't acted on
BALANCE_TOL_USD = 1.0                   # the balance cross-check: the larger of $1...
BALANCE_TOL_FRACTION = 0.005            # ...and 0.5%
# Coinbase's refusals of a stop-limit (NewOrderFailureReason / PreviewFailureReason in the spec;
# new_order_failure_reason is the one to read: error and preview_failure_reason are marked Deprecated in
# NewOrderErrorResponse). A band: the limit is too far from the market, so a tighter one is tried. Through: the price
# is already through the trigger.
BAND_CODES = ("LIMIT_PRICE_TOO_FAR_FROM_MARKET", "PREVIEW_LIMIT_PRICE_TOO_FAR_FROM_MARKET",
              "LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_BUY", "LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_SELL",
              "PREVIEW_LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_BUY",
              "PREVIEW_LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_SELL",
              "STOP_TRIGGER_PRICE_OUT_OF_BOUNDS", "PREVIEW_STOP_TRIGGER_PRICE_OUT_OF_BOUNDS",
              "PREVIEW_BREACHED_PRICE_LIMIT", "INVALID_LIMIT_PRICE", "PREVIEW_INVALID_LIMIT_PRICE")
THROUGH_CODES = ("PREVIEW_STOP_PRICE_ABOVE_LAST_TRADE_PRICE", "PREVIEW_STOP_PRICE_BELOW_LAST_TRADE_PRICE",
                 "STOP_ALREADY_TRIGGERED", "PREVIEW_STOP_TRIGGERED")
# Refusals that mean Coinbase isn't taking this order now (its session), not that the order is wrong
SESSION_CODES = ("INVALID_FCM_TRADING_SESSION", "PREVIEW_INVALID_FCM_TRADING_SESSION",
                 "FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE", "PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE",
                 "FUTURES_AFTER_HOUR_INVALID_TIME_IN_FORCE", "PREVIEW_FUTURES_AFTER_HOUR_INVALID_TIME_IN_FORCE")
# Cancel refusals (CancelOrderFailureReason) cancel_order acts on by name; every other one is CancelFailed
CANCEL_CODES = ("ORDER_IS_FULLY_FILLED", "INVALID_CANCEL_FCM_TRADING_SESSION", "UNKNOWN_CANCEL_ORDER",
                "DUPLICATE_CANCEL_REQUEST")
SESSION_OPEN_STATE = "FCM_TRADING_SESSION_STATE_OPEN"
SESSION_HALTED = "FCM_TRADING_SESSION_STATE_HALTED"
SESSION_NO_CANCEL = "FCM_TRADING_SESSION_STATE_PRE_OPEN_NO_CANCEL"


class OrderRefused(CoinbaseError):
    """Refused here, before anything was sent: the order could open or grow a position by mistake."""


class StopThroughMarket(CoinbaseError):
    """The price is already through the stop's trigger: the stop is hit, close at market."""


class StaleClientOrderId(CoinbaseError):
    """The client_order_id was already used, by an order that is gone or isn't this one: take the next id."""


class StopAlreadyFilled(CoinbaseError):
    """A cancel found the stop already filled: the position is (partly) closed, sync and book it."""


class SessionClosed(CoinbaseError):
    """Coinbase isn't taking this now (its market is closed, or the last 30 s before a reopen)."""
    transient = True


class CancelFailed(CoinbaseError):
    pass


class CancelPending(CancelFailed):
    """Coinbase took the cancel but the order still reads as resting (its cancel in flight) after CANCEL_READS reads:
    it's going, not gone, and could still fill. Neither protection nor safe to place a second stop beside."""


class CancelUnknown(CancelFailed):
    """The cancel got no clear answer and the order can't be read back: whether it still rests isn't known."""


class _AuthedFeed(CoinbaseFeed):
    """CoinbaseFeed's product parse and checks, on the authenticated product read (the public one is cached by a CDN,
    hours at a time on some answers)."""

    def __init__(self, rest, venue=COINBASE):
        super().__init__(venue)
        self.rest = rest
        self.raw: dict[str, dict] = {}
        self._override: dict[str, dict] = {}

    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        if path in self._override:
            return self._override[path]
        try:
            out = self.rest.get(f"{V3}/{path}", params)
        except CoinbaseRejected as exc:
            # While Coinbase is closed it may have no book at all (404 "no pricebook found", as the public feed
            # sees): an empty book, which context() takes as expected then and refuses while the session reads open.
            if path != "product_book" or exc.status != 404:
                raise
            out = {"pricebook": {}}
        self.raw[path] = out
        return out

    def product_lenient(self) -> dict:
        """The product parsed with the desk's contract size and order sizes in place of the ones its answer now gives (a
        product that changed while a position or an order rests on it: managed in whole contracts, never traded on).
        Raises if it isn't a futures product at all."""
        path = f"products/{self.product_id}"
        d = copy.deepcopy(self.raw.get(path) or self._get(path))
        if not isinstance(d.get("future_product_details"), dict):
            raise FeedError(f"Coinbase's answer for {self.product_id} isn't a futures product")
        v = self.venue
        d["future_product_details"]["contract_size"] = f"{v.contract_size:g}"
        d.update({"base_increment": "1", "base_min_size": str(v.min_contracts), "base_max_size": str(v.max_contracts)})
        self._override[path] = d
        try:
            return self.product()
        finally:
            self._override.pop(path, None)
            self.raw[path] = d


def _ccxt_side(side: str) -> str:
    s = str(side).lower()
    if s not in ("buy", "sell"):
        raise ValueError(f"side {side!r}")
    return s


class CoinbaseExchange:
    id = "coinbase"
    is_coinbase_adapter = True
    reduce_only_stops = False       # Coinbase documents no reduce-only: LiveBroker moves stops cancel-first
    ledger_ids_stable = True        # each v2 transaction keeps its id through its status changes
    precisionMode = 4               # ccxt TICK_SIZE: precision["amount"] is the size step itself
    walletAddress = ""
    has = {"fetchLedger": True, "fetchMyTrades": True, "fetchOrder": True, "fetchOpenOrders": True,
           "fetchFundingHistory": False}    # Coinbase has no funding history: trades carry the desk's estimate

    def __init__(self, rest, *, venue=COINBASE, coin: str = "BTC", on_alert=None, memo: dict | None = None,
                 sleep=None):
        if coin.upper() != "BTC":
            raise LiveSetupError("Coinbase's futures desk trades BTC only")
        self.rest = rest
        self.venue = venue
        self.product = venue.product
        self.coin = coin.upper()
        self.on_alert = on_alert
        self.memo = memo if memo is not None else {}
        self.memo.setdefault("alerted", [])
        self.memo.setdefault("sources", [])     # placement sources the desk's own orders turned out to carry
        self._tell_sources()
        self._quick = False                     # reads without retries (a protective step that has a fallback)
        self.sleep = sleep or rest.sleep
        self.options: dict = {}
        self.markets: dict | None = None
        self.details: dict | None = None
        self.feed = _AuthedFeed(rest, venue)
        self.foreign: dict = {"positions": [], "orders": []}
        self.balance: dict | None = None
        self.usable: float | None = None
        self.balance_block: str | None = None
        self.product_block: str | None = None
        self.usd_account: str | None = None
        self.usd_hold: float | None = None      # the USD account's hold (v3 accounts): money not usable as margin yet
        self.usdc: float | None = None
        self.permissions: dict | None = None
        self.fees: dict | None = None
        self.last_readback: dict | None = None
        self.watched: tuple = (None, [])        # what watchdog() last read: (the position, the desk's orders)

    # ---- alerts
    def _alert_once(self, key: str, text: str) -> None:
        if key in self.memo["alerted"]:
            return
        self.memo["alerted"] = (self.memo["alerted"] + [key])[-500:]
        if self.on_alert is not None:
            self.on_alert(text)

    # ---- time
    def time_left(self) -> float:
        return self.rest.time_left()

    def fits(self, seconds: float) -> bool:
        return self.rest.fits(seconds)

    def protecting(self):
        """A protective sequence may run past the run's deadline (CoinbaseREST.protecting)."""
        p = getattr(self.rest, "protecting", None)
        return p() if p is not None else contextlib.nullcontext()

    def milliseconds(self) -> int:
        return int(self.rest.now_s() * 1000)

    # ---- the market
    def _check_symbol(self, symbol) -> None:
        if symbol not in (None, self.product):
            raise OrderRefused(f"{symbol} isn't {self.product}: the desk trades that one product only")

    def _market(self) -> dict:
        v = self.venue
        return {"id": self.product, "symbol": self.product, "base": self.coin, "quote": "USD", "settle": "USD",
                "type": "swap", "swap": True, "contract": True, "linear": True, "active": True,
                "contractSize": v.contract_size, "precision": {"amount": 1.0, "price": float(co.PRICE_STEP)},
                "limits": {"amount": {"min": float(v.min_contracts), "max": float(v.max_contracts)},
                           "cost": {"min": 0.0, "max": None}},
                "info": {"name": self.product}}

    def load_markets(self, reload: bool = False) -> dict:
        """The product, read from Coinbase (authenticated) and checked: the contract the desk sizes for (0.01 BTC,
        whole contracts 1 to 5000, $5 ticks), a FUTURE on the FCM venue, with a funding rate (that's what tells the
        perp from the dated BIT futures; both report EXPIRING). A change refuses to start, unless a position or an
        order already rests on it: then entries are blocked and the stop and exits are still managed."""
        if self.markets is not None and not reload:
            return self.markets
        try:
            self.details = self.feed.product()
            raw = self.feed.raw.get(f"products/{self.product}") or {}
            fut = raw.get("future_product_details") or {}
            changed = [f"{k} is {raw.get(k)!r}" for k, want in (("product_type", "FUTURE"), ("product_venue", "FCM"))
                       if raw.get(k) != want]
            if not str(fut.get("funding_rate") or "").strip() and not self.details.get("funding_rate"):
                changed.append("it has no funding rate (not the perp)")
            inc = self.details.get("price_increment")
            if inc is not None and inc != co.PRICE_STEP:
                changed.append(f"price_increment is {inc:g}")
            if changed:
                raise FeedError("; ".join(changed))
        except FeedError as exc:
            why = f"{self.product} isn't the contract the desk is set up for any more ({exc}): not trading on it"
            if not self._holding_anything():
                raise LiveSetupError(why) from None
            self.product_block = why
            self._alert_once(f"product:{exc}", f"{why}. A position or an order rests on it, so the stop and exits are "
                                               f"still managed; no new entries")
            if self.details is None:
                try:
                    self.details = self.feed.product_lenient()
                except FeedError:
                    pass        # not even a futures product: the run can't read its candles, the safety pass still ran
        self.markets = {self.product: self._market()}
        return self.markets

    def _holding_anything(self) -> bool:
        try:
            pos, orders = self._fresh()
        except CoinbaseError:
            return True         # can't tell: assume something rests, keep managing it
        return pos is not None or bool(orders)

    def session_state(self) -> str | None:
        return None if self.details is None else self.details.get("session_state")

    def session_open(self) -> bool | None:
        """Whether Coinbase takes orders on the product now, as its product answer this run said (None: it didn't).
        Open only when is_session_open says so and session_state, when given, is OPEN: HALTED, PRE_OPEN,
        PRE_OPEN_NO_CANCEL, CLOSE and UNDEFINED read as closed, whatever is_session_open says."""
        if self.details is None:
            return None
        st = self.details.get("session_state")
        if st and st != SESSION_OPEN_STATE:
            return False
        return self.details.get("session_open")

    def halted(self) -> bool:
        """Coinbase's session reads HALTED: nothing is sent, not even a stop."""
        return self.session_state() == SESSION_HALTED

    def cancels_refused(self) -> bool:
        """The session takes no cancels now (PRE_OPEN_NO_CANCEL, HALTED)."""
        return self.session_state() in (SESSION_NO_CANCEL, SESSION_HALTED)

    def amount_to_precision(self, symbol, amount) -> str:
        n = math.floor(float(amount) + 1e-9)
        if n <= 0:
            raise ValueError(f"{amount} contracts rounds down to none")
        return str(int(n))

    def price_to_precision(self, symbol, price) -> str:
        return str(co.round_price(float(price)))

    # ---- account reads
    def fetch_balance(self, params=None) -> dict:
        """{"total": {"USD": the account's value}} (see parse_balance). Sets balance_block when it doesn't add up and
        usable (what entries are sized on) when part of it can't be margin."""
        b = co.parse_balance(self.rest.get(f"{V3}/cfm/balance_summary"))
        self.balance = b
        tol = max(BALANCE_TOL_USD, BALANCE_TOL_FRACTION * max(abs(b["equity"]), abs(b["cross"])))
        self.balance_block, self.usable = None, b["equity"]
        if abs(b["parts_diff"]) > 0.01:
            self.balance_block = ("Coinbase's balance doesn't add up (spot + futures isn't the total): no entries on a "
                                  "number that doesn't")
        elif b["cross"] - b["equity"] > tol:
            self.balance_block = ("Coinbase's balance doesn't add up (available margin + holds is more than spot + "
                                  "futures + open and unsettled P&L): no entries on a number that doesn't")
        elif b["equity"] - b["cross"] > tol:
            # More in the balance than available margin + holds: money that can't be margin yet (a deposit's ACH
            # hold) only when the USD account says it holds that much; otherwise the formula counts something twice
            # (today's realised P&L, say), and nothing is sized on it.
            gap = b["equity"] - b["cross"]
            hold = self._usd_hold()
            if hold is not None and abs(hold - gap) <= tol:
                self.usable = b["cross"]
            else:
                self.balance_block = ("Coinbase's balance doesn't add up (spot + futures + open and unsettled P&L is "
                                      "more than available margin + holds, and no hold on the USD account explains "
                                      "it): no entries on a number that doesn't")
        return {"info": b, "total": {"USD": b["equity"]}, "free": {"USD": b["available_margin"]},
                "USD": {"total": b["equity"], "free": b["available_margin"]}}

    def _positions(self) -> list[dict]:
        rows = self._get(f"{V3}/cfm/positions").get("positions")
        if not isinstance(rows, list):
            raise CoinbaseDataError("Coinbase's positions answer has no positions list")
        return [p for p in (co.parse_position(r) for r in rows) if p is not None]

    def fetch_positions(self, symbols=None, params=None) -> list[dict]:
        out, foreign = [], []
        for p in self._positions():
            if p["product_id"] == self.product:
                out.append({"symbol": self.product, "side": p["side"], "contracts": float(p["contracts"]),
                            "contractSize": self.venue.contract_size, "entryPrice": p["entry"],
                            "unrealizedPnl": p["upnl"], "info": p})
            else:
                foreign.append({"product_id": p["product_id"], "side": p["side"], "contracts": p["contracts"]})
        self.foreign["positions"] = foreign
        return out

    def _get(self, path: str, params=None):
        return self.rest.get(path, params, retries=0) if self._quick else self.rest.get(path, params)

    def _pages(self, path: str, params, items: str, max_pages: int = 10) -> list:
        if self._quick:
            return self.rest.get_pages(path, params, items, max_pages, retries=0)
        return self.rest.get_pages(path, params, items, max_pages)

    @contextlib.contextmanager
    def quick(self):
        """Reads with no retries: a protective step that has a fallback for a read that fails."""
        prev, self._quick = self._quick, True
        try:
            yield
        finally:
            self._quick = prev

    def _sources(self) -> list[str | None]:
        """The placement sources to list: the default (RETAIL_ADVANCED, the API's), the app's, and any other the desk's
        own orders turned out to carry."""
        out: list[str | None] = [None, *OTHER_SOURCES]
        out += [x for x in self.memo.get("sources") or [] if x not in out and x != co.DESK_SOURCE]
        return out

    def _tell_sources(self) -> None:
        """The client's protect-only check reads the open orders under the same sources as this adapter."""
        if isinstance(getattr(self.rest, "learned_sources", None), list):
            self.rest.learned_sources = [x for x in self._sources() if x is not None and x not in OTHER_SOURCES]

    def close_worst_s(self, cancels: int) -> float:
        """The worst case of a close that cancels its stop first, in seconds (CoinbaseREST.worst_s): each cancel and
        two reads of it (a protect-only client reads the order first too), the position and orders read again, a
        protect-only client's own reads, close_position and two reads of it, the position read again after it; and, if
        the close is refused, the same again to put the stop back. None of it may stop half way, so none of it starts
        without the time for all of it."""
        fresh = 1 + 2 * len(self._sources())
        protect = getattr(self.rest, "protect_only", False)
        check = fresh if protect else 0
        reqs = (4 if protect else 3) * cancels + fresh + check + 3 + fresh + (fresh + check + 3)
        worst = getattr(self.rest, "worst_s", None)
        return worst(reqs, (cancels + 3) * READBACK_WAIT_S) if worst is not None else 45.0

    def _open_orders_all(self) -> list[dict]:
        """Every open order on every product, from every placement source read (_sources): OPEN (which can't be combined
        with other statuses and leaves out PENDING and QUEUED orders that can still be cancelled), then PENDING, QUEUED,
        CANCEL_QUEUED and EDIT_QUEUED."""
        rows = []
        for src in self._sources():
            extra = {} if src is None else {"order_placement_source": src}
            rows += self._pages(f"{V3}/orders/historical/batch", {"order_status": "OPEN", "limit": 100, **extra},
                                "orders")
            rows += self._pages(f"{V3}/orders/historical/batch",
                                {"order_status": ["PENDING", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED"], "limit": 100,
                                 **extra}, "orders")
        out, seen = [], set()
        for r in rows:
            o = co.parse_order(r)
            if o["id"] in seen or o["status"] not in ("open", "canceling"):
                continue
            seen.add(o["id"])
            out.append(o)
        return out

    def is_desks(self, o: dict) -> bool:
        """Whether an open order is the desk's: on its product, with a desk client_order_id, placed through the API (or
        from a source the desk's own orders carry)."""
        src = o.get("placement")
        return o.get("symbol") == self.product and co.is_desk_cid(o.get("clientOrderId")) and \
            (src in (None, "", co.DESK_SOURCE) or src in (self.memo.get("sources") or []))

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None) -> list[dict]:
        """The desk's open orders on the product; every other open order goes to foreign (never touched)."""
        mine, foreign = [], []
        for o in self._open_orders_all():
            (mine if self.is_desks(o) else foreign).append(o)
        self.foreign["orders"] = [{"product_id": o["symbol"], "id": o["id"], "side": o["side"],
                                   "contracts": 0.0 if o.get("cancel_queued") else
                                   (o["remaining"] if o.get("remaining") is not None else o.get("amount")),
                                   "stop": o.get("triggerPrice"), "status": o["status"]} for o in foreign]
        return mine

    def foreign_on_product(self, side: str | None = None) -> list[dict]:
        """Orders resting on the desk's product that aren't the desk's (on `side`, if given), as of the last read."""
        return [o for o in self.foreign["orders"] if o["product_id"] == self.product and (side is None or
                                                                                         o["side"] == side)]

    def _fresh(self) -> tuple[dict | None, list[dict]]:
        """The product's position (or None) and its open orders, read now."""
        pos = next((p for p in self.fetch_positions([self.product])), None)
        return pos, self.fetch_open_orders(self.product)

    def _fresh_or(self, known: dict | None) -> tuple[dict | None, list[dict]]:
        """_fresh(), read once with no retries when `known` is given; if that read fails, `known`: what the caller
        read itself just before, after confirming the cancel of the old stop (the position, the desk's orders)."""
        if known is None:
            return self._fresh()
        try:
            with self.quick():
                return self._fresh()
        except CoinbaseUnavailable:
            pos = None if not known.get("side") else {"side": known["side"], "contracts": int(known["contracts"])}
            return pos, list(known.get("orders") or [])

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None) -> list[dict]:
        q = {"product_ids": self.product, "limit": 100}
        if since is not None:
            q["start_sequence_timestamp"] = co.iso(int(since))
        out = []
        for r in self.rest.get_pages(f"{V3}/orders/historical/fills", q, "fills", max_pages=FILL_PAGES):
            f = co.parse_fill(r)
            if f is None:
                self._alert_once(f"fill:{r.get('entry_id') or r.get('trade_id')}",
                                 f"Coinbase lists a {r.get('trade_type')} row (an adjusted fill) for order "
                                 f"{r.get('order_id')}: not booked as a fill. Check the trade in Coinbase; the balance "
                                 f"check catches what it changed")
                continue
            out.append(f)
        return sorted(out, key=lambda t: t["timestamp"] or 0)

    def fetch_order(self, id, symbol=None, params=None) -> dict:
        return co.parse_order(self.rest.get(f"{V3}/orders/historical/{id}").get("order") or {})

    def find_order(self, client_order_id: str, since_ms: int) -> dict | None:
        """The product's order with this client_order_id, placed since `since_ms` (any status), or None."""
        for src in self._sources():
            q = {"product_ids": self.product, "start_date": co.iso(int(since_ms)), "limit": 100,
                 **({} if src is None else {"order_placement_source": src})}
            for r in self.rest.get_pages(f"{V3}/orders/historical/batch", q, "orders"):
                if r.get("client_order_id") == client_order_id:
                    return co.parse_order(r)
        for o in self.fetch_open_orders(self.product):
            if o.get("clientOrderId") == client_order_id:
                return o
        return None

    def opened_by(self, side: str, since_ms: int) -> dict | None:
        """The order whose fills opened the position now held (`side` 'long' or 'short'), read from the fills since
        `since_ms`: the newest opening fill's order. None if no fill says."""
        opening = "buy" if side == "long" else "sell"
        fills = [t for t in self.fetch_my_trades(self.product, since_ms) if t["side"] == opening]
        if not fills:
            return None
        return self.fetch_order(fills[-1]["order"])

    # ---- money in and out (the v2 USD wallet)
    def wallets(self) -> dict:
        """The key's portfolio's USD account (its id, its hold) and the USDC in that portfolio (Coinbase can pull USDC
        into futures margin). Read from v3 GET /accounts, whose rows say their portfolio (retail_portfolio_id) and
        platform: the spot USD account (ACCOUNT_PLATFORM_CONSUMER) in the key's portfolio
        (key_permissions.portfolio_uuid). v2 /accounts (whose rows say no portfolio) must list that same account: its
        transactions are the deposits and withdrawals. Anything else raises: the flows aren't read from a wallet
        that may be another portfolio's."""
        if self.permissions is None:
            self.key_permissions()
        mine = self.permissions.get("portfolio_uuid")
        if not mine:
            raise CoinbaseDataError("the key's permissions don't name its portfolio: not guessing which USD wallet is "
                                    "the account's")
        rows = self.rest.get_pages(f"{V3}/accounts", {"limit": 250}, "accounts")
        usd, usdc, hold = None, 0.0, 0.0
        for a in rows:
            if a.get("retail_portfolio_id") != mine or a.get("deleted_at"):
                continue
            plat = a.get("platform")
            if a.get("currency") == "USD" and plat in (None, "", "ACCOUNT_PLATFORM_CONSUMER"):
                if usd is not None:
                    raise CoinbaseDataError("the key's portfolio lists two spot USD accounts: not guessing which one")
                usd = a
            elif a.get("currency") == "USDC":
                usdc += (co.amount(a.get("available_balance"), "USDC balance", required=False) or 0.0) + \
                    (co.amount(a.get("hold"), "USDC hold", required=False) or 0.0)
        if usd is None or not usd.get("uuid"):
            raise CoinbaseDataError("Coinbase lists no spot USD account in this key's portfolio")
        uid = str(usd["uuid"])
        v2 = self.rest.get_v2_pages("/v2/accounts", {"limit": 100})
        if not any(str(a.get("id")) == uid for a in v2):
            raise CoinbaseDataError("the portfolio's USD account isn't among the Coinbase App's (v2) accounts: its "
                                    "deposits and withdrawals can't be read")
        hold = co.amount(usd.get("hold"), "USD hold", required=False) or 0.0
        self.usd_account, self.usdc, self.usd_hold = uid, usdc, hold
        return {"usd_account": self.usd_account, "usdc": usdc, "usd_hold": hold}

    def _usd_hold(self) -> float | None:
        """The USD account's hold, read now (None if it can't be)."""
        try:
            self.wallets()
        except CoinbaseError:
            return None
        return self.usd_hold

    def fetch_ledger(self, code=None, since=None, limit=None, params=None) -> list[dict]:
        """The USD wallet's transactions from LEDGER_WINDOW_MS before `since` (newest first from Coinbase, paged
        through next_uri), as {id: "coinbase-<id>", timestamp, status, info}. One call reads the whole window."""
        if self.usd_account is None:
            self.wallets()
        floor = (int(since) if since is not None else self.milliseconds()) - LEDGER_WINDOW_MS
        rows = self.rest.get_v2_pages(f"/v2/accounts/{self.usd_account}/transactions", {"limit": 100},
                                      stop=lambda r: (co.ms(r.get("created_at")) or 0) < floor)
        out = []
        for r in rows:
            ts = co.ms(r.get("created_at")) or 0
            if ts < floor or not r.get("id"):
                continue
            out.append({"id": f"coinbase-{r['id']}", "timestamp": ts, "status": co.flow_status(r), "info": r,
                        "type": r.get("type")})
        return sorted(out, key=lambda e: e["timestamp"])

    parse_flow = staticmethod(co.parse_flow)
    flow_status = staticmethod(co.flow_status)

    # ---- other reads (the SHADOW checklist, the arming checks)
    def key_permissions(self) -> dict:
        self.permissions = co.parse_permissions(self.rest.get(f"{V3}/key_permissions"))
        return self.permissions

    def fee_tier(self) -> dict:
        r = self.rest.get(f"{V3}/transaction_summary", {"product_type": "FUTURE"})
        tier = r.get("fee_tier") if isinstance(r.get("fee_tier"), dict) else {}
        self.fees = {"tier": tier.get("pricing_tier"), "taker": co.amount(tier.get("taker_fee_rate"), "taker",
                                                                           required=False),
                     "maker": co.amount(tier.get("maker_fee_rate"), "maker", required=False),
                     "total_fees": co.amount(r.get("total_fees"), "total_fees", required=False),
                     "margin_rate": co.amount((r.get("margin_rate") or {}).get("value")
                                              if isinstance(r.get("margin_rate"), dict) else r.get("margin_rate"),
                                              "margin_rate", required=False)}
        return self.fees

    def last_trade(self) -> tuple[float, int] | None:
        """The product's last trade (price, ms), if it's fresh (PRICE_FRESH_MS); None otherwise."""
        t = self.rest.get(f"{V3}/products/{self.product}/ticker", {"limit": 1})
        trades = t.get("trades") if isinstance(t.get("trades"), list) else []
        if not trades:
            return None
        px, ts = co.amount(trades[0].get("price"), "price", required=False), co.ms(trades[0].get("time"))
        if not px or ts is None or self.milliseconds() - ts > PRICE_FRESH_MS:
            return None
        return px, ts

    def best_bid_ask(self) -> tuple[float, float, int] | None:
        """(best bid, best ask, ms) now, if fresh (PRICE_FRESH_MS); None otherwise."""
        r = self.rest.get(f"{V3}/best_bid_ask", {"product_ids": self.product})
        books = [b for b in r.get("pricebooks") or [] if b.get("product_id") == self.product]
        if not books or not books[0].get("bids") or not books[0].get("asks"):
            return None
        b = books[0]
        bid, ask, ts = co.amount(b["bids"][0].get("price"), "bid"), co.amount(b["asks"][0].get("price"), "ask"), \
            co.ms(b.get("time"))
        if ts is None or self.milliseconds() - ts > PRICE_FRESH_MS or not 0 < bid < ask:
            return None
        return bid, ask, ts

    def price_at(self, ts: int) -> float | None:
        """The product's price at `ts` (ms), for a deposit or withdrawal read after it happened: the open of the
        minute (else five-minute, else hour) candle it falls in. While Coinbase is closed there are no candles: the
        close of the last one before it, up to 12 hours back. None if Coinbase has neither."""
        for gran, sec in (("ONE_MINUTE", 60), ("FIVE_MINUTE", 300), ("ONE_HOUR", 3600)):
            t0 = int(ts // 1000) - int(ts // 1000) % sec
            rows = self.rest.get(f"{V3}/products/{self.product}/candles",
                                 {"granularity": gran, "start": t0, "end": t0 + sec}).get("candles") or []
            for r in rows:
                if int(r["start"]) <= ts / 1000 < int(r["start"]) + sec:
                    return co.amount(r.get("open"), "open")
        end = int(ts // 1000)
        rows = self.rest.get(f"{V3}/products/{self.product}/candles",
                             {"granularity": "ONE_HOUR", "start": end - 12 * 3600, "end": end}).get("candles") or []
        before = [r for r in rows if int(r["start"]) + 3600 <= end]
        if before:
            return co.amount(max(before, key=lambda r: int(r["start"])).get("close"), "close")
        return None

    def entry_block(self) -> str | None:
        """Why no entry may go out now, or None."""
        if self.product_block:
            return self.product_block
        if self.balance_block:
            return self.balance_block
        f = self.foreign
        if f["positions"] or f["orders"]:
            what = [f"a {p['side']} {p['contracts']} on {p['product_id']}" for p in f["positions"]] + \
                   [f"order {o['id']} on {o['product_id']}" for o in f["orders"]]
            return (f"the account holds {', '.join(what)}, not the desk's: no entries while it does (the desk never "
                    f"touches it)")
        if self.session_open() is False:
            return "Coinbase's market is closed now"
        return None

    def watchdog(self) -> list[tuple[str, str]]:
        """Read only: what is wrong with the account's protection right now. Any run that can still read the account
        runs this, even one that refuses to act. A stop-limit that triggered and didn't fill, or whose limit the price
        is past, is no working stop ("gapped"). Stops covering less than the position holds leave the rest with none
        ("unprotected", like no stop at all: only a stop counts, never a limit). Closing orders resting for more
        contracts than the position holds (the desk's and anyone's: with no reduce-only, if they all fire they open
        the other way) are "overclosing". What it read is kept in `watched` (position, the desk's orders)."""
        pos, orders = self._fresh()
        self.watched = (pos, orders)
        out = []
        closing = None if pos is None else ("sell" if pos["side"] == "long" else "buy")
        if pos is None:
            for o in orders:
                out.append(("stray", f"order {o['id']} ({o['side']} {o['amount'] or '?'} contracts"
                                     + (f", stop {o['triggerPrice']:,.0f}" if o.get("triggerPrice") else "") +
                                     f") rests on {self.product} with no position: Coinbase has no reduce-only, so it "
                                     f"would open a position if it fired. Cancel it in Coinbase now"))
            for o in self.foreign_on_product():
                out.append(("stray", f"order {o['id']} ({o['side']}, not the desk's) rests on {self.product} with no "
                                     f"position: it would open one if it fired. Cancel it in Coinbase now"))
        else:
            stops = [o for o in orders if o["side"] == closing and o.get("triggerPrice") and o["status"] == "open"]
            over = sum(float(o["remaining"] if o.get("remaining") is not None else (o.get("amount") or 0))
                       for o in orders if o["side"] == closing and not o.get("cancel_queued")) + \
                sum(float(o.get("contracts") or 0) for o in self.foreign_on_product(closing))
            if over > pos["contracts"] + 1e-9:
                out.append(("overclosing", f"closing orders for {over:g} contracts rest on {self.product} against "
                                           f"the {pos['side']} {pos['contracts']:g} held: Coinbase has no reduce-only, "
                                           f"so if they all fired they would open a position the other way. Cancel the "
                                           f"extra in Coinbase now"))
            theirs = sum(float(o.get("contracts") or 0) for o in self.foreign_on_product(closing)
                         if o.get("stop") is not None)          # stops that aren't the desk's protect too
            if not stops and theirs <= 1e-9:
                out.append(("unprotected", f"the {pos['side']} {pos['contracts']:g} contracts on {self.product} have "
                                           f"no stop resting on Coinbase"))
            else:
                try:
                    last = self.last_trade()
                except CoinbaseError:
                    last = None
                sign = 1 if pos["side"] == "long" else -1
                gapped = False
                cover = theirs
                for o in stops:
                    hit = o.get("triggerStatus") == "STOP_TRIGGERED"
                    if hit or (last is not None and o.get("price") is not None and (last[0] - o["price"]) * sign < 0):
                        gapped = True
                        out.append(("gapped", f"the stop {o['id']} at {o['triggerPrice']:,.0f} on the {pos['side']} "
                                              f"{pos['contracts']:g} contracts on {self.product} "
                                              + ("triggered and didn't fill" if hit else
                                                 f"is past its limit {o['price']:,.0f}")
                                              + ": a stop-limit doesn't fill past its limit, so the position has no "
                                                "working stop. Close it in Coinbase now"))
                    elif not o.get("cancel_queued"):
                        cover += float(o["remaining"] if o.get("remaining") is not None else (o.get("amount") or 0))
                if not gapped and cover + 1e-9 < pos["contracts"]:
                    # only part of the position has a stop (it grew in the app, say): the rest has none
                    out.append(("unprotected", f"the stops resting on the {pos['side']} {pos['contracts']:g} contracts "
                                               f"on {self.product} cover only {cover:g} of them: "
                                               f"{pos['contracts'] - cover:g} have no stop on Coinbase"))
        for p in self.foreign["positions"]:
            out.append(("foreign", f"a {p['side']} {p['contracts']} on {p['product_id']} (not the desk's)"))
        for o in self.foreign["orders"]:
            if pos is None and o["product_id"] == self.product:
                continue        # said above, as a stray
            out.append(("foreign", f"order {o['id']} on {o['product_id']} (not the desk's)"))
        return out

    # ---- orders
    def client_order_id(self, kind: str, *parts) -> str:
        return co.client_order_id(kind, self.coin, *parts)

    def _readback(self, oid: str, done) -> dict:
        """Read the order back until `done(order)` or READBACK_TRIES reads."""
        o = None
        for k in range(READBACK_TRIES):
            if k:
                if not self.fits(READBACK_WAIT_S + 1):
                    break
                self.sleep(READBACK_WAIT_S)
            try:
                o = self.fetch_order(oid)
            except CoinbaseRejected:
                o = None        # not visible yet
                continue
            if done(o):
                break
        if o is None:
            return {"id": oid, "status": None, "filled": None, "average": None, "fee": None}
        self.last_readback = o
        src = o.get("placement")
        if src and src != co.DESK_SOURCE and src not in self.memo["sources"]:
            self.memo["sources"] = self.memo["sources"] + [src]
            self._tell_sources()
            self._alert_once(f"source:{src}", f"Coinbase lists the desk's own orders under placement source {src}, "
                                              f"not {co.DESK_SOURCE}: the open orders are read under it too from now")
        return o

    def _post(self, path: str, body: dict) -> dict:
        try:
            return self.rest.post(path, body)
        except CoinbaseRejected as exc:
            if exc.has(*SESSION_CODES):
                raise SessionClosed(f"{exc}: Coinbase isn't taking this order type now") from None
            raise

    @staticmethod
    def _order_id(res: dict) -> str:
        sr = res.get("success_response") if isinstance(res.get("success_response"), dict) else {}
        oid = sr.get("order_id") or res.get("order_id")
        if not oid:
            raise CoinbaseDataError("Coinbase took the order but its answer has no order id")
        return str(oid)

    def create_order(self, symbol, type, side, amount, price=None, params=None) -> dict:
        params = dict(params or {})
        self._check_symbol(symbol)
        side = _ccxt_side(side)
        cid = params.get("clientOrderId")
        if not cid:
            raise OrderRefused("an order needs its clientOrderId (that's what makes a retry safe)")
        n = float(amount)
        if abs(n - round(n)) > 1e-9 or n < 1:
            raise OrderRefused(f"{amount} isn't a whole number of contracts")
        n = int(round(n))
        if self.halted():
            raise OrderRefused("not sent: Coinbase's session reads HALTED")
        known = params.get("known")
        if params.get("stopLossPrice") is not None:
            return self._stop(cid, side, n, float(params["stopLossPrice"]), known)
        if params.get("reduceOnly"):
            return self._close(cid, side, n, known)
        return self._entry(cid, side, n)

    def _entry(self, cid: str, side: str, n: int) -> dict:
        pos, orders = self._fresh()
        why = self.entry_block()
        if why:
            raise OrderRefused(f"entry not sent: {why}")
        if pos is not None or orders:
            raise OrderRefused("entry not sent: a position or an order already rests on the product (Coinbase has no "
                               "reduce-only, so the desk never stacks orders)")
        res = self._post(f"{V3}/orders", co.market(cid, side.upper(), n))
        oid = self._order_id(res)
        o = self._readback(oid, lambda o: o["status"] in ("closed", "canceled"))
        return {**o, "id": oid, "clientOrderId": cid}

    def _stop(self, cid: str, side: str, n: int, trigger: float, known: dict | None = None) -> dict:
        pos, orders = self._fresh_or(known)
        if pos is None:
            raise OrderRefused("stop not sent: the account holds no position on the product, and a stop with no "
                               "position would open one if it fired")
        closing = "sell" if pos["side"] == "long" else "buy"
        if side != closing:
            raise OrderRefused(f"stop not sent: a {side} stop doesn't close a {pos['side']} position")
        resting = sum(float(o["remaining"] if o.get("remaining") is not None else (o.get("amount") or 0))
                      for o in orders if o["side"] == closing and not o.get("cancel_queued"))
        others = sum(float(o.get("contracts") or 0) for o in self.foreign_on_product(closing))
        if resting + others + n > pos["contracts"] + 1e-9:
            raise OrderRefused(f"stop not sent: {resting + others:g} closing contracts already rest against the "
                               f"{pos['contracts']} held" + (f" ({others:g} of them in orders that aren't the desk's)"
                                                             if others else "")
                               + f"; {n} more would open a position if they all fired")
        last = None
        for k, offset in enumerate(co.STOP_LIMIT_FALLBACKS):
            body = co.stop_limit(cid if k == 0 else co.client_order_id("stop-offset", self.coin, cid, offset),
                                 closing.upper(), n, trigger, offset)
            try:
                res = self._post(f"{V3}/orders", body)
            except CoinbaseRejected as exc:
                if exc.has(*THROUGH_CODES):
                    raise StopThroughMarket(f"the price is already through the stop at {co.round_price(trigger):,} "
                                            f"({', '.join(exc.codes)})") from None
                if exc.has(*BAND_CODES) and k + 1 < len(co.STOP_LIMIT_FALLBACKS):
                    last = exc
                    self._alert_once(f"band:{offset}", f"Coinbase refused the stop-limit with its limit {offset:.0%} "
                                                       f"past the trigger ({', '.join(exc.codes)}); trying "
                                                       f"{co.STOP_LIMIT_FALLBACKS[k + 1]:.0%}")
                    continue
                raise
            oid = self._order_id(res)
            o = self._readback(oid, lambda o: o["status"] in ("open", "closed", "canceled"))
            sent = body["order_configuration"]["stop_limit_stop_limit_gtc"]
            if o.get("status") == "canceled" and not o.get("filled"):
                raise StaleClientOrderId(f"stop order {oid} came back {o.get('coinbase_status')}: its client id was "
                                         f"used before")
            if o.get("triggerPrice") is not None and (abs(o["triggerPrice"] - float(sent["stop_price"])) > 1e-6 or
                                                      abs((o.get("amount") or 0) - n) > 1e-9 or o.get("side") != closing):
                raise StaleClientOrderId(f"order {oid} isn't the stop just sent (its client id was used before)")
            return {**o, "id": oid, "clientOrderId": body["client_order_id"]}
        raise last      # pragma: no cover (the loop either returns or raises)

    def _close(self, cid: str, side: str, n: int, known: dict | None = None) -> dict:
        pos, _ = self._fresh_or(known)
        if pos is None:
            raise OrderRefused("close not sent: the account holds no position on the product")
        closing = "sell" if pos["side"] == "long" else "buy"
        if side != closing or n > pos["contracts"]:
            raise OrderRefused(f"close not sent: {side} {n} doesn't close the {pos['side']} {pos['contracts']} held "
                               f"(it would open the other way)")
        res = self._post(f"{V3}/orders/close_position", co.close_position(cid, n))
        oid = self._order_id(res)
        o = self._readback(oid, lambda o: o["status"] in ("closed", "canceled"))
        return {**o, "id": oid, "clientOrderId": cid}

    @staticmethod
    def _cancel_done(o: dict) -> bool:
        """A cancel is done when the order reads cancelled or filled, or its cancel is queued for the reopen
        (CANCEL_QUEUED). An open order whose cancel is in flight (pending_cancel) isn't done: it can still fill."""
        return o.get("status") in ("canceled", "closed") or str(o.get("coinbase_status")) == "CANCEL_QUEUED"

    def _cancel_readback(self, oid: str) -> dict:
        o = None
        for k in range(CANCEL_READS):
            if k:
                if not self.fits(READBACK_WAIT_S + 1):
                    break
                self.sleep(READBACK_WAIT_S)
            try:
                o = self.fetch_order(oid)
            except CoinbaseRejected:
                o = None
                continue
            if self._cancel_done(o):
                break
        return o or {"id": oid, "status": None}

    def cancel_order(self, id, symbol=None, params=None) -> dict:
        """Cancel one order and confirm it: done only when a read shows it cancelled (or its cancel queued for the
        reopen: 'canceling'). A cancel Coinbase took but hasn't finished (pending_cancel) is read again, CANCEL_READS
        times. A cancel with no clear answer is read back before anything is decided. Raises StopAlreadyFilled,
        SessionClosed, CancelPending (still going), CancelUnknown (can't tell) or CancelFailed."""
        oid = str(id)
        try:
            res = self._post(f"{V3}/orders/batch_cancel", co.batch_cancel([oid]))
        except CoinbaseUncertain:
            res = None          # it may have reached Coinbase: what the order reads now decides
        rows = [] if res is None else \
            [r for r in res.get("results") or [] if str(r.get("order_id")) == oid] or res.get("results") or []
        r = rows[0] if rows else {}
        reason = str(r.get("failure_reason") or "")
        if res is None or r.get("success") is True:
            o = self._cancel_readback(oid)
            if o.get("status") is None:
                raise CancelUnknown(f"the cancel of order {oid} got no clear answer and the order can't be read back")
        elif reason == "ORDER_IS_FULLY_FILLED":
            raise StopAlreadyFilled(f"order {oid} had already filled")
        elif reason == "INVALID_CANCEL_FCM_TRADING_SESSION":
            raise SessionClosed(f"Coinbase isn't taking cancels now (order {oid}); tried again next run")
        elif reason in ("UNKNOWN_CANCEL_ORDER", "DUPLICATE_CANCEL_REQUEST"):
            try:
                o = self.fetch_order(oid)
            except CoinbaseRejected:
                raise CancelFailed(f"order {oid} isn't known to Coinbase ({reason})") from None
        else:
            raise CancelFailed(f"cancel of order {oid} refused: {reason or 'no reason given'}")
        st = o.get("status")
        if st == "closed":
            raise StopAlreadyFilled(f"order {oid} had already filled")
        if st == "canceling" and not self._cancel_done(o):
            raise CancelPending(f"order {oid}'s cancel was taken but it still reads {o.get('coinbase_status')} "
                                f"(its cancel not completed)")
        if st not in ("canceled", "canceling"):
            raise CancelFailed(f"order {oid} still reads {o.get('coinbase_status') or 'unknown'} after its cancel")
        return {"id": oid, "status": st}
