"""Critic quick check: frozen xs_carry rules (L=7,k=3,R=1, 0.6x gross) on a wider universe incl. delisted coins."""
import sys, math, json
E = "/tmp/claude-0/-home-user/c67283e4-8e62-5bce-a0e4-e537e0ac564b/scratchpad/edge"
sys.path.insert(0, E); sys.path.insert(0, E + "/verify/cross_sectional_D_alt_rotation_independent_p"); sys.path.insert(0, E + "/hunters/funding_basis")
import numpy as np, pandas as pd
import harness as H
import ext
from fb import daily_k
EXTRA = sorted(p.name[:-4] for p in (ext.ROOT / "k1d").iterdir())
def load(c, extra):
    if extra:
        df = ext.k1d(c); f = ext.bar_fund(df, c); f0 = ext.fund(c).index[0]
    else:
        df = daily_k(c); f = H.bar_funding(df, c, "1d"); f0 = H.funding(c).index[0]
    return df, f, f0
def run(coins_extra, L=7, k=3, lev=0.6, top_vol=None, cost_mult=1.0, base_only_mask=None):
    D = {}
    for c, ex in coins_extra:
        try: D[c] = load(c, ex)
        except Exception as e: print("skip", c, e, file=sys.stderr)
    idx = pd.date_range("2020-01-01", "2026-09-30", freq="1D", tz="UTC")
    O = pd.DataFrame({c: D[c][0]["open"] for c in D}).reindex(idx)
    C = pd.DataFrame({c: D[c][0]["close"] for c in D}).reindex(idx)
    QV = pd.DataFrame({c: D[c][0]["quote_volume"] for c in D}).reindex(idx)
    F = pd.DataFrame({c: D[c][1] for c in D}).reindex(idx).fillna(0.0)
    age = pd.DataFrame({c: (idx - D[c][2]).days for c in D}, index=idx)
    sig = pd.DataFrame({c: D[c][1] for c in D}).reindex(idx).rolling(L, min_periods=L).mean()
    elig = (age >= 60) & C.notna() & sig.notna()
    if top_vol:
        rank = QV.rolling(30, min_periods=20).mean().where(elig).rank(axis=1, ascending=False)
        elig &= rank <= top_vol
    W = pd.DataFrame(0.0, index=idx, columns=list(D))
    for t in idx:
        s = sig.loc[t][elig.loc[t]]
        if len(s) < 2 * k: continue
        s = s.sort_values(kind="mergesort")
        W.loc[t, s.index[:k]] = 0.5 / k; W.loc[t, s.index[-k:]] = -0.5 / k
    W = (W * lev).where(C.notna(), 0.0)
    h = W.shift(1).fillna(0.0)
    Cf, Of = C.ffill(), O.ffill()
    gap = (Of / Cf.shift(1) - 1).fillna(0.0); intra = (Cf / Of - 1).fillna(0.0)
    gross = (h.shift(1).fillna(0.0) * gap + h * intra).sum(axis=1)
    cost = (h - h.shift(1).fillna(0.0)).abs().sum(axis=1) * 7.5e-4 * cost_mult
    fund = (h * F).sum(axis=1)
    r = gross - cost - fund
    res = pd.DataFrame({"ret": r, "gross": gross, "cost": cost, "fund": fund})
    return res, W
def st(r, a, b):
    x = r[(r.index >= a) & (r.index < b)]
    sh = x.mean() / x.std() * math.sqrt(365); eq = (1 + x).cumprod()
    return dict(sharpe=round(float(sh), 3), ret=round(float(eq.iloc[-1] - 1), 4), dd=round(float((1 - eq / eq.cummax()).max()), 3))
base = [(c, False) for c in H.UNIVERSE]
allc = base + [(c, True) for c in EXTRA]
out = {}
for nm, coins, tv in [("base25", base, None), ("all74", allc, None), ("pit_top25_vol", allc, 25), ("pit_top40_vol", allc, 40)]:
    res, W = run(coins, top_vol=tv)
    r2, _ = run(coins, top_vol=tv, cost_mult=2.0)
    yr = {int(y): round(float((1 + g).prod() - 1), 3) for y, g in res["ret"].groupby(res.index.year)}
    held = (W.abs() > 0)
    top = held.sum().sort_values(ascending=False).head(8).to_dict()
    out[nm] = dict(IS=st(res["ret"], H.IS_START, H.IS_END), OOS=st(res["ret"], H.IS_END, H.OOS_END), OOS_2x=st(r2["ret"], H.IS_END, H.OOS_END), per_year=yr,
                   luna_days_held=int(held.get("LUNA", pd.Series(dtype=bool)).sum()) if "LUNA" in W else None,
                   ftt_days_held=int(held.get("FTT", pd.Series(dtype=bool)).sum()) if "FTT" in W else None)
    print(nm, json.dumps(out[nm]), flush=True)
