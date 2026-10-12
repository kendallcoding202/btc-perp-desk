# PLAN — positioning & order-flow hunter (written before any out-of-sample number was looked at)

Harness: ../../harness.py (timing: decide at bar close t, fill at open t+1; costs 7.5 bps/side on |dpos| + real funding;
IS 2020-01-01..2023-12-31, OOS 2024-01-01..2026-09-30; pass rule P1-P8 as in harness docstring).

## Data facts checked before planning (IS only)
- metrics(): BTC rows from 2020-09-01; ETH and SOL rows only from **2021-12-01** (so ETH/SOL metrics in-sample is just 2 years).
  toptrader ratios ~14-18% NaN, taker ratio ~6-7% NaN.
- Timestamp alignment test (2021-22, BTC): metrics taker ratio rows stamped in (h, h+60] correlate best (0.85) with the
  1h kline [h, h+60) taker share -> a row stamped T describes the 5 min ENDING at T. OI value / OI implied price at T
  equals the kline close at T (median diff 0.7 bp) -> OI row stamped T is a snapshot at T.
- Safety lag used everywhere: for a decision at bar close C, metrics features use the last row stamped <= C - 5 min
  (one extra row of lag, to allow for publication delay). Kline taker_buy features use bars <= t (exact, no lag issue).
- Survivorship: the 25-coin universe was picked today. For order-flow work on many coins, I use only coins listed on
  Binance futures by 2020-01/02 for in-sample selection: BTC ETH BNB XRP ADA LINK LTC BCH TRX ATOM ETC XLM (12, "U12").

## Hypotheses and rule families
A. Order-flow imbalance (kline taker buy). F_n(t) = sum_{last n bars}(2*taker_buy_base - volume) / sum(volume).
   z_A = (F_n - rolling mean_L)/rolling std_L with L = 90 days of bars (taker share has drifting structural levels).
   A1 time-series, per coin: pos = d * sign(z_A) when |z_A| > k, held H bars (re-armed each bar), d = +1 momentum / -1 contrarian.
   A2 "absorption": flow that did NOT move price. resid = z_A - b*z_ret (b from IS regression over the same n);
       pos = -sign(resid) (contrarian to unrewarded aggression) when |resid| > k.
   A3 cross-sectional on U12: rank z_A each bar, long top tercile / short bottom tercile, equal weight, rebalanced every H bars.
   IS grid: interval {4h, 1d}; n {1, 3, 6, 12}; k {0.5, 1.0, 1.5}; H {1, 3, 6}; d {+1,-1}.
   First pass = IS event study (rank-IC of signal vs fwd H-bar return, pooled over U12, net of nothing) to prune;
   only configs with consistent sign in >=3 of 4 IS years go to a costed backtest.
B. OI vs price regimes (BTC/ETH/SOL metrics). Over n bars: dP = log close change, dOI = log change of
   sum_open_interest (contracts). z of each vs rolling 90d. Regimes: up/up "new longs", down/up "new shorts",
   up/down "short covering", down/down "long liquidation". B-flush: dOI z < -k AND dP z < -k -> long for H bars (capitulation bounce).
   B-squeeze: dP<0 & dOI z>k (shorts piling in) -> long (contrarian) or short (follow), H bars.
   B-conviction: dP>0 & dOI z>k -> long (follow) for H bars.
   IS grid: interval {2h, 4h, 1d}; n {6, 12, 24 bars on 2h | 3, 6, 12 on 4h | 1, 3, 7 on 1d}; k {1.0, 1.5, 2.0}; H {3, 6, 12} bars.
C. Crowding (BTC/ETH/SOL metrics). R = log(count_long_short_ratio) (all-account ratio, "retail"),
   T = log(sum_toptrader_long_short_ratio) (top-trader position ratio). z over rolling W days.
   C1 contrarian retail: pos = -sign(z_R) when |z_R| > k. C2 follow the smart money vs retail divergence:
   D = z_T - z_R, pos = sign(D) when |D| > k. Held while condition holds, min hold H bars.
   IS grid: interval {4h, 1d}; W {14, 30, 90} days; k {1.0, 1.5, 2.0}; H {1, 6} bars (4h) / {1, 3} (1d).
D. Desk-breakout filter. Replicate the desk long breakout (2h, close > prior-60-bar high, EMA100>EMA400; 3*ATR(28)
   initial stop, chandelier best-close - 3*ATR checked vs bar low, exit on EMA100<EMA400; fills at next open, stops at
   stop price or the open if gapped) with my own simulator under the harness cost rules. At each entry record
   dOI over the last n 2h bars (n {12, 30, 60}) and the OFI over the same n; test whether trades with rising OI
   (dOI > 0, or z > 0) have better outcomes. Filter rule: skip entry if dOI_n < thr, thr in {0 (only rising OI), -inf (none)}
   and the mirror "skip if OI rising" in case the sign is reversed.

## Selection protocol
- All selection on IS (2020-01-01..2023-12-31; metrics features effectively 2020-09 BTC / 2021-12 ETH,SOL).
- Max 3 finalists to OOS, once each. Every evaluated config counted in TRIALS below.
- Sizing: vol-target chosen on IS (target IS max DD well under 35%); leverage stated. P7 checked on full period.
- P6 neighbour grid = each tuned parameter moved +/-1 step in the IS grid above (one at a time, others fixed),
  declared per finalist below BEFORE the OOS run.

## TRIALS log (running)
- [IS event studies, exploreA.py] A: 24 configs x 2 signals (raw z, price-residual z) = 48 cells. Findings: 4h/1d H=1
  flow is mildly CONTRARIAN (IC -0.025..-0.035, negative all 4 years); 1d n=3, H=3..6 flow is MOMENTUM (IC ~0.028,
  3/4 years positive, 2021 negative); residual flow (controlling price) IC ~0.033-0.037 (3/4 yrs).
- [exploreBC.py] B: 324 cells (iv x n x H x k x 4 regimes). Only "new longs" (price up & OI z>k) is consistent:
  2h n=6 H=3 k=1: +25 bps excess fwd return, 4/4 yrs, t~2.8 (overlapping). "new shorts" negative (follow, not squeeze),
  "flush" inconsistent (no capitulation-bounce edge in IS). "cover" weakly positive (3/4 yrs).
- [exploreBC.py] C: 36 cells. retail_contra (-z of log account L/S ratio) positive IC in 4/4 IS years in every
  4h H=6 and 1d H=3 cell (IC 0.044-0.11). top-trader signals inconsistent across years.
  Running trials: 408 event-study cells.
- [exploreC2.py] C momentum check, 24 signal cells: price momentum / price-vs-mean has NEGATIVE IC in IS at these
  horizons, corr(zR, price z) = -0.18..-0.55, and the retail signal residualised on price is STRONGER (IC 0.09-0.12,
  4/4 yrs). So retail_contra is not a disguised momentum signal.
- [btC.py] C1 costed IS grid, 72 configs (iv x W x k x H x side), 3-coin equal-weight (1x notional per coin account):
  ALL 72 have IS Sharpe >= 0.72; best 4h W90 k1.5 H6 LS 2.08; k=2.0 cells weaker (~1.0); long side > short side.
- [lagtest.py] leak check, 14 lag variants + side/coin splits: extra publication lag 15/65/245 min -> IS Sharpe
  2.08 -> 1.91/1.96/1.81 (4h). Bar lags 1/2/6 -> 1.79/1.58/1.03 (gradual decay, half-life ~1 day). Not a timestamp leak.
  Per coin IS Sharpe BTC 1.40 ETH 1.44 SOL 1.35. Long side 1.82, short side 0.81.
- [btAB.py] A1 OFI time-series on U12, 144 costed configs: NONE reaches IS Sharpe 0.7 (best 0.53). DEAD.
  B OI-confirmed moves, 108 costed configs: 17.6% >= 0.7; best 2h n6 k1.5 H3 long-only 1.35 (but 48% of return in 2023).
- [exploreD.py + btD.py] D desk-entry study, 20 feature cells + 6 filter variants. Rising OI into a breakout:
  rho +0.03..+0.06 pooled, sign flips by coin (BTC +, SOL -); skipping falling-OI entries would skip winners. DEAD.
  OFI(30 bars) filter: desk IS Sharpe 1.51 -> 1.64 but SOL 0.76 -> 0.46; OFI 60/120 no gain. Not robust -> not taken.
- Running trials: ~800 cells total (408 event-study, 24 C2, 14 lag, 72 C1, 144 A1, 108 B, 26 D).

## FINALISTS (declared before any OOS number; 3 taken to OOS, once each)
Sizing for all: three accounts (BTC, ETH, SOL), each at 1.0x notional when signalled (portfolio weight 1/3 per coin,
gross <= 1x), equal-weight blend = H.backtest_portfolio(weights = pos/3). IS max DD 10-15% -> no vol scaling needed.
F1 C1_ls_4h  : 4h bars. R_t = log(count_long_short_ratio) from the last metrics row stamped <= bar close - 5 min.
               z = (R - mean_540)/std_540 (90 days of 4h bars, min 180). Event: z > 1.5 -> short, z < -1.5 -> long.
               Hold the latest event for 6 bars (24h), re-armed by each new event; opposite event flips. Long/short.
               IS: Sharpe 2.08, ret +686%, DD 15.3%, 267 entries; 2x cost Sharpe 1.96. IS corr with desk 0.13.
F2 C1_long_4h: identical to F1 but long side only (short events -> flat). IS: Sharpe 1.82, DD 10.1%, 144 entries.
F3 B_newlong_2h: 2h bars. dOI = log(sum_open_interest) change over 6 bars, z vs 90d (1080 bars); long when
               z(dOI) > 1.5 AND 6-bar price change > 0; hold 3 bars (re-armed). IS Sharpe 1.30, DD 10.9%, 363 entries.
P6 neighbour grids (6 neighbours each, centre excluded; one parameter moved one step, edges extended by one natural step):
  F1/F2: W in {30, 180} days; k in {1.0, 2.0}; H in {1, 12} bars.
  F3   : n in {3, 12} bars; k in {1.0, 2.0}; H in {1, 6} bars.
P8: H.correlation_with_desk (all overlapping days) and H.blend_check (OOS) on the reported sizing.

## OUT-OF-SAMPLE RESULTS (3 finalists, each run once: out_final_*.json)
F1 C1_ls_4h    PASS (P1-P8 all hold). IS Sh 2.08 / OOS Sh 0.96, OOS +78.5%, OOS DD 29.6% (full 29.6%), entries 267/292,
               OOS at 2x cost +54.3% (Sh 0.75). Neighbours OOS Sh: W30 0.66, W180 0.95, k1.0 0.62, k2.0 0.66, H1 0.42,
               H12 0.91 -> median 0.66, 6/6 positive. corr with desk 0.21; OOS blend Sh 1.11 vs desk 0.85.
               Per year: 2024 +27.8%, 2025 +39.8%, 2026 -0.1%.
F2 C1_long_4h  FAIL (P2: OOS Sh 0.39 < 0.5; OOS +16.2%; blend 0.73 < desk 0.85 but corr 0.24 so P8 holds).
F3 B_newlong_2h FAIL (P2, P5, P6: OOS Sh -0.06, -3.9%; 2x cost -20.9%; neighbours median 0.04, 1/6 positive).
Post-hoc diagnostics on F1 (diag_F1.py; NOT used for any choice): per-coin OOS Sh BTC 1.16 (DD 24%), ETH 0.56 (DD 54%
at 1x), SOL 0.54 (DD 42% at 1x); OOS long side Sh 0.39 / short side 0.93; extra 65/245-min data lag -> OOS Sh 0.98/0.80;
OOS P&L concentrated: 292 trades, win rate 38%, top-10 trades = all of the net profit; 2026H1 half-year Sh -0.14.
