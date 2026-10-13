# Backtest — BTC perp on Coinbase rules (BIP-20DEC30-CDE, whole 0.01 BTC contracts), 2h bars

2020-01-01 → 2026-10-01 (6.7 years). Generated 2026-10-13T22:24:51+00:00.
**COINBASE · BTC PERP rules** (BIP-20DEC30-CDE, simulated): whole contracts of 0.01 BTC at each bar's price, at most 4x, the overnight margin check at 21.02%, no entry into the weekly break (Fri 17:00-18:00 New York) or a known maintenance window (8 Binance bars wholly inside one left out). Fees: max($0.20 x contracts, 5 bps of notional) per side, plus 3 bps slippage, an ESTIMATE until the real fee tier is known. History: Binance BTCUSDT perp prices and funding prints, the proxy for Coinbase's (BIP listed 2025-07-18); Coinbase keeps no funding history, so funding is an ESTIMATE too. Stops are filled at the stop (or the open, gapped through), as Coinbase's stop-limit would if its limit is met. Start equity $500, risk 5.0%/trade.

Contracts per trade: 1 to 23 (median 12). Entries skipped by the venue (once per episode): 0 too small for one contract, 0 short of overnight margin, 1 into a closure.

| | Desk | Buy & hold BTC |
|---|---|---|
| Total return | +1125.6% | +1059.1% |
| CAGR | +45.0% | |
| Max drawdown | 27.8% | 77.2% |
| Sharpe (daily) | 1.15 | |
| Final equity | $6,128 | |

Trades 118 (118 long / 0 short), win rate 39%, profit factor 1.69, avg win $299, avg loss $-113. Fees paid $804, funding paid $528. In the market 12% of the time.

## In-sample vs out-of-sample

| | From | To | Return | Max DD | PF | Trades | Buy & hold |
|---|---|---|---|---|---|---|---|
| In-sample (first 60%) | 2020-01-01 | 2024-01-19 | +559.6% | 26.5% | 2.94 | 64 | +472.3% |
| Out-of-sample (last 40%) | 2024-01-19 | 2026-10-01 | +85.8% | 27.8% | 1.42 | 54 | +102.5% |

## By year

| Year | Return | Max DD | Trades | Win rate | Buy & hold |
|---|---|---|---|---|---|
| 2020 | +103.3% | 26.5% | 21 | 38% | +302.7% |
| 2021 | +8.3% | 11.8% | 16 | 44% | +59.6% |
| 2022 | -5.2% | 13.3% | 6 | 17% | -64.2% |
| 2023 | +210.2% | 18.9% | 19 | 42% | +155.9% |
| 2024 | +64.0% | 24.8% | 25 | 40% | +121.1% |
| 2025 | +31.0% | 19.2% | 18 | 50% | -6.4% |
| 2026 | -11.8% | 27.8% | 13 | 23% | -4.6% |

## Go-live gate (`python -m desk live` checks this)

- ✅ history ≥ 3 years — 6.7 years
- ✅ out-of-sample return > 0% — +85.8%
- ✅ out-of-sample profit factor ≥ 1.15 — 1.42
- ✅ max drawdown ≤ 35% — 27.8%
- ✅ never hit the drawdown halt — ok

Passing this gate is necessary, not sufficient: the desk also needs 14+ days of profitable paper trading before it will trade real money.

## Coinbase rules vs fractional sizing

The same strategy on the same Binance history, Coinbase's rules added one at a time. The first row is this report as it stood before the BTC account moved to Coinbase rules. The fee minimum binds while a contract is worth under $400, BTC under $40,000: at the entry of 45 of the 118 trades (2020-23). The sessions skip entries into the weekly break and leave out the bars inside known maintenance windows.

| sizing | total return | CAGR | max drawdown | out-of-sample (CAGR) | PF | trades | fees | funding | final |
|---|---|---|---|---|---|---|---|---|---|
| fractional as before: Hyperliquid 4.5 bps, 5x (the previous report) | +1475.2% | +50.5% | 28.2% | +95.4% (+28.2%/yr) | 1.70 | 118 | $846 | $687 | $7,876 |
| fractional, 4x, 5 bps | +1442.9% | +50.0% | 28.3% | +93.3% (+27.7%/yr) | 1.69 | 118 | $927 | $679 | $7,714 |
| + whole 0.01 BTC contracts, the margin check | +1358.9% | +48.8% | 27.6% | +94.0% (+27.8%/yr) | 1.72 | 118 | $844 | $616 | $7,295 |
| + the $0.20 a contract fee minimum | +1201.2% | +46.3% | 27.9% | +92.1% (+27.4%/yr) | 1.70 | 118 | $844 | $554 | $6,506 |
| + the sessions: **Coinbase rules** (this report) | +1125.6% | +45.0% | 27.8% | +85.8% (+25.8%/yr) | 1.69 | 118 | $804 | $528 | $6,128 |

## Pace to $100,000 with $500 + $100 a month

A fresh $500 account opened 2024-01-01 under these rules, $100 put in on the 1st of every month after it (32 deposits, $3,200, to 2026-10-01), as the paper account will: it ended at $4,442 with 56 trades, its worst drop 27% (time-weighted). Its time-weighted pace (deposits aren't growth) was +22.4%/yr; the venue skipped no entry. Fractional sizing as before (Hyperliquid, 4.5 bps, 5x) from the same start: $4,561, +28.0%/yr.

At that pace (the one the board projects the account at: it's this account's situation), $500 plus $100 a month for 5 years reaches $100,000 in about 16 years (about $11,655 after the 5 years, $6,500 of it put in).

At the full run's out-of-sample pace (+25.8%/yr, from an account that held thousands by then, where whole contracts are much finer), the same plan reaches it in about 14 years.

Backtest paces, not forecasts: the settings were picked on 2020-23, and 2026 so far has lost money.

## Sensitivity — does a nearby setting still make money?

Full-period total return / max drawdown / profit factor.

| breakout bars \ stop=trail ATR | 2.0 | 3.0 | 4.0 |
|---|---|---|---|
| 20 | -14% / 41% / 0.84 (halted 2020-10-10, 30 trades) | +1020% / 40% / 1.31 (halted 2026-05-01, 193 trades) | +370% / 41% / 1.32 (halted 2026-07-31, 167 trades) |
| 30 | -16% / 41% / 0.78 (halted 2020-10-10, 27 trades) | +593% / 41% / 1.30 (halted 2026-07-31, 171 trades) | +461% / 35% / 1.47 |
| 45 | -15% / 41% / 0.78 (halted 2020-10-10, 22 trades) | +1407% / 30% / 1.61 | +709% / 25% / 1.80 |
| 60 | -20% / 41% / 0.68 (halted 2020-10-13, 19 trades) | +1126% / 28% / 1.69 | +487% / 23% / 1.83 |

## Ablation — which gates earn their place (chosen on 2020–2023 only)

| variant | in-sample return | IS PF | out-of-sample return | OOS PF | OOS trades | hit drawdown halt |
|---|---|---|---|---|---|---|
| original design: all 4 soft gates, longs+shorts | -24.5% | 0.64 | -11.4% | 0.28 | 6 | no |
| all 4 soft gates, longs only | -1.4% | 0.96 | +3.0% | 1.27 | 10 | no |
| no soft gates, longs+shorts | +348.7% | 1.67 | +40.8% | 1.12 | 92 | yes, 2026-04-23 |
| no soft gates, longs only | +397.2% | 2.46 | +88.2% | 1.42 | 57 | no |
| + trend_strength only | +229.2% | 1.85 | +18.8% | 1.12 | 52 | no |
| + efficiency only | +350.8% | 2.45 | +82.5% | 1.50 | 49 | no |
| + extension only | +32.9% | 1.59 | +88.8% | 2.44 | 27 | no |
| + crowding only  ← default | +547.3% | 2.89 | +89.3% | 1.43 | 56 | no |

## Trade sooner? — pullback entries

Breakouts can mean days without a trade. These variants trade more often; chosen on in-sample only.

| entries | trades/yr | in-sample return | out-of-sample return | max drawdown | hit drawdown halt |
|---|---|---|---|---|---|
| breakouts only ← default | 17 | +547% | +89% | 28% | no |
| pullbacks only, 0.5 ATR dip | 28 | -8% | +22% | 39% | no |
| pullbacks only, 1 ATR dip | 22 | +3% | +2% | 40% | yes, 2026-04-29 |
| pullbacks only, 2 ATR dip | 8 | -18% | -16% | 41% | yes, 2025-09-22 |
| breakouts + 0.5 ATR pullbacks | 41 | +292% | +21% | 40% | yes, 2026-07-22 |
| breakouts + 1 ATR pullbacks | 36 | +396% | +30% | 40% | yes, 2026-04-18 |
| breakouts + 2 ATR pullbacks | 19 | +381% | +15% | 40% | yes, 2024-10-08 |

## Shorts — three designs

Each design is added to the longs. 'Short PF' = what the short trades won ÷ what they lost (above 1.00 = the shorts made money).

| shorts | total return | max drawdown | hit drawdown halt | short trades | short PF in / out of sample |
|---|---|---|---|---|---|
| no shorts ← default | +1126% | 28% | no | 0 | — / — |
| breakdown (mirror of the long breakout) | +696% | 40% | yes, 2026-04-23 | 101 | 0.89 / 0.77 |
| failed rally, 0 ATR rejection | +69% | 40% | yes, 2023-01-11 | 79 | 0.92 / — |
| failed rally, 0.5 ATR rejection | +14% | 40% | yes, 2022-12-12 | 85 | 0.71 / — |
| bull trap, 12h window | +694% | 41% | yes, 2026-08-18 | 125 | 0.80 / 0.96 |
| bull trap, 0.5 ATR, 12h window | +733% | 40% | yes, 2026-07-07 | 98 | 0.73 / 0.89 |
| bull trap, 24h window | +648% | 40% | yes, 2024-10-21 | 108 | 0.94 / 0.89 |
| both failed setups | -24% | 40% | yes, 2020-10-19 | 28 | 0.37 / — |

## Take profit — sell when it's up, or let the trailing stop do it?

The trailing stop already sells winners: it ratchets up behind every new high and never moves down. This adds a fixed profit target on top, in ATRs from the entry.

| take profit at | in-sample return | out-of-sample return | total return | avg win ÷ avg loss |
|---|---|---|---|---|
| off (trailing stop only) ← default | +547% | +89% | +1126% | 2.6x |
| +2 ATR | +39% | +30% | +81% | 0.6x |
| +4 ATR | +194% | +49% | +339% | 1.5x |
| +6 ATR | +219% | +82% | +483% | 1.9x |
| +12 ATR | +345% | +81% | +704% | 2.2x |

## Position size — profit scales with risk, and so does the pain

| risk per trade | total return | CAGR | max drawdown | worst year | hit 40% halt? | trades | entries skipped: no whole contract fits |
|---|---|---|---|---|---|---|---|
| 1% | +38% | +4.9% | 5.6% | +0.0% | no | 19 | 235 |
| 2% | +213% | +18.4% | 12.1% | -4.2% | no | 94 | 59 |
| 3% | +381% | +26.2% | 17.6% | -6.2% | no | 111 | 16 |
| 5% ← default | +1126% | +45.0% | 27.8% | -11.8% | no | 118 | 0 |

## Cost stress

| taker fee bps | slippage bps | total return | PF |
|---|---|---|---|
| 5 | 3 | +1125.6% | 1.69 |
| 5 | 10 | +894.0% | 1.60 |
| 7 | 15 | +740.4% | 1.52 |

## Jev experiment — does a second opinion predict which trades win?

_Carried over from the 2026-10-09 report (fractional sizing, the $50 start); it needs JEV_API_KEY to re-run. Under Coinbase rules the desk makes the same 118 entries (117 at the same bar, one a bar later: a Friday entry that fell in the weekly break) and the same exits, and Jev's answers don't depend on the size, so it was not re-run. Its dollar figures are from the $50 fractional run._

Every one of the 118 historical entries was described to Jev (relative numbers only, no dates or prices) and asked the desk's three questions. 0 correlation = no predictive value.

| question | answer range | correlation with trade P&L | P&L when Jev was below median | above median |
|---|---|---|---|---|
| momentum_already_spent (noul) | 0.41–0.65 | -0.05 | $313 | $424 |
| follow_through (noul) | 0.34–0.60 | +0.06 | $-81 | $819 |
| regime = trend (choice) | 0.30–0.99 | -0.05 | $489 | $249 |

With Jev allowed to veto at the default thresholds: +1480.3% (117 trades) vs +1475.2% (118 trades) without.

Verdict: Jev stays **advisory** (shown on the board, no veto) unless a future test says otherwise.
