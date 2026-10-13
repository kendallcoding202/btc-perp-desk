import sys, json, math
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import harness as H
COINS = ["BTC", "ETH", "SOL"]
BPD = {"2h": 12, "4h": 6, "1d": 1}
STEP = {"2h": "2h", "4h": "4h", "1d": "1D"}
_K = {}
def kl(c, iv):
    if (c, iv) not in _K: _K[(c, iv)] = H.klines(c, iv)
    return _K[(c, iv)]
_M = {}
def metrics_at_close(c, iv, lag_min=5):
    """last metrics row stamped <= bar close - lag_min (index = bar open time); zeros -> NaN; stale (>1h) -> NaN"""
    if (c, iv) in _M: return _M[(c, iv)]
    m = H.metrics(c); df = kl(c, iv)
    t = df.index + pd.Timedelta(STEP[iv]) - pd.Timedelta(minutes=lag_min)
    p = m.index.searchsorted(t, side="right") - 1
    ok = p >= 0; pp = np.where(ok, p, 0)
    vals = np.where(ok[:, None], m.values[pp], np.nan)
    out = pd.DataFrame(vals, index=df.index, columns=m.columns)
    out[(~ok) | ((t - m.index[pp]) > pd.Timedelta("1h"))] = np.nan
    out = out[out.index >= m.index[0]].astype(float)
    _M[(c, iv)] = out.where(out > 0)
    return _M[(c, iv)]
def zroll(x, win):
    mp = max(10, win // 3)
    return (x - x.rolling(win, min_periods=mp).mean()) / x.rolling(win, min_periods=mp).std()
def hold_signal(raw, h):
    s = raw.replace(0, np.nan)
    return (s.ffill(limit=h - 1) if h > 1 else s).fillna(0.0)
def portfolio(iv, sigf, cost_mult=1.0):
    dfs = {c: kl(c, iv) for c in COINS}
    idx = dfs["BTC"].index
    W = pd.DataFrame({c: sigf(c).reindex(idx).fillna(0.0) / len(COINS) for c in COINS})
    return H.backtest_portfolio(dfs, W, iv, cost_mult=cost_mult), W
def entries(W, a, b):
    return int(sum(H._entries(W[c].shift(1).loc[(W.index >= a) & (W.index < b)]) for c in W.columns))
def evaluate(name, iv, sigf, neighbours, rules, leverage_note):
    res, W = portfolio(iv, sigf)
    res2, _ = portfolio(iv, sigf, cost_mult=2.0)
    ss = H.split_stats(res); ss2 = H.split_stats(res2)
    for k in ss: ss[k]["entries"] = entries(W, *{"is": (H.IS_START, H.IS_END), "oos": (H.IS_END, H.OOS_END), "full": (H.IS_START, H.OOS_END)}[k])
    nb = {}
    for lab, f in neighbours.items():
        r, _ = portfolio(iv, f); s = H.split_stats(r)
        nb[lab] = {"is_sharpe": s["is"]["sharpe"], "oos_sharpe": s["oos"]["sharpe"], "oos_return": s["oos"]["return"]}
    nbo = [v["oos_sharpe"] for v in nb.values()]
    corr = H.correlation_with_desk(res); bl = H.blend_check(res)
    I, O, F = ss["is"], ss["oos"], ss["full"]
    P = {"P1": True,
         "P2": O["return"] > 0 and O["sharpe"] >= 0.5,
         "P3": I["sharpe"] >= 0.7,
         "P4": I["entries"] >= 30 and O["entries"] >= 20,
         "P5": ss2["oos"]["return"] > 0,
         "P6": float(np.median(nbo)) >= 0.25 and np.mean([v["oos_return"] > 0 for v in nb.values()]) >= 2 / 3,
         "P7": max(I["max_dd"], O["max_dd"], F["max_dd"]) <= 0.35,
         "P8": corr <= 0.6 or bl["blend_oos_sharpe"] > bl["desk_oos_sharpe"]}
    core = all(P[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    verdict = "PASS" if all(P.values()) else ("PLAUSIBLE" if core else "FAIL")
    py = H.per_year(res)
    out = {"name": name, "interval": iv, "rules": rules, "sizing": leverage_note,
           "is": I, "oos": O, "full": F, "oos_2x_cost": ss2["oos"], "per_year": py,
           "neighbours": nb, "neighbour_median_oos_sharpe": float(np.median(nbo)),
           "neighbour_frac_positive": float(np.mean([v["oos_return"] > 0 for v in nb.values()])),
           "corr_with_desk": corr, "blend_check": bl, "pass_rule": {k: bool(v) for k, v in P.items()}, "verdict": verdict}
    print(json.dumps(out, indent=1, default=lambda x: float(x) if isinstance(x, (np.floating, np.integer)) else str(x)))
    return out
# ---- F1: retail (all-account) long/short ratio contrarian, 4h, long/short
def c1(c, iv="4h", Wd=90, k=1.5, Hh=6, side="ls"):
    m = metrics_at_close(c, iv)
    z = zroll(np.log(m["count_long_short_ratio"]), Wd * BPD[iv])
    raw = pd.Series(np.where(z > k, -1.0, np.where(z < -k, 1.0, 0.0)), index=z.index)
    p = hold_signal(raw, Hh)
    if side == "long": p = p.clip(lower=0)
    return p
if __name__ == "__main__":
    SIDE = "ls"
    nbrs = {f"W={w}": (lambda c, w=w: c1(c, Wd=w, side=SIDE)) for w in (30, 180)}
    nbrs.update({f"k={k}": (lambda c, k=k: c1(c, k=k, side=SIDE)) for k in (1.0, 2.0)})
    nbrs.update({f"H={h}": (lambda c, h=h: c1(c, Hh=h, side=SIDE)) for h in (1, 12)})
    evaluate("C1_ls_4h", "4h", lambda c: c1(c, side=SIDE), nbrs,
             "4h Binance perp bars. R = log(count_long_short_ratio) (global account L/S ratio, Binance metrics) from the last 5-min row stamped <= bar close - 5min; z = (R - rolling mean)/rolling std over 540 bars (90d, min 180). z>1.5 -> short 1x, z<-1.5 -> long 1x; hold latest event 6 bars (24h), re-armed by new events, opposite event flips. Decide at close, fill next open, 7.5bps/side + funding.",
             "each coin account 1.0x notional when signalled; desk = equal-weight average of BTC/ETH/SOL accounts (weights 1/3, gross <= 1x)")
