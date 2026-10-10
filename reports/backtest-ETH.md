# Backtest — ETH perp, 2h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-10T20:00:55+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $50, risk 2.0%/trade, max 5x.

| | Desk | Buy & hold ETH |
|---|---|---|
| Total return | +104.3% | +1956.0% |
| CAGR | +11.2% | |
| Max drawdown | 19.4% | 81.2% |
| Sharpe (daily) | 0.62 | |
| Final equity | $102 | |

Trades 236 (138 long / 98 short), win rate 35%, profit factor 1.33, avg win $3, avg loss $-1. Fees paid $8, funding paid $6. In the market 20% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +55.8% | 15.3% | 1.35 | 134 | +1786.8% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +31.2% | 19.4% | 1.30 | 102 | +9.0% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +47.0% | 7.3% | 27 | 44% | +470.9% |
| 2021 | -5.2% | 12.3% | 34 | 38% | +398.7% |
| 2022 | -0.8% | 15.3% | 36 | 33% | -67.5% |
| 2023 | +13.8% | 11.8% | 34 | 38% | +90.9% |
| 2024 | -6.8% | 14.2% | 39 | 33% | +46.1% |
| 2025 | +23.0% | 11.9% | 38 | 34% | -11.0% |
| 2026 | +13.2% | 17.2% | 28 | 25% | -9.6% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.7 years
- ✅ out-of-sample return > 0% — +31.2%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.30
- ✅ max drawdown ≤ 35% — 19.4%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.
