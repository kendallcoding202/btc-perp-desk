# Backtest — BTC perp, 2h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-09T22:57:05+00:00.
Costs charged: taker fee 4.5 bps + slippage 3.0 bps per side, plus real funding prints. Start equity $50, risk 5.0%/trade, max 5x.

| | Desk | Buy & hold BTC |
|---|---|---|
| Total return | +1475.2% | +1059.1% |
| CAGR | +50.5% | |
| Max drawdown | 28.2% | 77.2% |
| Sharpe (daily) | 1.21 | |
| Final equity | $788 | |

Trades 118 (118 long / 0 short), win rate 42%, profit factor 1.70, avg win $37, avg loss $-15. Fees paid $85, funding paid $69. In the market 12% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +706.1% | 25.1% | 3.02 | 64 | +472.3% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +95.4% | 28.2% | 1.44 | 54 | +102.5% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +128.4% | 25.1% | 21 | 43% | +302.7% |
| 2021 | +9.6% | 14.9% | 16 | 44% | +59.6% |
| 2022 | -4.8% | 14.0% | 6 | 17% | -64.2% |
| 2023 | +231.3% | 18.4% | 19 | 53% | +155.9% |
| 2024 | +69.1% | 25.6% | 25 | 40% | +121.1% |
| 2025 | +33.1% | 19.6% | 18 | 50% | -6.4% |
| 2026 | -11.4% | 28.2% | 13 | 23% | -4.6% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.7 years
- ✅ out-of-sample return > 0% — +95.4%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.44
- ✅ max drawdown ≤ 35% — 28.2%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.

## Sensitivity — does a nearby setting still make money?

Full-period total return / max drawdown / profit factor.

| breakout bars \ stop=trail ATR | 2.0 | 3.0 | 4.0 |
|---|---|---|---|
| 20 | +222% / 41% / 1.27 | +1340% / 40% / 1.32 | +530% / 41% / 1.33 |
| 30 | +198% / 41% / 1.14 | +905% / 41% / 1.32 | +480% / 40% / 1.37 |
| 45 | -10% / 40% / 0.86 | +1872% / 30% / 1.62 | +815% / 25% / 1.75 |
| 60 | +335% / 36% / 1.30 | +1475% / 28% / 1.70 | +590% / 25% / 1.78 |

## Ablation — which gates earn their place (chosen on 2020–2023 only)

| variant | in-sample return | IS PF | out-of-sample return | OOS PF | OOS trades | hit drawdown halt |
|---|---|---|---|---|---|---|
| original design: all 4 soft gates, longs+shorts | -34.9% | 0.56 | +0.0% | 0.00 | 0 | yes, 2022-10-13 |
| all 4 soft gates, longs only | +2.5% | 1.05 | -6.0% | 0.74 | 12 | no |
| no soft gates, longs+shorts | +549.1% | 1.76 | +49.9% | 1.13 | 91 | yes, 2026-04-18 |
| no soft gates, longs only | +541.9% | 2.54 | +89.9% | 1.41 | 57 | no |
| + trend_strength only | +315.9% | 1.94 | +9.9% | 1.06 | 51 | yes, 2026-09-04 |
| + efficiency only | +387.1% | 2.35 | +99.0% | 1.57 | 48 | no |
| + extension only | +41.1% | 1.51 | +110.9% | 2.38 | 27 | no |
| + crowding only  ← default | +689.6% | 2.97 | +99.5% | 1.45 | 56 | no |

## Trade sooner? — pullback entries and shorts

Breakouts can mean days without a trade. These variants trade more often; chosen on in-sample only.

| entries | trades/yr | in-sample return | out-of-sample return | max drawdown | hit drawdown halt |
|---|---|---|---|---|---|
| breakouts only ← default | 17 | +690% | +99% | 28% | no |
| pullbacks only, 0.5 ATR dip | 16 | -26% | +0% | 41% | yes, 2023-10-11 |
| pullbacks only, 1 ATR dip | 24 | +36% | +13% | 41% | yes, 2026-01-19 |
| pullbacks only, 2 ATR dip | 7 | -22% | +0% | 40% | yes, 2023-10-11 |
| breakouts + 0.5 ATR pullbacks | 30 | +465% | +10% | 40% | yes, 2024-10-09 |
| breakouts + 1 ATR pullbacks | 27 | +652% | +14% | 40% | yes, 2024-10-14 |
| breakouts + 2 ATR pullbacks | 20 | +461% | +17% | 40% | yes, 2024-10-07 |
| breakouts + 1 ATR pullbacks + shorts | 8 | +4% | +0% | 40% | yes, 2020-10-19 |

## Take profit — sell when it's up, or let the trailing stop do it?

The trailing stop already sells winners: it ratchets up behind every new high and never moves down. This adds a fixed profit target on top, in ATRs from the entry.

| take profit at | in-sample return | out-of-sample return | total return | avg win ÷ avg loss |
|---|---|---|---|---|
| off (trailing stop only) ← default | +690% | +99% | +1475% | 2.4x |
| +2 ATR | +106% | +52% | +213% | 0.6x |
| +4 ATR | +333% | +60% | +593% | 1.5x |
| +6 ATR | +300% | +94% | +673% | 1.8x |
| +12 ATR | +443% | +84% | +898% | 2.0x |

## Position size — profit scales with risk, and so does the pain

| risk per trade | total return | CAGR | max drawdown | worst year | hit 40% halt? |
|---|---|---|---|---|---|
| 1% | +98% | +10.7% | 6.4% | -2.1% | no |
| 2% | +256% | +20.7% | 12.4% | -4.3% | no |
| 3% | +513% | +30.8% | 18.0% | -6.6% | no |
| 5% ← default | +1475% | +50.5% | 28.2% | -11.4% | no |

## Cost stress

| taker fee bps | slippage bps | total return | PF |
|---|---|---|---|
| 4.5 | 3 | +1475.2% | 1.70 |
| 5 | 10 | +1082.1% | 1.58 |
| 7 | 15 | +800.4% | 1.49 |

## Jev experiment — does a second opinion predict which trades win?

Every one of the 118 historical entries was described to Jev (relative numbers only, no dates or prices) and asked the desk's three questions. 0 correlation = no predictive value.

| question | answer range | correlation with trade P&L | P&L when Jev was below median | above median |
|---|---|---|---|---|
| momentum_already_spent (noul) | 0.41–0.65 | -0.05 | $313 | $424 |
| follow_through (noul) | 0.34–0.60 | +0.06 | $-81 | $819 |
| regime = trend (choice) | 0.30–0.99 | -0.05 | $489 | $249 |

With Jev allowed to veto at the default thresholds: +1480.3% (117 trades) vs +1475.2% (118 trades) without.

Verdict: Jev stays **advisory** (shown on the board, no veto) unless a future test says otherwise.
