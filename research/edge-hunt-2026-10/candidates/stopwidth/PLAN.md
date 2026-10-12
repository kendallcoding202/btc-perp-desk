# desk_upgrades_p — PLAN (written before any out-of-sample number was computed)

Family: improvements to the desk's own 2h Donchian-60 / EMA100>EMA400 breakout, run on the REAL engine.
Engine: `xdesk.py` subclasses `desk.engine.Desk` and `desk.brokers.sim.SimBroker` (no repo edits). With no
extension it reproduces `desk.backtest.run` bit-for-bit on BTC, ETH and SOL (check_equiv.py: IDENTICAL curves and
trades). Same timing (decide at close, fill at next open), stops vs bar high/low (gap fills at open), fees 4.5 bps +
slippage 3 bps per side, real Binance funding, all brakes (10% day lock, 40% halt, 5-loss cooldown), crowding gate.
Live per-coin params (`params_for`): BTC 5% risk long-only; ETH 2% risk + breakdown shorts; SOL 2% risk long-only.

Known before starting (from the brief, not computed by me): baseline daily Sharpe IS/OOS BTC 1.41/0.87,
ETH 0.71/0.52, SOL 0.76/0.24, desk 1.51/0.85; BTC about -11% in 2026. I will not use these OOS numbers to choose.

## Split and scoring
In-sample 2020-01-01..2023-12-31 (selection ONLY). OOS 2024-01-01..2026-09-30, once per finalist.
Score = daily Sharpe from the engine's equity curve (same as make_baseline.py). "Desk" = equal-weight mean of the
three accounts' daily returns (same as baseline_daily.csv 'desk'). Entries = trades whose entry falls in the window.
Grid runs save the full curve to results/ but the grid/selection scripts PRINT ONLY in-sample numbers.

## Hypotheses and in-sample grids (each variant applied to all three coins with ONE shared setting)
- H-a re-entry after a stop-out while trend intact: next entry may trigger on the prior R-bar extreme instead of
  60 until next entry / trend flip. R in {10, 20, 30}.  Neighbours (P6): R +/- 1 step in {10, 20, 30, 45}.
- H-b time stop: one check at the close of the N-th bar held, exit if (close-entry)*dir < k*ATR.
  N in {12, 24, 48} x k in {0, 1}.  Neighbours: 3x3 box (minus centre) on N in {6,12,24,48,96}, k in {-0.5,0,0.5,1,1.5}.
- H-c1 chandelier on best HIGH (low for shorts) instead of best close, k in {3, 3.5, 4}.  Neighbours: k +/- 0.5.
- H-c2 trail width by vol regime: k_eff = 3 * clip((ATR336/ATR28)^alpha, 0.67, 1.5), alpha in {-0.5, 0.5, 1.0}.
  Neighbours: alpha +/- 0.5 (same W) and W in {168, 672} (same alpha).
- H-d pyramiding: add one unit (fresh-entry qty at current equity, total <= 5x) on a new 60-bar breakout while in
  the trade if cond holds. max_units in {2, 3} x cond in {be (stop at/through avg entry), 2atr (close >= avg entry +
  2 ATR)}.  Neighbours: max_units +/- 1 in {2,3,4} same cond, and the other cond at same units.
- H-e slow-vol sizing: size with ATR(W) instead of ATR(28) (stop unchanged). W in {84, 168, 360}.
  Neighbours: W +/- 1 step in {42, 84, 168, 360, 720}.
- H-f lookback ensemble: three equal sub-accounts with breakout lookbacks (a, 60, b), each at the coin's live risk,
  daily returns averaged (1/3 capital each). a in {20, 30, 40} x b in {90, 120, 180}.
  Neighbours: 3x3 box (minus centre) on a in {15,20,30,40,45}, b in {80,90,120,180,240}.
- H-g daily trend confirmation: long entries need last completed daily close > SMA(n) of daily closes (shorts <).
  n in {50, 100, 200}.  Neighbours: n +/- 1 step in {30, 50, 100, 200, 300}.
- H-s stop = trail width (ETH and SOL, selected PER COIN; BTC run for information only): k in {2.5, 3, 3.5, 4, 4.5}.
  Neighbours: k +/- 0.5. Risk per trade unchanged (wider stop = smaller size).

## Selection (in-sample only)
For each hypothesis pick the variant with the highest IS desk Sharpe. It enters the finalist pool only if it beats the
baseline IS desk Sharpe by >= 0.10 AND improves IS Sharpe on >= 2 of 3 coins (H-s: improves the coin's IS Sharpe by
>= 0.10). Finalists = top 3 of the pool by IS desk-Sharpe gain (if two pool members are compatible, their
combination may be one of the 3, counted as an extra trial). At most 3 go to OOS, once each.

## Pass rule applied to an upgrade
The "strategy" = the modified 3-account desk (unchanged coins at baseline). P2/P3/P5/P7 on the desk series; P4 on
total entries; P6 over the neighbour grid above; P8 = corr with 'desk' <= 0.6 OR 50/50 blend with the baseline desk has
higher OOS Sharpe than the desk alone. Also report per coin vs the matching baseline column. 2x costs = fee 9 +
slippage 6 bps per side. Sizing = live risk settings (5%/2%/2%, max 5x); report max DD.

## H-h (2026 diagnosis) — done LAST, after finalists' OOS, purely descriptive; nothing chosen on 2026.

## Trial log
(appended below as I go)

### Trial log 1 — in-sample grid (grid.py -> results/, select_is.py -> is_grid.txt; IS numbers only)
Variant configurations evaluated in-sample: 44 (+ baseline): H-a 3, H-b 6, H-c1 3, H-c2 3, H-d 4, H-e 3,
H-f 6 single lookbacks + 9 ensembles, H-g 3, H-s 4 (k=3.0 is the baseline). Each on BTC, ETH, SOL = 108 engine runs.
Baseline IS desk Sharpe 1.51 (BTC 1.41, ETH 0.71, SOL 0.76) — reproduces the brief.
Best per hypothesis (IS desk Sharpe; gate = >= 1.61 and >= 2/3 coins improved):
  H-a re30 1.37 FAIL | H-b ts48_0 1.53 FAIL | H-c1 hi4.0 1.36 FAIL | H-c2 reg0.5 1.51 FAIL | H-d pyr2_be 1.39 FAIL |
  H-e sz84 1.49 FAIL | H-f ens40_60_180 1.54 FAIL | H-g htf50 1.53 FAIL
  H-s per coin: ETH best k=3.5 (IS 0.96 vs 0.71, +0.25) PASS gate; SOL best k=4.0 (IS 1.30 vs 0.76, +0.54) PASS gate.
Finalist pool = {H-s}. Finalists taken to OOS: 1.
  F1 "stopwidth": BTC unchanged (3.0), ETH stop_atr = trail_atr = 3.5, SOL stop_atr = trail_atr = 4.0; risk per trade
  unchanged (5%/2%/2%, max 5x); ETH keeps breakdown shorts (same k applies to its shorts via stop_atr/trail_atr).
P6 neighbour grid for F1 (declared now, before OOS): 3x3 box minus centre over ETH k in {3.0, 3.5, 4.0} x SOL k in
{3.5, 4.0, 4.5} = 8 desk-level neighbours; also per coin: ETH {3.0, 4.0}, SOL {3.5, 4.5}.
The F1 final script uses the UNMODIFIED repo engine (desk.backtest.run), since stop/trail width are native params.

### Trial log 2 — F1 out-of-sample (final_stopwidth.py, run once; JSON in final_stopwidth.json)
Desk IS 1.65 / OOS 0.92 (baseline 1.51 / 0.85); OOS +49.2% vs +46.2%; OOS 2x cost +37.7%; max DD 12.3%.
P6: 8 desk neighbours median OOS 0.93, 8/8 positive. P8: corr 0.976 but 50/50 blend 0.889 > desk 0.852. All P1-P8 hold.
Per coin: ETH k3.5 OOS 0.71 vs 0.52 (neighbours 3.0: 0.52, 4.0: 0.75) — passes per-coin too; SOL k4.0 OOS 0.19 vs
0.24 (neighbours 3.5: 0.28, 4.5: 0.32) — SOL leg does NOT improve SOL, fails P2 and per-coin P8.
Paired block bootstrap (boot_f1.py): desk Sharpe gain +0.07 (5-95% -0.09..+0.25, P(<=0)=0.23); ETH +0.19
(-0.12..+0.59, P(<=0)=0.16). Not statistically decisive.
Caveat: the 4-ATR idea for ETH/SOL was first noticed post hoc by the desk on full-period data (brief), so this OOS
is not fully independent of earlier looks.

### H-h — 2026 diagnosis (descriptive only, after the finalist was evaluated; nothing is selected on it)
diag_2026.py (BTC live params): 13 trades in 2026, 23% winners (IS 44%), avg -0.16R (IS +0.84R), best +2.15R (IS +14.5R),
median MFE 1.57 ATR (IS 3.01). Entries not later than usual (median 4.9 ATR above EMA100 vs 4.2 IS; breakout margin
0.65 ATR = IS). ATR% lower (0.88 vs 1.16). Tape: EMA100>EMA400 on 47% of bars, buy&hold -5%. Losers clustered:
5 failed breakouts 11-22 Apr + 2 in early May. All exits are trail/initial stops. Diagnosis: range-bound year, breakouts
without follow-through, no fat-tail winner; not "stops too tight" (losers' MFE 0.5-2.4 ATR; wider BTC stops were worse IS).
-11% in 9 months: IS rolling-273-day min was -13.3% (<-11% in 0.3% of windows); 2026 DD 27.9% > IS max 22.5%.
diag_2026_variants.py (descriptive, IS-best of each hypothesis, 2026 BTC): re30 -22.6%, hi4 -16.2%, ens -16.5%,
pyr2_be -10.6%, reg0.5 -10.4%, sz84 -7.7%, htf50 / ts48 -11.4% (= baseline). None rescues 2026.

### Final count
In-sample variant configurations: 44 (+ baseline), 108 engine runs. Finalists taken to OOS: 1 (F1 stopwidth), once.
OOS neighbour runs for P6: 8 desk combos (6 extra engine runs). Verdict F1: PASS on P1-P8 as written; per-coin ETH leg
passes, SOL leg fails (P2, per-coin P8). Gain is small and not statistically decisive.
