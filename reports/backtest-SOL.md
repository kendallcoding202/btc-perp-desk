# Backtest — SOL perp, 2h bars

2020-09-14 → 2026-10-01 (6.0 years). Generated 2026-10-10T19:37:32+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $50, risk 2.0%/trade, max 5x.

| | Desk | Buy & hold SOL |
|---|---|---|
| Total return | +39.6% | +3406.8% |
| CAGR | +5.7% | |
| Max drawdown | 16.7% | 96.8% |
| Sharpe (daily) | 0.51 | |
| Final equity | $70 | |

Trades 107 (107 long / 0 short), win rate 39%, profit factor 1.42, avg win $2, avg loss $-1. Fees paid $2, funding paid $2. In the market 9% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-09-14 | 2024-04-30 | +21.5% | 15.1% | 1.40 | 63 | +3663.3% |
| Out-of-sample (last 40%) | 2024-05-01 | 2026-10-01 | +14.9% | 13.3% | 1.45 | 44 | -6.8% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | -1.7% | 2.1% | 1 | 0% | -53.0% |
| 2021 | +19.1% | 6.7% | 17 | 41% | +11199.6% |
| 2022 | -0.2% | 6.8% | 9 | 44% | -94.1% |
| 2023 | +12.3% | 13.7% | 27 | 52% | +921.0% |
| 2024 | -12.5% | 15.8% | 25 | 20% | +85.9% |
| 2025 | +10.7% | 6.2% | 16 | 56% | -34.2% |
| 2026 | +10.0% | 7.6% | 12 | 25% | -5.2% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.0 years
- ✅ out-of-sample return > 0% — +14.9%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.45
- ✅ max drawdown ≤ 35% — 16.7%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.
