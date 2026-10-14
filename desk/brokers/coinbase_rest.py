"""CoinbaseREST: the desk's only way to call Coinbase's private API (Advanced Trade v3, and the Coinbase App v2 reads for
deposits and withdrawals). Standard-library urllib, like desk/feeds/coinbase.py; every call signed with a fresh CDP JWT
(coinbase_auth.build_jwt) on Coinbase's own clock.

What it can't do is the point:
  * GET only on READ_ALLOWLIST: the futures account, positions, orders, fills, the fee tier, key permissions, sweeps
    (read), the margin window, products, and the v2 USD wallet's transactions. Any other path is refused before
    anything is sent (PathNotAllowed).
  * POST only with a valid Armed token (desk/coinbase_live.py, the one place that makes one, after every arming
    check), and even then only to ORDER_PATHS: create an order, batch-cancel, close a position. Without it every POST
    raises OrdersLocked before a socket opens: SHADOW holds a client built with no token, so even a bug in its overlay
    can't reach the exchange. Never: /orders/preview (it needs only view, but this desk doesn't preview with
    credentials), /orders/edit (futures orders can't be edited: CANNOT_EDIT_FUTURES_ORDER), sweeps or any transfer
    (they need the Transfer permission the desk's keys never have), the intraday margin setting, portfolio moves, any
    v2 write. There is no delete() or put().

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
A run has a deadline (deadline_s): reads give up past it (DeadlineExceeded) and the broker starts no order sequence
whose worst case doesn't fit in what's left (fits()).
"""

from __future__ import annotations

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
    """A POST whose outcome isn't known: it may have reached Coinbase. Read the account before deciding anything."""


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
        self.min_gap_s = min_gap_s
        self._emit, self._env = emit, env
        self.skew_s: float | None = None        # Coinbase's clock minus this machine's, once measured
        self.requests = 0
        self.posts_sent = 0                     # non-GET requests that left this process
        self.last_latency_ms = 0.0
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
        return math.inf if self.deadline_s is None else self.deadline_s - self.clock()

    def fits(self, seconds: float) -> bool:
        """Whether a sequence whose worst case is `seconds` can finish before the run's deadline."""
        return self.time_left() >= seconds

    def server_skew(self) -> float:
        """Coinbase's clock minus this machine's, from its public time endpoint (no key). Over MAX_SKEW_S refuses:
        a token signed on a clock that far off is refused with a bare 401. Every token after this is signed on
        Coinbase's clock (this machine's plus the skew), so a smaller skew either way can't make one look early."""
        t0 = self.clock()
        out = self._request("GET", PUBLIC_TIME, None, None, auth=False, retries=GET_RETRIES)
        t1 = self.clock()
        try:
            server = float(out.get("epochMillis")) / 1000 if out.get("epochMillis") is not None else \
                float(out["epochSeconds"])
        except (KeyError, TypeError, ValueError):
            raise CoinbaseDataError("Coinbase's time answer has no epochMillis or epochSeconds") from None
        skew = server - (t0 + t1) / 2
        if abs(skew) > MAX_SKEW_S:
            raise ClockSkewError(f"this machine's clock is {-skew:+.1f} s off Coinbase's (more than {MAX_SKEW_S:.0f} "
                                 f"s): Coinbase's signed tokens live 2 minutes. Not calling it.")
        self.skew_s = skew
        return skew

    # ---- calls
    def get(self, path: str, params: dict | None = None) -> dict:
        if not _allowed(path):
            raise PathNotAllowed(f"GET {path} isn't on the desk's list of Coinbase calls; refused before anything was "
                                 f"sent")
        return self._request("GET", path, params, None, auth=True, retries=GET_RETRIES)

    def get_pages(self, path: str, params: dict | None, items: str, max_pages: int = 10) -> list:
        """A v3 list read to its end, cursor by cursor (orders: has_next + cursor; fills: a non-empty cursor). More
        than `max_pages` raises: a partial list would read as fewer orders or fills than there are."""
        out, params, seen = [], dict(params or {}), set()
        for _ in range(max_pages):
            page = self.get(path, params)
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
        unless `path` is one of ORDER_PATHS."""
        from ..coinbase_live import Armed       # the lock (imported here: coinbase_live imports this module)

        armed = self._armed
        if not (isinstance(armed, Armed) and armed.valid(self.clock())):
            raise OrdersLocked(f"orders are locked: POST {path} was refused before anything was sent. SHADOW never "
                               f"sends orders, and LIVE sends them only after every arming check passes (README: "
                               f"Going live on Coinbase)")
        if path not in ORDER_PATHS:
            raise PathNotAllowed(f"POST {path} isn't an order call the desk makes; refused before anything was sent")
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()     # the same bytes on every retry
        return self._request("POST", path, None, data, auth=True, retries=POST_RETRIES)

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
            if status is not None:
                out = self._answer(method, path, status, body, ctype)
                if out is not None:
                    return out
                last = f"HTTP {status}"
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
