"""(a) cross-sectional funding carry: short highest trailing funding, long lowest. IS grid only unless --final."""
import sys, json, itertools
from fb import *

COINS = H.UNIVERSE


@lru_cache(maxsize=None)
def base():
    dfs = {c: daily_k(c) for c in COINS}
    idx = pd.date_range(min(d.index[0] for d in dfs.values()), max(d.index[-1] for d in dfs.values()), freq="1D", tz="UTC")
    fund = pd.DataFrame({c: H.bar_funding(dfs[c], c, "1d") for c in COINS}).reindex(idx)  # per-day funding, known at bar close
    close = pd.DataFrame({c: dfs[c]["close"] for c in COINS}).reindex(idx)
    first_f = {c: H.funding(c).index[0] for c in COINS}
    age = pd.DataFrame({c: (idx - first_f[c]).days for c in COINS}, index=idx)
    ret = close.pct_change(fill_method=None)
    b = ret["BTC"]
    beta = pd.DataFrame({c: ret[c].rolling(60, min_periods=40).cov(b) / b.rolling(60, min_periods=40).var() for c in COINS})
    return dfs, idx, fund, close, age, beta.clip(0.2, 3.0)


def weights(L, k, R, beta_neutral, end=None):
    dfs, idx, fund, close, age, beta = base()
    sig = fund.rolling(L, min_periods=L).mean()
    elig = (age >= 60) & close.notna() & sig.notna()
    W = pd.DataFrame(0.0, index=idx, columns=COINS)
    for j, t in enumerate(idx):
        if j % R:
            W.iloc[j] = np.nan
            continue
        s = sig.loc[t][elig.loc[t]]
        if len(s) < 2 * k:
            continue
        s = s.sort_values()
        lo, hi = s.index[:k], s.index[-k:]
        gL, gS = 0.5, 0.5
        if beta_neutral:
            bl = beta.loc[t, lo].fillna(1.0).mean(); bs = beta.loc[t, hi].fillna(1.0).mean()
            gL, gS = bs / (bl + bs), bl / (bl + bs)
        W.loc[t, lo] = gL / k
        W.loc[t, hi] = -gS / k
    W = W.ffill().fillna(0.0)
    W = W.where(close.notna(), 0.0)
    if end is not None:
        W = W[W.index < end]
    return W


def run(L, k, R, bn, lev=1.0, end=None, cost_mult=1.0):
    dfs = base()[0]
    W = weights(L, k, R, bn, end) * lev
    res = H.backtest_portfolio(dfs, W, "1d", cost_mult=cost_mult)
    return res, W


if __name__ == "__main__":
    out = []
    for L, k, R, bn in itertools.product([1, 3, 7, 14], [3, 5], [1, 3, 7], [False, True]):
        res, W = run(L, k, R, bn, end=IS_END)
        st = pstats(res, W, IS_START, IS_END)
        d = H.daily(res[(res.index >= IS_START) & (res.index < IS_END)])
        row = dict(L=L, k=k, R=R, bn=bn, **short(st), vol=round(float(d.std() * math.sqrt(365)), 3),
                   price_pnl=round(float(res.loc[res.index < IS_END, "gross"].sum()), 3))
        out.append(row)
        print(json.dumps(row), flush=True)
    pd.DataFrame(out).to_csv("a_xs_is_grid.csv", index=False)
