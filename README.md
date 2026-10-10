# BTC Perp Desk (now BTC · ETH · SOL)

A multi-agent trading desk for **BTC, ETH and SOL perpetual futures** (three separate $50 paper accounts), modelled on the "GROKBOT + JEV" dashboard, with one difference: every number it trades on has been backtested, with fees and funding charged.

```
CHIEF    never takes a position     runs the gates in order, every 2h close
SCAN     finding what moves         60-bar (5-day) Donchian breakout, 100/400 EMA trend filter
VET      is the move real           efficiency ratio, volatility band       (asks Jev)
CROWD    who's on the other side    funding pressure: don't join a crowd paying 50%+ APR (asks Jev)
SIZE     how much                   5% of equity at risk per trade (aggressive), ≤5x leverage
FILLS    execution                  taker in, taker out; fees + slippage always charged
RISK     the stop                   3 ATR stop, trails every new high, never loosens; kill switches
CRAWLER  reads first, then trades   Hyperliquid live data; Binance archive for 6+ years of history
JEV      second opinion             TypeSafe AI's decision model, advisory (see why below)
```

> **Not financial advice. This can lose money.** Nothing here is a promise of profit. The results below are what history says. The future doesn't have to agree.

## What the backtest says (2020-01 → 2026-09, BTCUSDT perp, 2h candles, 5% risk per trade)

Full report with every table: [`reports/backtest.md`](reports/backtest.md). Re-run it any time with `python -m desk backtest --research`.

| | Desk | Buy & hold BTC |
|---|---|---|
| Total return | **+1,475%** ($50 → $788) | +1,059% |
| Per year | +50% | |
| Worst drop from a peak | **28%** | 77% |
| Out-of-sample (2024-01 → 2026-09; settings were picked on 2020–23) | +95%, profit factor 1.44 | +103% |
| Trades | 118 (≈17 a year), 42% winners, avg win 2.4× avg loss | |

By year: 2020 +128% · 2021 +10% · 2022 −5% · 2023 +231% · 2024 +69% · 2025 +33% · **2026 so far −11%**.

The honest read:
- **This is the aggressive profile you asked for.** Each trade risks 5% of the account. Leverage is set by the stop distance: a tight setup gets more (median 1.7×, max 4×). The worst losing streak so far was 8 trades in a row. At 2% risk the same strategy made +21%/yr with a 12% worst drop.
- **The backtest flatters it.** Many variants were tried to get here, and the 2h/5% choice was made after seeing them. 2026 has been a losing year. The paper experiment is the real test.
- **Whole-period profit isn't the result of one lucky setting.** Every nearby setting tested was profitable, and tripling fees and slippage still leaves it profitable.
- **$50 → $100,000 is a 2,000× goal.** At +50%/yr that's about 19 years at the historical pace. The board shows this projection live.

### ETH and SOL: same desk, smaller bets

ETH and SOL run the exact BTC strategy (nothing was re-tuned for them), at **2% risk per trade instead of 5%**. Reports: [`reports/backtest-ETH.md`](reports/backtest-ETH.md), [`reports/backtest-SOL.md`](reports/backtest-SOL.md).

| | ETH (2020-01 → 2026-09) | SOL (2020-09 → 2026-09) |
|---|---|---|
| Total return at 2% risk | +65% ($50 → $83) | +40% ($50 → $70) |
| Worst drop from a peak | 26% | 17% |
| First 60% of the period / last 40% | +32% / +25% | +22% / +15% |
| Buy & hold, same period | +1,956% (worst drop 81%) | +3,407% (worst drop 97%) |

Why 2%: at the BTC account's 5% risk, both hit the 40% drawdown halt in the backtest. ETH stopped in 2024 and SOL in 2021, which would have frozen the account. At 2% neither came close. The edge here is much thinner than on BTC: about +6–8% a year, and both lost money in 2024 (ETH −15%, SOL −12.5%). A wider 4 ATR stop looked better for both, but it was found after looking at these results, so it isn't trusted and isn't used.

### Design decisions the data made
- **2h candles, not 1h or 4h.** 2h beat 4h on 2020–2023 (+157% vs +104% at equal risk) and held up afterwards. 1h was worse: noise and fees.
- **3 of the 4 "soft" gates were removed** (trend strength, efficiency, extension). Tested on 2020–2023 only, each one cost money. They still show on the board as information.
- **Longs only.** Three short designs were tested on top of the longs: breakdowns, failed rallies, and bull traps (13 versions in all). The shorts' winnings never clearly beat their losses (0.64–1.12× in 2020–23, at most 1.00× after). Every version made the total worse and hit the 40% drawdown halt. Shorts lost even in 2022, when BTC fell 64%. The setups stay in the code (`allow_shorts`, `short_mode`) but off.
- **No fixed take-profit.** The trailing stop already sells winners. Adding a profit target cut total return from +1,475% to between +213% and +898%, depending on the level.
- **Jev is advisory.** All 118 historical entries were replayed through Jev (with dates and prices stripped out). Its answers' correlation with trade outcome was about zero (−0.05 to +0.06). Its "follow-through" answer did split winners from losers somewhat (−$81 below its median, +$819 above), which is worth watching on paper, but it's too weak to give it a veto yet.

## Quick start

```bash
python3 -m pip install -r requirements.txt   # only needed for live trading and tests
cp .env.example .env                         # optional: JEV_API_KEY, NTFY_TOPIC

python3 -m desk backtest --research          # download history, write reports/backtest.md
python3 -m desk backtest --coin ETH          # same for ETH (or SOL): reports/backtest-ETH.md
python3 -m desk replay                       # watch it trade 2024→now at http://127.0.0.1:8787
python3 -m desk paper                        # a $50 paper account (add --coin ETH), live board at :8787
python3 -m desk step                         # one step for every coin, the way the Action runs it
python3 -m pytest                            # 25 tests, incl. "paper ledger == backtest"
```

## The $50 paper experiments

**Three $50 paper accounts** (BTC, ETH, SOL; $150 in all) trade live Hyperliquid prices. Each account is separate: its own cash, position, record and kill switches. A GitHub Actions job ([`.github/workflows/paper.yml`](.github/workflows/paper.yml)) runs `python -m desk step` every 15 minutes, so the experiments keep running with no computer on. If one coin's step fails (say, a rate-limited API call), the other two still run and save, and the failed one catches up next run.

- **The record** lives on the [`paper-ledger`](../../tree/paper-ledger/ledger) branch, saved whenever something happens, so the history is permanent and public:
  - `ledger/<COIN>/paper.json` (`BTC`, `ETH`, `SOL`): the account, open position, every trade, and the desk's memory
  - `ledger/<COIN>/kills.json`: that account's kill switches. Create or edit it on that branch, e.g. `ledger/SOL/kills.json` with `{"entry_kill": true}`.
- **The board** for your phone is published to GitHub Pages every run: **https://kendallcoding202.github.io/btc-perp-desk/** (open a coin directly with `#ETH` or `#SOL`). It is read-only; kill switches go through `kills.json`.
  - Tabs at the top switch coins and show all three balances, plus the $150 total.
  - **Between runs, your browser pulls prices straight from Hyperliquid every 5 seconds.** The price, open-position P&L, balance and the candle that's still forming move live. Decisions, fills and the record change only when the desk runs: every 2h candle close, picked up within 15 minutes.
- **Locally:** `python -m desk paper` runs the same step every minute, with a live board and working kill buttons at http://127.0.0.1:8787. Don't run it against the same ledger as the Action, or the two will diverge.

Fills follow the backtest's rules exactly (a test enforces it):
- Decisions are made at each 2h close (every even UTC hour).
- Entries fill at the next candle's open.
- Stops are checked against each candle's range as it forms.
- Funding is charged every hour, as on Hyperliquid.

Each `step` processes whatever has closed since the last run, so a missed run never loses or double-counts a candle.

**Phone notifications:** set `NTFY_TOPIC`, install the [ntfy](https://ntfy.sh) app, and subscribe to the same topic. You'll get a push for every buy, sell, stop, or halt.

## Kill switches

| switch | what it does |
|---|---|
| `flat_kill` | close the position now, take no new entries |
| `entry_kill` | keep the position and its stop, take no new entries |
| `jev_kill` | stop consulting Jev |
| drawdown halt (auto) | down 40% from peak → flat and halted until `python -m desk reset-halt` |
| daily loss lock (auto) | down 10% on the UTC day → flat until tomorrow |
| cooldown (auto) | 5 losers in a row → sit out 6 candles |

## Going live (only after paper earns it)

`python -m desk live --max-notional 50` refuses to start unless **all** of these hold:
1. That coin's backtest report (`reports/backtest.json` for BTC, `reports/backtest-ETH.json`, `reports/backtest-SOL.json`) passes the go-live gate: 3+ years, profitable out-of-sample with profit factor ≥ 1.15, max drawdown ≤ 35%, never halted.
2. That coin's paper ledger has **14+ days** of trading and is up overall.
3. `DESK_LIVE_CONFIRM` is set to the exact phrase in `.env.example`.
4. You set a hard per-position dollar cap with `--max-notional`.

The live broker (`desk/brokers/live.py`) uses ccxt. On Hyperliquid it uses an **API wallet**, not your main key. The protective stop rests **on the exchange** as a reduce-only order, so a crash of this process never leaves a position unprotected. **It has not yet been run against a funded account.** Start with the smallest cap, and watch the first fills by hand.

Perpetual futures are restricted in some countries, including for US persons on most venues. Check that your venue is legal where you live.

## Layout

```
desk/thresholds.py   every number the desk believes (change things here, then re-run the backtest);
                     COINS and COIN_OVERRIDES set which coins trade and their risk
desk/filter.py       the gate order: kills → data → hard → scan → soft → size → jev
desk/engine.py       the desk (CHIEF + RISK); identical in backtest, paper and live
desk/backtest.py     replay with costs; in/out-of-sample, per-year, go-live gate
desk/research.py     sensitivity, ablation, take-profit, sizing, cost and Jev tables
desk/paper.py        the $50 ledger step (one ledger per coin under ledger/<COIN>/)
desk/jev.py          TypeSafe Jev client (POST api.typesafe.ai/v1/systemone)
desk/brokers/        sim.py (backtest + paper), live.py (ccxt)
desk/feeds/          hyperliquid.py (live), binance_vision.py (history)
desk/board.py        state → dashboard JSON (agent cards, bubbles, gates, thresholds)
dashboard/           the board: no build step, phone-friendly
```
