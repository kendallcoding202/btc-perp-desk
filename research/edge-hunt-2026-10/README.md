# Edge hunt, October 2026

This is the research behind the desk's October 2026 audit, kept so it can be re-run. The quarterly re-audit ([`REAUDIT.md`](REAUDIT.md)) uses it.

## What's here

- `harness.py`: the shared harness every hunter used. Its docstring defines the timing rule, the cost model, the split and the pass rule P1–P8.
  - Timing: decide at a bar's close, fill at the next bar's open.
  - Costs: 4.5 bps fee + 3 bps slippage per side, plus real Binance funding.
  - Split: settings chosen on 2020-01-01..2023-12-31 only; out-of-sample 2024-01-01..2026-09-30.
  - The pass rule P1–P8 was fixed before any result was seen.
- `download.py`: fetches the data into `data/` (gitignored), up to the last complete month. It covers 25 Binance USDT-M perps (1h candles and funding) and, for BTC/ETH/SOL, premium index, spot and positioning metrics.
- `make_baseline.py` → `baseline_daily.csv`: the live desk's daily returns at the $150 account size, for correlation and blend checks.
- `ALL_RESULTS.json`: every hunter's, checker's and auditor's structured result. `synthesis/combo.json`: the blend and bootstrap test. `checks/`: two quick checks the final critic ran, each reviewed by another agent (see its README).
- `candidates/`: the frozen finalists that survived or are still waiting for evidence. Each has its `PLAN.md`: what was tested, the parameter grid, and the trial count.
  - `voltgt/`: the vol-trend basket, the one idea both skeptics passed. It now runs as the VT paper account. `held_quantity_version.py` is the held-quantity form the account trades, with the $10-minimum analysis.
  - `tsmom20/`: the 20-coin trend portfolio (contested).
  - `c1_lsratio/`: the contrarian bet on the retail long/short ratio (contested).
  - `stopwidth/`: wider ETH/SOL stops on the real desk engine (unconfirmed).
  - `funding/`: funding carry and the relative crowding gate (unconfirmed / refuted; the ETH short-side gate is a lead).

## Running

```
python3 research/edge-hunt-2026-10/download.py                   # ~10k small files, a few minutes
python3 research/edge-hunt-2026-10/candidates/voltgt/final_voltgt.py   # prints IS/OOS/full stats as JSON
```

The scripts find the harness and the repo relative to their own location, so they run from anywhere.

**These harness candidates reproduce the audit exactly** when re-run from here at its end date (2026-09-30): voltgt, tsmom20_long, C1_ls_4h and xs_carry. tsmom20_ls was not re-run. For example, voltgt has IS Sharpe 1.795 and OOS 0.896 (+63.6%), and tsmom20_long has 1.573 and 0.938.

**The desk-engine candidates (`stopwidth`, `rel_crowding`) run on the live desk code.** That code now starts accounts at $150 instead of the audit's $50, so their 2020–23 numbers moved. 2024–26 is unchanged.

| desk-engine run | 2020–23 Sharpe (was, at $50) | 2024–26 Sharpe |
|---|---|---|
| SOL desk as is | 0.27 (0.76) | 0.24 |
| SOL at 4 ATR | 1.13 (1.30) | 0.19 |
| ETH desk as is | 0.68 (0.71) | 0.52 |
| ETH at 3.5 ATR | 1.03 (0.96) | 0.71 |

`rel_crowding` has not been re-run at $150. The re-audit's same-sign check (N3) compares against the 2024–26 column.

The full write-up of the audit is [`reports/edge-hunt-2026-10.md`](../../reports/edge-hunt-2026-10.md).
