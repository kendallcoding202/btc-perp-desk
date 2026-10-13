"""FINAL CANDIDATE tsmom20_ls (hunter: trend_portfolio). Standalone: python3 final_tsmom20_ls.py  -> prints JSON.

Rules (frozen in-sample, see PLAN.md):
  Data: Binance USDT-M perp DAILY candles (00:00 UTC) via harness H.klines(coin, "1d"); real funding.
  Coins: BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT LTC BCH TRX ATOM NEAR UNI AAVE FIL ETC XLM
  Signal per coin at each daily close t: s = mean over L in {7,14,28,56,112} days (x m) of sign(close_t/close_{t-L}-1).
  Side: ls (long-only clips s at 0).
  Per-coin raw weight = s * (0.40 / sigma_ann) / N_eligible, sigma_ann = std of last VW daily returns * sqrt(365).
  Portfolio scaler k = TV / (std of raw portfolio's last 60 daily returns * sqrt(365)), clipped [0, 4]; gross <= 3x.
  No-trade band B: update a coin only if |target-held| > B*|target| or sign flips or either side is zero.
  Timing/costs: harness backtest_portfolio (decide at close t, fill next open, 7.5 bps/side, funding).
"""
import json, math, sys, itertools
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import harness as H

COINS = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT', 'LTC', 'BCH', 'TRX', 'ATOM', 'NEAR', 'UNI', 'AAVE', 'FIL', 'ETC', 'XLM']
SIDE = "ls"
M, VW, B, TV = 1.0, 40, 0.2, 0.2
LB = (7, 14, 28, 56, 112)


def weights(dfs, coins, m=M, vw=VW, band=B, tv=TV, side=SIDE):
    idx = H.klines("BTC", "1d").index
    close = pd.DataFrame({c: dfs[c]["close"] for c in coins}).reindex(idx)
    rets = close.pct_change(fill_method=None)
    sd = rets.rolling(vw, min_periods=vw).std()
    sig_ann = sd * math.sqrt(365)
    lbs = sorted({max(1, int(round(L * m))) for L in LB})
    s = sum(np.sign(close / close.shift(L) - 1) for L in lbs) / len(lbs)
    elig = s.notna() & sig_ann.notna() & (sig_ann > 0)
    s = s.where(elig)
    if side == "long":
        s = s.clip(lower=0)
    n_el = elig.sum(axis=1).replace(0, np.nan)
    raw = (s * (0.40 / sig_ann)).div(n_el, axis=0).fillna(0.0)
    rp = (raw.shift(1) * rets.fillna(0.0)).sum(axis=1)
    sp = rp.rolling(60, min_periods=30).std() * math.sqrt(365)
    tgt = raw.mul((tv / sp).clip(0, 4).fillna(0.0), axis=0)
    g = tgt.abs().sum(axis=1)
    tgt = tgt.mul(np.minimum(1.0, 3.0 / g.replace(0, np.nan)).fillna(1.0), axis=0)
    T = tgt.values; out = np.zeros_like(T); h = np.zeros(T.shape[1])
    for t in range(len(T)):
        x = T[t]
        upd = (np.abs(x - h) > band * np.abs(x)) | (np.sign(x) != np.sign(h)) | (x == 0) | (h == 0)
        h = np.where(upd, x, h); out[t] = h
    return pd.DataFrame(out, index=idx, columns=coins)


def entries(W, start, end):
    s = np.sign(W.fillna(0).values); prev = np.vstack([np.zeros((1, s.shape[1])), s[:-1]])
    e = pd.Series(((s != 0) & (s != prev)).sum(1), index=W.index)
    return int(e[(e.index >= start) & (e.index < end)].sum())


def run(coins=COINS, cost_mult=1.0, **kw):
    dfs = {c: H.klines(c, "1d") for c in coins}
    W = weights(dfs, coins, **kw)
    res = H.backtest_portfolio(dfs, W, "1d", cost_mult=cost_mult)
    return res, W


def main(neighbours=True):
    res, W = run()
    ss = H.split_stats(res)
    for k, (a, b) in {"is": (H.IS_START, H.IS_END), "oos": (H.IS_END, H.OOS_END), "full": (H.IS_START, H.OOS_END)}.items():
        ss[k]["entries"] = entries(W, a, b)
    res2, _ = run(cost_mult=2.0)
    oos2 = H.stats(res2, H.IS_END, H.OOS_END)
    out = {"name": "tsmom20_ls", "params": dict(coins=COINS, side=SIDE, m=M, VW=VW, B=B, TV=TV, lookbacks=LB),
           "is": ss["is"], "oos": ss["oos"], "full": ss["full"], "oos_return_2x_cost": oos2["return"],
           "oos_sharpe_2x_cost": oos2["sharpe"], "per_year": H.per_year(res[res.index < H.OOS_END]),
           "corr_with_desk_full": H.correlation_with_desk(res), "blend": H.blend_check(res),
           "avg_gross_oos": float(res.loc[(res.index >= H.IS_END) & (res.index < H.OOS_END), "gross_exposure"].mean()),
           "max_gross": float(res["gross_exposure"].max())}
    if neighbours:
        nb = []
        for m, vw, band in itertools.product((M * 0.5, M, M * 2), (VW // 2, VW, VW * 2), (B - 0.1, B, B + 0.1)):
            if (m, vw, round(band, 2)) == (M, VW, B):
                continue
            r, _ = run(m=m, vw=vw, band=band)
            st = H.stats(r, H.IS_END, H.OOS_END)
            nb.append({"m": m, "VW": vw, "B": round(band, 2), "oos_sharpe": st["sharpe"], "oos_return": st["return"],
                       "is_sharpe": H.stats(r, H.IS_START, H.IS_END)["sharpe"]})
        sh = [x["oos_sharpe"] for x in nb]
        out["neighbours"] = nb
        out["neighbor_median_oos_sharpe"] = float(np.median(sh))
        out["neighbor_frac_positive"] = float(np.mean([x["oos_return"] > 0 for x in nb]))
    o, i = out["oos"], out["is"]
    full_dd = max(i["max_dd"], o["max_dd"], out["full"]["max_dd"])
    P = {"P2": o["return"] > 0 and o["sharpe"] >= 0.5, "P3": i["sharpe"] >= 0.7,
         "P4": i["entries"] >= 30 and o["entries"] >= 20, "P5": out["oos_return_2x_cost"] > 0,
         "P6": (out.get("neighbor_median_oos_sharpe", -9) >= 0.25 and out.get("neighbor_frac_positive", 0) >= 2 / 3),
         "P7": full_dd <= 0.35,
         "P8": out["blend"]["oos_corr"] <= 0.6 or out["blend"]["blend_oos_sharpe"] > out["blend"]["desk_oos_sharpe"]}
    out["pass_rule"] = P
    print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main(neighbours="--quick" not in sys.argv)
