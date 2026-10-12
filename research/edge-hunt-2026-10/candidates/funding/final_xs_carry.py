"""FINALIST xs_carry: cross-sectional funding carry on 25 Binance USDT-M perps (survivorship-biased universe).
Rules: daily bars (decision at 00:00 UTC close, fill next open). Signal = mean of the last 7 daily funding sums
(prints in each day's (open, close]). Eligible = coin has >= 60 days of funding history and a close that day.
Each day: short the 3 highest-signal coins, long the 3 lowest, equal weight, 0.5 gross per side, then x0.6
(gross 0.6x of equity, 0.1x per leg). Rebalanced daily (only changes are charged 7.5 bps/side). Funding real."""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fb import *
import a_xs
LEV = 0.6
CFG = dict(L=7, k=3, R=1, bn=False)
NEIGH = [dict(L=3, k=3, R=1, bn=False), dict(L=14, k=3, R=1, bn=False), dict(L=7, k=5, R=1, bn=False), dict(L=7, k=3, R=3, bn=False)]


def full(cfg, cost_mult=1.0):
    return a_xs.run(cfg["L"], cfg["k"], cfg["R"], cfg["bn"], lev=LEV, cost_mult=cost_mult)


res, W = full(CFG)
out = {"name": "xs_carry", "leverage_gross": LEV, "params": CFG}
for nm, (a, b) in {"is": (IS_START, IS_END), "oos": (IS_END, OOS_END), "full": (IS_START, OOS_END)}.items():
    out[nm] = short(pstats(res, W, a, b))
r2, _ = full(CFG, 2.0)
out["oos_2x_cost"] = short(H.stats(r2, IS_END, OOS_END))
out["per_year"] = {k: round(v, 4) for k, v in H.per_year(res[res.index < OOS_END]).items()}
out["corr_with_desk_full"] = round(H.correlation_with_desk(res[res.index < OOS_END]), 3)
out["blend"] = {k: round(v, 3) for k, v in H.blend_check(res).items()}
nb = []
for c in NEIGH:
    rn, wn = full(c)
    s = H.stats(rn, IS_END, OOS_END)
    nb.append({**c, "oos_sharpe": round(s["sharpe"], 3), "oos_return": round(s["return"], 4)})
out["neighbours"] = nb
out["neighbour_median_oos_sharpe"] = float(np.median([x["oos_sharpe"] for x in nb]))
out["neighbour_frac_positive"] = float(np.mean([x["oos_return"] > 0 for x in nb]))
print(json.dumps(out, indent=1))
