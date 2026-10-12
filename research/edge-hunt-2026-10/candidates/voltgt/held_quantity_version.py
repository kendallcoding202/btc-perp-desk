"""Vol-trend basket (one account, BTC/ETH/SOL thirds), the faithful version: band on the sticky target units exactly
as tested, quantities held between changes (no free daily rebalancing). Order sizes as a fraction of equity, the equity
needed to clear Hyperliquid's $10 minimum, and a replay with the $10 floor at several start sizes."""
import sys, math; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import harness as H, numpy as np, pandas as pd
COINS = ["BTC", "ETH", "SOL"]; LEV = 0.65
d = {c: H.klines(c, "1d") for c in COINS}; f = {c: H.bar_funding(d[c], c, "1d") for c in COINS}
def sticky_units(df, band=0.25):
    c = df.close; rv = c.pct_change().rolling(10).std() * math.sqrt(365)
    t = (0.40 / rv).clip(upper=2.0).where(c > c.rolling(50).mean(), 0.0).fillna(0.0)
    out = np.zeros(len(t)); cur = 0.0
    for i, x in enumerate(t.values):
        if x == 0 or cur == 0 or abs(x - cur) > band * cur: cur = x
        out[i] = cur
    return pd.Series(out, index=t.index)
U = {c: sticky_units(d[c]) for c in COINS}
idx = d["BTC"].index
def sim(eq0, t0, t1, floor=10.0):
    cash, qty, units, pend = eq0, {c: 0.0 for c in COINS}, {c: 0.0 for c in COINS}, {}
    curve, fracs, kinds, skipped, orders = [], [], [], 0, 0
    for t in idx[(idx >= t0) & (idx < t1)]:
        for c in list(pend):
            if t not in d[c].index: continue                     # coin's bar missing: fill on its next bar
            dq = pend.pop(c); px = d[c].open[t]
            fill = px * (1 + 3e-4 * np.sign(dq)); cash -= dq * fill + abs(dq) * fill * 4.5e-4; qty[c] += dq; orders += 1
        for c in COINS:
            if t in d[c].index and qty[c]: cash -= qty[c] * d[c].close[t] * f[c].get(t, 0.0)
        px_now = {c: d[c].close[t] for c in COINS if t in d[c].index}
        eq = cash + sum(qty[c] * px_now[c] for c in px_now); curve.append((t, eq))
        for c, px in px_now.items():
            u = U[c][t]
            if u == units[c]: continue
            cur = qty[c] * px; tgt = eq * LEV / 3 * u; delta = tgt - cur
            kind = "exit" if u == 0 else ("entry" if units[c] == 0 else "resize")
            fracs.append(abs(delta) / eq); kinds.append(kind)
            if kind != "exit" and abs(delta) < floor:
                skipped += 1
                if kind == "entry": continue                    # no position opened: try again on the next change
            else:
                pend[c] = delta / px
            units[c] = u
    s = pd.Series([e for _, e in curve], index=[t for t, _ in curve]); r = s.pct_change().dropna()
    return s, r, np.array(fracs), np.array(kinds), skipped, orders
s, r, fr, kd, _, _ = sim(1.0, H.IS_START, H.OOS_END, floor=0.0)
for k in ("entry", "resize", "exit"):
    a = fr[kd == k]
    print(f"{k:6} orders: {len(a):4d}  size = equity x median {np.median(a):.3f}, 10th pct {np.quantile(a, .1):.3f}, min {a.min():.4f}")
nonexit = fr[kd != "exit"]
for share in (0.90, 0.99):
    need = 10 / np.quantile(nonexit, 1 - share)
    print(f"  entries+resizes clearing $10 on {share:.0%}: equity ${need:,.0f}; start ${need / 0.6:,.0f} to keep it after a 40% drawdown")
print()
for t0, t1, lab in ((H.IS_START, H.IS_END, "2020-23"), (H.IS_END, H.OOS_END, "2024-26"), (pd.Timestamp("2025-10-01", tz="UTC"), H.OOS_END, "last 12 months")):
    for eq0 in (50, 150, 250, 500, 1000, None):
        s, r, fr, kd, sk, n = sim(eq0 or 1e9, t0, t1, floor=10.0 if eq0 else 0.0)
        print(f"{lab:14} start {'$' + format(eq0, ',') if eq0 else 'no $10 rule':>12}: return {s.iloc[-1]/s.iloc[0]-1:+7.1%}  Sharpe {r.mean()/r.std()*math.sqrt(365):5.2f}  "
              f"maxDD {float((1 - s/s.cummax()).max()):5.1%}  orders {n:4d}  blocked by $10 {sk:4d}")
