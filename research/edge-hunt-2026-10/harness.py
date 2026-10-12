"""Shared edge-hunt harness. Every hunter uses THIS for loading data, simulating and scoring, so results
are comparable and the timing/cost rules can't drift between agents.

TIMING (no look-ahead): pos[t] is decided with data up to and including the CLOSE of bar t. It is filled at
the OPEN of bar t+1 and held through bar t+1. Indicators must only use rows <= t (use .shift(1) when in doubt;
a rolling max that includes bar t is fine for a decision made at t's close, NOT for one made at t's open).

COSTS (same as the live desk, desk/thresholds.py Costs): 4.5 bps taker fee + 3.0 bps slippage per side, charged
on every change of exposure (|delta pos| x 7.5 bps). Funding: real Binance prints; a long pays a positive rate
(ret -= pos * rate) for every funding timestamp inside the bar it is held. Exposure is a fraction of equity
(1.0 = 1x notional, -0.5 = half-size short); drift rebalancing between changes is not charged.

SPLIT (pre-registered): in-sample 2020-01-01 -> 2023-12-31 is the ONLY data used to choose rules/parameters.
Out-of-sample 2024-01-01 -> 2026-09-30 is evaluated once per final candidate.

PASS RULE (pre-registered, before any results were seen) — a new strategy PASSES only if all hold:
  P1  parameters chosen on in-sample only; report how many final candidates were taken to out-of-sample
  P2  out-of-sample: net return > 0 and daily Sharpe >= 0.5 (base costs)
  P3  in-sample daily Sharpe >= 0.7
  P4  >= 30 entries in-sample and >= 20 out-of-sample
  P5  out-of-sample net return still > 0 at 2x costs
  P6  robustness: over the pre-declared +/-1 step neighbour grid, median OOS Sharpe >= 0.25 and >= 2/3 of
      neighbours have positive OOS return
  P7  max drawdown <= 35% at the sizing reported (vol-target it if needed)
  P8  additive: daily-return correlation with the current desk (baseline_daily.csv) <= 0.6, OR a 50/50 blend with
      the desk has a higher OOS Sharpe than the desk alone
PLAUSIBLE = passes P1-P5 and P7 but misses P6 or P8, or has a thin sample.
"""
from __future__ import annotations

import io
import math
import os
import zipfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
IS_START = pd.Timestamp("2020-01-01", tz="UTC")
IS_END = pd.Timestamp("2024-01-01", tz="UTC")       # exclusive
OOS_END = pd.Timestamp(os.environ.get("EDGE_END", "2026-10-01"), tz="UTC")   # exclusive; re-audits set EDGE_END
NEW_START = pd.Timestamp("2026-10-01", tz="UTC")    # data after the Oct 2026 audit: never seen by anyone
FEE_BPS, SLIP_BPS = 4.5, 3.0
UNIVERSE = "BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT LTC BCH TRX ATOM NEAR UNI AAVE FIL ETC XLM SUI APT ARB OP INJ".split()
KCOLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
         "taker_buy_base", "taker_buy_quote", "ignore"]


def _read_zip(p: Path) -> pd.DataFrame:
    with zipfile.ZipFile(p) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    header = not first.split(",")[0].strip().lstrip("-").isdigit()
    return pd.read_csv(io.StringIO(raw), header=0 if header else None)


def _klines_dir(d: Path) -> pd.DataFrame:
    parts = []
    for p in sorted(d.glob("*.zip")):
        df = _read_zip(p)
        df = df.iloc[:, :12]
        df.columns = KCOLS
        parts.append(df)
    if not parts:
        raise FileNotFoundError(d)
    df = pd.concat(parts).drop(columns=["ignore", "close_time"])
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df = df.drop_duplicates("open_time").set_index("open_time").sort_index().astype(float)
    return df


@lru_cache(maxsize=None)
def _k1h(coin: str, kind: str = "klines1h") -> pd.DataFrame:
    return _klines_dir(DATA / kind / f"{coin}USDT")


def _resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval == "1h":
        return df.copy()
    rule = {"2h": "2h", "4h": "4h", "8h": "8h", "12h": "12h", "1d": "1D", "1w": "W-MON"}[interval]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "quote_volume": "sum",
           "trades": "sum", "taker_buy_base": "sum", "taker_buy_quote": "sum"}
    agg = {k: v for k, v in agg.items() if k in df.columns}
    out = df.resample(rule, label="left", closed="left").agg(agg)
    # drop incomplete buckets (missing hours)
    n = df["close"].resample(rule, label="left", closed="left").count()
    need = {"2h": 2, "4h": 4, "8h": 8, "12h": 12, "1d": 24, "1w": 168}[interval]
    return out[n >= need].dropna(subset=["close"])


def klines(coin: str, interval: str = "2h") -> pd.DataFrame:
    """Binance USDT-M perp candles, index = bar OPEN time (UTC). Columns: open high low close volume quote_volume
    trades taker_buy_base taker_buy_quote. Resampled from 1h; only complete bars kept."""
    return _resample(_k1h(coin.upper()), interval)


def spot(coin: str, interval: str = "1h") -> pd.DataFrame:
    """Binance SPOT candles (BTC/ETH/SOL only)."""
    return _resample(_k1h(coin.upper(), "spot1h"), interval)


@lru_cache(maxsize=None)
def premium(coin: str) -> pd.DataFrame:
    """Binance premium-index 1h klines (perp vs index basis, as a fraction) for BTC/ETH/SOL: open high low close."""
    d = DATA / "premium1h" / f"{coin.upper()}USDT"
    parts = []
    for p in sorted(d.glob("*.zip")):
        df = _read_zip(p).iloc[:, :5]
        df.columns = ["open_time", "open", "high", "low", "close"]
        parts.append(df)
    df = pd.concat(parts)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    return df.drop_duplicates("open_time").set_index("open_time").sort_index().astype(float)


@lru_cache(maxsize=None)
def funding(coin: str) -> pd.Series:
    """Binance funding prints: index = funding time (UTC), value = rate per print (0.0001 = 0.01%)."""
    d = DATA / "funding" / f"{coin.upper()}USDT"
    parts = []
    for p in sorted(d.glob("*.zip")):
        df = _read_zip(p)
        parts.append(pd.DataFrame({"t": df.iloc[:, 0].astype("int64"), "r": df.iloc[:, 2].astype(float)}))
    df = pd.concat(parts).drop_duplicates("t")
    s = pd.Series(df["r"].values, index=pd.to_datetime(df["t"], unit="ms", utc=True)).sort_index()
    return s


@lru_cache(maxsize=None)
def metrics(coin: str) -> pd.DataFrame:
    """Binance daily 'metrics' files, 5-minute rows from 2020-09 (BTC/ETH/SOL): sum_open_interest,
    sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
    count_long_short_ratio, sum_taker_long_short_vol_ratio. Index = create_time (UTC).
    CAUTION: a row stamped T describes the 5 minutes ending ~T; to use it for a decision at a bar close, take
    rows with index <= bar close."""
    d = DATA / "metrics" / f"{coin.upper()}USDT"
    parts = [_read_zip(p) for p in sorted(d.glob("*.zip"))]
    df = pd.concat(parts)
    df["create_time"] = pd.to_datetime(df["create_time"], utc=True)
    df = df.drop(columns=[c for c in ("symbol",) if c in df.columns])
    return df.drop_duplicates("create_time").set_index("create_time").sort_index().apply(pd.to_numeric, errors="coerce")


def bar_funding(df: pd.DataFrame, coin: str, interval: str) -> pd.Series:
    """Sum of funding rates whose timestamp falls in (bar open, bar open + interval] — paid by the bar's holder."""
    f = funding(coin)
    step = pd.Timedelta(interval.replace("1d", "1D").replace("1w", "7D"))
    idx = df.index
    t = f.index.round("1min")                       # prints land a few ms after the hour
    p = idx.searchsorted(t - step, side="left")      # candidate bar: open >= t - step
    out = np.zeros(len(idx))
    ok = p < len(idx)
    pi = np.where(ok, p, 0)
    ok &= (idx[pi] < t) & (t <= idx[pi] + step)      # print inside (open, open + step]
    np.add.at(out, pi[ok], f.values[ok])
    return pd.Series(out, index=idx)


def panel(coins, interval: str = "1d", field: str = "close") -> pd.DataFrame:
    return pd.DataFrame({c: klines(c, interval)[field] for c in coins})


# ------------------------------------------------------------------ simulation

def backtest(df: pd.DataFrame, pos: pd.Series, fund: pd.Series | None = None, cost_mult: float = 1.0,
             fee_bps: float = FEE_BPS, slip_bps: float = SLIP_BPS) -> pd.DataFrame:
    """Single-asset. df = klines(...), pos = target exposure decided at each bar's close (index aligned to df).
    fund = bar_funding(df, coin, interval) (strongly recommended). Returns a frame with per-bar columns
    h (held exposure), gross, cost, fund, ret (net), equity."""
    pos = pos.reindex(df.index).fillna(0.0).astype(float)
    h = pos.shift(1).fillna(0.0)                        # held during bar t = decided at close of t-1
    o, c = df["open"], df["close"]
    prev_c = c.shift(1)
    gap = (o / prev_c - 1).fillna(0.0)                  # previous holding carries the close->open gap
    intrabar = c / o - 1
    gross = h.shift(1).fillna(0.0) * gap + h * intrabar
    cost = (h - h.shift(1).fillna(0.0)).abs() * (fee_bps + slip_bps) / 1e4 * cost_mult
    fpay = (h * fund.reindex(df.index).fillna(0.0)) if fund is not None else 0.0 * h
    ret = gross - cost - fpay
    out = pd.DataFrame({"h": h, "gross": gross, "cost": cost, "fund": fpay, "ret": ret})
    out["equity"] = (1 + out["ret"]).cumprod()
    return out


def backtest_portfolio(dfs: dict, weights: pd.DataFrame, interval: str, cost_mult: float = 1.0,
                       with_funding: bool = True) -> pd.DataFrame:
    """Multi-asset. weights: DataFrame (index = bar open times, columns = coins) of target exposures decided at
    each bar's close. Sums single-asset P&L (each leg uses the same timing/cost rules)."""
    idx = weights.index
    tot = None
    for coin in weights.columns:
        df = dfs[coin].reindex(idx)
        w = weights[coin].fillna(0.0)
        valid = df["close"].notna()
        w = w.where(valid, 0.0)
        dfc = df.ffill()
        fund = bar_funding(dfs[coin], coin, interval).reindex(idx).fillna(0.0) if with_funding else None
        r = backtest(dfc, w, fund, cost_mult=cost_mult)[["gross", "cost", "fund", "ret"]].fillna(0.0)
        r["turn"] = (w.shift(1).fillna(0) - w.shift(2).fillna(0)).abs()
        tot = r if tot is None else tot + r
    tot["equity"] = (1 + tot["ret"]).cumprod()
    tot["gross_exposure"] = weights.abs().sum(axis=1).shift(1).fillna(0.0)
    return tot


# ------------------------------------------------------------------ scoring

def daily(res: pd.DataFrame) -> pd.Series:
    """Daily net returns from a per-bar result."""
    return (1 + res["ret"]).resample("1D").prod() - 1


def _entries(h: pd.Series) -> int:
    s = np.sign(h.fillna(0.0).values)
    prev = np.concatenate([[0.0], s[:-1]])
    return int(((s != 0) & (s != prev)).sum())


def _trade_pf(res: pd.DataFrame) -> float:
    if "h" not in res:
        return float("nan")
    s = np.sign(res["h"].values)
    grp = np.cumsum(np.concatenate([[1], (s[1:] != s[:-1]).astype(int)]))
    g = pd.DataFrame({"g": grp, "s": s, "r": res["ret"].values})
    pnl = g[g.s != 0].groupby("g")["r"].sum()
    w, l = pnl[pnl > 0].sum(), -pnl[pnl <= 0].sum()
    return float(w / l) if l > 0 else float("inf")


def stats(res: pd.DataFrame, start=None, end=None) -> dict:
    r = res.loc[(res.index >= (start or res.index[0])) & (res.index < (end or res.index[-1] + pd.Timedelta(1)))]
    if len(r) < 2:
        return {}
    d = daily(r)
    eq = (1 + d).cumprod()
    yrs = (r.index[-1] - r.index[0]).total_seconds() / (365.25 * 86400)
    tot = float(eq.iloc[-1] - 1)
    sd = d.std()
    out = {
        "from": str(r.index[0].date()), "to": str(r.index[-1].date()), "years": round(yrs, 2),
        "return": tot, "cagr": (1 + tot) ** (1 / yrs) - 1 if yrs > 0.2 and tot > -1 else None,
        "sharpe": float(d.mean() / sd * math.sqrt(365)) if sd > 0 else 0.0,
        "max_dd": float((1 - eq / eq.cummax()).max()),
        "cost_paid": float(r["cost"].sum()), "funding_paid": float(r["fund"].sum()),
    }
    if "h" in r:
        out.update({"entries": _entries(r["h"]), "exposure": float((r["h"] != 0).mean()),
                    "avg_abs_pos": float(r["h"].abs().mean()), "trade_pf": _trade_pf(r)})
    if "turn" in r:
        out["turnover_per_year"] = float(r["turn"].sum() / yrs) if yrs > 0 else None
    return out


def split_stats(res: pd.DataFrame) -> dict:
    return {"is": stats(res, IS_START, IS_END), "oos": stats(res, IS_END, OOS_END), "full": stats(res, IS_START, OOS_END)}


def per_year(res: pd.DataFrame) -> dict:
    d = daily(res)
    return {int(y): float((1 + g).prod() - 1) for y, g in d.groupby(d.index.year)}


@lru_cache(maxsize=None)
def baseline_daily() -> pd.DataFrame:
    """The CURRENT desk's daily returns (desk.backtest, Binance 2h, live params): columns BTC, ETH, SOL and
    'desk' = equal-weight blend of the three $50 accounts."""
    return pd.read_csv(ROOT / "baseline_daily.csv", index_col=0, parse_dates=True)


def correlation_with_desk(res: pd.DataFrame, which: str = "desk") -> float:
    d = daily(res)
    b = baseline_daily()[which]
    j = pd.concat([d, b], axis=1, join="inner").dropna()
    return float(j.corr().iloc[0, 1])


def blend_check(res: pd.DataFrame, which: str = "desk") -> dict:
    """OOS Sharpe of the desk alone vs a 50/50 daily-rebalanced blend with this strategy."""
    d = daily(res)
    b = baseline_daily()[which]
    j = pd.concat([d.rename("s"), b.rename("b")], axis=1, join="inner").dropna()
    j = j[(j.index >= IS_END) & (j.index < OOS_END)]

    def sh(x):
        return float(x.mean() / x.std() * math.sqrt(365)) if x.std() > 0 else 0.0
    return {"desk_oos_sharpe": sh(j["b"]), "strategy_oos_sharpe": sh(j["s"]), "blend_oos_sharpe": sh(0.5 * j["s"] + 0.5 * j["b"]),
            "oos_corr": float(j.corr().iloc[0, 1])}


def fmt(s: dict) -> str:
    if not s:
        return "—"
    keys = ("return", "cagr", "sharpe", "max_dd", "entries", "trade_pf", "exposure", "turnover_per_year")
    parts = []
    for k in keys:
        v = s.get(k)
        if v is None:
            continue
        parts.append(f"{k}={v:+.1%}" if k in ("return", "cagr", "max_dd", "exposure") else f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}")
    return f"{s['from']}..{s['to']} " + " ".join(parts)
