"""F1 'stopwidth' — re-runnable with: python3 final_stopwidth.py   (prints JSON)

Rule: the live desk unchanged except the initial-stop AND chandelier-trail width (stop_atr = trail_atr):
  BTC 3.0 ATR(28) (unchanged), ETH 3.5, SOL 4.0. Risk per trade unchanged (BTC 5%, ETH 2%, SOL 2%, max 5x), so a
  wider stop means a smaller position. ETH keeps its breakdown shorts. Everything else = desk/thresholds.py.
Parameters were selected on 2020-01..2023-12 only (per-coin argmax of IS daily Sharpe over k in {2.5,...,4.5}).
Runs the UNMODIFIED repo engine: desk.backtest.run on Binance 2h history from desk.__main__._history.
"""
import json
import math
import os
import sys
from multiprocessing import Pool

import pandas as pd

REPO = str(__import__("pathlib").Path(__file__).resolve().parents[4])
sys.path.insert(0, REPO)
os.chdir(REPO)                      # _history reads data/binance/ relative to the repo
BASELINE_CSV = str(__import__("pathlib").Path(__file__).resolve().parents[2]) + "/baseline_daily.csv"
IS0, SPLIT, END = (pd.Timestamp(x, tz="UTC") for x in ("2020-01-01", "2024-01-01", "2026-10-01"))
CHOSEN = {"BTC": 3.0, "ETH": 3.5, "SOL": 4.0}
NEIGH_ETH, NEIGH_SOL = (3.0, 3.5, 4.0), (3.5, 4.0, 4.5)


def one(args):
    coin, k, cost_mult = args
    import io, contextlib
    from desk import backtest as bt
    from desk.__main__ import _history
    from desk.thresholds import params_for
    p = params_for(coin)
    with contextlib.redirect_stdout(io.StringIO()):
        bars, funding = _history(p, "binance", "2020-01", None)
    p = p.with_(strategy={"stop_atr": k, "trail_atr": k},
                costs={"taker_fee_bps": p.costs.taker_fee_bps * cost_mult, "slippage_bps": p.costs.slippage_bps * cost_mult})
    r = bt.run(bars, funding, p)
    s = pd.Series([e for _, e in r.curve], index=pd.to_datetime([t for t, _ in r.curve], unit="ms", utc=True))
    d = s.resample("1D").last().ffill().pct_change()
    entries = pd.to_datetime([t.entry_ts for t in r.broker.trades], unit="ms", utc=True)
    return (coin, k, cost_mult), {"daily": d, "entries": entries, "trades": [t.as_dict() for t in r.broker.trades],
                                  "halted": r.desk.risk.halted}


def sh(x):
    x = x.dropna()
    return float(x.mean() / x.std() * math.sqrt(365)) if len(x) > 2 and x.std() > 0 else 0.0


def seg(d, entries, a, b):
    x = d[(d.index >= a) & (d.index < b)].dropna()
    eq = (1 + x).cumprod()
    n = int(((entries >= a) & (entries < b)).sum()) if entries is not None else None
    return {"sharpe": round(sh(x), 3), "return": round(float(eq.iloc[-1] - 1), 4),
            "max_dd": round(float((1 - eq / eq.cummax()).max()), 4), "entries": n}


def desk_series(runs, ks, cm=1.0):
    df = pd.DataFrame({c: runs[(c, ks[c], cm)]["daily"] for c in ks})
    ent = pd.DatetimeIndex(sorted(sum((list(runs[(c, ks[c], cm)]["entries"]) for c in ks), [])))
    return df.mean(axis=1, skipna=True).dropna(), ent


def main():
    jobs = {("BTC", 3.0, 1.0), ("SOL", 3.0, 1.0)}
    jobs |= {("ETH", k, 1.0) for k in NEIGH_ETH} | {("SOL", k, 1.0) for k in NEIGH_SOL}
    jobs |= {(c, k, 2.0) for c, k in CHOSEN.items()} | {("ETH", 3.0, 2.0), ("SOL", 3.0, 2.0)}
    with Pool(4) as pool:
        runs = dict(pool.map(one, sorted(jobs)))
    base = pd.read_csv(BASELINE_CSV, index_col=0, parse_dates=True)
    out = {"rule": __doc__.strip().splitlines()[2:6], "chosen_k": CHOSEN}

    d, ent = desk_series(runs, CHOSEN)
    db, entb = desk_series(runs, {"BTC": 3.0, "ETH": 3.0, "SOL": 3.0})
    d2, _ = desk_series(runs, CHOSEN, 2.0)
    out["desk"] = {w: seg(d, ent, a, b) for w, (a, b) in {"is": (IS0, SPLIT), "oos": (SPLIT, END), "full": (IS0, END)}.items()}
    out["desk_baseline_recomputed"] = {w: seg(db, entb, a, b) for w, (a, b) in {"is": (IS0, SPLIT), "oos": (SPLIT, END)}.items()}
    out["desk_oos_2x_cost"] = seg(d2, None, SPLIT, END)
    out["desk_per_year"] = {int(y): round(float((1 + g).prod() - 1), 4) for y, g in d.groupby(d.index.year)}
    out["desk_baseline_per_year"] = {int(y): round(float((1 + g).prod() - 1), 4) for y, g in db.groupby(db.index.year)}

    per = {}
    for c, k in CHOSEN.items():
        r = runs[(c, k, 1.0)]
        rb = runs[(c, 3.0, 1.0)]
        dc = r["daily"]
        j = pd.concat([dc.rename("s"), base[c].rename("b")], axis=1, join="inner").dropna()
        jo = j[(j.index >= SPLIT) & (j.index < END)]
        per[c] = {"k": k, **{w: seg(dc, r["entries"], a, b) for w, (a, b) in {"is": (IS0, SPLIT), "oos": (SPLIT, END)}.items()},
                  "baseline_k3_is": seg(rb["daily"], rb["entries"], IS0, SPLIT),
                  "baseline_k3_oos": seg(rb["daily"], rb["entries"], SPLIT, END),
                  "oos_2x_cost": seg(runs[(c, k, 2.0)]["daily"], None, SPLIT, END),
                  "baseline_k3_oos_2x_cost": seg(runs[(c, 3.0, 2.0)]["daily"], None, SPLIT, END),
                  "corr_with_own_baseline": round(float(j.corr().iloc[0, 1]), 3),
                  "own_baseline_oos_sharpe": round(sh(jo["b"]), 3),
                  "blend50_with_own_baseline_oos_sharpe": round(sh(0.5 * jo["s"] + 0.5 * jo["b"]), 3),
                  "halted": r["halted"],
                  "per_year": {int(y): round(float((1 + g).prod() - 1), 4) for y, g in dc.groupby(dc.index.year)}}
    out["per_coin"] = per

    j = pd.concat([d.rename("s"), base["desk"].rename("b")], axis=1, join="inner").dropna()
    jo = j[(j.index >= SPLIT) & (j.index < END)]
    out["p8_vs_desk"] = {"corr_full": round(float(j.corr().iloc[0, 1]), 3), "desk_oos_sharpe": round(sh(jo["b"]), 3),
                         "blend50_oos_sharpe": round(sh(0.5 * jo["s"] + 0.5 * jo["b"]), 3)}

    neigh = []
    for ke in NEIGH_ETH:
        for ks in NEIGH_SOL:
            if (ke, ks) == (CHOSEN["ETH"], CHOSEN["SOL"]):
                continue
            dn, en = desk_series(runs, {"BTC": 3.0, "ETH": ke, "SOL": ks})
            s = seg(dn, en, SPLIT, END)
            neigh.append({"eth_k": ke, "sol_k": ks, "oos_sharpe": s["sharpe"], "oos_return": s["return"]})
    out["p6_neighbours_desk"] = neigh
    out["p6_neighbours_per_coin"] = {c: [{"k": k, **seg(runs[(c, k, 1.0)]["daily"], None, SPLIT, END)} for k in ks]
                                     for c, ks in (("ETH", (3.0, 4.0)), ("SOL", (3.5, 4.5)))}
    ns = sorted(n["oos_sharpe"] for n in neigh)
    med = (ns[len(ns) // 2 - 1] + ns[len(ns) // 2]) / 2
    frac = sum(n["oos_return"] > 0 for n in neigh) / len(neigh)

    o, i = out["desk"]["oos"], out["desk"]["is"]
    rules = {
        "P1": True,
        "P2": o["return"] > 0 and o["sharpe"] >= 0.5,
        "P3": i["sharpe"] >= 0.7,
        "P4": i["entries"] >= 30 and o["entries"] >= 20,
        "P5": out["desk_oos_2x_cost"]["return"] > 0,
        "P6": med >= 0.25 and frac >= 2 / 3,
        "P7": out["desk"]["full"]["max_dd"] <= 0.35,
        "P8": out["p8_vs_desk"]["corr_full"] <= 0.6 or out["p8_vs_desk"]["blend50_oos_sharpe"] > out["p8_vs_desk"]["desk_oos_sharpe"],
    }
    out["p6_summary"] = {"median_oos_sharpe": round(med, 3), "frac_positive": round(frac, 3)}
    out["pass_rule"] = rules
    core = all(rules[k] for k in ("P1", "P2", "P3", "P4", "P5", "P7"))
    out["verdict"] = "PASS" if all(rules.values()) else ("PLAUSIBLE" if core else "FAIL")
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
