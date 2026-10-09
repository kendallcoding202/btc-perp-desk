"""CRAWLER (history): BTCUSDT perpetual candles + funding from data.binance.vision.

Binance's public archive goes back to the perp's launch (Sept 2019), which gives
the backtest years of real perp prices and real funding prints — far more than
any exchange's live API will hand you. Files are cached under data/.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from ..models import Bar, FundingEvent

BASE = "https://data.binance.vision/data/futures/um"


def _get(url: str, cache: Path, refresh: bool = False) -> bytes | None:
    if cache.exists() and not refresh:
        return cache.read_bytes()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def _rows(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0].strip().lstrip("-").isdigit():
        rows = rows[1:]  # newer files have a header row, older ones don't
    return rows


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def load(symbol: str = "BTCUSDT", interval: str = "4h", start: str = "2020-01", end: str | None = None,
         data_dir: str | Path = "data", log=print) -> tuple[list[Bar], list[FundingEvent]]:
    """Complete months only. Each month is downloaded once and cached under data/binance/."""
    data_dir = Path(data_dir) / "binance"
    today = datetime.now(timezone.utc).date()
    start_d = datetime.strptime(start, "%Y-%m").date()
    end_d = datetime.strptime(end, "%Y-%m").date() if end else today

    bars: dict[int, Bar] = {}
    funding: dict[int, FundingEvent] = {}
    this_month = (today.year, today.month)
    for y, m in _months(start_d, end_d):
        tag = f"{y}-{m:02d}"
        if (y, m) >= this_month:
            continue  # monthly files appear after the month closes; the live feed covers the rest
        kname = f"{symbol}-{interval}-{tag}.zip"
        blob = _get(f"{BASE}/monthly/klines/{symbol}/{interval}/{kname}", data_dir / "klines" / kname)
        if blob is None:
            log(f"  no klines for {tag}")
        for r in _rows(blob) if blob else []:
            b = Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            bars[b.ts] = b
        fname = f"{symbol}-fundingRate-{tag}.zip"
        blob = _get(f"{BASE}/monthly/fundingRate/{symbol}/{fname}", data_dir / "funding" / fname)
        for r in _rows(blob) if blob else []:
            e = FundingEvent(int(r[0]), float(r[2]))
            funding[e.ts] = e
    out_bars = [bars[k] for k in sorted(bars)]
    out_funding = [funding[k] for k in sorted(funding)]
    log(f"  crawler: {len(out_bars):,} {interval} bars, {len(out_funding):,} funding prints "
        f"({_fmt(out_bars[0].ts) if out_bars else '-'} → {_fmt(out_bars[-1].ts) if out_bars else '-'})")
    return out_bars, out_funding


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
