# meanrev_vol hunter — PLAN (written before any out-of-sample number was computed)

Family: short-horizon mean reversion and volatility regimes, 1h-4h bars (E uses 1d bars for daily rebalancing).
Harness: ../../harness.py (timing: decide at close t, fill at open t+1; costs 7.5 bps/side taker; real funding).
In-sample (IS) 2020-01-01..2023-12-31 is the ONLY data used for choosing. All IS grid runs TRUNCATE the
candles to index < 2024-01-01 before computing anything, so no OOS bar can leak in. OOS 2024-01..2026-09 is
touched once per finalist (max 3), plus the pre-declared P6 neighbour grid around each finalist.

## Universe / sizing
- Primary test book = "core3": BTC, ETH, SOL (what the desk can actually trade), each coin a 1/3 sleeve,
  signal in {-1,0,+1} x sleeve weight x leverage multiplier L. SOL sleeve is cash before 2020-09.
  Selection metric = IS daily Sharpe of the core3 book (via H.backtest_portfolio).
- Sanity book = "old12": BTC ETH BNB XRP ADA LINK LTC BCH TRX ATOM ETC XLM (all with data from early 2020;
  still survivorship-biased because the universe was picked in 2026). Used only as an IS sanity check
  (an idea that works on core3 but has the opposite sign on old12 is treated as suspicious), not for ranking.
- After choosing rules, L is set on IS so IS max DD <= ~25% (leaves headroom for P7 <= 35%).

## Hypotheses and exact rule families

A. Sigma-shock fade (outsized-move reversal / capitulation bounce)
   r_t = close_t/close_{t-1}-1; sig_t = std(r) over the prior 7 days of bars (t-L..t-1, excludes bar t).
   Long at close t if r_t < -k*sig_t [and volume filter]; short mirror if dir=both. Hold n bars
   (position active for bars t+1..t+n; a new trigger restarts the clock; opposite trigger flips).
   Volume filter: off | vol (volume_t > 2x median volume of prior 7 days) | flow (vol AND taker-buy share
   of bar volume < 0.45 for longs / > 0.55 for shorts = forced selling / buying).
   Grid: interval {1h,2h,4h} x k {2,3,4} x n {1,3,6,12} x filter {off,vol,flow} x dir {long,both} = 216.

B. RSI(2) / IBS dips, by regime
   RSI2 = Wilder RSI with period 2 on closes. Long at close t if RSI2_t < th; exit at close when RSI2 > ex.
   IBS_t = (close-low)/(high-low). IBS entry: long if IBS < th_ibs, exit when IBS > 0.5 at a close.
   Regime (EMA spans scaled to the desk's 2h time-scale: span = 100*2h/interval and 400*2h/interval):
   any | up (EMA_fast > EMA_slow) | flat (|EMA_fast/EMA_slow - 1| < 2%).
   Grid RSI: interval {1h,2h,4h} x th {5,10,20} x ex {50,70} x regime {any,up,flat} = 54.
   Grid IBS: interval {1h,2h,4h} x th_ibs {0.1,0.2} x regime {any,up,flat} = 18.     B total = 72.

C. Range trading only while the desk's trend filter says "no trend"
   Regime "no trend" = |EMA_fast/EMA_slow - 1| < g (spans scaled as in B), or ER(48 bars) < 0.3
   (Kaufman efficiency ratio). Bollinger(N, m) on closes: long when close < mid - m*sd, short when
   close > mid + m*sd, only entered while regime is "no trend"; exit when close crosses the mid band,
   when the regime ends, or after 2N bars.
   Grid: interval {1h,2h,4h} x N {20,50} x m {1.5,2,2.5} x regime {g<1%, g<2%, g<4%, ER<0.3} = 72.

D. Volatility compression -> expansion breakout (long only)
   Squeeze_t = Bollinger(20,2) bandwidth percentile rank over the prior 90 days of bars < q, OR NR7
   (bar range smallest of last 7). Armed for W=6 bars after a squeeze bar. Entry long at close t when armed
   and close_t > upper band (BB) or close_t > prior 20-bar high (HH). Optional trend filter = desk filter
   (EMA_fast > EMA_slow, spans scaled). Exit: close < mid band (mid), or close-based chandelier
   (close < highest close since entry - 3*ATR(28)) (chand).
   Grid BB-squeeze: interval {2h,4h} x q {10,20,30}% x trigger {BB,HH} x trend {none,desk} x exit {mid,chand} = 48
   Grid NR7: interval {2h,4h} x trend {none,desk} x exit {mid,chand} (trigger HH) = 8.  D total = 56.
   D-complement: desk-approx 2h breakout (close > prior 60-bar high & EMA100>EMA400, chandelier 3ATR on
   close) with vs without "a squeeze (q in {20,30}%) in the last 12/24 bars" gate: 1 baseline + 4 = 5.

E. Volatility targeting of a simple long exposure (1d bars, decided at the daily close)
   exposure_coin = min(2, tv / realized_vol(L days, annualised)) with tv = 40%/yr; rebalance only when
   |target - current| > band * current (no-trade band); gate: none | close > SMA(S days).
   Grid: L {10,30,60} x band {0.1,0.25} x gate {none, SMA50, SMA100} = 18 (core3 book).

Planned grid total ~= 216 + 72 + 72 + 56 + 5 + 18 = 439 trials (+ any extras logged below).

## Selection rules (IS only)
Rank by IS core3 daily Sharpe, require IS entries >= 30 (summed over coins), IS return > 0 at 2x costs,
and prefer a plateau: the IS median Sharpe of the same P6 neighbour box should be >= 60% of the pick's.
Prefer finalists from different families and with low IS correlation to H.baseline_daily()['desk'].
At most 3 finalists go to OOS, once each.

## P6 neighbour grids (pre-declared, +/-1 step on these ladders, categorical choices held fixed;
## neighbours = full box around the pick minus the centre)
- A: k ladder [2, 2.5, 3, 3.5, 4, 5]; n ladder [1, 2, 3, 6, 12, 24]                        (8 neighbours)
- B: RSI th ladder [3, 5, 10, 15, 20, 30]; ex ladder [40, 50, 60, 70, 80]; IBS th [0.05,0.1,0.15,0.2,0.3]
                                                                                            (8 / 2 neighbours)
- C: m ladder [1.5, 1.75, 2, 2.25, 2.5, 3]; N ladder [10, 20, 30, 50, 80];
     g ladder [0.5%, 1%, 2%, 3%, 4%, 6%] (ER ladder [0.2, 0.25, 0.3, 0.35, 0.4])               (26 neighbours)
- D: q ladder [5, 10, 15, 20, 30, 40]%; BB length ladder [14, 20, 30]; W ladder [3, 6, 12]       (26 neighbours)
- E: L ladder [5, 10, 20, 30, 60, 90]; band ladder [0.05, 0.1, 0.25, 0.5]; SMA ladder [30, 50, 100, 150, 200]
                                                                                            (26 / 8 neighbours)
A ladder end (e.g. n=1) has fewer neighbours; that is accepted as is.

## Cost notes
Main numbers at taker 4.5 bps + 3 bps slippage per side. Side note only: maker 1.5 bps + 0 slippage.

## Log (appended as work proceeds)

### Log 1 — IS grids done (no OOS touched). Trials so far: A 216 + B 72 + C 72 + D 56 + DK 5 + E 18 = 439
- A best: 1h k=4 n=3 long-only (filter irrelevant: 4-sigma bars nearly always have 2x volume): IS Sh 1.07,
  375 entries, exposure 2%, IS corr with desk 0.01, 2x-cost Sh 0.78. Fading UP-shocks (dir=both) hurts.
  k=2-3 variants die on costs.
- B best: 4h IBS<0.2 in desk uptrend, exit IBS>0.5: IS Sh 1.58 but cost-heavy (2x-cost Sh 0.58);
  1h RSI2<5 ex70 up: 1.43 (2x 0.45). Regime 'up' >> 'any' >> 'flat'.
- C: all 72 range-trading variants have NEGATIVE IS Sharpe (best -0.13). Clean fail.
- D best: 2h BB-squeeze q=0.2 + 20-bar-high breakout + desk trend filter + chandelier: IS 1.61, corr 0.42.
- DK: desk-approx core3 IS 1.30; + 'squeeze (q=0.2) in last 12 bars' gate: 1.65 (fewer trades 202->108).
- E best: vol-target (tv 40%, L=10d) long gated by close>SMA50d: IS 1.79, corr 0.44 (ungated ~1.1-1.3).
- Added (pre-OOS) neighbour ladder for DK, which PLAN did not spell out: gate q [0.1,0.15,0.2,0.3,0.4],
  gate W [6,12,24,48], BB length [14,20,30] (26 neighbours).

### Log 2 — IS robustness + diagnostics (still no OOS). Trials: 439 + 120 (IS neighbour boxes, per-coin,
### old12 sanity for 6 picks) + 3 diagnostics = 562 IS evaluations in total.
IS neighbour-box medians (all neighbours IS-positive in every case): A 0.86 (centre 1.07), B 1.24 (1.58),
B1 1.18 (1.43), D 1.43 (1.61), DK 1.50 (1.65), E 1.55 (1.80). old12 sanity IS Sharpe: A 0.89, B 1.06,
B1 1.23, D 0.38 (weak -> D de-prioritised), DK 0.61, E 1.08.
Diagnostics: E without vol-targeting (constant exposure, same SMA50 gate) IS Sh 1.71 vs 1.80 -> the edge in E
is mainly the SMA50 gate, not the vol targeting. Plain 'long whenever desk uptrend' on 4h: IS Sh 1.70 (DD 60%),
so B's 1.58 is partly uptrend beta; but IBS<0.2 in uptrend has a real next-4h edge (BTC +19.8 vs +3.5 bps,
ETH +20.6 vs +4.1, SOL +55 vs +11; t = 2.7 / 2.3 / 3.3). A: after a 4-sigma 1h drop the next 3h average
+37 / +56 / +165 bps (BTC/ETH/SOL; medians +22 / +36 / +58) vs ~+2-7 unconditional, well above 15 bps round trip.

FINALISTS (3, chosen on IS only, one per sub-family):
 1. crashfade = A: 1h, long-only, k=4, n=3, no volume filter. Sizing lev 1.0 (each coin sleeve = 1/3 equity,
    so 1/3x notional per triggered coin; IS DD 21%).
 2. ibsdip = B: 4h, IBS<0.2 while EMA50>EMA200 (4h; = desk EMA100/400 on 2h), exit at close with IBS>0.5.
    lev 1.0 (IS DD 22.5%).
 3. voltgt = E: 1d, exposure = min(2, 0.40/realised vol(10d)) per coin sleeve, only while close > SMA50,
    25% no-trade band. lev 0.65 (IS DD 26.5%).
 Not taken to OOS: DK (squeeze-gated desk breakout, IS 1.30 -> 1.65) — reported as an IS-only lead;
 D (weak on old12); B1 (same idea as B, lower IS Sharpe).
 P6 neighbours exactly as in the ladders above: A 8, B 2 (thin), E 26.

### Log 3 — OOS run once per finalist (2026-10-12T21:51:57Z): python3 final_<name>.py > out/final_<name>.json
OOS results (taker costs, verdicts per P1-P8 exactly as written; JSON in out/final_*.json):
 - crashfade: IS Sh 1.07 / OOS -0.93 (OOS ret -21.0%, 292 entries), 2x-cost OOS -31.7%, neighbours 0/8 positive
   (median OOS Sh -0.65). FAIL (P2, P5, P6). Maker side-note OOS still -11%.
 - ibsdip: IS Sh 1.58 / OOS -0.07 (OOS ret -6.1%, 1206 entries, costs 60% of equity), 2x-cost OOS -48.6%,
   neighbours 0/2 positive. FAIL (P2, P5, P6). Maker side-note OOS +52% / Sh 1.06 (optimistic: dip-buy limit
   orders suffer adverse selection).
 - voltgt: IS Sh 1.80 / OOS 0.90 (OOS ret +63.6%, DD 22.1%, 85 entries), 2x-cost OOS +58.0%, neighbours 26/26
   positive (median OOS Sh 0.69), corr with desk 0.50 full / 0.60 OOS, 50/50 blend OOS Sh 0.98 vs desk 0.85.
   PASS (all P1-P8) at lev 0.65.
Context only (candidate already frozen; not used for any choice): core3 buy&hold OOS Sh 0.46 (DD 32% at 0.4x,
64% at 1x); ungated vol-target OOS Sh 0.61 (DD 40%) -> the SMA50 gate is what adds value OOS too.
Single-account voltgt at lev 0.65: BTC OOS Sh 0.89 (DD 21%), ETH 1.06 (DD 27%), SOL 0.35 (DD 32%).
TOTAL: 562 IS evaluations; 3 finalists taken to OOS once each; 36 neighbour OOS runs for P6; 9 context runs.
