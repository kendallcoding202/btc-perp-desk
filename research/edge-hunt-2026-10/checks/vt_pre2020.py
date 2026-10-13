"""Critic quick check: frozen voltgt rules on Binance SPOT BTC/ETH 2017-08..2019-12 (data no hunter used; includes the 2018 bear).
Each coin a 1/3 sleeve (SOL sleeve idle, as it was before SOL listed in the original IS). Funding: none on spot; a
long-perp version is approximated by charging a flat funding APR on held exposure (0%, 10%, 20%)."""
import sys, math, io, zipfile, glob, json
sys.path.insert(0, "/tmp/claude-0/-home-user/c67283e4-8e62-5bce-a0e4-e537e0ac564b/scratchpad/edge/hunters/meanrev_vol")
import numpy as np, pandas as pd
import final_voltgt as V
D = "/tmp/claude-0/-home-user/c67283e4-8e62-5bce-a0e4-e537e0ac564b/scratchpad/critic_dl_spot"
def load(sym):
    parts = []
    for p in sorted(glob.glob(f"{D}/{sym}-1d-*.zip")):
        with zipfile.ZipFile(p) as z:
            parts.append(pd.read_csv(io.StringIO(z.read(z.namelist()[0]).decode()), header=None).iloc[:, :6])
    df = pd.concat(parts); df.columns = ["t", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df.t.astype("int64"), unit="ms", utc=True)
    return df.drop(columns="t").astype(float).sort_index()
dfs = {c: load(c + "USDT") for c in ("BTC", "ETH")}
def run(lev=0.65, fund_apr=0.0, cost_bps=7.5, gate=True, book=("BTC", "ETH"), nsleeves=3):
    tot = None
    for c in book:
        df = dfs[c]
        pos = V.signal(df) if gate else None
        if not gate:   # ungated vol target, for context
            cc = df.close; rv = cc.pct_change().rolling(10).std() * math.sqrt(365); pos = (0.4 / rv).clip(upper=2.0).fillna(0.0)
        w = pos * lev / nsleeves
        h = w.shift(1).fillna(0.0)
        gap = (df.open / df.close.shift(1) - 1).fillna(0.0); intra = df.close / df.open - 1
        r = h.shift(1).fillna(0.0) * gap + h * intra - (h - h.shift(1).fillna(0.0)).abs() * cost_bps / 1e4 - h * fund_apr / 365
        tot = r if tot is None else tot.add(r, fill_value=0.0)
    return tot
def st(r, a="2017-10-01", b="2020-01-01"):
    x = r[(r.index >= a) & (r.index < b)]
    eq = (1 + x).cumprod()
    return dict(sharpe=round(float(x.mean() / x.std() * math.sqrt(365)), 2), ret=round(float(eq.iloc[-1] - 1), 3),
                max_dd=round(float((1 - eq / eq.cummax()).max()), 3),
                per_year={int(y): round(float((1 + g).prod() - 1), 3) for y, g in x.groupby(x.index.year)})
out = {}
for f in (0.0, 0.10, 0.20):
    out[f"voltgt_fund{int(f*100)}"] = st(run(fund_apr=f))
out["voltgt_2x_cost_fund10"] = st(run(fund_apr=0.10, cost_bps=15))
out["ungated_voltarget_fund10"] = st(run(fund_apr=0.10, gate=False))
bh = sum((dfs[c].close.pct_change().fillna(0.0) / 2) for c in ("BTC", "ETH"))
out["buyhold_BTC_ETH_5050_1x"] = st(bh)
out["2018_only_voltgt_fund10"] = st(run(fund_apr=0.10), "2018-01-01", "2019-01-01")
print(json.dumps(out, indent=1))
