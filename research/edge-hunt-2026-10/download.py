"""Shared research data: Binance USDT-M perp 1h klines + funding (25 coins), premium index 1h and
daily positioning metrics (BTC/ETH/SOL). Cached under edge/data, never re-downloaded."""
import sys, urllib.request, urllib.error
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).parent / "data"
B = "https://data.binance.vision/data/futures/um"
UNIVERSE = "BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT LTC BCH TRX ATOM NEAR UNI AAVE FIL ETC XLM SUI APT ARB OP INJ".split()
_now = datetime.now(timezone.utc)
LAST = (_now.year, _now.month - 1) if _now.month > 1 else (_now.year - 1, 12)   # last complete month
months = [f"{y}-{m:02d}" for y in range(2020, LAST[0] + 1) for m in range(1, 13) if (y, m) <= LAST]
jobs = []
for c in UNIVERSE:
    s = c + "USDT"
    for t in months:
        jobs.append((f"{B}/monthly/klines/{s}/1h/{s}-1h-{t}.zip", ROOT / "klines1h" / s / f"{t}.zip"))
        jobs.append((f"{B}/monthly/fundingRate/{s}/{s}-fundingRate-{t}.zip", ROOT / "funding" / s / f"{t}.zip"))
for c in ("BTC", "ETH", "SOL"):
    s = c + "USDT"
    for t in months:
        jobs.append((f"{B}/monthly/premiumIndexKlines/{s}/1h/{s}-1h-{t}.zip", ROOT / "premium1h" / s / f"{t}.zip"))
        jobs.append((f"https://data.binance.vision/data/spot/monthly/klines/{s}/1h/{s}-1h-{t}.zip", ROOT / "spot1h" / s / f"{t}.zip"))
    d = date(2020, 9, 1)
    last_day = date(_now.year, _now.month, 1) - timedelta(days=1)
    while d <= last_day:
        jobs.append((f"{B}/daily/metrics/{s}/{s}-metrics-{d}.zip", ROOT / "metrics" / s / f"{d}.zip"))
        d += timedelta(days=1)
def get(job):
    url, path = job
    if path.exists() or path.with_suffix(".404").exists():
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                path.write_bytes(r.read()); return 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                path.with_suffix(".404").write_text(""); return 0
        except Exception:
            pass
    print("FAILED", url, file=sys.stderr); return 0
with ThreadPoolExecutor(24) as ex:
    n = sum(ex.map(get, jobs))
print("downloaded", n, "of", len(jobs), "jobs")
