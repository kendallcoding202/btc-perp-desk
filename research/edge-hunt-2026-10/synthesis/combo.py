"""Combined-portfolio test: does adding the audit's candidates to the current desk help, and is it distinguishable from noise?

Series (daily net returns, each from its OWN script, rules unchanged):
  desk   baseline_daily.csv 'desk' (equal-weight BTC/ETH/SOL desk accounts, $150 each, current main 4b978e7)
  VT     vol-trend basket, faithful held-quantity sim from vt_build/min_capital_vt.py (sim, no $10 floor);
         the harness version (hunters/meanrev_vol/final_voltgt.py) is reported alongside as a check
  TS     tsmom20_long (hunters/trend_portfolio/final_tsmom20_long.py run())
  C1     C1_ls_4h (hunters/positioning_flow_p/final_C1_ls_4h.py portfolio('4h', c1 side='ls'))
Windows: IS 2020-01-01..2023-12-31, OOS 2024-01-01..2026-09-30 (harness split). Sharpe = mean/sd x sqrt(365) of daily
returns. Combos are rebalanced daily to fixed weights (equal capital, or inverse-vol with vols estimated on IS only,
each series from its first active day). Planned money split: 3 x $150 desk + $500 VT, both as fixed weights
(VT 500/950) and as four separate un-rebalanced accounts (VT with the $10 minimum, started at $500 each window).
Bootstrap: circular moving-block, 10-day blocks, 10,000 draws, same blocks for all series (keeps cross-correlation).
Day-boundary check: baseline_daily.csv's desk day ends at 22:00 UTC (its curve is stamped at 2h bar closes); the other
series end at 00:00 UTC. desk_curves.py re-ran the desk (exact match to the CSV) and the 00:00-aligned desk is used as
a sensitivity ('aligned') block.
Run: python3 combo.py   (writes combo.json and series.csv next to this file)."""
import contextlib, importlib.util, io, json, math, pickle, runpy, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
sys.path.insert(0, str(EDGE))
import harness as H  # noqa: E402

IS0, IS1, OOS1 = H.IS_START, H.IS_END, H.OOS_END
WIN = {"is": (IS0, IS1), "oos": (IS1, OOS1)}
SEED, NBOOT, BLOCK = 20261012, 10_000, 10


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cut(s):
    s = s.copy()
    s.index = pd.to_datetime(s.index, utc=True).normalize()
    return s[(s.index >= IS0) & (s.index < OOS1)]


def build_series():
    out = {}
    b = pd.read_csv(EDGE / "baseline_daily.csv", index_col=0, parse_dates=True)
    for c in ("desk", "BTC", "ETH", "SOL"):
        out[{"desk": "desk"}.get(c, "desk_" + c)] = _cut(b[c])
    # desk on 00:00 UTC day boundary (sensitivity)
    curves = pickle.load(open(HERE / "desk_curves.pkl", "rb"))
    al = {}
    for c, s in curves.items():
        eod = s.resample("1D", closed="right", label="left").last()      # equity at 00:00 of D+1 = end of day D
        start = pd.Series([150.0], index=[eod.index[0] - pd.Timedelta("1D")])
        eod = pd.concat([start, eod]).ffill()
        al[c] = eod.pct_change().iloc[1:]
    al = pd.DataFrame(al)
    al.loc[al.index < curves["SOL"].index[0].normalize(), "SOL"] = np.nan
    out["desk_00utc"] = _cut(al.mean(axis=1, skipna=True))
    # VT faithful (held quantities) + $500/$10-floor runs per window
    with contextlib.redirect_stdout(io.StringIO()):
        g = runpy.run_path(str(EDGE / "vt_build" / "min_capital_vt.py"))
    _, r, *_ = g["sim"](1.0, IS0, OOS1, floor=0.0)
    out["VT"] = _cut(r)
    for w, (a, z) in WIN.items():
        _, r5, _, _, sk, n = g["sim"](500.0, a, z, floor=10.0)
        out[f"VT500_{w}"] = _cut(r5)
    # VT harness version
    vt = _load(EDGE / "hunters/meanrev_vol/final_voltgt.py", "final_voltgt")
    res, _ = vt.run({"L": 10, "band": 0.25, "S": 50}, 0.65)
    out["VT_harness"] = _cut(H.daily(res))
    ts = _load(EDGE / "hunters/trend_portfolio/final_tsmom20_long.py", "final_tsmom20_long")
    res, _ = ts.run()
    out["TS"] = _cut(H.daily(res))
    c1 = _load(EDGE / "hunters/positioning_flow_p/final_C1_ls_4h.py", "final_C1_ls_4h")
    res, _ = c1.portfolio("4h", lambda c: c1.c1(c, side="ls"))
    out["C1"] = _cut(H.daily(res))
    return pd.DataFrame(out)


def sharpe(x):
    x = np.asarray(x, float)
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(365)) if sd > 0 else 0.0


def stats(x):
    x = pd.Series(x).dropna()
    eq = (1 + x).cumprod()
    yrs = len(x) / 365.25
    tot = float(eq.iloc[-1] - 1)
    return {"sharpe": round(sharpe(x), 3), "ann_return": round((1 + tot) ** (1 / yrs) - 1, 4),
            "total_return": round(tot, 4), "max_dd": round(float((1 - eq / eq.cummax()).max()), 4),
            "ann_vol": round(float(x.std() * math.sqrt(365)), 4), "days": int(len(x))}


def win(df, w):
    a, z = WIN[w]
    return df[(df.index >= a) & (df.index < z)]


def first_active(s):
    nz = s.fillna(0).ne(0)
    return s.index[nz.values.argmax()] if nz.any() else s.index[0]


COMBOS = {"desk alone": ["desk"], "desk + VT": ["desk", "VT"], "desk + VT + TS": ["desk", "VT", "TS"],
          "desk + VT + C1": ["desk", "VT", "C1"], "desk + VT + TS + C1": ["desk", "VT", "TS", "C1"]}


def weights_for(members, scheme, isvol):
    if scheme == "equal":
        w = np.ones(len(members))
    else:
        w = np.array([1 / isvol[m] for m in members])
    return dict(zip(members, (w / w.sum()).round(6)))


def combo_ret(df, wts):
    return sum(df[m] * w for m, w in wts.items())


def block_boot_idx(n, rng, nboot=NBOOT, block=BLOCK):
    nb = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(nboot, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n      # circular blocks
    return idx.reshape(nboot, -1)[:, :n]


def boot_diff(a, base, idx):
    """Sharpe(a) - Sharpe(base) over bootstrap resamples (same rows for both). p_diff_le_0 = share of resamples where a
    did not beat base (a rough one-sided p-value)."""
    A, Bm = np.asarray(a)[idx], np.asarray(base)[idx]
    sa = A.mean(1) / A.std(1, ddof=1) * math.sqrt(365)
    sb = Bm.mean(1) / Bm.std(1, ddof=1) * math.sqrt(365)
    d = sa - sb
    obs = sharpe(a) - sharpe(base)
    return {"observed_diff": round(obs, 3), "boot_median": round(float(np.median(d)), 3),
            "ci90": [round(float(np.quantile(d, .05)), 3), round(float(np.quantile(d, .95)), 3)],
            "ci95": [round(float(np.quantile(d, .025)), 3), round(float(np.quantile(d, .975)), 3)],
            "p_diff_le_0": round(float((d <= 0).mean()), 4),
            "combo_p_sharpe_le_0": round(float((sa <= 0).mean()), 4),
            "base_p_sharpe_le_0": round(float((sb <= 0).mean()), 4)}


def analyse(df, desk_col, rng_seed):
    d = df.copy()
    d["desk"] = d[desk_col]
    isd = win(d, "is")
    isvol = {m: float(isd.loc[isd.index >= first_active(isd[m]), m].std() * math.sqrt(365)) for m in ("desk", "VT", "TS", "C1")}
    out = {"is_vol_used_for_inverse_vol": {k: round(v, 4) for k, v in isvol.items()}, "combos": {}, "bootstrap_oos": {},
           "bootstrap_oos_vs_desk_plus_VT": {}}
    oos = win(d, "oos")
    idx = block_boot_idx(len(oos), np.random.default_rng(rng_seed))
    for scheme in ("equal", "inverse_vol_IS"):
        dvt = win(combo_ret(d, weights_for(COMBOS["desk + VT"], scheme, isvol)).to_frame("r"), "oos")["r"].values
        for name, mem in COMBOS.items():
            wts = weights_for(mem, scheme, isvol)
            r = combo_ret(d, wts)
            key = f"{name} | {scheme}"
            out["combos"][key] = {"weights": wts, "is": stats(win(r.to_frame("r"), "is")["r"]),
                                  "oos": stats(win(r.to_frame("r"), "oos")["r"]),
                                  "per_year_return": {int(y): round(float((1 + g).prod() - 1), 4) for y, g in r.groupby(r.index.year)}}
            if name != "desk alone":
                ro = win(r.to_frame("r"), "oos")["r"].values
                out["bootstrap_oos"][key] = boot_diff(ro, oos["desk"].values, idx)
                if name != "desk + VT":          # does TS / C1 add anything on top of desk + VT?
                    out["bootstrap_oos_vs_desk_plus_VT"][key] = boot_diff(ro, dvt, idx)
    # planned split, fixed weights (rebalanced daily)
    wts = {"desk": round(450 / 950, 6), "VT": round(500 / 950, 6)}
    r = combo_ret(d, wts)
    key = "planned 3x$150 desk + $500 VT | fixed weights"
    out["combos"][key] = {"weights": wts, "is": stats(win(r.to_frame("r"), "is")["r"]), "oos": stats(win(r.to_frame("r"), "oos")["r"]),
                          "per_year_return": {int(y): round(float((1 + g).prod() - 1), 4) for y, g in r.groupby(r.index.year)}}
    out["bootstrap_oos"][key] = boot_diff(win(r.to_frame("r"), "oos")["r"].values, oos["desk"].values, idx)
    return out


def planned_accounts(df):
    """Four separate accounts, never rebalanced, restarted each window: $150 BTC/ETH/SOL desk + $500 VT ($10 floor)."""
    out = {}
    for w in WIN:
        x = win(df, w)
        eq = {c: 150 * (1 + x["desk_" + c].fillna(0)).cumprod() for c in ("BTC", "ETH", "SOL")}
        eq["VT"] = 500 * (1 + x[f"VT500_{w}"].fillna(0)).cumprod()
        desk_eq = eq["BTC"] + eq["ETH"] + eq["SOL"]
        tot = desk_eq + eq["VT"]
        r_tot = tot.pct_change().fillna(tot.iloc[0] / 950 - 1)
        r_desk = desk_eq.pct_change().fillna(desk_eq.iloc[0] / 450 - 1)
        out[w] = {"total_4_accounts": {**stats(r_tot), "end_value_usd": round(float(tot.iloc[-1]), 0)},
                  "desk_3_accounts_only": {**stats(r_desk), "end_value_usd": round(float(desk_eq.iloc[-1]), 0)},
                  "VT_account_only": {**stats(x[f"VT500_{w}"].fillna(0)), "end_value_usd": round(float(eq["VT"].iloc[-1]), 0)},
                  "end_share_in_VT": round(float(eq["VT"].iloc[-1] / tot.iloc[-1]), 3)}
        out[w]["_r_tot"], out[w]["_r_desk"] = r_tot, r_desk
    return out


def main():
    cache = HERE / "series.csv"
    if cache.exists() and "--rebuild" not in sys.argv:
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
    else:
        df = build_series()
        df.to_csv(cache)
    df.index = pd.to_datetime(df.index, utc=True)
    base_days = df["desk"].dropna().index
    df = df.loc[base_days]
    fill = {c: int(df[c].isna().sum()) for c in df.columns}
    cand = ["VT", "VT_harness", "TS", "C1", "desk_00utc"]
    df[cand] = df[cand].fillna(0.0)          # before a strategy has data / its first bar: flat
    res = {"what": __doc__.split("\n\n")[0], "days": {w: int(len(win(df, w))) for w in WIN},
           "nan_days_filled_with_0": {k: v for k, v in fill.items() if v and k in cand},
           "standalone": {}, "correlations": {}}
    for s in ["desk", "desk_00utc", "VT", "VT_harness", "TS", "C1"]:
        res["standalone"][s] = {w: stats(win(df, w)[s]) for w in WIN}
        res["standalone"][s]["first_active_day"] = str(first_active(df[s]).date())
    res["standalone"]["VT_$500_with_$10_min_fresh_start_each_window"] = {w: stats(win(df, w)[f"VT500_{w}"].fillna(0)) for w in WIN}
    for w in WIN:
        x = win(df, w)
        for lab, dc in (("baseline_day", "desk"), ("aligned_00utc", "desk_00utc")):
            c = x[[dc, "VT", "TS", "C1"]].rename(columns={dc: "desk"}).corr()
            res["correlations"][f"{w} | {lab}"] = {a: {b: round(float(c.loc[a, b]), 3) for b in c.columns} for a in c.index}
    res["main_baseline_day"] = analyse(df, "desk", SEED)
    res["sensitivity_aligned_00utc"] = analyse(df, "desk_00utc", SEED)
    pa = planned_accounts(df)
    oos = win(df, "oos")
    idx = block_boot_idx(len(oos), np.random.default_rng(SEED))
    r_tot = pa["oos"]["_r_tot"].reindex(oos.index).fillna(0).values
    r_desk = pa["oos"]["_r_desk"].reindex(oos.index).fillna(0).values
    pa["bootstrap_oos_vs_desk_3_accounts"] = boot_diff(r_tot, r_desk, idx)
    for w in WIN:
        pa[w].pop("_r_tot"); pa[w].pop("_r_desk")
    res["planned_separate_accounts"] = pa
    res["notes"] = [
        "Sharpe uses daily returns x sqrt(365). Bootstrap = circular 10-day blocks, 10,000 draws, seed %d." % SEED,
        "desk column of baseline_daily.csv ends each day at 22:00 UTC; others at 00:00 UTC. 'sensitivity_aligned_00utc' uses the "
        "desk re-run (identical trades) with a 00:00 UTC day boundary.",
        "C1 has no data before 2020-09 (BTC) / 2021-12 (ETH, SOL): flat then. VT/TS/C1 inverse-vol weights use IS days from each "
        "series' first active day.",
        "Desk per-coin series come from the 2020-start $150 run; 'planned_separate_accounts' restarts their equity at $150 at each "
        "window start without re-running the desk (the $10 minimum interacts with equity, a second-order effect).",
    ]
    (HERE / "combo.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float)[:3000])


if __name__ == "__main__":
    main()
