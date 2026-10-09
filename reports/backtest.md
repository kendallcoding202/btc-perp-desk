# Backtest — BTC perp, 4h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-09T22:42:32+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $50, risk 2.0%/trade, max 3x.

| | Desk | Buy & hold BTC |
|---|---|---|
| Total return | +157.3% | +1057.3% |
| CAGR | +15.0% | |
| Max drawdown | 12.3% | 77.1% |
| Sharpe (daily) | 1.14 | |
| Final equity | $129 | |

Trades 94 (94 long / 0 short), win rate 38%, profit factor 2.13, avg win $4, avg loss $-1. Fees paid $4, funding paid $9. In the market 17% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +101.3% | 11.7% | 2.86 | 52 | +468.4% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +27.8% | 12.3% | 1.66 | 42 | +103.6% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +30.4% | 11.7% | 16 | 44% | +302.7% |
| 2021 | +2.0% | 8.2% | 14 | 36% | +59.6% |
| 2022 | -2.8% | 6.0% | 5 | 20% | -64.2% |
| 2023 | +57.6% | 7.0% | 15 | 40% | +155.9% |
| 2024 | +28.9% | 8.8% | 18 | 39% | +121.1% |
| 2025 | +2.8% | 9.2% | 15 | 40% | -6.4% |
| 2026 | -4.7% | 8.2% | 11 | 36% | -4.6% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.7 years
- ✅ out-of-sample return > 0% — +27.8%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.66
- ✅ max drawdown ≤ 35% — 12.3%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.

## Sensitivity — does a nearby setting still make money?

Full-period total return / max drawdown / profit factor.

| breakout bars \ stop=trail ATR | 2.0 | 3.0 | 4.0 |
|---|---|---|---|
| 20 | +235% / 17% / 1.78 | +198% / 13% / 2.19 | +130% / 12% / 2.19 |
| 30 | +188% / 17% / 1.79 | +157% / 12% / 2.13 | +108% / 12% / 2.10 |
| 45 | +95% / 19% / 1.50 | +104% / 15% / 1.84 | +79% / 12% / 1.87 |
| 60 | +104% / 20% / 1.55 | +110% / 15% / 1.88 | +87% / 12% / 1.96 |

## Ablation — which gates earn their place (chosen on 2020–2023 only)

| variant | in-sample return | IS PF | out-of-sample return | OOS PF | OOS trades | hit drawdown halt |
|---|---|---|---|---|---|---|
| original design: all 4 soft gates, longs+shorts | +1.2% | 1.01 | +0.0% | 0.00 | 0 | yes, 2023-11-09 |
| all 4 soft gates, longs only | +6.0% | 1.15 | -2.9% | 0.91 | 33 | no |
| no soft gates, longs+shorts | +98.7% | 1.78 | +20.4% | 1.25 | 80 | no |
| no soft gates, longs only | +105.5% | 2.94 | +26.3% | 1.62 | 44 | no |
| + trend_strength only | +44.6% | 2.02 | +4.7% | 1.12 | 39 | no |
| + efficiency only | +98.7% | 2.86 | +24.0% | 1.58 | 42 | no |
| + extension only | +100.3% | 2.73 | +22.7% | 1.57 | 41 | no |
| + crowding only  ← default | +103.7% | 2.99 | +26.3% | 1.62 | 44 | no |

## Take profit — sell when it's up, or let the trailing stop do it?

The trailing stop already sells winners: it ratchets up behind every new high and never moves down. This adds a fixed profit target on top, in ATRs from the entry.

| take profit at | in-sample return | out-of-sample return | total return | avg win ÷ avg loss |
|---|---|---|---|---|
| off (trailing stop only) ← default | +104% | +26% | +157% | 3.4x |
| +2 ATR | +29% | +10% | +43% | 0.7x |
| +4 ATR | +51% | +18% | +79% | 1.8x |
| +6 ATR | +71% | +24% | +112% | 2.2x |
| +12 ATR | +78% | +21% | +116% | 2.7x |

## Position size — profit scales with risk, and so does the pain

| risk per trade | total return | CAGR | max drawdown | worst year | hit 25% halt? |
|---|---|---|---|---|---|
| 1% | +59% | +7.1% | 6.3% | -2.3% | no |
| 2% ← default | +157% | +15.0% | 12.3% | -4.7% | no |
| 3% | +291% | +22.4% | 17.3% | -7.1% | no |
| 5% | +643% | +34.6% | 26.2% | -11.6% | yes |

## Cost stress

| taker fee bps | slippage bps | total return | PF |
|---|---|---|---|
| 4.5 | 3 | +157.3% | 2.13 |
| 5 | 10 | +136.0% | 1.93 |
| 7 | 15 | +125.7% | 1.85 |

## Jev experiment — does a second opinion predict which trades win?

Every one of the 94 historical entries was described to Jev (relative numbers only, no dates or prices) and asked the desk's three questions. 0 correlation = no predictive value.

| question | answer range | correlation with trade P&L | P&L when Jev was below median | above median |
|---|---|---|---|---|
| momentum_already_spent (noul) | 0.42–0.64 | -0.08 | $47 | $32 |
| follow_through (noul) | 0.43–0.68 | +0.04 | $28 | $51 |
| regime = trend (choice) | 0.47–1.00 | +0.01 | $32 | $46 |

With Jev allowed to veto at the default thresholds: +157.3% (94 trades) vs +157.3% (94 trades) without.

Verdict: Jev stays **advisory** (shown on the board, no veto) unless a future test says otherwise.
