"""FINALIST rel_crowding: the live desk (desk.backtest engine, live per-coin params, equal-weight BTC/ETH/SOL $50
accounts) with the absolute crowding gate REPLACED by a relative one: block a long entry when the desk's 3-day
funding APR (desk indicator funding_apr) is above the 90th percentile of its own trailing 365 days (min 180 days,
past bars only); block a short entry (ETH breakdown shorts) when it is below the 10th percentile. Everything else
(hard gates incl. |funding| <= 150% APR, sizing, brakes, costs, funding) unchanged. Monkeypatched at runtime;
the repo is not edited."""
import sys, json, os
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from d_overlay import *

Q, MODE = 0.90, "replace"
NEIGH = [0.80, 0.95]
P = {"is": (IS_START, IS_END), "oos": (IS_END, OOS_END), "full": (IS_START, OOS_END)}

df, ntr = desk_run(q=Q, mode=MODE)
base, nb0 = desk_run()
out = {"name": "rel_crowding", "params": {"q": Q, "mode": MODE, "window_days": 365}}
for nm, (a, b) in P.items():
    out[nm] = seg(df, ntr, a, b); out[nm + "_stock_desk"] = seg(base, nb0, a, b)
d2, n2 = desk_run(q=Q, mode=MODE, cost_mult=2.0)
out["oos_2x_cost"] = seg(d2, n2, IS_END, OOS_END)
x = df["desk"]; out["per_year"] = {int(y): round(float((1 + g).prod() - 1), 4) for y, g in x.groupby(x.index.year)}
j = pd.concat([df["desk"].rename("s"), base["desk"].rename("b")], axis=1).dropna()
jo = j[(j.index >= IS_END) & (j.index < OOS_END)]
out["corr_with_stock_desk_full"] = round(float(j.corr().iloc[0, 1]), 3)
out["blend"] = {"desk_oos_sharpe": round(sharpe(jo.b), 3), "strategy_oos_sharpe": round(sharpe(jo.s), 3),
                "blend_oos_sharpe": round(sharpe(0.5 * jo.s + 0.5 * jo.b), 3), "oos_corr": round(float(jo.corr().iloc[0, 1]), 3)}
nb = []
for q in NEIGH:
    dn, nn = desk_run(q=q, mode=MODE); s = seg(dn, nn, IS_END, OOS_END)
    nb.append({"q": q, "oos_sharpe": s["sharpe"], "oos_return": s["return"]})
out["neighbours"] = nb
out["neighbour_median_oos_sharpe"] = float(np.median([v["oos_sharpe"] for v in nb]))
out["neighbour_frac_positive"] = float(np.mean([v["oos_return"] > 0 for v in nb]))
print(json.dumps(out, indent=1))
