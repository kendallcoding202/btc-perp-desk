"""(d) funding-aware overlay variants on the real desk engine (BTC/ETH/SOL live params, equal-weight desk).
Usage: python3 d_overlay.py [--end 2024-01-01]  (default: IS only)."""
import sys, os, json, math
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from fb import *
os.chdir(str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from desk import backtest as bt, indicators as ind, filter as flt
from desk.__main__ import _history
from desk.thresholds import params_for

_orig_compute, _orig_crowd = ind.compute, ind.crowding


class TF(float):
    rel = 0


def install(q=None, mode="replace", window_days=365, min_days=180):
    """q=None -> stock engine. Else block a long when 3d funding APR > its trailing-window q quantile, a short when < (1-q)."""
    if q is None:
        bt.ind.compute = _orig_compute; flt.ind.crowding = _orig_crowd; return

    def compute(bars, funding, p):
        f = _orig_compute(bars, funding, p)
        s = pd.Series(f.funding_apr)
        w = int(window_days * 86400e3 / f.bar_ms); m = int(min_days * 86400e3 / f.bar_ms)
        past = s.shift(1).rolling(w, min_periods=m)
        hi, lo = past.quantile(q), past.quantile(1 - q)
        out = []
        for v, h_, l_ in zip(s.values, hi.values, lo.values):
            t = TF(v); t.rel = 1 if (not math.isnan(h_) and v > h_) else (-1 if (not math.isnan(l_) and v < l_) else 0)
            out.append(t)
        f.funding_apr = out
        return f

    def crowd(apr, d):
        base = 0.5 if mode == "replace" else _orig_crowd(apr, d)
        return 1.0 if getattr(apr, "rel", 0) * d > 0 else base

    bt.ind.compute = compute; flt.ind.crowding = crowd


_hist = {}
def desk_run(soft=None, q=None, mode="replace", end=None, cost_mult=1.0):
    install(q, mode)
    daily, ntr = {}, {}
    for coin in ("BTC", "ETH", "SOL"):
        p = params_for(coin)
        if soft is not None:
            p = p.with_(soft=soft)
        if cost_mult != 1.0:
            p = p.with_(costs={"taker_fee_bps": 4.5 * cost_mult, "slippage_bps": 3.0 * cost_mult})
        if coin not in _hist:
            _hist[coin] = _history(p, "binance", "2020-01", None)
        bars, funding = _hist[coin]
        if end is not None:
            e = int(end.timestamp() * 1000)
            bars = [b for b in bars if b.ts < e]; funding = [x for x in funding if x.ts < e]
        r = bt.run(bars, funding, p)
        s = pd.Series([e for _, e in r.curve], index=pd.to_datetime([t for t, _ in r.curve], unit="ms", utc=True))
        daily[coin] = s.resample("1D").last().ffill().pct_change()
        ntr[coin] = pd.Series(1, index=pd.to_datetime([t.entry_ts for t in r.broker.trades], unit="ms", utc=True))
    install(None)
    df = pd.DataFrame(daily)
    df["desk"] = df[["BTC", "ETH", "SOL"]].mean(axis=1, skipna=True)
    return df.dropna(how="all"), ntr


def seg(df, ntr, a, b, col="desk"):
    x = df[col][(df.index >= a) & (df.index < b)].dropna()
    eq = (1 + x).cumprod()
    n = sum(int(((s.index >= a) & (s.index < b)).sum()) for s in ntr.values())
    return {"return": round(float(eq.iloc[-1] - 1), 4), "sharpe": round(sharpe(x), 3),
            "max_dd": round(float((1 - eq / eq.cummax()).max()), 4), "entries": n,
            **{f"sh_{c}": round(sharpe(df[c][(df.index >= a) & (df.index < b)]), 3) for c in ("BTC", "ETH", "SOL")}}


if __name__ == "__main__":
    variants = [("current_0.85", dict(soft=None)), ("crowd_off", dict(soft={"max_crowding": None}))]
    variants += [(f"crowd_{t}", dict(soft={"max_crowding": t})) for t in (0.75, 0.80, 0.90, 0.95)]
    variants += [(f"rel_q{q}_{m}", dict(q=q, mode=m)) for m in ("replace", "add") for q in (0.80, 0.90, 0.95)]
    rows = []
    for name, kw in variants:
        df, ntr = desk_run(end=IS_END, **kw)
        rows.append({"variant": name, **seg(df, ntr, IS_START, IS_END)})
        print(json.dumps(rows[-1]), flush=True)
    pd.DataFrame(rows).to_csv(str(__import__("pathlib").Path(__file__).with_name("d_overlay_is.csv")), index=False)
