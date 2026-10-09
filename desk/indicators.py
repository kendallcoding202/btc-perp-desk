"""Indicators. Pure Python, no numpy, so backtest and live compute identical numbers.

Every series is aligned to the input bars: value[i] uses bars[0..i] only (no look-ahead).
Entries that are not yet warmed up are None.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Bar, FundingEvent
from .thresholds import INTERVAL_SECONDS, Params


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (n + 1)
    acc = values[0]
    for i, v in enumerate(values):
        acc = v if i == 0 else alpha * v + (1 - alpha) * acc
        out[i] = acc if i >= n - 1 else None
    return out


def true_range(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: list[Bar], n: int) -> list[float | None]:
    """Wilder's ATR, seeded with the simple mean of the first n true ranges."""
    tr = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n:
        return out
    acc = sum(tr[:n]) / n
    out[n - 1] = acc
    for i in range(n, len(bars)):
        acc = (acc * (n - 1) + tr[i]) / n
        out[i] = acc
    return out


def donchian(bars: list[Bar], n: int) -> tuple[list[float | None], list[float | None]]:
    """Highest high / lowest low of the PRIOR n bars (the current bar is excluded)."""
    hi: list[float | None] = [None] * len(bars)
    lo: list[float | None] = [None] * len(bars)
    for i in range(n, len(bars)):
        window = bars[i - n:i]
        hi[i] = max(b.high for b in window)
        lo[i] = min(b.low for b in window)
    return hi, lo


def efficiency_ratio(closes: list[float], n: int) -> list[float | None]:
    """Kaufman efficiency ratio: net move / path length over n bars. 1 = straight line, 0 = pure chop."""
    out: list[float | None] = [None] * len(closes)
    for i in range(n, len(closes)):
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(closes[i] - closes[i - n]) / path if path > 0 else 0.0
    return out


def funding_per_bar(bars: list[Bar], funding: list[FundingEvent], bar_ms: int) -> list[float]:
    """Sum of funding rates charged while each bar was open: events with bar.ts < ts <= bar.ts + bar_ms."""
    out = [0.0] * len(bars)
    if not bars or not funding:
        return out
    events = sorted(funding, key=lambda e: e.ts)
    j = 0
    for i, b in enumerate(bars):
        start, end = b.ts, b.ts + bar_ms
        while j < len(events) and events[j].ts <= start:
            j += 1
        k = j
        total = 0.0
        while k < len(events) and events[k].ts <= end:
            total += events[k].rate
            k += 1
        out[i] = total
    return out


@dataclass
class Features:
    """Everything the agents look at, one value per bar."""
    bars: list[Bar]
    ema_fast: list[float | None]
    ema_slow: list[float | None]
    atr: list[float | None]
    don_hi: list[float | None]
    don_lo: list[float | None]
    efficiency: list[float | None]
    funding_bar: list[float]          # funding rate summed over each bar
    funding_apr: list[float]          # trailing 3-day average, annualised, in %
    bar_ms: int

    def __len__(self) -> int:
        return len(self.bars)


def compute(bars: list[Bar], funding: list[FundingEvent], p: Params) -> Features:
    s = p.strategy
    bar_s = INTERVAL_SECONDS[p.market.interval]
    closes = [b.close for b in bars]
    hi, lo = donchian(bars, s.breakout_bars)
    fbar = funding_per_bar(bars, funding, bar_s * 1000)

    bars_per_year = 365 * 86400 / bar_s
    window = max(1, int(round(3 * 86400 / bar_s)))
    apr: list[float] = []
    running = 0.0
    for i, f in enumerate(fbar):
        running += f
        if i >= window:
            running -= fbar[i - window]
        n = min(i + 1, window)
        apr.append(running / n * bars_per_year * 100.0)

    return Features(
        bars=bars,
        ema_fast=ema(closes, s.ema_fast),
        ema_slow=ema(closes, s.ema_slow),
        atr=atr(bars, s.atr_bars),
        don_hi=hi,
        don_lo=lo,
        efficiency=efficiency_ratio(closes, s.breakout_bars),
        funding_bar=fbar,
        funding_apr=apr,
        bar_ms=bar_s * 1000,
    )


# ---- score mappings: raw numbers -> 0..1 scores the soft gates compare against ----

def trend_strength(ema_fast: float, ema_slow: float, atr_value: float, direction: int) -> float:
    """0 when the EMAs disagree with `direction`; rises toward 1 as they separate (in ATRs)."""
    if atr_value <= 0:
        return 0.0
    spread = (ema_fast - ema_slow) / atr_value * direction
    return math.tanh(spread / 2.0) if spread > 0 else 0.0


def extension(close: float, ema_fast: float, atr_value: float, direction: int) -> float:
    """How far price has already run from the fast EMA in our direction. ~0.8 at 4.4 ATRs."""
    if atr_value <= 0:
        return 0.0
    ext = (close - ema_fast) / atr_value * direction
    return math.tanh(max(0.0, ext) / 4.0)


def crowding(funding_apr: float, direction: int) -> float:
    """0.5 = neutral funding. ->1 when we'd be joining a crowd paying heavily to hold our side."""
    return 0.5 + 0.5 * math.tanh(funding_apr * direction / 60.0)
