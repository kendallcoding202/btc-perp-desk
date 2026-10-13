"""CRAWLER (live): Coinbase's public market data for its US nano BTC perp (BIP-20DEC30-CDE). No API key.

The same interface the paper step uses for Hyperliquid (candles, funding, context, requests, last_latency_ms, bar_ms),
plus product(): the contract's details, read every run (funding rate, margin rates, session and maintenance).

    GET api.coinbase.com/api/v3/brokerage/market/products/<id>                      details
    GET .../market/products/<id>/candles?granularity=TWO_HOUR&start=<s>&end=<s>     at most 300 candles, newest first
    GET .../market/product_book?product_id=<id>&limit=<n>                           sizes in contracts
    GET .../market/products/<id>/ticker?limit=1                                     best bid / ask

Coinbase has no funding history: funding() answers from a log of the rates this feed (and earlier runs, through the
ledger) have read, one snapshot a run (venue.FundingLog). A short, malformed or failed answer raises, so the account's
step fails on its own and the next run catches up. A candle missing outside any closure is a hole: a few are listed in
`holes` for paper to wait for (paper.py), more than MAX_HOLES is a short answer and raises.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ..models import Bar, FundingEvent, MarketContext
from ..thresholds import COINBASE, INTERVAL_SECONDS, ContractVenue
from ..venue import FundingLog, Sessions, missing

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
GRANULARITY = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE",
               "1h": "ONE_HOUR", "2h": "TWO_HOUR", "6h": "SIX_HOUR", "1d": "ONE_DAY"}
PAGE = 300                      # candles per call, the API's maximum
MAX_HOLES = 12                  # more candles missing than this, outside every closure known: a short answer, raise


class FeedError(RuntimeError):
    """Coinbase's answer can't be used this run (missing, short or malformed): the step fails, the next run retries."""


def _iso_ms(s: str | None) -> int | None:
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _message(exc: urllib.error.HTTPError) -> str:
    """Coinbase's own error message from an HTTP error's body ({"message": "no pricebook found"}), or its code."""
    try:
        return str(json.loads(exc.read()).get("message") or exc.code)
    except Exception:
        return f"HTTP {exc.code}"


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


class CoinbaseFeed:
    def __init__(self, venue: ContractVenue = COINBASE, interval: str = "2h", timeout: float = 15.0,
                 funding_log=None, clock=None):
        if interval not in GRANULARITY:
            raise ValueError(f"Coinbase has no {interval} candles (it has {', '.join(GRANULARITY)})")
        self.venue = venue
        self.product_id = venue.product
        self.interval = interval
        self.timeout = timeout
        self.requests = 0
        self.last_latency_ms = 0.0
        self.log = FundingLog(funding_log, venue.funding_log_days)
        self.details: dict | None = None
        self.clock = clock or time.time         # seconds, like time.time (tests freeze it)
        # What explains a missing candle (paper sets these from the ledger before candles()): the closures (the weekly
        # break, the known and the recorded maintenance windows) and the candles Coinbase never published (accepted
        # gaps).
        self.sessions = Sessions(venue)
        self.gaps: list[int] = []
        self.holes: list[int] = []              # the last candles() answer's unexplained missing candles

    # ---- http
    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        """GET with backoff on 429/5xx, network errors, a body cut short and an answer that isn't JSON (a CDN or
        maintenance page); anything else (or the last try) raises."""
        url = f"{BASE}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        for attempt in range(retries + 1):
            t = time.time()
            body = b""
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                            timeout=self.timeout) as resp:
                    body = resp.read()
                out = json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == retries:
                    raise
            except (ValueError, http.client.HTTPException) as exc:     # not JSON, or cut short (IncompleteRead)
                if attempt == retries:
                    what = f"something that isn't JSON: {body[:80]!r}" if isinstance(exc, ValueError) else \
                        f"an answer cut short ({type(exc).__name__}: {exc})"
                    raise FeedError(f"Coinbase answered {path} with {what}") from None
            else:
                self.requests += 1
                self.last_latency_ms = (time.time() - t) * 1000
                if not isinstance(out, dict):
                    raise FeedError(f"Coinbase answered {path} with {type(out).__name__}, not an object")
                return out
            time.sleep(min(20.0, 2.0 * 2 ** attempt))

    @property
    def bar_ms(self) -> int:
        return INTERVAL_SECONDS[self.interval] * 1000

    # ---- candles
    def candles(self, start_ms: int = 0, end_ms: int | None = None, closed_only: bool = True) -> list[Bar]:
        """The bars that opened in [start_ms, end_ms], oldest first, paged 300 at a time (newest page first). Never
        earlier than the product's listing (_listed). `closed_only` drops the bar that is still forming, which may be
        missing (no trade in it yet).

        A closed candle missing outside every closure (self.sessions) and not an accepted gap (self.gaps) is a hole,
        listed in self.holes: paper waits for one (paper.py). More than MAX_HOLES is a short answer: FeedError."""
        now = int(self.clock() * 1000)
        bar = self.bar_ms
        end = min(end_ms or now, now)
        listed = self._listed(start_ms)
        start = max(start_ms, listed or 0)
        out: dict[int, Bar] = {}
        hi = end - end % bar                               # the newest bar's open
        while hi >= start:
            lo = max(start - start % bar, hi - (PAGE - 1) * bar)
            rows = self._get(f"products/{self.product_id}/candles", {
                "granularity": GRANULARITY[self.interval], "start": lo // 1000, "end": hi // 1000})
            got = rows.get("candles")
            if not isinstance(got, list):
                raise FeedError(f"Coinbase's candles answer has no candle list: {str(rows)[:120]}")
            for r in got:
                try:
                    b = Bar(int(r["start"]) * 1000, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r["volume"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise FeedError(f"a malformed Coinbase candle: {r!r} ({exc})") from None
                if not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high and b.low > 0):
                    raise FeedError(f"an impossible Coinbase candle: {r!r}")
                if start <= b.ts <= end:
                    out[b.ts] = b
            hi = lo - bar
        bars = [out[k] for k in sorted(out)]
        newest_closed = (now - now % bar) - bar            # the forming bar's open is now - now % bar
        self.holes = missing(out, self.sessions, start, min(end - end % bar, newest_closed), bar, self.gaps)
        if len(self.holes) > MAX_HOLES:
            t0, t1 = (time.strftime('%Y-%m-%d %H:%M', time.gmtime(x / 1000)) for x in (self.holes[0], self.holes[-1]))
            raise FeedError(f"{len(self.holes)} {self.interval} candles missing from Coinbase's answer outside any "
                            f"closure ({t0} to {t1} UTC): a short answer; the next run tries again")
        if closed_only:
            bars = [b for b in bars if b.ts + bar <= now]
        return bars

    def _listed(self, start_ms: int) -> int | None:
        """When the product listed, for the history's start: its new_at, a display field. A new_at later than the
        listing desk/thresholds.py knows that would cut the window asked for is checked: if Coinbase has candles
        before it, it moved and isn't the listing (no cut)."""
        listed = _iso_ms(self.details.get("new_at")) if self.details else None
        known = _iso_ms(self.venue.listed + "Z") if self.venue.listed else None
        if listed and known and listed > known and start_ms < listed:
            lo = max(start_ms, listed - PAGE * self.bar_ms)
            rows = self._get(f"products/{self.product_id}/candles", {
                "granularity": GRANULARITY[self.interval], "start": lo // 1000, "end": (listed - self.bar_ms) // 1000})
            if rows.get("candles"):
                return None
        return listed

    # ---- the product
    def product(self) -> dict:
        """The contract's details, parsed; this run's funding snapshot goes into the log. Raises if the contract isn't
        the one the desk is set up for (its size, its venue)."""
        d = self._get(f"products/{self.product_id}")
        fut = d.get("future_product_details")
        if d.get("product_id") != self.product_id or not isinstance(fut, dict):
            raise FeedError(f"Coinbase's answer for {self.product_id} isn't that futures product: {str(d)[:160]}")
        size = _num(fut.get("contract_size"))
        if size is None or abs(size - self.venue.contract_size) > 1e-12:
            raise FeedError(f"{self.product_id}'s contract size is {fut.get('contract_size')!r}, not the "
                            f"{self.venue.contract_size:g} BTC the desk sizes for (desk/thresholds.py): "
                            "not trading on it")
        # Coinbase's order-size rules: orders the desk would size and Coinbase would refuse must not be simulated. A
        # field the answer has must agree (a missing one says nothing); a higher maximum refuses nothing.
        inc, lo, hi = (_num(d.get(k)) for k in ("base_increment", "base_min_size", "base_max_size"))
        v = self.venue
        has = {k: d.get(k) not in (None, "") for k in ("base_increment", "base_min_size", "base_max_size")}
        if (has["base_increment"] and inc != 1) or (has["base_min_size"] and lo != v.min_contracts) or \
                (has["base_max_size"] and (hi is None or hi < v.max_contracts)):
            raise FeedError(f"{self.product_id}'s order sizes changed: base_increment {d.get('base_increment')!r}, "
                            f"base_min_size {d.get('base_min_size')!r}, base_max_size {d.get('base_max_size')!r}; the "
                            f"desk sizes whole contracts from {v.min_contracts} to {v.max_contracts} "
                            "(desk/thresholds.py): not trading on it")
        ses = d.get("fcm_trading_session_details")
        ses_ok = isinstance(ses, dict) and isinstance(ses.get("is_session_open"), bool)
        ses = ses if isinstance(ses, dict) else {}
        mt = ses.get("maintenance") or {}
        m0, m1 = _iso_ms(mt.get("start_time")), _iso_ms(mt.get("end_time"))
        im, om = fut.get("intraday_margin_rate") or {}, fut.get("overnight_margin_rate") or {}
        halted = bool(d.get("trading_disabled") or d.get("cancel_only") or d.get("is_disabled"))
        # No session block (or no is_session_open in it): None, said as such, never read as "Coinbase says closed"
        open_now = (ses["is_session_open"] and not halted) if ses_ok else (False if halted else None)
        # The funding rate: future_product_details', or perpetual_details' if that one's empty
        perp = fut.get("perpetual_details") if isinstance(fut.get("perpetual_details"), dict) else {}
        rate, ftime = _num(fut.get("funding_rate")), _iso_ms(fut.get("funding_time"))
        if rate is None or ftime is None:
            rate, ftime = _num(perp.get("funding_rate")), _iso_ms(perp.get("funding_time"))
        self.details = {
            "product_id": self.product_id, "display_name": d.get("display_name"), "contract_size": size,
            "price": _num(d.get("price")), "mid": _num(d.get("mid_market_price")),
            "funding_rate": rate, "funding_time": ftime,
            "funding_interval": fut.get("funding_interval"),
            "open_interest": _num(fut.get("open_interest")),
            "margin": {"intraday_long": _num(im.get("long_margin_rate")),
                       "intraday_short": _num(im.get("short_margin_rate")),
                       "overnight_long": _num(om.get("long_margin_rate")),
                       "overnight_short": _num(om.get("short_margin_rate"))},
            "session_open": open_now, "session_state": ses.get("session_state") if ses_ok else None,
            "session_note": None if ses_ok else (
                "Coinbase's answer has no session details (fcm_trading_session_details.is_session_open); no entry "
                "until it does"),
            "session_close": _iso_ms(ses.get("close_time")),
            "maintenance": [m0, m1] if m0 and m1 and m1 > m0 else None,
            "base_increment": inc, "base_min_size": lo, "base_max_size": hi,
            "price_increment": _num(d.get("price_increment")), "new_at": d.get("new_at"),
        }
        self.log.add(self.details["funding_time"], self.details["funding_rate"])
        return self.details

    # ---- funding
    def funding(self, start_ms: int, end_ms: int | None = None) -> list[FundingEvent]:
        """One event per funding hour in (start_ms, end_ms], at the nearest rate the log knows: an estimate, as
        Coinbase keeps no funding history. Reads the product first if this run hasn't."""
        if self.details is None:
            self.product()
        if not self.log.rows:
            raise FeedError(f"no funding rate from Coinbase for {self.product_id} yet")
        return self.log.events(start_ms, end_ms or int(self.clock() * 1000),
                               self.venue.funding_interval_hours * 3600_000)

    # ---- the book
    def context(self) -> MarketContext:
        """Spread, depth (contracts x 0.01 x price, within 10 bps each side), mid, open interest, the hourly funding
        rate and whether the session is open: the live-only facts for the HARD gates.

        While the session reads closed Coinbase may have no book (404 "no pricebook found") or an empty one: that's
        expected then, and the context has the last trade's price, no spread and no depth (the session gate drops any
        entry). While it reads open, either raises."""
        if self.details is None:
            self.product()
        closed = self.details.get("session_open") is not True
        try:
            book = self._get("product_book", {"product_id": self.product_id, "limit": 100}).get("pricebook") or {}
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            if not closed:
                raise FeedError(f"Coinbase has no book for {self.product_id} ({_message(exc)}) while its session reads "
                                "open") from None
            book = {}
        bids, asks = book.get("bids") or [], book.get("asks") or []
        tick = self._get(f"products/{self.product_id}/ticker", {"limit": 1})
        if (not bids or not asks) and closed:
            trades = tick.get("trades") if isinstance(tick.get("trades"), list) else []
            last = _num(trades[0].get("price")) if trades and isinstance(trades[0], dict) else None
            mid = last or self.details.get("price") or self.details.get("mid")
            if not mid:
                raise FeedError(f"Coinbase's session for {self.product_id} reads closed and its answers have no price")
            oi = self.details.get("open_interest")
            return MarketContext(
                mid=mid, spread_bps=None, depth_usd=None,
                open_interest_usd=oi * self.venue.contract_size * mid if oi is not None else None,
                funding_rate_now=self.details.get("funding_rate"),
                funding_period_hours=float(self.venue.funding_interval_hours),
                session_open=False, session_note=self.details.get("session_note"))
        if not bids or not asks:
            raise FeedError(f"Coinbase's book for {self.product_id} is empty on one side")
        best_bid = _num(tick.get("best_bid")) or float(bids[0]["price"])
        best_ask = _num(tick.get("best_ask")) or float(asks[0]["price"])
        if not 0 < best_bid < best_ask:
            raise FeedError(f"Coinbase's best bid/ask for {self.product_id} don't make a market: {best_bid} / {best_ask}")
        mid = (best_bid + best_ask) / 2
        band = mid * 0.001
        cs = self.venue.contract_size
        bid_depth = sum(float(b["price"]) * float(b["size"]) * cs for b in bids if float(b["price"]) >= mid - band)
        ask_depth = sum(float(a["price"]) * float(a["size"]) * cs for a in asks if float(a["price"]) <= mid + band)
        oi = self.details.get("open_interest")
        return MarketContext(
            mid=mid,
            spread_bps=(best_ask - best_bid) / mid * 1e4,
            depth_usd=min(bid_depth, ask_depth),
            open_interest_usd=oi * cs * mid if oi is not None else None,
            funding_rate_now=self.details.get("funding_rate"),
            funding_period_hours=float(self.venue.funding_interval_hours),
            # no session details: closed for entries, saying why (the session gate's note)
            session_open=False if self.details.get("session_open") is None else self.details["session_open"],
            session_note=self.details.get("session_note"),
        )

    def mid(self) -> float:
        t = self._get(f"products/{self.product_id}/ticker", {"limit": 1})
        return (float(t["best_bid"]) + float(t["best_ask"])) / 2
