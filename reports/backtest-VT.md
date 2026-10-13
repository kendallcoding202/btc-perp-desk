# Backtest — VT vol-trend basket (BTC · ETH · SOL), 1d bars

PAPER EXPERIMENT. There is no live mode for this account.

2020-01-01 → 2026-09-30 (6.7 years), Binance perp daily bars and funding. Generated 2026-10-13T00:32:04+00:00.
Rules (thresholds.VolTrend, frozen): a third of the book per coin; units = min(2, 0.40 / 10-day realised vol) while the close is above its 50-day average, else 0; held units change only past a 25% band; notional = equity x 0.65 / 3 x units. Decided at the 00:00 UTC close, filled at the next day's open.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per fill, plus real funding prints. Start equity $500, Hyperliquid's $10 minimum order applied (exits always go through).

| | VT | Buy & hold, equal weight |
|---|---|---|
| Total return | +519.4% | +2210.2% |
| CAGR | +31.0% | |
| Max drawdown | 26.3% | 88.4% |
| Sharpe (daily) | 1.41 | 0.99 |
| Final equity | $3,097 | |

Orders 795 (6 more blocked by the $10 minimum), 189 positions closed (46 won, 143 lost). Fees paid $73, funding paid $351. Average notional 0.25x equity. Buy & hold: a third in each coin from the start (SOL's third in cash until it listed in 2020-09), never rebalanced, no costs.

## In-sample vs out-of-sample

The split was fixed before the out-of-sample years were looked at: the rules were picked on 2020-2023 only.

| | From | To | Return | Sharpe | Max DD | Orders | Blocked | Fees | Funding | Buy & hold | B&H Sharpe | B&H max DD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| In-sample | 2020-01-01 | 2023-12-31 | +274.6% | 1.84 | 26.3% | 448 | 6 | $22.81 | $177.28 | +1745.9% | 1.30 | 88.4% |
| Out-of-sample | 2024-01-01 | 2026-09-30 | +65.4% | 0.90 | 22.4% | 347 | 0 | $50.06 | $173.48 | +43.7% | 0.52 | 62.4% |

## By year

| Year | Return | Sharpe | Max DD | Orders | Fees | Funding | Buy & hold |
|---|---|---|---|---|---|---|---|
| 2020 | +40.8% | 2.17 | 6.9% | 84 | $2.30 | $24.93 | +240.2% |
| 2021 | +77.2% | 2.84 | 11.2% | 130 | $4.08 | $96.97 | +3886.7% |
| 2022 | -16.9% | -1.30 | 22.0% | 85 | $4.79 | $6.53 | -75.3% |
| 2023 | +80.6% | 2.69 | 10.7% | 149 | $11.65 | $48.84 | +389.3% |
| 2024 | +18.8% | 0.91 | 22.4% | 133 | $13.92 | $113.99 | +84.4% |
| 2025 | +18.1% | 0.85 | 10.9% | 114 | $16.91 | $36.79 | -17.2% |
| 2026 | +17.9% | 0.97 | 14.5% | 100 | $19.23 | $22.71 | -6.5% |

## The $10 minimum: what start size it takes

Fresh accounts opened on 2020-01-01 (in-sample) and on 2024-01-01 (out-of-sample), each run to the end of its period with the minimum on. "Blocked" = entries and resizes the minimum skipped.

| Start | IS return | IS Sharpe | IS max DD | IS blocked | OOS return | OOS Sharpe | OOS max DD | OOS orders | OOS blocked |
|---|---|---|---|---|---|---|---|---|---|
| $50 | +309.2% | 1.86 | 27.9% | 464 | +90.7% | 1.14 | 21.0% | 166 | 351 |
| $150 | +268.2% | 1.85 | 25.6% | 92 | +69.1% | 0.95 | 21.0% | 294 | 52 |
| $250 | +269.1% | 1.83 | 26.1% | 36 | +63.0% | 0.89 | 22.1% | 318 | 28 |
| $500 | +274.6% | 1.84 | 26.3% | 6 | +61.7% | 0.87 | 22.4% | 343 | 3 |
| $1,000 | +278.6% | 1.85 | 26.3% | 3 | +61.8% | 0.87 | 22.4% | 346 | 0 |
| no minimum | +278.5% | 1.85 | 26.3% | 0 | +61.8% | 0.87 | 22.4% | 346 | 0 |

Every order the rule asked for since 2020 (no minimum), as a fraction of equity: entry median 0.140x (10th percentile 0.071x), resize median 0.059x (10th percentile 0.023x), exit median 0.149x (10th percentile 0.068x). Clearing $10 on 90% of entries and resizes takes $371 of equity; 99% takes $1,194. A small account that blocks most orders can still look good: it mostly isn't trading, so its result is luck, not the strategy.

Daily-return correlation with the desk's three accounts (research/edge-hunt-2026-10/baseline_daily.csv): 0.51 over 2,464 days, 0.60 since 2024. It moves with the desk more than against it.

## Check against the research simulation

The same rules were simulated in the edge hunt with pandas on Binance 1h bars resampled to days (`research/edge-hunt-2026-10/candidates/voltgt/held_quantity_version.py`). Returns agree. Two things differ:

- **Its 2020-23 Sharpe is lower (1.63) for a reason that isn't the strategy.** Its data drops the 5 days in 2022 when SOL's 1h file is missing an hour, and on those days it leaves SOL's position out of equity: a fake -12.5% day, then the money comes back. Keeping SOL at its last close (as this backtest does) gives 1.86 in that same simulation, with the same +279%.
- **Blocked-order counts differ on small accounts.** The research version runs the 25% band on one continuous series; this account runs it on the units it actually holds, as the rule is written (a skipped entry leaves it at 0 units). With the band on held units, the research simulation reproduces this report's minimum-capital table.

| Sharpe / return | This backtest | Research |
|---|---|---|
| Continuous from 2020, no minimum: 2020-23 | 1.85 / +278.5% | 1.63 / +279.0% |
| Continuous from 2020, no minimum: 2024 on | 0.90 / +65.4% | 0.91 / +65.0% |
| Fresh $500 on 2024-01-01, minimum on (max DD, blocked) | 0.87 / +61.7% (22%, 3) | 0.87 / +61.7% (22%, 4) |
| Fresh $50 on 2024-01-01, minimum on (max DD, blocked) | 1.14 / +90.7% (21%, 351) | 1.10 / +82.3% (19%, 417) |
