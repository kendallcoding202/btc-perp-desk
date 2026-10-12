"""final_voltgt.py — meanrev_vol hunter finalist 3 (family E: volatility-targeted long, SMA50-gated).
Rules (1d Binance perp bars, decided at the 00:00 UTC daily close, BTC/ETH/SOL, each coin a 1/3 sleeve):
  rv_t = std of the last 10 daily returns x sqrt(365). target_t = min(2, 0.40 / rv_t) if close_t > SMA50_t
  else 0. Rebalance to target only when target is 0, current is 0, or |target - current| > 25% of current.
  Sleeve exposure = lev/3 x current; lev = 0.65 (so a coin sleeve is at most 0.43x equity, book max 1.3x).
  Filled at the next daily open. Costs 4.5 bps fee + 3 bps slippage per side + real funding (harness).
Chosen on IS 2020-2023 only. Run: python3 final_voltgt.py  (prints JSON)."""
# ---------------------------------------------------------------- shared evaluation (inlined in every final_*.py)
import sys, json, math, itertools
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import harness as H
import numpy as np
import pandas as pd
BOOK = ["BTC", "ETH", "SOL"]


def run(params, lev, cost_mult=1.0, book=BOOK):
    dfs = {c: H.klines(c, IV) for c in book}
    dfs = {c: d[d.index < H.OOS_END] for c, d in dfs.items()}
    pos = {c: signal(dfs[c], **params) for c in book}
    idx = dfs[book[0]].index
    for c in book[1:]:
        idx = idx.union(dfs[c].index)
    W = pd.DataFrame({c: pos[c] * lev / len(book) for c in book}).reindex(idx).fillna(0.0)
    res = H.backtest_portfolio(dfs, W, IV, cost_mult=cost_mult)
    held = {c: pos[c].shift(1).fillna(0.0) for c in book}
    return res, held


def entries(held, a, b):
    n = 0
    for h in held.values():
        s = np.sign(h); prev = s.shift(1).fillna(0.0)
        e = (s != 0) & (s != prev)
        n += int(e[(e.index >= a) & (e.index < b)].sum())
    return n


def st(res, held, a, b):
    s = H.stats(res, a, b)
    keep = {k: s[k] for k in ("from", "to", "return", "cagr", "sharpe", "max_dd", "cost_paid", "funding_paid")}
    keep["entries"] = entries(held, a, b)
    keep["exposure_any"] = float((res.loc[(res.index >= a) & (res.index < b), "gross_exposure"] > 0).mean())
    return keep


def evaluate(name, params, lev, ladders):
    res, held = run(params, lev)
    out = {"name": name, "interval": IV, "params": params, "leverage": lev,
           "is": st(res, held, H.IS_START, H.IS_END), "oos": st(res, held, H.IS_END, H.OOS_END),
           "full": st(res, held, H.IS_START, H.OOS_END), "per_year": H.per_year(res)}
    r2, h2 = run(params, lev, cost_mult=2.0)
    out["is_return_2x_cost"] = H.stats(r2, H.IS_START, H.IS_END)["return"]
    out["oos_return_2x_cost"] = H.stats(r2, H.IS_END, H.OOS_END)["return"]
    out["oos_sharpe_2x_cost"] = H.stats(r2, H.IS_END, H.OOS_END)["sharpe"]
    rm, _ = run(params, lev, cost_mult=0.2)   # side note: maker 1.5 bps, no slippage
    out["oos_maker_side_note"] = {k: H.stats(rm, H.IS_END, H.OOS_END)[k] for k in ("return", "sharpe")}
    out["oos_per_coin_sharpe"] = {}
    for c in BOOK:
        rc, _ = run(params, 1.0, book=[c])
        out["oos_per_coin_sharpe"][c] = H.stats(rc, H.IS_END, H.OOS_END)["sharpe"]
    out["corr_with_desk_full"] = H.correlation_with_desk(res)
    out["blend"] = H.blend_check(res)
    # P6 neighbour grid (pre-declared +/-1 step box, centre excluded)
    keys = list(ladders)
    boxes = []
    for k in keys:
        lad = ladders[k]; i = lad.index(params[k]); boxes.append(lad[max(0, i - 1): i + 2])
    nbs = []
    for combo in itertools.product(*boxes):
        p = dict(params); p.update(dict(zip(keys, combo)))
        if p == params:
            continue
        rn, _ = run(p, lev)
        s = H.stats(rn, H.IS_END, H.OOS_END)
        nbs.append({**dict(zip(keys, combo)), "oos_sharpe": s["sharpe"], "oos_return": s["return"],
                    "is_sharpe": H.stats(rn, H.IS_START, H.IS_END)["sharpe"]})
    out["neighbours"] = nbs
    med = float(np.median([n["oos_sharpe"] for n in nbs])); fpos = float(np.mean([n["oos_return"] > 0 for n in nbs]))
    out["neighbour_median_oos_sharpe"] = med; out["neighbour_frac_positive"] = fpos
    o, i_ = out["oos"], out["is"]
    P = {"P1": True,
         "P2": o["return"] > 0 and o["sharpe"] >= 0.5,
         "P3": i_["sharpe"] >= 0.7,
         "P4": i_["entries"] >= 30 and o["entries"] >= 20,
         "P5": out["oos_return_2x_cost"] > 0,
         "P6": med >= 0.25 and fpos >= 2 / 3,
         "P7": out["full"]["max_dd"] <= 0.35,
         "P8": out["corr_with_desk_full"] <= 0.6 or out["blend"]["blend_oos_sharpe"] > out["blend"]["desk_oos_sharpe"]}
    out["pass_rule"] = P
    core = all(P[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    out["verdict"] = "PASS" if all(P.values()) else ("PLAUSIBLE" if core else "FAIL")
    out["finalists_taken_to_oos_by_this_hunter"] = 3
    print(json.dumps(out, indent=1, default=float))
    return out

IV = "1d"


def signal(df, L=10, band=0.25, S=50, tv=0.4, cap=2.0):
    c = df.close
    rv = c.pct_change().rolling(L).std() * math.sqrt(365)
    tgt = (tv / rv).clip(upper=cap)
    tgt = tgt.where(c > c.rolling(S).mean(), 0.0).fillna(0.0).values
    pos = np.zeros(len(c)); cur = 0.0
    for i in range(len(c)):
        t = tgt[i]
        if t == 0.0 or cur == 0.0 or abs(t - cur) > band * cur:
            cur = t
        pos[i] = cur
    return pd.Series(pos, index=c.index)


if __name__ == "__main__":
    evaluate("voltgt", {"L": 10, "band": 0.25, "S": 50}, 0.65,
             {"L": [5, 10, 20, 30, 60, 90], "band": [0.05, 0.1, 0.25, 0.5], "S": [30, 50, 100, 150, 200]})
