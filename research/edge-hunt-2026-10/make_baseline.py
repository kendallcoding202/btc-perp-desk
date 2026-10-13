"""Export the CURRENT desk's daily returns (live params per coin) for the correlation / blend checks."""
import pandas as pd
from desk import backtest as bt
from desk.__main__ import _history
from desk.thresholds import params_for
out = {}
for coin in ("BTC", "ETH", "SOL"):
    p = params_for(coin)
    bars, funding = _history(p, "binance", "2020-01", None)
    r = bt.run(bars, funding, p)
    s = pd.Series([e for _, e in r.curve], index=pd.to_datetime([t for t, _ in r.curve], unit="ms", utc=True))
    d = s.resample("1D").last().ffill().pct_change()
    out[coin] = d
    print(coin, len(r.broker.trades), "trades, final", round(r.curve[-1][1], 2))
df = pd.DataFrame(out)
df["desk"] = df[["BTC", "ETH", "SOL"]].mean(axis=1, skipna=True)
df = df.dropna(how="all")
df.to_csv(str(__import__("pathlib").Path(__file__).with_name("baseline_daily.csv")))
import math
for c in df.columns:
    x = df[c].dropna(); o = x[x.index >= "2024-01-01"]; i = x[x.index < "2024-01-01"]
    sh = lambda y: y.mean() / y.std() * math.sqrt(365)
    print(c, "IS sharpe %.2f OOS sharpe %.2f" % (sh(i), sh(o)))
