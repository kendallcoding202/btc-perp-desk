# Backtest — ETH perp, 2h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-10T19:37:20+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $50, risk 2.0%/trade, max 5x.

| | Desk | Buy & hold ETH |
|---|---|---|
| Total return | +65.1% | +1956.0% |
| CAGR | +7.7% | |
| Max drawdown | 25.6% | 81.2% |
| Sharpe (daily) | 0.53 | |
| Final equity | $83 | |

Trades 138 (138 long / 0 short), win rate 34%, profit factor 1.40, avg win $2, avg loss $-1. Fees paid $4, funding paid $6. In the market 12% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +32.0% | 14.0% | 1.37 | 82 | +1786.8% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +25.1% | 20.0% | 1.44 | 56 | +9.0% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +34.1% | 7.3% | 24 | 42% | +470.9% |
| 2021 | -0.1% | 7.5% | 23 | 39% | +398.7% |
| 2022 | -1.8% | 8.4% | 8 | 25% | -67.5% |
| 2023 | +1.4% | 11.4% | 24 | 38% | +90.9% |
| 2024 | -15.0% | 18.2% | 28 | 29% | +46.1% |
| 2025 | +33.0% | 10.4% | 16 | 44% | -11.0% |
| 2026 | +9.6% | 14.9% | 15 | 13% | -9.6% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.7 years
- ✅ out-of-sample return > 0% — +25.1%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.44
- ✅ max drawdown ≤ 35% — 25.6%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.
