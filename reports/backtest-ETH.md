# Backtest — ETH perp, 2h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-12T22:50:47+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $150, risk 2.0%/trade, max 5x.

| | Desk | Buy & hold ETH |
|---|---|---|
| Total return | +100.2% | +1956.0% |
| CAGR | +10.8% | |
| Max drawdown | 19.4% | 81.2% |
| Sharpe (daily) | 0.60 | |
| Final equity | $300 | |

Trades 237 (138 long / 99 short), win rate 35%, profit factor 1.31, avg win $8, avg loss $-3. Fees paid $24, funding paid $18. In the market 20% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +52.6% | 17.0% | 1.33 | 135 | +1786.8% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +31.2% | 19.4% | 1.30 | 102 | +9.0% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +47.0% | 7.3% | 27 | 44% | +470.9% |
| 2021 | -5.2% | 12.3% | 34 | 38% | +398.7% |
| 2022 | -2.8% | 17.0% | 37 | 32% | -67.5% |
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
