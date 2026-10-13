# Edge hunt, October 2026

The audit of the BTC/ETH/SOL perp desk, and the hunt for anything worth adding to it. Written 2026-10-12. Nothing here is a sure thing, and no profit is promised.

## The short version

- **What we did.** About 3,600 rule variations across 9 families, chosen on 2020–23 data only. Then 24 finalists were each scored once on January 2024 to September 2026.
- **One idea passed the pre-set bar, and both checkers called it real but modest:** the vol-trend basket (VT). It holds BTC, ETH and SOL only while each is above its 50-day average, sized by volatility. It still fails the stricter many-trials luck test, and its edge over simply holding the coins is not proven. Expect a Sharpe nearer 0.5–0.7 than the backtest's 0.9. It is paper only, at $500. Its account code is being built and reviewed, and goes live on the dashboard once it passes.
- **Two ideas are contested** (one checker said real, the other was unsure). One is the 20-coin trend portfolio, which needs $1–5k and can't run at $150. The other is fading Binance's retail long/short ratio (C1). Five more are unconfirmed, a sixth had only one check, one was never checked, two are refuted or not usable, two are a fee-saving execution tweak (limit-order entries) that passed, and the other 10 failed.
- **Adding VT to the desk looked better in 2024–26** (Sharpe 0.98 vs 0.85). That gain can't be told apart from luck: there is a 32% chance it is zero or negative.
- **The SOL desk account has no demonstrated edge.** Its Sharpe was 0.27 in 2020–23 and 0.24 since, with about a 39% chance that its true Sharpe is zero or below. ETH is thin too.
- **Live trading has never run with money.** The audit found five live-only bugs. ETH and SOL orders would have gone to BTC at about 30× (ETH) to 550–770× (SOL) the size cap, and on any coin a failed stop placement could leave a position with no stop. All five are now fixed, with tests against a fake exchange (2026-10-13, see "Code and execution" below). The paper ledger-reset bug was fixed earlier (ef00987). Paper trading is not affected.
- **The 2024–26 data is now used up.** Only data from 2026-10-01 on can confirm anything. The quarterly re-audit's pass bar needs tightening before its first use in April 2027.

*Sharpe* = return per unit of bumpiness, on daily returns. In a backtest, 1+ is strong, 0.5 is modest and 0 is nothing.

## How we tested

- **Rules fixed first.** `research/edge-hunt-2026-10/harness.py` fixes the timing (decide at a bar's close, fill at the next open), costs (4.5 bps fee + 3 bps slippage per side, plus real Binance funding) and the pass rule P1–P8, before any result was seen.
- **The pass rule, in short:** chosen on 2020–23 only. On 2024–26, Sharpe at least 0.5 and still profitable at double costs. Sharpe at least 0.7 in 2020–23. Enough trades. Neighbouring settings mostly work too. Worst drop at most 35%. Adds something to the desk.
- **Two checkers for each finalist that passed its own check**, except cash_carry (one) and wf_full_lin (none). One reviewed the code. The other rebuilt the strategy from the written rules alone. Each voted REAL, UNCERTAIN or REFUTED.
- **Which desk baseline.** "Desk" means the current desk at $150 per account. The hunters and checkers compared against the older $50 baseline, which has the same 2024–26 numbers but higher 2020–23 numbers (desk 1.51, ETH 0.71, SOL 0.76). Where a hunter's comparison is quoted below, it was made against $50.

## The current desk, at $150

| | 2020–23 Sharpe | 2024–26 Sharpe | Whole period |
|---|---|---|---|
| BTC | 1.41 | 0.87 | +1,475% |
| ETH | 0.68 | 0.52 | +100% |
| SOL | 0.27 | 0.24 | +16% |
| Desk | 1.43 | 0.85 | +322% |

In 2024–26 the desk made +46%, with a worst drop of 15%.

- **SOL would fail the audit's own bar** (P2 and P3), and ETH would fail P3. The chance that the true 2024–26 Sharpe is zero or below: BTC 8%, ETH 22%, SOL 39%.
- **The desk's 0.85 is not a clean out-of-sample number.** ETH and SOL's 2% risk was chosen after seeing ETH hit the 40% halt in 2024 at 5% risk. ETH's shorts were switched on after missing their own bar.

## What held up

### Passed the bar, both checkers REAL: VT (vol-trend basket)

Its status in the audit files is SURVIVES: both checkers voted REAL. It reproduced exactly and survived the cost, delay, bar-timing and neighbouring-settings stress tests. It fails the many-trials luck test and the drop-the-best-days test (below), so it is not proven.

**Three versions of its numbers exist.** Always say which one is meant.

1. **Harness version** (the one both checkers verified): 2020–23 Sharpe 1.80; 2024–26 Sharpe 0.90, +64%. Worst drop 22% in 2024–26 and 26.5% in 2020–23.
2. **Held-quantity version, run continuously from 2020.** The VT account trades this version. With no order minimum: 2020–23 Sharpe 1.63, +279%; 2024–26 Sharpe 0.91, +65%.
3. **Fresh $500 start on 2024-01-01, with the $10 minimum order:** Sharpe 0.87, +62%, worst drop 22%, 4 orders blocked. With no minimum: 0.87, +61%. Over the last 12 months at $500: Sharpe 0.62, +12.5%.

**Why "modest":**

- It is mostly the 50-day gate. Buy-and-hold of the same coins scored 0.46 in 2024–26. Its gain over buy-and-hold has a t of only about 0.65. Its own 2024–26 t is about 1.6.
- It cleared the luck bar for 3 finalists by just 0.002, and fails the bar for the 562 variations its hunter tried. Its deflated Sharpe is 0.68–0.75, against the usual 0.95.
- A few rally days carry it. Without the best 5 days of 2024–26 its Sharpe is 0.25.
- On Hyperliquid over the last ~400 days it scored 0.18–0.44, though buy-and-hold lost 24% over the same stretch. Hyperliquid's higher funding costs it about 0.1 Sharpe according to the code checker (the other called it negligible). REAUDIT.md's "expected VT 0.87" uses Binance funding, so on Hyperliquid expect about 0.77 at most; the independent checker expects 0.5–0.7.
- Run it as one 3-coin book. As separate per-coin accounts at the same size, BTC's 2020–23 drop reaches 36%, and SOL alone scores 0.35.

### Contested: one checker REAL, one UNCERTAIN

- **20-coin trend portfolio, long only (tsmom20_long).** 2024–26 Sharpe 0.94 on a coin list picked today. On lists that include dead coins it scores about 0.6–0.8 (independent checker 0.59–0.69, code checker 0.64–0.78; 0.93 on a top-20-by-volume list), with worst drops near 35%. Its excess over just being long crypto is not significant. It is fragile: the top 5 days are 80% of its 2024–26 gain (0.32 without them), it lost 14% from Dec 2024 to Jul 2026, its worst drop passes 35% with one day's delay or double costs, it scored 0.38 on Hyperliquid's last 13 months, and it fails on coins listed in 2021–23. It needs about $1–5k because of the $10 minimum order, so it can't run at $150.
- **Retail long/short-ratio fade, 4h, long and short (C1_ls_4h).** 2024–26 Sharpe 0.96, about half its 2020–23 figure. Its hunter claimed 146 variations, but the log shows about 800. On 8 coins it never saw, only 4 are positive. The top 10 of about 290 trades make all the profit. As separate accounts at 1×, ETH and SOL would have dropped 54% and 42%. At best it is a small, BTC-weighted paper sleeve.

### Unconfirmed: no checker said REAL

- **Cross-coin funding carry (xs_carry).** Its 0.88 comes from a lucky tie-break; splitting ties gives 0.77. On coins listed by 2020 it scores 0.44 and fails. Watch only. See "What this audit didn't cover" for a survivorship check that looks much worse.
- **Spot-vs-perp carry (cash_carry).** It had one checker only (UNCERTAIN), and no code check. That includes its private fix for the harness's 2025 spot-timestamp bug. It earns about a T-bill yield, and made +0.25% over the last 17 months.
- **20-coin trend, long/short (tsmom20_ls).** The short side loses money. The long leg is the same finding as tsmom20_long.
- **Wider ETH/SOL stops (stopwidth F1).** The desk gain is +0.07 Sharpe (t 0.34), which is noise. The SOL leg fails on its own. ETH at 3.5 ATR (0.71 vs 0.52) is only a lead, waiting in the re-audit.
- **Walk-forward models (wf_price_bin, wf_full_bin).** No edge over a plain "long when the 30-day return is positive" rule. They are the same trend bet the desk already has.

### Never verified

- **wf_full_lin** passed P1–P8 by its own check (2024–26 Sharpe 0.75, +51%) but got no checker. It is 0.77–0.81 correlated with its two verified twins above, and both of those came back UNCERTAIN.
- **The 10 finalists that failed their own check** were not checked for bug-caused false failures.

### Refuted, or not usable

- **Relative funding gate on the desk (rel_crowding).** Both checkers voted REFUTED: it adds nothing over the desk. One part is a lead: skipping ETH breakdown shorts when funding is unusually negative. It is waiting in the re-audit.
- **Alt rotation under a BTC gate (D_alt_rotation).** One checker voted REFUTED and one UNCERTAIN, so it is not usable. November 2024 carries it all. With Hyperliquid listing dates it scores 0.415 in one check and 0.48 in the other, below the 0.5 bar either way. Plain BTC under the same 50-day gate did better (0.88).

### Execution: maker entries

Posting entries as limit orders, with a market-order chase if they don't fill, passes P1–P8 as an overlay. Its checker CONFIRMED that. It is a fee saving, not an edge: about $0.86 a year from the fee cut alone, or $1.6 a year in the backtest's cost model. Both figures are for three $50 accounts, so roughly three times that at $150. The checker lowered it from medium to low severity.

## What didn't work

**Failed on 2024–26.** These ran once out of sample and failed P1–P8:

- turn-of-month (2020–23 Sharpe 1.95, then −1.10)
- buying SOL or alts after a BTC crash
- buying 1h crash bounces
- buying 4h dips (IBS)
- cross-coin momentum
- ETH/BTC and SOL/BTC ratio trends
- rising-open-interest longs
- the long-only C1
- 3-coin trend (worst drop 38%)

**Rejected on 2020–23, never run on 2024–26.** These failed a hunter's in-sample screen, not P1–P8:

- desk re-entry after stops, time stops, trailing on highs, volatility-based trails, pyramiding, slow-ATR sizing, lookback ensembles and a daily SMA filter
- the desk breakout on 20 coins
- hour-of-day windows, weekend and funding-hour entry blocks, CME gap fades, weekday and Deribit-expiry effects, 1h lead-lag
- Bollinger range trading, RSI dips, squeeze breakouts
- funding or premium as a contrarian signal
- taker flow, open-interest flushes, top-trader ratios, an order-flow desk filter
- short-term reversal across coins

**Untested leads, with their 2024–26 data still unused:**

- a Bollinger-squeeze gate on the desk breakout (2020–23 Sharpe 1.30 → 1.65 in a rough desk model, half the trades)
- residual momentum (ranked 4th, so not taken forward)
- blocking entries whose signal closes 22:00–02:00 UTC
- a retail-ratio filter at desk entry
- stops based on bar closes (prices tended to recover after stops)

## Running VT next to the desk

All figures are for 2024–26, with the bootstrap chance that the gain is really zero or negative.

- **Desk + VT, 50/50, rebalanced daily:** Sharpe 0.98 vs 0.85 for the desk alone. Gain +0.13, 90% range −0.34 to +0.62, chance of no gain 32%. Worst drop 15.9% vs 14.7%.
- **The planned split, $450 desk and $500 VT, as fixed weights:** 0.98 too, chance of no gain 33%.
- **As four separate accounts, never rebalanced:** 0.91 vs 0.76 for the three desk accounts alone, chance of no gain 31%. $950 would have grown to $1,462, with VT 55% of the end value. The 0.76 is lower than 0.85 because these accounts aren't rebalanced.
- **Desk + VT + tsmom20 + C1, equal:** 1.27, chance of no gain 13%. Added on top of desk + VT, C1 adds +0.23 (chance of no gain 19%) and tsmom20 adds +0.10 (35%).
- **No mix has a chance of no gain below 11%.** VT and the desk are 0.60 correlated. So none of these is a proven improvement.

## Code and execution: fixed and open

- **Fixed:** a failed git fetch could reset all three paper ledgers to fresh accounts and overwrite the real ones (commit ef00987, pushed).
- **Fixed 2026-10-13, the five live-only bugs** (branch `live-broker-fixes`). Each has tests in `tests/test_live.py`, run against a fake exchange that answers the way ccxt's Hyperliquid class does, and each test fails on the code before its fix:
  - **HIGH, live ETH or SOL orders went to BTC-PERP** (fe3ff60). `desk/brokers/live.py` hard-coded BTC for every venue. The size depended on which repro you read: 31.5× (ETH) and 547× (SOL) the $50 cap in the code audit, about 33× and about 770× (0.467 BTC, about $38.6k) for the execution checker, and about $24.8k (SOL) for the execution auditor. The stop also went on BTC at the ETH or SOL price level. Now each coin trades its own perp (`<COIN>/USDC:USDC` on Hyperliquid, checked against its public market list). Sizes are cut down to the size step and kept under the cap, and a ticket under the $10 minimum isn't sent. Live refuses to start on a coin the venue doesn't list.
  - **MEDIUM, a failed stop after an entry left the position with no exchange stop** (6f458b6). This affected every coin, BTC included. The stop now counts as placed only once it's read back from the open orders. It's tried 4 times with backoff; then the position is closed and an alert goes out. Every sync checks that a stop is resting and puts it back if not.
  - **MEDIUM, an exchange stop fill was booked at the next mid, not the fill** (f80b18d). In the repro, a −$4.14 loss was booked as a +$2.85 win. (On a restart it was booked at the local stop, so the auditor's "runner was down" case was wrong.) It's now booked at the exchange's own fills (price and fee), with the exchange's funding. It falls back to the stop order, then to the stop level with an alert, and never to the next mid.
  - **MEDIUM, after a gap the live loop processed only the newest bar** (9f79284). It now processes every missed bar in order, and on each one the desk decides as paper does. Exits and trail stops paper would have acted on are acted on now. Entries from missed bars are not taken. A gap longer than the history window stops live rather than guess.
  - **MEDIUM, live state files were shared across coins** (5237aef). This was in `desk/runner.py` and `desk/__main__.py`. Each coin's files now live in `state/<COIN>/`. An old shared file makes live refuse, and a second process for the same coin is refused.
- **Live has still never run against a funded account.** Only the Hyperliquid path was checked. Still open, live only:
  - other venues' symbols and stop-order queries were not checked
  - by default, ccxt's Hyperliquid class tries to approve a 0.01% builder fee and add it to every order. That is a main-wallet action, so it probably fails with an API wallet, but it wasn't checked, and the backtest doesn't charge the fee
  - two coins on one account each size from the whole account
- **Paper is not affected by any of these, and the fixes don't touch its code.** Before and after the fixes:
  - `python -m desk backtest` for BTC, ETH and SOL, and `vt-backtest`, wrote the same reports apart from the generated time
  - `python -m desk step` on copies of the four real ledgers, given the same recorded Hyperliquid answers, wrote the same files byte for byte
  - so did 1,632 paper steps per coin over June 2025 to September 2026 on real 2h history, with missed runs (21, 49 and 21 trades)
- **Open, LOW (these do touch paper):**
  - one account's ledger with the wrong bar interval stops every account's step and skips the save
  - paper's stale-data gate never fires
  - paper fills unrounded quantities (Hyperliquid's size steps would trim SOL orders by up to 12.8%)
  - pending orders lose their take-profit when the ledger is saved
  - the crowding gate trips slightly more often on Hyperliquid funding than in the Binance backtest (0.79% of bars vs 0%)
- **Paper matches the backtest.** The code audit replayed the desk's paper step on 2024–26 with missed runs and got identical trades and cash. The backtest's 3 bps slippage is about 6× (SOL) to 50× (BTC) the real Hyperliquid half-spread at these sizes. Hyperliquid funding runs about twice Binance's.

## Corrections made after the first draft

- **Main README, SOL:** it said the $10 minimum "had blocked 18 SOL trades in 2021 that happened to lose". It now says that at $150 SOL takes 13 trades the $50 account skipped (12 in 2021), and 12 of them lost (net −$16.46). The 18 was a count of blocked signals, not trades.
- **Main README, going live:** it said a crash "never leaves a position unprotected". It now says live trading isn't safe yet and lists the open bugs.
- **`research/edge-hunt-2026-10/README.md`:** it now says which frozen scripts reproduce the audit exactly when re-run from the repo (voltgt, tsmom20_long, C1_ls_4h and xs_carry). It also gives the desk-engine candidates' new 2020–23 numbers at $150; their 2024–26 numbers are unchanged:
  - SOL baseline 0.27 (was 0.76)
  - SOL at 4 ATR 1.13 (was 1.30)
  - ETH at 3.5 ATR 1.03 (was 0.96)

  rel_crowding was not re-run at $150.
- **`REAUDIT.md`:** the quarterly bar was tightened on 2026-10-13, before any new data existed (see item 2 below).
- **The audit's working notes:**
  - the status list called D_alt_rotation REFUTED; it is one REFUTED and one UNCERTAIN, filed here as refuted or not usable
  - it grouped cash_carry with the two-check candidates
  - it left out wf_full_lin and the maker overlay
  - it lumped in-sample rejections and untested leads in with real failures
  - one note mixed the continuous VT run with the fresh-start run

  This report uses the corrected versions.

## What this audit didn't cover

1. **This report was fact-checked line by line after it was written:** 205 claims checked, and 18 errors found, mostly overstatements. All 18 are fixed here.
2. **The re-audit's pass bar is too easy to pass by luck.** With 6 months of new data, an idea with no edge at all clears "Sharpe ≥ 0.5" about a third of the time. A decent one (true Sharpe 0.7) clears it only about half the time. With 5 ideas waiting and a check every quarter, a false pass becomes likely. The bar has no test against simply holding coins, and the rule for account changes lets a do-nothing change through 1 time in 5.
   *Done 2026-10-13* (see `REAUDIT.md`). The fix was to rewrite N1–N3:
   - require 12+ months of new data
   - require the new-data return to beat a passive benchmark (BTC under its 50-day gate, or a vol-targeted always-long book)
   - require the 2024–26 and new-data excess together to have a block-bootstrap P ≤ 0.05, split across a fixed number of looks
   - tighten the account-change rule to P < 0.05
   - put this power table in REAUDIT.md
3. **The go-live gate rests on 14 days of paper,** which is pure noise. Before the fixes, the first live ETH or SOL entry would have opened a BTC position about 30× (ETH) to 550–770× (SOL) too big.
   *Done 2026-10-13* (see "Code and execution" above):
   - take the symbol from the coin setting
   - give each coin its own state files
   - retry a failed stop, or flatten the position
   - book exchange stops at the real fill
   - add a stub-exchange test that an ETH desk sends ETH orders under the cap
   - catch up on missed bars as paper does

   *Still next:* tie going live to the re-audit's evidence bar, not to 14 days of paper. Live has never run with money.
4. **VT's paper-account code is new and still under review.**
   - The checkers verified the research version. The account trades the held-quantity form, and the code checker's drifting-holdings test gave 0.92, close to the 0.90 verified.
   - VT doesn't depend on exact fill timing. Fills delayed by 1, 4 or 12 hours gave 0.89–0.96, and a full day gave 0.72.

   *Next:* the account's build review replays its paper step against the backtest (`research/edge-hunt-2026-10/candidates/voltgt/held_quantity_version.py`) with missed days, and requires identical fills and cash, before it goes live.
5. **xs_carry fails on a wider list of coins.** A reviewed quick check ran its frozen rules on a point-in-time top 25 by volume. The pool was 75 coins, including LUNA and FTT.
   - The reviewer fixed two bugs in the check: dead contracts' flat bars were treated as tradable, and ties were broken arbitrarily.
   - 2024–26 Sharpe about 0.45 (0.40–0.51 across tie-breaks), and about +2% at double costs.
   - About −30% in 2022, and a 43–50% worst drop in 2020–23, mostly from the LUNA long.
   - On all 75 coins: −0.49. That's driven by shorting rallying memecoins: 1000PEPE and 1000BONK in 2024, and the ORDI squeeze in April 2026.

   The pool is still hand-picked, so call it "fails on a wider, less survivor-biased universe", not "survivor-free". It is dropped from the re-audit's watch list.
6. **No correction for testing many ideas.** 24 finalists were scored on the same 2¾ years. With 24 independent no-edge tests over that span, the luckiest would be expected to show about 1.2 (the checkers' rougher formula gives 1.5), and less if the tests are correlated. The best results here (0.90–0.96, setting aside cash_carry's low-volatility 6.8) sit at or below that level. Each checker corrected only for its own hunter's count.
   *Next:*
   - estimate the effective number of independent tests from the finalists' correlations
   - report an audit-wide deflated Sharpe for VT, C1 and tsmom20_long
7. **All the trend survivors are one bet, tested in one market.** VT, tsmom20 and the walk-forward books are long-biased trend books, 0.56–0.63 correlated with each other, and 2024–26 was mostly a bull market. Nobody used pre-2020 data. A reviewed quick check ran VT's frozen rules on BTC/ETH spot from 2017-10 to 2019-12 (two coins, the third sleeve idle).
   - Sharpe 1.20, +43%, worst drop 19% with no funding; 1.12, +39% with a 10%/yr funding charge as a perp stand-in.
   - From 2018-01 on, the Sharpe is only 0.5–0.6.
   - It lost 10% in 2018 while buy-and-hold lost 77%. That gap is all low exposure: VT averaged about 6% of equity that year and was flat on 64% of days, and a passive book that small lost 6%.
   - Without the 50-day gate it lost 37–39% in 2018.

   So the gate helped by keeping the book out of a falling market, not by better timing. It is one short, weak sample.
   *Next:* run the same frozen-rules test on 2017–19 for the desk's 2h breakout (BTC, ETH) and for tsmom, with funding at 0/10/20%. Report it as an extra holdout, not a selection step.
8. **The existing accounts were never held to this bar.** See the desk section above: SOL fails it, ETH is thin, and the desk's 0.85 is partly in-sample.
   *Next:* pre-register a "keep" rule for the accounts, and a capital split across BTC/ETH/SOL/VT chosen on 2020–23 only. Judge both on new data.
9. **Leads with unused 2024–26 data were never run** (see "What didn't work").
   *Next:* pre-register the squeeze gate in the real engine (`research/edge-hunt-2026-10/candidates/stopwidth/xdesk.py`). Check it on 2020–23 at $150, run it once on 2024–26, then hand it to the re-audit. The other leads get the same treatment, at lower priority.

The wording fixes are applied throughout this report rather than listed here:

- which VT version a number comes from
- which desk baseline was used
- a bootstrap chance next to every blend figure
- checker verdicts in the checkers' own words

Lower priority:

- a code check of cash_carry
- re-screening the cost-killed rules at about 5 bps per side, on new data only
- known ideas nobody tested: Coinbase premium, FOMC/CPI days, low-volatility and size factors, Hyperliquid-native carry

## Files, trial counts, harness quirks

- **Kept in the repo** under `research/edge-hunt-2026-10/`:
  - the harness, downloader and $150 baseline
  - the frozen scripts of the finalists still being watched (voltgt, tsmom20, C1, stopwidth, funding) in `candidates/`
  - `ALL_RESULTS.json`: every hunter, checker and audit result
  - `synthesis/combo.json`: the blend and bootstrap test
  - `checks/`: the two reviewed quick checks

  The hunters' and checkers' working scripts and logs lived in the session's scratch folder and were not kept.
- **Variations tried on 2020–23:**
  - calendar 1,153
  - positioning about 800 (146 claimed)
  - walk-forward 691
  - mean reversion/volatility 562
  - cross-coin 142
  - funding 109
  - trend portfolio 97
  - desk upgrades 44
  - execution audit 55

  Each hunter took 3 finalists to 2024–26; desk upgrades took 1 and the execution audit 2.
- **Harness quirks:**
  - `H.spot()` misreads Binance spot timestamps from 2025 on. Only cash_carry used spot, and its hunter patched around it.
  - The portfolio backtest forward-fills missing bars, which double-counts about 5 days in 2022. That doesn't change any result.
