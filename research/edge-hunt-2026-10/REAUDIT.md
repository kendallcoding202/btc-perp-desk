# Quarterly re-audit — the rules, fixed on 2026-10-12

The re-audit runs on the 3rd of January, April, July and October. It **never changes anything**: no settings, no ledgers, no code on `main`. It writes a report with proposals, and the user decides.

These rules were written before any of the data they will judge existed. A re-audit applies them as written. If a rule turns out to be wrong, change it in a commit on its own, explain why, and apply it from the next quarter.

## The data

Everything from **2026-10-01** on is new: no hunter, verifier or setting has seen it. It is the only honest test left.

```
python3 research/edge-hunt-2026-10/download.py        # extends the cache to the last complete month
export EDGE_END=$(date -u +%Y-%m-01)                   # harness.OOS_END = first day of this month
```

`harness.NEW_START` is 2026-10-01. Score new data with `H.stats(res, H.NEW_START, H.OOS_END)`.

## A. The accounts we run

The accounts: BTC, ETH and SOL desk accounts at $150 each, and the VT vol-trend basket at $500.

1. **Paper vs backtest.** Run each account's backtest over its paper period on Hyperliquid data, or replay its fills, and compare with its ledger. REVIEW if a paper account's cumulative return differs from the same-period backtest by more than 2 percentage points without an explained cause, such as a single wick, a missed run or a known venue difference.
2. **Health.** For each account, from its paper start and on the new data: return, max drawdown, trades, and fees and funding paid.
   - REVIEW if its max drawdown exceeds the backtest's worst. Worst drawdowns: BTC 28%, ETH 19%, SOL 19%, VT 26% over the full period (22% from 2024 on).
   - CONSIDER PAUSING if the drawdown exceeds 1.25× that, or the 40% drawdown halt fired.
   - Once there are 12 months of new data: REVIEW if the trailing 12-month daily Sharpe is below 0.
   - Nothing is paused automatically; the report says what it would pause and why.
3. **Backtest refresh.** Re-run every account's backtest through the last complete month (`python -m desk backtest --coin BTC|ETH|SOL`, `python -m desk vt-backtest`). Compare the new-data slice with what we expected.
   - Expected out-of-sample daily Sharpe, 2024-01 to 2026-09: BTC 0.87, ETH 0.52, SOL 0.24, VT 0.87.
   - The skeptics expect VT nearer 0.5–0.7.

## B. Ideas waiting for more evidence

The scripts are in `candidates/`, with their rules frozen. Evaluate them on new data only, with no parameter changes and no re-selection.

| candidate | where | why it's waiting |
|---|---|---|
| `tsmom20_long`: 20-coin daily trend, long-only, vol-targeted | `candidates/tsmom20/final_tsmom20_long.py` | contested: real on a survivorship-free universe (OOS Sharpe ≈0.69), but its excess over plain long exposure isn't significant; needs ~$1k+ |
| `C1_ls_4h`: fade extreme Binance retail long/short ratios, 4h, long/short | `candidates/c1_lsratio/final_C1_ls_4h.py` | contested: works on BTC, mixed on other coins; needs Binance's live ratio, which may be blocked from US servers (check first) |
| ETH stop at 3.5 ATR (ETH leg of `stopwidth`) | `candidates/stopwidth/final_stopwidth.py` | unconfirmed: +0.19 Sharpe out of sample on ETH, within noise |
| ETH short-side crowding gate: skip a breakdown short when 3-day funding is below its 10th percentile over the prior year | `candidates/funding/final_rel_crowding.py` (short side only) | a lead from the rel_crowding verifiers: helped ETH in both periods, untested on its own |
| `xs_carry`: cross-sectional funding carry, Binance funding signal | `candidates/funding/final_xs_carry.py` | unconfirmed: tie-break luck and survivorship; watch only |

**Bar for proposing a candidate as a new paper account, or a change to an account.** All of these must hold:

- **N1** At least 6 months of new data (the first eligible re-audit is 2027-04).
- **N2** New-data daily Sharpe ≥ 0.5 and return > 0 after full costs, and return > 0 at 2× costs.
- **N3** The same sign as its 2024–26 out-of-sample result (no flip).
- **N4** Two independent checks fail to refute it: a code review for look-ahead, cost and data errors, and a re-implementation from the rules text alone.
- **N5** It fits a stated account size, given Hyperliquid's $10 minimum order. Say the capital it needs.
- **For a change to an existing account:** the new-data improvement over the current settings is positive, and a paired block bootstrap gives P(improvement ≤ 0) < 0.2.

Report every candidate's numbers, pass or fail. Count every variant evaluated. A re-audit runs no new parameter searches. A new idea gets its own audit with its own written-down rules, like the October 2026 one.

## C. What a re-audit delivers

- `reports/reaudit-YYYY-QN.md` on a branch `reaudit-YYYY-QN`, pushed. No pull request, and nothing merged to `main`.
- A short summary for the user's phone: account health first (any REVIEW or CONSIDER PAUSING at the top), then whether any candidate cleared the bar, then the one thing to decide, if any.
