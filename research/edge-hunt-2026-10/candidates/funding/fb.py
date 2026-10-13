"""Shared helpers for the funding/basis hunter (uses harness for data, sim and scoring)."""
import sys, math
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import numpy as np, pandas as pd
import harness as H
from functools import lru_cache

IS_START, IS_END, OOS_END = H.IS_START, H.IS_END, H.OOS_END


def entries_from_weights(w: pd.DataFrame) -> int:
    """New legs opened: a coin's sign goes from 0/other sign to a non-zero sign (held exposure)."""
    s = np.sign(w.fillna(0.0))
    prev = s.shift(1).fillna(0.0)
    return int(((s != 0) & (s != prev)).sum().sum())


def pstats(res: pd.DataFrame, w: pd.DataFrame | None, start, end) -> dict:
    st = H.stats(res, start, end)
    if w is not None and st:
        ww = w.shift(1)  # held
        ww = ww[(ww.index >= start) & (ww.index < end)]
        st["entries"] = entries_from_weights(ww)
    return st


def short(st: dict) -> dict:
    keys = ("return", "sharpe", "max_dd", "entries", "funding_paid", "cost_paid", "cagr")
    return {k: (round(st[k], 4) if isinstance(st.get(k), float) else st.get(k)) for k in keys if k in st}


@lru_cache(maxsize=None)
def daily_k(coin):
    return H.klines(coin, "1d")


@lru_cache(maxsize=None)
def spot_1h(coin: str) -> pd.DataFrame:
    """Binance spot 1h with the 2025 microsecond-timestamp switch fixed (harness H.spot breaks after 2025-01)."""
    d = H.DATA / "spot1h" / f"{coin.upper()}USDT"
    parts = []
    for p in sorted(d.glob("*.zip")):
        df = H._read_zip(p).iloc[:, :12]
        df.columns = H.KCOLS
        parts.append(df)
    df = pd.concat(parts).drop(columns=["ignore", "close_time"])
    t = df["open_time"].astype("int64")
    t = np.where(t > 1e14, t // 1000, t)
    df["open_time"] = pd.to_datetime(t, unit="ms", utc=True)
    return df.drop_duplicates("open_time").set_index("open_time").sort_index().astype(float)


def spot(coin, interval):
    return H._resample(spot_1h(coin), interval)


def sharpe(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else 0.0
