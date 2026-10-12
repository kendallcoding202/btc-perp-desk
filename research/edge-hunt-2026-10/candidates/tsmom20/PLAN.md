# PLAN — diversified trend-following (hunter: trend_portfolio)

Written 2026-10-12 BEFORE looking at any out-of-sample (2024-01-01 .. 2026-09-30) number.
Harness: ../../harness.py (timing: decide at close t, fill at open t+1; costs 7.5 bps/side on |delta exposure|;
real Binance funding; IS 2020-01-01..2023-12-31; OOS 2024-01-01..2026-09-30; pass rule P1-P8 as in docstring).

## Universe
- MAIN (used for all in-sample selection AND the OOS test): the 20 coins listed on Binance futures by 2020:
  BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT LTC BCH TRX ATOM NEAR UNI AAVE FIL ETC XLM.
  A coin becomes tradeable once its indicators are warm (no back-filling).
- EXTRA HOLDOUT (never used for selection): SUI APT ARB OP INJ (2022-23 listings). Reported separately, OOS only,
  with the finalist's frozen rules, as a sanity check. Not part of the pass rule.
- SURVIVORSHIP BIAS: the 25-coin universe was picked in 2026 from coins that still exist and are liquid. Dead /
  collapsed 2020-era majors (LUNA, FTT, EOS, XTZ, ...) are missing. This flatters LONG-only breadth portfolios in
  particular. Every result below inherits this bias.

## Hypotheses
H1 (family a): the desk's own 2h breakout rules, applied to all 20 coins with small EQUAL RISK per coin, give a
    higher and smoother Sharpe than 1-3 coins (breadth), and are a useful complement to the desk.
H2 (family b): a vol-targeted time-series-momentum (TSMOM) ensemble over 1-16 week lookbacks on daily bars,
    across the 20 coins, has OOS Sharpe >= 0.5 after costs and low-ish correlation with the desk.
H3 (family c): alts should only be held long when BTC itself is in an uptrend (BTC regime filter improves Sharpe/DD).

## (a) Desk breakout on every coin — rule family (own simulator, intrabar stops)
- 2h bars. Entry long: close_t > max(high[t-N..t-1]) and EMAfast_t > EMAslow_t (EMA = ewm(span, adjust=False)).
  Short (L/S variant only): close_t < min(low[t-N..t-1]) and EMAfast_t < EMAslow_t.
- Initial stop = close_t -/+ k*ATR28_t (Wilder ATR, ewm alpha=1/28). Chandelier: stop = max(stop, best_close - k*ATR_t)
  updated at each close. Stop is checked INTRABAR against the next bars' low/high; fill at the stop price, or at the
  open if the bar gaps through it. Trend-flip exit: EMAfast crosses back (long: fast < slow) at a close -> exit next open.
  Re-entry is allowed on any later close that qualifies.
- Timing: decision at close t, filled at open t+1 (harness rule); stop fills happen inside bar t+1.. ; costs 7.5 bps
  per side on every exposure change (incl. stop fills); funding charged on exposure held from each bar's open.
- Sizing: equal risk per coin: weight = r / (k*ATR_t/close_t) at entry (fraction of portfolio equity), held constant
  through the trade; per-coin cap 1.0x; portfolio gross cap 3.0x applied at entry (an entry is cut to fit the remaining
  headroom; coins processed in universe order).
  r (sizing, not a signal parameter) is picked on IS from {0.5%, 0.75%, 1.0%, 1.5%}: the largest with IS maxDD <= 30%.
- IS grid: center = desk params (N=60, k=3, EMA 100/400) x side {long-only, long/short} x BTC filter
  {none, gate (alt entries only when BTC 2h EMA100>EMA400), gate+exit (also exit alt longs when BTC flips down)} = 6.
  (For shorts under a BTC filter: alt shorts only when BTC EMA100<EMA400.)
  Plus an IS-only look at the 27-point cube N {40,60,90} x k {2.5,3,4} x EMA {(50,200),(100,400),(150,600)} for the
  best side/filter, to see whether the desk params sit on a plateau (I will NOT move away from the desk params unless a
  neighbour is clearly better on IS AND I say so).
- P6 neighbour grid (declared now): the full +/-1-step cube around the finalist's (N, k, EMA) on the steps
  N {40,60,90} (extend to 30/120 if the center is an edge), k {2.5,3,4} (2/5 if edge), EMA {(50,200),(100,400),(150,600)}
  ((200,800) if edge) — 26 neighbours, side/filter/sizing held fixed.

## (b) Vol-targeted TSMOM ensemble — rule family (harness backtest_portfolio, daily bars)
- Daily bars (00:00 UTC). For each coin and lookback L in a set: signal_L = sign(close_t/close_{t-L} - 1) ("sign") or
  clip((close_t/close_{t-L}-1) / (sigma_d*sqrt(L)), -1, 1) ("z"); coin signal s = mean over the lookback set.
- Lookback sets: base = {7,14,28,56,112} days (1..16 weeks); multiplier m in {0.5, 1, 2} (i.e. {4,7,14,28,56},
  base, {14,28,56,112,224}).
- Per-coin raw weight = s * (0.40 / sigma_ann_i) / N_eligible, sigma from the last VW=40 daily returns.
  Portfolio scaler: k_t = TV / (std of the raw portfolio's last 60 daily returns * sqrt(365)), clipped to [0, 4];
  gross exposure capped at 3.0x. Everything uses data <= close t.
- Side: long/short or long-only (s clipped at 0). BTC filter (c): none, or alt longs only when BTC's own s > 0.
- Turnover control: no-trade band B=0.2: a coin's held weight is updated only if |target-held| > B*|target|, or sign
  changes, or target/held is zero.
- IS grid: signal {sign, z} x m {0.5,1,2} x side {LS, long-only} x BTC filter {none, gate} = 24 variants, VW=40, B=0.2,
  TV=25%. Plus, for the best one only, the same rules on 4h bars (lookbacks x6 bars) = 1 more trial.
- Sizing: TV (portfolio vol target) picked on IS from {20%, 25%, 30%, 40%}: largest with IS maxDD <= 30%.
- P6 neighbour grid (declared now): full cube over m {x0.5, x1, x2 around finalist}, VW {20,40,80}, B {0.1,0.2,0.3}
  — 26 neighbours, signal type / side / filter / TV fixed.

## Selection (in-sample only)
Primary: IS daily Sharpe; must also have >=30 IS entries and IS maxDD <= 35% at the chosen sizing. Prefer the
desk-param center / simpler variant when IS Sharpe differs by < 0.1. Also look at IS correlation with the desk.
At most 3 finalists go to OOS, once each. Each gets final_<name>.py printing IS/OOS/full stats as JSON.

## Trial log (running count of variants evaluated)
(appended below as I go)

### In-sample results (2020-01-01..2023-12-31 only; no OOS number seen yet)
Simulator check (not counted as trials): my breakout sim on BTC alone at 5% risk vs desk baseline BTC IS:
Sharpe 1.31 vs 1.41, daily corr 0.91 (desk also has funding gate/brakes/cooldown that I don't model).

(a) desk breakout on 20 coins, r=1%/trade, coin cap 1x, gross cap 3x (intrabar stops, own simulator):
  center N60/k3/EMA100-400:  long none 0.31 (DD 66%), long gate 0.34 (58%), long gate_exit 0.35 (59%),
                             ls none 0.37 (82%), ls gate 0.42 (80%), ls gate_exit 0.42 (79%).  [6 trials]
  no gross cap, long none: 0.14 (DD 73%) [1 trial]. Per-coin IS Sharpe: only BTC 1.32, ADA 0.90, FIL 0.87, ETH 0.80,
  BNB 0.55 clearly positive; 11 of 20 coins negative. Gross P&L 1.35 vs costs 0.44 + funding 0.47.
  27-point cube (gate_exit), long: best 0.53 (N40 k4 100/400), 0.52 (N60 k4 100/400); all 27 in [-0.01, 0.53],
  DD 46-75%. ls: all in [-0.05, 0.42], DD 77-94%. [54 trials, 52 new]
  => H1 REJECTED IN-SAMPLE: every variant misses P3 (IS Sharpe >= 0.7) by a wide margin. Wider stop (k=4) is
  consistently the best row (alts whipsaw). Not taken to OOS as a pass candidate.

(b) TSMOM daily, 20 coins, VW40 B0.2 TV25% [24 trials]: long-only 1.27-1.65, L/S 0.94-1.12. BTC gate: +/-0.06
  except +0.14 at m=2. Best: sign m0.5 long gate 1.65; sign m1 long none 1.62 (within 0.1 -> pre-declared rule picks
  the simpler / base: sign, m=1, long-only, no gate). Best L/S: sign m1 ls none 1.12 (z m1 ls gate 1.12).
  Sizing [6 new trials]: long TV20/25/30/40 -> DD 16/21/24/32% => TV=30%. ls TV20/25 -> DD 24.6/30.2% => TV=20%.
  4h bars [2 trials]: long 1.59, ls 1.04 (no better than daily, more cost) -> stay daily.
  Subsets, long-only [3 trials]: 10 coins (MAIN[:10], today's cap order = hindsight) 2.03; ex-desk 17 coins 1.24;
  desk's 3 coins BTC/ETH/SOL 2.21 (TV20/25/30/40 -> DD 17/23/28/34% [3 new sizing trials] => TV=30%).
  => breadth LOWERS Sharpe in-sample: the majors trend best; adding small alts dilutes.

### Trial count before OOS: (a) 59 + (b) 38 = 97 variants evaluated in-sample.

### FINALISTS (frozen before OOS; 3 taken to out-of-sample, once each)
F1 tsmom20_long : TSMOM sign, lookbacks {7,14,28,56,112}d, long-only, no BTC gate, VW40, B0.2, TV30%, 20 MAIN coins.
F2 tsmom20_ls   : same but long/short, TV20%.
F3 tsmom3_long  : same as F1 on the desk's own 3 coins (BTC/ETH/SOL), TV30%. (Coins fixed by the desk, not chosen by me;
                  SOL's inclusion is itself 2026 hindsight.)
P6 cube for each: m {0.5,1,2} x VW {20,40,80} x B {0.1,0.2,0.3}, 26 neighbours, everything else fixed.
Extra holdout: F1/F2 rules on SUI APT ARB OP INJ, OOS only, reported separately.

### OOS (run once per finalist, 2026-10-12): see out_<name>.json
F1 tsmom20_long: OOS Sharpe 0.94, +100%, DD 34.0%, 2x cost +80%, nb median 0.83 (26/26 positive), OOS corr 0.49 -> all P pass
F2 tsmom20_ls  : OOS Sharpe 0.75, +52%, DD 25.6%, 2x cost +38%, nb median 0.72 (26/26 positive), OOS corr 0.25 -> all P pass
F3 tsmom3_long : OOS Sharpe 0.75, +69%, DD 38.4% (> 35%: P7 FAIL), corr 0.62, blend 0.863 vs 0.852 -> FAIL
Post-OOS checks below are diagnostics only; nothing is re-tuned.
Post-OOS diagnostics (no tuning): 1 extra day of execution delay -> OOS Sharpe F1 0.68, F2 0.49. Ex-TRX OOS Sharpe
F1 0.76, F2 0.67 (TRX = largest OOS contributor). New-coin holdout (SUI APT ARB OP INJ, OOS): F1 rules Sharpe 0.16
(+0.7%, DD 39%), F2 rules 0.12 (+0.5%, DD 33%). Median per-coin weight 2.9% (F1) / 1.5% (F2) of equity; median
rebalance 2.2% / 1.2% of equity; ~1650 / ~2850 orders per year -> not executable on $150 with $10 min orders.
TOTAL trials: 97 IS variants + 3 finalists + 78 pre-declared OOS neighbours (P6) + 4 post-OOS diagnostics.
