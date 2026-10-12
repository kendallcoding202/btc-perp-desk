# Backtest — SOL perp, 2h bars

2020-09-14 → 2026-10-01 (6.0 years). Generated 2026-10-12T22:51:52+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $150, risk 2.0%/trade, max 5x.

| | Desk | Buy & hold SOL |
|---|---|---|
| Total return | +16.0% | +3406.8% |
| CAGR | +2.5% | |
| Max drawdown | 18.7% | 96.8% |
| Sharpe (daily) | 0.26 | |
| Final equity | $174 | |

Trades 119 (119 long / 0 short), win rate 35%, profit factor 1.17, avg win $4, avg loss $-2. Fees paid $6, funding paid $4. In the market 10% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-09-14 | 2024-04-30 | +0.9% | 18.7% | 1.02 | 75 | +3663.3% |
| Out-of-sample (last 40%) | 2024-05-01 | 2026-10-01 | +14.9% | 13.3% | 1.45 | 44 | -6.8% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | -3.7% | 3.7% | 2 | 0% | -53.0% |
| 2021 | +1.0% | 18.0% | 28 | 25% | +11199.6% |
| 2022 | -0.2% | 6.8% | 9 | 44% | -94.1% |
| 2023 | +12.3% | 13.7% | 27 | 52% | +921.0% |
| 2024 | -12.5% | 15.8% | 25 | 20% | +85.9% |
| 2025 | +10.7% | 6.2% | 16 | 56% | -34.2% |
| 2026 | +10.0% | 7.6% | 12 | 25% | -5.2% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.0 years
- ✅ out-of-sample return > 0% — +14.9%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.45
- ✅ max drawdown ≤ 35% — 18.7%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.
