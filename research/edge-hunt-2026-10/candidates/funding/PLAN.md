# Funding & basis hunter — PLAN (written before any out-of-sample number was looked at)

Split / costs / timing / pass rule: exactly harness.py (IS 2020-01-01..2023-12-31, OOS 2024-01-01..2026-09-30,
7.5 bps per side per unit of exposure change, real Binance funding, decide at bar close, fill next open).
At most 3 finalists in total (across all sub-families) go to OOS, once each.

## Descriptive work (not strategy selection; may use full-period data because nothing is tuned on it)
- D1. How much funding the current desk pays (desk engine, live params), per coin and per year, vs fees.
- D2. Hyperliquid vs Binance funding level over the overlap (HL hourly prints summed to Binance 8h windows):
      mean APR, median difference, correlation, per year. Download HL into this directory (data_hl/).
- D3. Spot loader check (Binance spot zips switch to microsecond timestamps in 2025 -> harness H.spot() index
      breaks after 2025; I use my own loader that fixes the unit).

## Hypotheses and pre-declared grids

### (a) Cross-sectional funding carry (market-neutral)
Universe at day t: H.UNIVERSE coins with >= 60 days of funding history (new listings enter late; survivorship
bias noted). Daily bars (1d, decision at 00:00 UTC close; that print is already published).
Signal = trailing mean of funding prints over L days. Short the top-k, long the bottom-k, equal weight,
0.5 gross per side (gross 1.0). Rebalance every R days (weights held constant between).
Beta variant: legs scaled so beta-to-BTC (60d rolling daily) nets to zero.
Grid: L in {1,3,7,14} days x k in {3,5} x R in {1,3,7} days x beta in {off,on} = 48 variants.
P6 neighbours of a chosen point: +/-1 step in L, k, R (beta fixed at chosen value) -> up to 6 neighbours
(edge points have fewer).
Sizing: static gross leverage chosen on IS so IS annualised vol ~20% (cap 3x gross), then verify MDD <= 35%.

### (a2) Cash-and-carry, BTC/ETH/SOL (long spot + short perp, delta neutral), equal-weight 3-coin book
Enter when trailing L-day funding APR > theta_in, exit when < theta_in/2. Per-coin exposure: +1 spot, -1 perp
(per unit of capital in that sleeve; capital efficiency caveat). Spot leg: same 7.5 bps/side cost, no funding.
Grid: theta_in in {5,10,20,30}% APR x L in {1,3,7} days = 12 variants.
P6 neighbours: theta +/-1 step, L +/-1 step.
Sizing: 1x (it is delta-neutral; MDD is basis noise).

### (b) Funding as contrarian timing (BTC/ETH/SOL), 8h bars
F_L = trailing mean funding over L days, z = (F_L - rolling 365d mean)/rolling 365d std (min 180d, past only).
Step 1 (diagnostic, IS only): forward returns over 8h/1d/3d/7d by z quintile, per coin and pooled.
Step 2 rule: long when z < -z0 / short when z > +z0 (side chosen from step-1 IS diagnostics: long-only,
short-only or both), hold H, re-armed by fresh signals. 3-coin equal-weight book.
Grid: L in {1,3} x z0 in {1.5,2.0,2.5} x H in {1d,3d,7d} = 18 variants (per chosen side).
P6 neighbours: +/-1 step in z0, H, L.
Sizing: vol-target if needed (chosen IS).

### (c) Premium index (H.premium, 1h) extremes, BTC/ETH/SOL
P_h = mean premium-index close over the last P hours, z vs rolling 365d (past only). Same rule family as (b)
on 8h bars (side from IS diagnostic). Grid: P in {8,24} h x z0 in {1.5,2.0,2.5} x H in {8h,1d,3d} = 18.
P6 neighbours: +/-1 step in P, z0, H. Spot-perp close basis is checked for correlation with premium only.

### (d) Funding-aware overlay on the real desk engine (equal-weight BTC/ETH/SOL desk, live params)
d1: max_crowding in {off, 0.75, 0.80, 0.90, 0.95} (0.85 = current)                    -> 5 variants
d2: RELATIVE crowding: block a long (short) entry when the 3d funding APR is above (below) its own trailing
    365d q (1-q) quantile, q in {0.80, 0.90, 0.95}; mode "replace" the absolute gate or "add" to it -> 6 variants
P6 neighbours: q +/-1 step (same mode); for d1, threshold +/-1 step.
Scored on the desk's 3-account daily returns; entries = desk trades.

## Trial log (running count)
- (a) XS carry IS grid: 48 variants run (IS only). Trials so far: 48.
  IS picture: L=1-3 days strong (Sharpe 0.9-1.44), L=7-14 weaker; beta-neutral generally slightly worse.
  Both funding income AND price P&L are positive in IS (short high-funding coins also fell relative).
  Chosen centre (robust neighbourhood rather than the single best cell): L=3, k=5, R=1, beta off,
  IS Sharpe 1.21 at gross 1.0 (vol 32%, MDD 37%) -> report at gross 0.6 (IS vol ~19%).
  Neighbours for P6: (L=1,k5,R1) (L=7,k5,R1) (L=3,k3,R1) (L=3,k5,R3)  [k=7 and R=0 not in grid].
- D1/D2 descriptive done (d_describe.json, d1_desk_funding.json).
- (b)/(c) IS diagnostic (diag_bc.py): NO contrarian pattern. Extreme negative funding/premium z (< -2.5, n~55
  8h bars, a handful of crash episodes) is followed by FURTHER declines; z 1-2 is followed by gains (trend);
  z > 2 by small losses. Side chosen from diagnostic = short-only on z > z0 (the only contrarian side with any
  IS support). Grids run: (b) 18 variants, (c) 18 variants -> all IS Sharpe <= 0.39, most negative. REJECTED, no finalist.
- (a2) cash-and-carry IS grid: 12 variants; IS Sharpe 3.8-8.0, CAGR 7-14%, MDD <= 1-17%. Plateau.
  Chosen centre: theta=10% APR, L=7d (IS Sharpe 7.76, 42 entries). Neighbours: (5,7) (20,7) (10,3).
- (a) decomposition check: XS 3-day reversal (k5,R1) IS Sharpe -1.38 (1 trial); funding-signal rank corr with past
  3d/7d return ~0.00/0.02 -> the carry book is not disguised short-term reversal.
- Trials so far: 48 + 36 + 12 + 1 = 97.
- (d) overlay IS grid: 11 variants (d_overlay_is.csv). Stock desk IS Sharpe 1.515 (reproduces baseline).
  Absolute threshold sweep is flat (off 1.45, 0.75 1.56, 0.80 1.53, 0.90 1.50, 0.95 1.51).
  Relative crowding: q0.8/0.9/0.95 replace = 1.59/1.67/1.59; add = 1.63/1.66/1.60. Chosen: q=0.90 replace.
  Neighbours for P6: q=0.80 replace, q=0.95 replace.
- Trials so far: 97 + 11 = 108.
- AMENDMENT before any OOS look (IS-only reason): a_peryear.py shows the earlier XS pick (L3,k5,R1) is purely a
  2021 effect (2022 -13%, 2023 -10% at gross 1; IS Sharpe 2022-23 = -0.52). New IS selection rule for (a):
  positive Sharpe in every IS year, then the highest full-IS Sharpe -> L=7, k=3, R=1, beta off (IS Sharpe 1.09;
  yearly Sharpe 0.77/2.00/0.56/0.43). Only 2 of 48 cells pass the every-year filter (the other is its beta-on twin).
  P6 neighbours: (L3,k3,R1) (L14,k3,R1) (L7,k5,R1) (L7,k3,R3). Report at gross 0.6x.
- FINALISTS (3, each taken to OOS once): xs_carry (L7,k3,R1), cash_carry (theta 10%, L 7d), rel_crowding (q 0.90 replace).
- final_cash_carry.py first run crashed in the IS entry count (duplicate column names) before any OOS number was printed; fixed the column naming and re-ran (same rules).
- final_cash_carry.py: entry count was inflated by reindexing to BTC's 8h index (bar gaps = fake re-entries); now counted per coin on its own index. Returns unchanged; re-run.

## OOS results (each finalist run once; full JSON in final_*.json)
- xs_carry (L7,k3,R1, gross 0.6x): IS Sharpe 1.09 / OOS 0.88, OOS +39.1%, MDD IS 23% / OOS 10%, OOS 2x cost +17.3%,
  neighbours OOS Sharpe 0.14/0.95/0.95/0.70 (median 0.82, 4/4 positive), corr with desk -0.01, blend 1.28 vs desk 0.85.
  -> PASS by the letter of P1-P8. Caveats: survivorship-biased universe (long leg = lowest funding coins; dead coins
  like LUNA/FTT had very negative funding before collapsing and are absent), 2026 YTD +1.4%, IS pick was amended
  once on an IS-only per-year check. OOS P&L: price +0.39, funding +0.14, costs -0.17 (sums); long leg did most.
- cash_carry (10%/5% APR hysteresis, 7d): IS Sharpe 7.76 / OOS 6.83, OOS +11.8% (CAGR 4.1%), MDD 0.3%,
  entries IS 42 / OOS 16 (<20) -> PLAUSIBLE (thin OOS sample, P4). With HL funding (descriptive, Binance prices as
  proxy) 2023-06..2026-09: +49.8% vs +15.0% on Binance funding.
- rel_crowding (q 0.90 replace): desk IS 1.67 (stock 1.52) / OOS 0.878 (stock 0.852), OOS return 45.2% vs 46.2%,
  2x cost OOS +33.9%, neighbours OOS 0.92/1.02. PASS by the letter (P8 via blend 0.875 > 0.852) but the OOS gain
  over the stock desk is +0.03 Sharpe and -1 pt return: indistinguishable from noise; BTC worse, ETH/SOL better.
- Total trials: (a) 48 + 1 reversal + 1 universe check, (b) 18, (c) 18, (a2) 12, (d) 11 = 109.
