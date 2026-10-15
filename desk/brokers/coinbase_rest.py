"""CoinbaseREST: the desk's only way to call Coinbase's private API (Advanced Trade v3, and the Coinbase App v2 reads for
deposits and withdrawals). Standard-library urllib, like desk/feeds/coinbase.py; every call signed with a fresh CDP JWT
(coinbase_auth.build_jwt) on Coinbase's own clock.

What it can't do is the point:
  * GET only on READ_ALLOWLIST: the futures account, positions, orders, fills, the fee tier, key permissions, sweeps
    (read), the margin window, products, the portfolio's accounts (v3), and the v2 USD wallet's transactions. Any other
    path is refused before anything is sent (PathNotAllowed).
  * POST only with a valid Armed token (desk/coinbase_live.py, the one place that makes one, after every arming check),
    and even then only to ORDER_PATHS: create an order, batch-cancel, close a position. Without it every POST raises
    OrdersLocked before a socket opens: SHADOW holds a client built with no token, so even a bug in its overlay can't
    reach the exchange. Whatever the token's scope, an order or a close is refused unless it is for the product the
    token was armed for, and an entry unless its contracts at Coinbase's price, read just before, are within the token's
    cap (_scope_check). Whatever the scope too, checked against the position and every open order on the product read
    just before (_order_check: every open status, a QUEUED one included, a CANCEL_QUEUED one counted for nothing since
    Coinbase cancels it at the reopen before anything trades; and every placement source the desk reads, the app's
    included), and only in the exact shapes coinbase_orders builds: a cancel only of the desk's own orders on the
    product (each read first; not checked for the protection it takes away, which the caller puts back); a stop-limit
    only on the closing side of the position held (its stop_direction that side's, its limit on the closing side of its
    trigger) that, with the closing contracts already resting, covers at most the position (never an entry by stop). A
    PROTECT-ONLY token (an armed LIVE whose new-risk check failed this run) lets through only what can't add risk: those,
    and a close of at most the contracts held that leaves no more closing contracts resting than the position it leaves;
    its stop no wider than the saved stop, unless it's at the level of a desk stop resting, or of one it cancelled this
    run (put back after a close that didn't go). An
    entry, an add, a stop on the wrong side, a body with a key the desk never sends: OrdersLocked, before anything is
    sent. Never: /orders/preview (it needs only view, but this desk doesn't preview with credentials), /orders/edit
    (futures orders can't be edited: CANNOT_EDIT_FUTURES_ORDER), sweeps or any transfer (they need the Transfer
    permission the desk's keys never have), the intraday margin setting, portfolio moves, any v2 write. There is no
    delete() or put().

Errors (all CoinbaseError; no message carries a header, a token, a 2xx body or an amount):
  401  Coinbase answers text/plain "Unauthorized\\n" and nothing else, for a wrong key, a bad signature, an expired
       token or an unknown path alike (checked from here on every private path): CoinbaseAuthError with the usual causes.
  403  "Missing required scopes": CoinbasePermissionError.
  429, 5xx, network errors, timeouts, a body cut short, an answer that isn't JSON: retried with backoff
       (GET 2, 4, 8 s; POST at most twice, the same bytes, so the same client_order_id: a repeated id doesn't make a
       second order, Coinbase returns the first). Out of tries: CoinbaseUnavailable, or for a POST CoinbaseUncertain
       (it may have reached Coinbase: the caller reads the account before deciding anything).
  JSON {error, error_details, message} or {success: false, error_response: {...}} with HTTP 200 or 4xx (which one
       Coinbase uses isn't documented): CoinbaseRejected, carrying the codes.
       A rejection that answers a POST resent after a try with no clear answer is CoinbaseUncertain too (carrying the
       codes): the first try may have done it.
A run has a deadline (deadline_s): reads give up past it (DeadlineExceeded) and the broker starts no order sequence
whose worst case doesn't fit in what's left (fits()). A protective sequence (a stop put back on a position after its old
one was cancelled, a close at market because no stop would rest) runs inside protecting(), which lets it go on PROTECT_S
past the deadline: cutting it off half way would leave a position with no stop. The workflow's step timeout is above
deadline + PROTECT_S.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .coinbase_auth import CdpKey, build_jwt, mask_lines

DEFAULT_BASE = "https://api.coinbase.com"
LOOPBACK = re.compile(r"http://127\.0\.0\.1:\d{1,5}")
MAX_SKEW_S = 30.0
MIN_GAP_S = 0.12            # at least this between two calls (documented: 10,000 requests an hour a key, 30/s)
GET_RETRIES = 3
POST_RETRIES = 2
BACKOFF_S = 2.0
BACKOFF_CAP_S = 20.0
PROTECT_S = 60.0            # how far past the run's deadline a protective sequence may go (protecting())
CAP_MOVE = 0.01             # an entry may be this much over the cap at the price the client reads (_scope_check)
MIN_REQUEST_S = 2.0         # what one request is counted as, at least, in a sequence's worst case (worst_s)
ERROR_BODY_MAX = 64_000
USER_AGENT = "btc-perp-desk"
_V3 = "/api/v3/brokerage"
_ID = r"[A-Za-z0-9_:-][A-Za-z0-9._:-]{0,79}"

READ_ALLOWLIST = tuple(re.compile(p) for p in (
    rf"{_V3}/key_permissions",
    rf"{_V3}/cfm/balance_summary",
    rf"{_V3}/cfm/positions",
    rf"{_V3}/cfm/positions/{_ID}",
    rf"{_V3}/cfm/sweeps",
    rf"{_V3}/cfm/intraday/margin_setting",
    rf"{_V3}/cfm/intraday/current_margin_window",
    rf"{_V3}/orders/historical/batch",
    rf"{_V3}/orders/historical/fills",
    rf"{_V3}/orders/historical/{_ID}",
    rf"{_V3}/transaction_summary",
    rf"{_V3}/portfolios",
    rf"{_V3}/accounts",
    rf"{_V3}/products/{_ID}",
    rf"{_V3}/products/{_ID}/ticker",
    rf"{_V3}/products/{_ID}/candles",
    rf"{_V3}/product_book",
    rf"{_V3}/best_bid_ask",
    r"/v2/accounts",
    r"/v2/accounts/[0-9A-Za-z-]{8,64}/transactions",
))
PUBLIC_TIME = f"{_V3}/time"           # Coinbase's clock: public, no key, answered no-store
ORDER_PATHS = frozenset({f"{_V3}/orders", f"{_V3}/orders/batch_cancel", f"{_V3}/orders/close_position"})
RETRY_STATUS = {429, 500, 502, 503, 504}
# order_placement_source read besides the default (RETAIL_ADVANCED, the API's): the app's. Coinbase lists only the
# default unless asked, so every read of the open orders asks for these too (and for any the desk's own orders carry)
OTHER_SOURCES = ("RETAIL_SIMPLE",)
OPEN_LIKE = ("PENDING", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED")     # read besides OPEN (which can't be combined)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: it would carry the signed token to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CoinbaseError(RuntimeError):
    """Anything that went wrong talking to Coinbase. `transient`: worth trying again at the next run."""
    transient = False


class PathNotAllowed(CoinbaseError):
    pass


class OrdersLocked(CoinbaseError):
    pass


class ClockSkewError(CoinbaseError):
    pass


class CoinbaseAuthError(CoinbaseError):
    pass


class CoinbasePermissionError(CoinbaseError):
    pass


class CoinbaseDataError(CoinbaseError):
    """An answer the desk can't act on (a shape it doesn't know, a balance that doesn't add up, a changed product)."""


class CoinbaseUnavailable(CoinbaseError):
    transient = True


class CoinbaseUncertain(CoinbaseUnavailable):
    """A POST whose outcome isn't known: it may have reached Coinbase. Read the account before deciding anything.
    `codes`: what Coinbase answered a later try with, when it answered with a rejection (the first try may still have
    done it: a funds check before the client_order_id dedupe, say)."""

    def __init__(self, text: str, codes: tuple = ()):
        super().__init__(text)
        self.codes = tuple(codes)


class DeadlineExceeded(CoinbaseUnavailable):
    pass


class CoinbaseRejected(CoinbaseError):
    """Coinbase refused the request, definitely (nothing was done). The codes are attributes; str() names only the
    path and the codes. detail() adds Coinbase's own wording, for private logs only."""

    def __init__(self, method: str, path: str, status: int, body: dict | None):
        b = body if isinstance(body, dict) else {}
        er = b.get("error_response") if isinstance(b.get("error_response"), dict) else {}
        self.method, self.path, self.status = method, path, status
        self.error = er.get("error") or b.get("error")
        self.new_order_failure_reason = er.get("new_order_failure_reason")
        self.preview_failure_reason = er.get("preview_failure_reason")
        self.message = er.get("message") or b.get("message")
        self.error_details = er.get("error_details") or b.get("error_details")
        codes = [c for c in (self.error, self.new_order_failure_reason, self.preview_failure_reason)
                 if c and c not in ("UNKNOWN_FAILURE_REASON", "UNKNOWN_PREVIEW_FAILURE_REASON")]
        self.codes = tuple(dict.fromkeys(str(c) for c in codes))
        super().__init__(f"Coinbase refused {method} {path} (HTTP {status}"
                         + (f": {', '.join(self.codes)}" if self.codes else "") + ")")

    def has(self, *codes: str) -> bool:
        return any(c in self.codes for c in codes)

    def detail(self) -> str:
        return f"{self} {self.message or ''} {self.error_details or ''}".strip()


def _allowed(path: str) -> bool:
    if ".." in path or "//" in path or "%" in path or "?" in path:
        return False
    return any(p.fullmatch(path) for p in READ_ALLOWLIST)


class CoinbaseREST:
    def __init__(self, key: CdpKey, *, armed=None, base_url: str = DEFAULT_BASE, timeout: float = 15.0,
                 clock=time.time, sleep=time.sleep, deadline_s: float | None = None, emit=print, env=None,
                 min_gap_s: float = MIN_GAP_S):
        if not isinstance(key, CdpKey):
            raise TypeError("CoinbaseREST needs a CdpKey (coinbase_auth.load_cdp_key)")
        if base_url != DEFAULT_BASE and not LOOPBACK.fullmatch(base_url):
            raise ValueError(f"CoinbaseREST talks to {DEFAULT_BASE} (or the tests' fake on 127.0.0.1), not {base_url!r}")
        self._key = key
        self._armed = armed
        self.base_url = base_url
        self.timeout = timeout
        self.clock = clock
        self.sleep = sleep
        self.deadline_s = deadline_s
        self.protect_s = PROTECT_S
        self._protecting = 0
        self.min_gap_s = min_gap_s
        self._emit, self._env = emit, env
        self.skew_s: float | None = None        # Coinbase's clock minus this machine's, once measured
        self.key_perms: dict | None = None      # the key's permissions, as arm() read them for an order client
        self.requests = 0
        self.posts_sent = 0                     # non-GET requests that left this process (each try)
        self.posts_paths: list[str] = []        # each order call asked for (its path's last part), for the run's report
        self.last_latency_ms = 0.0
        self.slowest_s = 0.0                    # the slowest try this run (an answer, an error or a timeout)
        self.learned_sources: list[str] = []    # placement sources the desk's own orders turned out to carry
        self.cancelled_stops: list[tuple[str, float]] = []    # (side, trigger) of each desk stop this client cancelled
        self._last_call: float | None = None
        # The tests' fake runs on loopback: never through a proxy from the environment
        handlers = [_NoRedirect()] + ([urllib.request.ProxyHandler({})] if base_url != DEFAULT_BASE else [])
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        return f"CoinbaseREST({self.base_url}, {'orders armed' if self._armed is not None else 'read only'})"

    # ---- time
    def now_s(self) -> float:
        """Coinbase's clock, as best known: this machine's plus the skew measured (none yet: this machine's)."""
        return self.clock() + (self.skew_s or 0.0)

    def time_left(self) -> float:
        if self.deadline_s is None:
            return math.inf
        return self.deadline_s + (self.protect_s if self._protecting else 0.0) - self.clock()

    def fits(self, seconds: float) -> bool:
        """Whether a sequence whose worst case is `seconds` can finish before the run's deadline."""
        return self.time_left() >= seconds

    @contextlib.contextmanager
    def protecting(self):
        """A protective sequence: it may run PROTECT_S past the deadline (module docstring)."""
        self._protecting += 1
        try:
            yield self
        finally:
            self._protecting -= 1

    @property
    def is_protecting(self) -> bool:
        return self._protecting > 0

    @property
    def protect_only(self) -> bool:
        """This client holds a protect-only token (_order_check reads the account before each order, not only before
        each stop and cancel)."""
        return self._armed is not None and getattr(self._armed, "scope", "trade") != "trade"

    def worst_s(self, requests: int, waits_s: float = 0.0) -> float:
        """The worst case, in seconds, of a sequence of `requests` requests and `waits_s` of waiting: each request at
        half again the slowest try this run has seen, and never under MIN_REQUEST_S. A slow Coinbase makes a sequence
        that must not stop half way (a stop cancelled for a close) not start at all."""
        return requests * max(MIN_REQUEST_S, 1.5 * self.slowest_s) + waits_s

    def sources(self) -> list[str | None]:
        """The placement sources every read of the open orders lists: the default (None: RETAIL_ADVANCED), the app's,
        and any the desk's own orders turned out to carry."""
        out: list[str | None] = [None, *OTHER_SOURCES]
        return out + [x for x in self.learned_sources if x not in out and x != "RETAIL_ADVANCED"]

    def measure_skew(self) -> float:
        """Coinbase's clock minus this machine's, from its public time endpoint (no key), kept for signing whatever
        it is. server_skew refuses one over MAX_SKEW_S; the read-only watchdog of a run refused over the clock still
        signs its reads on Coinbase's clock with it."""
        t0 = self.clock()
        out = self._request("GET", PUBLIC_TIME, None, None, auth=False, retries=GET_RETRIES)
        t1 = self.clock()
        try:
            server = float(out.get("epochMillis")) / 1000 if out.get("epochMillis") is not None else \
                float(out["epochSeconds"])
        except (KeyError, TypeError, ValueError):
            raise CoinbaseDataError("Coinbase's time answer has no epochMillis or epochSeconds") from None
        self.skew_s = server - (t0 + t1) / 2
        return self.skew_s

    def server_skew(self) -> float:
        """measure_skew, refused over MAX_SKEW_S: a token signed on a clock that far off is refused with a bare 401,
        and a machine's clock that far off isn't one to trade on. Every token after this is signed on Coinbase's clock
        (this machine's plus the skew), so a smaller skew either way can't make one look early."""
        skew = self.measure_skew()
        if abs(skew) > MAX_SKEW_S:
            raise ClockSkewError(f"this machine's clock is {-skew:+.1f} s off Coinbase's (more than {MAX_SKEW_S:.0f} "
                                 f"s): Coinbase's signed tokens live 2 minutes. Not calling it")
        return skew

    # ---- calls
    def get(self, path: str, params: dict | None = None, retries: int = GET_RETRIES) -> dict:
        if not _allowed(path):
            raise PathNotAllowed(f"GET {path} isn't on the desk's list of Coinbase calls; refused before anything was "
                                 f"sent")
        return self._request("GET", path, params, None, auth=True, retries=retries)

    def get_pages(self, path: str, params: dict | None, items: str, max_pages: int = 10,
                  retries: int = GET_RETRIES) -> list:
        """A v3 list read to its end, cursor by cursor (orders: has_next + cursor; fills: a non-empty cursor). More
        than `max_pages` raises: a partial list would read as fewer orders or fills than there are."""
        out, params, seen = [], dict(params or {}), set()
        for _ in range(max_pages):
            page = self.get(path, params, retries=retries)
            rows = page.get(items)
            if not isinstance(rows, list):
                raise CoinbaseDataError(f"Coinbase's answer to GET {path} has no {items} list")
            out += rows
            cursor = page.get("cursor") or ""
            more = page.get("has_next") if "has_next" in page else bool(cursor)
            if not more or not cursor or cursor in seen:
                return out
            seen.add(cursor)
            params["cursor"] = cursor
        raise CoinbaseDataError(f"GET {path} is still going after {max_pages} pages: not acting on part of a list")

    def get_v2_pages(self, path: str, params: dict | None = None, max_pages: int = 10, stop=None) -> list:
        """A v2 list (newest first), page by page through pagination.next_uri until it's empty, or until `stop(row)`
        says the rest is older than needed."""
        out, params = [], dict(params or {})
        for _ in range(max_pages):
            page = self.get(path, params)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise CoinbaseDataError(f"Coinbase's answer to GET {path} has no data list")
            out += rows
            if stop is not None and rows and stop(rows[-1]):
                return out
            nxt = (page.get("pagination") or {}).get("next_uri") or ""
            if not nxt:
                return out
            u = urllib.parse.urlsplit(nxt)
            path, params = u.path, dict(urllib.parse.parse_qsl(u.query))
        raise CoinbaseDataError(f"GET {path} is still going after {max_pages} pages: not acting on part of a list")

    def post(self, path: str, body: dict) -> dict:
        """An order call. Refused before anything is sent unless this client holds a valid Armed token, and even then
        unless `path` is one of ORDER_PATHS. Whatever the token's scope: an order or a close for any product but the
        one the token was armed for is refused, and so is an entry (an order that isn't a stop-limit) bigger than the
        token's cap at Coinbase's price read just before (_scope_check); a cancel goes only for the desk's own orders
        on that product, and a stop-limit only on the closing side of the position Coinbase holds, never for more than
        it leaves uncovered (_order_check). A protect-only token's calls are checked further there."""
        from ..coinbase_live import Armed       # the lock (imported here: coinbase_live imports this module)

        armed = self._armed
        if not (isinstance(armed, Armed) and armed.valid(self.clock())):
            raise OrdersLocked(f"orders are locked: POST {path} was refused before anything was sent. SHADOW never "
                               f"sends orders, and LIVE sends them only after every arming check passes (README: "
                               f"Going live on Coinbase)")
        if path not in ORDER_PATHS:
            raise PathNotAllowed(f"POST {path} isn't an order call the desk makes; refused before anything was sent")
        self._scope_check(path, body, armed)
        self._order_check(path, body, armed)
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()     # the same bytes on every retry
        return self._request("POST", path, None, data, auth=True, retries=POST_RETRIES)

    def _scope_check(self, path: str, body: dict, armed) -> None:
        """What every token names, checked for every scope before anything is sent: an order or a close is for the
        product the token was armed for (a cancel names no product: each is read, _order_check),
        and an entry (any order body that isn't the protective stop-limit) is no bigger than the token's cap: its
        contracts at Coinbase's price for its side, read now, may be at most the cap and CAP_MOVE more (the price can
        move between the desk sizing it and this read). No fresh price: refused, as LiveBroker skips an entry then."""
        from ..thresholds import COINBASE
        from .coinbase_orders import amount

        def refuse(why: str):
            raise OrdersLocked(f"POST {path} refused before anything was sent: {why}")

        b = body if isinstance(body, dict) else {}
        if path == f"{_V3}/orders/batch_cancel":
            return
        if b.get("product_id") != armed.product:
            refuse(f"it is for {str(b.get('product_id'))[:40]!r}, not {armed.product}, the one product this run was "
                   f"armed for")
        if path != f"{_V3}/orders":
            return
        cfg = b.get("order_configuration") if isinstance(b.get("order_configuration"), dict) else {}
        if set(cfg) == {"stop_limit_stop_limit_gtc"}:
            return              # the protective stop (the adapter sizes it against the position it reads first)
        sizes = [v.get("base_size") for v in cfg.values() if isinstance(v, dict)]
        try:
            n = sum(amount(s, "base_size") for s in sizes) if sizes else None
        except CoinbaseDataError:
            n = None
        if not n or n <= 0:
            refuse("its size can't be read, so it can't be checked against the cap")
        r = self.get(f"{_V3}/best_bid_ask", {"product_ids": armed.product})
        book = next((x for x in r.get("pricebooks") or [] if x.get("product_id") == armed.product), {}) \
            if isinstance(r, dict) else {}
        side = book.get("asks" if b.get("side") == "BUY" else "bids") or []
        px = amount((side[0] or {}).get("price"), "price", required=False) if side else None
        if not px or px <= 0:
            refuse("there's no price from Coinbase to check it against the cap")
        notional = n * COINBASE.contract_size * px
        if not armed.cap or notional > armed.cap * (1 + CAP_MOVE):
            refuse(f"{n:g} contracts at {px:,.0f} is ${notional:,.2f}, over the ${armed.cap:,.2f} cap this run was "
                   f"armed with")

    def _order_check(self, path: str, body: dict, armed) -> None:
        """Every scope: a cancel only of the desk's own orders on the armed product (each read first: a stop's level is
        kept, cancelled_stops), and a stop-limit only in the exact shape coinbase_orders builds, on the closing side of
        the position Coinbase holds now, for no more than the closing orders resting leave uncovered (an entry by stop,
        or one that would open the other way, never goes). A protect-only token's order further: only those, or a close
        that can only shrink the position, and no stop wider than the saved one (its floor) unless it's at the level
        of a desk stop resting now, or of one this client cancelled this run (put back after a close that didn't go):
        neither loosens anything. The position and
        every open order on the product are read here, just before (every open status and every placement source the
        desk reads: a stop queued while Coinbase is closed, or one placed in the app, closes as much as any). Anything
        else raises OrdersLocked, before anything is sent. A cancel isn't checked for the protection it removes (a stop
        cancelled for a close comes off before the close can go): the caller stays responsible for putting back any
        protection a cancel took away (the safety pass's put_back, LiveBroker's _cb_protect_remainder)."""
        from .coinbase_orders import STOP_LIMIT_FALLBACKS, PRICE_STEP, amount, is_desk_cid, parse_order, \
            parse_position

        protect = armed.scope != "trade"

        def refuse(why: str):
            if not protect:
                raise OrdersLocked(f"POST {path} refused before anything was sent: {why}")
            raise OrdersLocked(f"protect only: POST {path} refused before anything was sent: {why}. An armed LIVE whose "
                               f"new-risk check failed this run sends only cancels and orders that shrink the position")

        b = body if isinstance(body, dict) else {}
        if path == f"{_V3}/orders/batch_cancel":
            ids = b.get("order_ids")
            if set(b) != {"order_ids"} or not isinstance(ids, list) or not ids or \
                    not all(isinstance(i, str) and i for i in ids):
                refuse("its body isn't a cancel the desk builds")
            for oid in ids:
                # only the desk's own orders on the armed product: never one placed in the app, or on another product.
                # Not checked: the protection a cancel takes away. The caller that cancels a stop (the safety pass's
                # close, desk/coinbase_run.py) puts it back if what it cancelled it for doesn't go through.
                if not re.fullmatch(_ID, oid):
                    refuse(f"order id {oid[:40]!r} isn't one Coinbase gives")
                try:
                    o = parse_order(self.get(f"{_V3}/orders/historical/{oid}").get("order") or {})
                except CoinbaseRejected as exc:
                    refuse(f"order {oid} can't be read (HTTP {exc.status}), so it isn't known to be one of the desk's "
                           f"own orders")
                src = o.get("placement")
                if o.get("symbol") != armed.product or not is_desk_cid(o.get("clientOrderId")) or \
                        src not in (None, "", "RETAIL_ADVANCED", *self.learned_sources):
                    refuse(f"order {oid} isn't one of the desk's own orders on {armed.product}")
                if o.get("triggerPrice") is not None and o.get("side"):
                    self.cancelled_stops.append((str(o["side"]), float(o["triggerPrice"])))
            return
        close = path == f"{_V3}/orders/close_position"
        if not protect:
            cfg = body.get("order_configuration") if isinstance(body, dict) else None
            if close or not isinstance(cfg, dict) or set(cfg) != {"stop_limit_stop_limit_gtc"}:
                return              # an entry (the cap is checked in _scope_check) or a close (the adapter's checks)
        want = {"client_order_id", "product_id", "size"} if close else \
            {"client_order_id", "product_id", "side", "order_configuration"}
        if set(b) != want or not is_desk_cid(b.get("client_order_id")):
            refuse("its body isn't one the desk builds (a key it never sends, or no desk client id)")
        if b.get("product_id") != armed.product:
            refuse(f"it isn't for {armed.product}")
        cfg = b.get("order_configuration") if isinstance(b.get("order_configuration"), dict) else {}
        sl = cfg.get("stop_limit_stop_limit_gtc")
        if not close and (set(cfg) != {"stop_limit_stop_limit_gtc"} or not isinstance(sl, dict)):
            refuse("it isn't a stop (an entry or an add)")
        if not close and set(sl) != {"base_size", "stop_price", "limit_price", "stop_direction"}:
            refuse("its stop isn't one the desk builds")
        try:
            n = amount(b.get("size") if close else sl.get("base_size"), "size")
            trig = None if close else amount(sl.get("stop_price"), "stop_price")
            limit = None if close else amount(sl.get("limit_price"), "limit_price")
        except CoinbaseDataError:
            refuse("its body isn't one the desk builds")
        if n is None or n < 1 or abs(n - round(n)) > 1e-9:
            refuse("its size isn't a whole number of contracts")
        # a trade token's stop: read once, no retries, and a read that fails lets it through (the adapter sized it
        # against the position it read, or, a read failing after a cancel-first move, the one it knew: a stop that
        # move cancelled the old one for is never held back by a read)
        tries = GET_RETRIES if protect else 0
        try:
            rows = self.get(f"{_V3}/cfm/positions", retries=tries).get("positions")
            if not isinstance(rows, list):
                raise CoinbaseDataError("Coinbase's positions answer has no positions list")
            pos = next((q for q in (parse_position(r) for r in rows) if q and q["product_id"] == armed.product), None)
            if pos is None:
                refuse("there is no position to protect")
            rows = []
            for src in self.sources():
                extra = {} if src is None else {"order_placement_source": src}
                rows += self.get_pages(f"{_V3}/orders/historical/batch", {"order_status": "OPEN", "product_ids":
                                       armed.product, "limit": 100, **extra}, "orders", retries=tries)
                rows += self.get_pages(f"{_V3}/orders/historical/batch", {"order_status": list(OPEN_LIKE),
                                       "product_ids": armed.product, "limit": 100, **extra}, "orders", retries=tries)
        except CoinbaseUnavailable:
            if protect:
                raise
            return
        closing = "SELL" if pos["side"] == "long" else "BUY"
        seen, resting, desk_levels = set(), 0.0, []
        for o in (parse_order(r) for r in rows):
            if o["id"] in seen or o["symbol"] != armed.product or o["side"] != closing.lower() or \
                    o["status"] not in ("open", "canceling") or o.get("cancel_queued"):
                continue            # (a CANCEL_QUEUED order is cancelled at the reopen, before anything trades)
            seen.add(o["id"])
            resting += o["remaining"] if o["remaining"] is not None else (o["amount"] or 0.0)
            if o.get("triggerPrice") is not None and is_desk_cid(o.get("clientOrderId")):
                desk_levels.append(float(o["triggerPrice"]))
        if close:
            if n > pos["contracts"]:
                refuse(f"a close of {n:g} contracts is more than the {pos['contracts']} held")
            if resting > pos["contracts"] - n + 1e-9:
                refuse(f"{resting:g} closing contracts rest against the {pos['contracts']} held: a close of {n:g} "
                       f"would leave them to open a position")
            return
        if b.get("side") != closing:
            refuse(f"a {b.get('side')} stop doesn't close a {pos['side']} position")
        if sl.get("stop_direction") != ("STOP_DIRECTION_STOP_DOWN" if closing == "SELL" else "STOP_DIRECTION_STOP_UP"):
            refuse(f"its stop_direction isn't a {closing} stop's")
        sign = 1 if pos["side"] == "long" else -1
        if (trig - limit) * sign < 0 or abs(trig - limit) > max(STOP_LIMIT_FALLBACKS) * trig + PRICE_STEP:
            refuse(f"its limit {limit:,.0f} isn't on the closing side of its trigger {trig:,.0f}, within "
                   f"{max(STOP_LIMIT_FALLBACKS):.0%}")
        if resting + n > pos["contracts"] + 1e-9:
            refuse(f"{resting:g} closing contracts already rest against the {pos['contracts']} held; {n:g} more could "
                   f"open a position")
        floor = armed.floor if protect else None
        if floor and floor.get("side") == pos["side"]:
            # a level a desk stop holds now, or held until this client cancelled it this run, loosens nothing
            held = desk_levels + [level for side, level in self.cancelled_stops if side == closing.lower()]
            if (trig - float(floor["stop"])) * sign < -PRICE_STEP and \
                    not any(abs(trig - level) <= PRICE_STEP for level in held):
                refuse(f"the stop at {trig:,.0f} is wider than the saved one ({float(floor['stop']):,.0f}), and no desk "
                       f"stop rests at that level or was cancelled from it this run")

    # ---- transport
    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self.min_gap_s - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def _request(self, method: str, path: str, params, data: bytes | None, *, auth: bool, retries: int) -> dict:
        if method not in ("GET", "POST"):
            raise PathNotAllowed(f"{method} isn't a method the desk uses")
        url = self.base_url + path + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
        sent = False
        unclear = False             # a POST try went out and got no clear answer: it may have reached Coinbase
        last = "no answer"
        for attempt in range(retries + 1):
            if self.time_left() <= 0:
                if method == "POST" and sent:
                    raise CoinbaseUncertain(f"POST {path}: the run's time ran out after a try that got no clear "
                                            f"answer ({last}); it may have reached Coinbase")
                raise DeadlineExceeded(f"{method} {path} not sent: the run's time is up")
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if auth:
                token = build_jwt(self._key, method, path, self.now_s())
                mask_lines(token, emit=self._emit, env=self._env)
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            self._pace()
            t = self.clock()
            status, body, ctype = None, b"", ""
            try:
                if method == "POST":
                    if not sent:
                        self.posts_paths.append(path.rsplit("/", 1)[-1])
                    sent = True
                    self.posts_sent += 1
                with self._opener.open(req, timeout=max(1.0, min(self.timeout, self.time_left()))) as resp:
                    status, ctype = resp.status, resp.headers.get("Content-Type", "")
                    body = resp.read()
            except urllib.error.HTTPError as exc:
                status, ctype = exc.code, (exc.headers.get("Content-Type", "") if exc.headers else "")
                try:
                    body = exc.read(ERROR_BODY_MAX)
                except Exception:
                    body = b""
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                last = type(exc).__name__
                status = None
            self.requests += 1
            self.last_latency_ms = (self.clock() - t) * 1000
            self.slowest_s = max(self.slowest_s, self.last_latency_ms / 1000)
            if status is not None:
                try:
                    out = self._answer(method, path, status, body, ctype)
                except CoinbaseRejected as exc:
                    if method == "POST" and unclear:
                        raise CoinbaseUncertain(f"POST {path}: a try got no clear answer ({last}) and the next was "
                                                f"refused ({', '.join(exc.codes) or f'HTTP {exc.status}'}): the first "
                                                f"may have reached Coinbase", exc.codes) from None
                    raise
                if out is not None:
                    return out
                last = f"HTTP {status}"
            if method == "POST":
                unclear = True
            if attempt == retries:
                break
            wait = min(BACKOFF_CAP_S, BACKOFF_S * 2 ** attempt)
            if wait >= self.time_left():
                break
            self.sleep(wait)
        if method == "POST" and sent:
            raise CoinbaseUncertain(f"POST {path} got no clear answer after {retries + 1} tries ({last}): it may "
                                    f"have reached Coinbase")
        raise CoinbaseUnavailable(f"{method} {path}: no usable answer from Coinbase after {retries + 1} tries ({last})")

    def _answer(self, method: str, path: str, status: int, body: bytes, ctype: str) -> dict | None:
        """The parsed answer, None to try again, or raises."""
        if status == 401:
            skew = "not checked" if self.skew_s is None else f"checked: {-self.skew_s:+.1f} s"
            raise CoinbaseAuthError(
                f"Coinbase answered 401 Unauthorized to {method} {path}. It gives no reason. Usual causes: the key was "
                f"deleted, the secret isn't the whole key file (name and privateKey), the key isn't ECDSA, or the "
                f"clock is off ({skew}).")
        if status in RETRY_STATUS:
            return None
        if 300 <= status < 400:
            raise CoinbaseDataError(f"Coinbase redirected {method} {path} (HTTP {status}): not following it")
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        if status == 403:
            scopes = b"Missing required scopes" in body
            raise CoinbasePermissionError(f"Coinbase answered 403: this key lacks the permission {path} needs"
                                          + ("" if scopes else " (or the request isn't allowed)"))
        if 200 <= status < 300:
            if not isinstance(parsed, dict):
                return None         # a CDN or maintenance page, or a body cut short: try again
            er = parsed.get("error_response")
            if parsed.get("success") is False and isinstance(er, dict) and er:
                raise CoinbaseRejected(method, path, status, parsed)
            return parsed
        if 400 <= status < 500:
            raise CoinbaseRejected(method, path, status, parsed if isinstance(parsed, dict) else None)
        return None
