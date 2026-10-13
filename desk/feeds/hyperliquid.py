"""CRAWLER (live): Hyperliquid public info API — candles, funding, book, open interest.

No API key is needed to read. Hyperliquid funds hourly; the desk sums funding
per bar, so hourly (Hyperliquid) and 8-hourly (Binance) prints both just work.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..models import Bar, FundingEvent, MarketContext
from ..thresholds import INTERVAL_SECONDS

URL = "https://api.hyperliquid.xyz/info"


class HyperliquidFeed:
    def __init__(self, coin: str = "BTC", interval: str = "4h", timeout: float = 15.0):
        self.coin = coin
        self.interval = interval
        self.timeout = timeout
        self.requests = 0
        self.last_latency_ms = 0.0

    def _post(self, body: dict, retries: int = 4):
        """POST with backoff on 429/5xx: Hyperliquid rate-limits by IP weight, and cloud IPs are often shared."""
        for attempt in range(retries + 1):
            req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            t = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise
                time.sleep(min(30.0, 2.0 * 2 ** attempt))
                continue
            self.requests += 1
            self.last_latency_ms = (time.time() - t) * 1000
            return out

    def candles(self, start_ms: int = 0, end_ms: int | None = None, closed_only: bool = True) -> list[Bar]:
        """Up to the most recent 5000 candles. `closed_only` drops the bar that is still forming."""
        now = int(time.time() * 1000)
        rows = self._post({"type": "candleSnapshot", "req": {
            "coin": self.coin, "interval": self.interval, "startTime": start_ms, "endTime": end_ms or now}})
        bars = []
        for r in rows:
            if closed_only and int(r["T"]) >= now:
                continue
            bars.append(Bar(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])))
        return bars

    def funding(self, start_ms: int, end_ms: int | None = None) -> list[FundingEvent]:
        """Funding prints since start_ms (paginates; the API returns at most 500 per call)."""
        out: list[FundingEvent] = []
        cursor = start_ms
        end_ms = end_ms or int(time.time() * 1000)
        while cursor < end_ms:
            rows = self._post({"type": "fundingHistory", "coin": self.coin, "startTime": cursor, "endTime": end_ms})
            if not rows:
                break
            for r in rows:
                out.append(FundingEvent(int(r["time"]), float(r["fundingRate"])))
            last = int(rows[-1]["time"])
            if last <= cursor or len(rows) < 500:
                break
            cursor = last + 1
        dedup = {e.ts: e for e in out}
        return [dedup[k] for k in sorted(dedup)]

    def context(self) -> MarketContext:
        """Live order-book and positioning facts for the HARD gates."""
        meta, ctxs = self._post({"type": "metaAndAssetCtxs"})
        idx = [u["name"] for u in meta["universe"]].index(self.coin)
        c = ctxs[idx]
        book = self._post({"type": "l2Book", "coin": self.coin})
        bids, asks = book["levels"][0], book["levels"][1]
        best_bid, best_ask = float(bids[0]["px"]), float(asks[0]["px"])
        mid = (best_bid + best_ask) / 2
        band = mid * 0.001  # 10 bps
        bid_depth = sum(float(b["px"]) * float(b["sz"]) for b in bids if float(b["px"]) >= mid - band)
        ask_depth = sum(float(a["px"]) * float(a["sz"]) for a in asks if float(a["px"]) <= mid + band)
        return MarketContext(
            mid=mid,
            spread_bps=(best_ask - best_bid) / mid * 1e4,
            depth_usd=min(bid_depth, ask_depth),
            open_interest_usd=float(c["openInterest"]) * float(c["markPx"]),
            funding_rate_now=float(c["funding"]),
            funding_period_hours=1.0,
        )

    def mid(self) -> float:
        return float(self._post({"type": "allMids"})[self.coin])

    def price_at(self, ts: int) -> float | None:
        """The coin's price at `ts` (ms): the open of the 1-minute candle it falls in, or of the hour's once the
        minute is too old (Hyperliquid serves the last 5,000 candles of each interval: about 3.5 days of minutes, 208
        days of hours). None if neither has it. Live prices a deposit it reads late with this (brokers/live.py)."""
        for interval, ms in (("1m", 60_000), ("1h", 3600_000)):
            t0 = ts - ts % ms
            rows = self._post({"type": "candleSnapshot", "req": {
                "coin": self.coin, "interval": interval, "startTime": t0, "endTime": t0 + ms - 1}})
            for r in rows or []:
                if int(r["t"]) <= ts <= int(r["T"]):
                    return float(r["o"])
        return None

    @property
    def bar_ms(self) -> int:
        return INTERVAL_SECONDS[self.interval] * 1000
