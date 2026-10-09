# BTC Perp Desk

A multi-agent trading desk for **BTC perpetual futures**, modelled on the "GROKBOT + JEV" dashboard, with one difference: every number it trades on has been backtested, with fees and funding charged.

```
CHIEF    never takes a position     runs the gates in order, every 4h close
SCAN     finding what moves         30-bar Donchian breakout, 50/200 EMA trend filter
VET      is the move real           efficiency ratio, volatility band       (asks Jev)
CROWD    who's on the other side    funding pressure: don't join a crowd paying 50%+ APR (asks Jev)
SIZE     how much                   2% of equity at risk per trade, ≤3x leverage
FILLS    execution                  taker in, taker out; fees + slippage always charged
RISK     the stop                   3 ATR stop, trails every new high, never loosens; kill switches
CRAWLER  reads first, then trades   Hyperliquid live data; Binance archive for 6+ years of history
JEV      second opinion             TypeSafe AI's decision model, advisory (see why below)
```

> **Not financial advice. This can lose money.** Nothing here is a promise of profit. The results below are what history says. The future doesn't have to agree.

## What the backtest says (2020-01 → 2026-09, BTCUSDT perp, 4h)

Full report with every table: [`reports/backtest.md`](reports/backtest.md). Re-run it any time with `python -m desk backtest --research`.

| | Desk (2% risk) | Buy & hold BTC |
|---|---|---|
| Total return | **+157%** ($50 → $129) | +1057% |
| Per year | +15.0% | |
| Worst drop from a peak | **12.3%** | 77% |
| Out-of-sample (2024-01 → 2026-09, never used for tuning) | +27.8%, profit factor 1.66 | +104% |

The honest read:
- **It has made money, but not spectacularly.** Holding BTC made far more, with far deeper crashes along the way. The desk is in the market 17% of the time and sits out the bear markets (2022: −2.8% vs BTC's −64%).
- **It has been weaker lately.** On Hyperliquid's own data from 2024-06 to 2026-10 it made +2.6%. 2025 was +2.8%, and 2026 so far is −4.7%.
- **Whole-period profit isn't the result of one lucky setting.** Every one of the 12 nearby settings tested (breakout length × stop width) was profitable, and tripling fees and slippage still leaves +126%.
- **$50 → $100,000 is a 2,000× goal.** At the backtested pace that's about 54 years at 2% risk, or about 21 years at the most aggressive setting tested, which had 41% drawdowns. The board shows this projection live.

### Design decisions the data made
- **3 of the 4 "soft" gates were removed** (trend strength, efficiency, extension). Tested on 2020–2023 only, each one cost money. Trend strength did the most damage: it rejects breakouts before the averages spread apart, which is exactly when entries pay best. They still show on the board as information.
- **Longs only.** Shorts added nothing in 2020–2023 and roughly doubled the drawdown. `allow_shorts` turns them back on.
- **No fixed take-profit.** The trailing stop already sells winners. Adding a profit target cut total return from +157% to between +43% and +116%, depending on the level.
- **Jev is advisory.** All 94 historical entries were replayed through Jev (with dates and prices stripped out). Its answers had about zero correlation with which trades won (−0.08, +0.04, +0.01). It's on the board; it gets no veto.

## Quick start

```bash
python3 -m pip install -r requirements.txt   # only needed for live trading and tests
cp .env.example .env                         # optional: JEV_API_KEY, NTFY_TOPIC

python3 -m desk backtest --research          # download history, write reports/backtest.md
python3 -m desk replay                       # watch it trade 2024→now at http://127.0.0.1:8787
python3 -m desk paper                        # the $50 paper experiment, live board at :8787
python3 -m pytest                            # 19 tests, incl. "paper ledger == backtest"
```

## The $50 paper experiment

`python -m desk paper` (or `python -m desk step` from a scheduler) trades a **$50 paper account** on live Hyperliquid BTC prices. Its record lives in [`ledger/`](ledger/), which is committed so the history is permanent:

- `ledger/paper.json`: the account, open position, every trade, and the desk's memory
- `ledger/board.json`: exactly what the dashboard shows
- `ledger/kills.json`: kill switches (edit it, or use the board's buttons)

Fills follow the backtest's rules exactly (a test enforces it):
- Decisions are made at each 4h close (00/04/08/12/16/20 UTC).
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
| drawdown halt (auto) | down 25% from peak → flat and halted until `python -m desk reset-halt` |
| daily loss lock (auto) | down 3% on the UTC day → flat until tomorrow |
| cooldown (auto) | 5 losers in a row → sit out 6 candles |

## Going live (only after paper earns it)

`python -m desk live --max-notional 50` refuses to start unless **all** of these hold:
1. `reports/backtest.json` passes the go-live gate: 3+ years, profitable out-of-sample with profit factor ≥ 1.15, max drawdown ≤ 35%, never halted.
2. The paper ledger has **14+ days** of trading and is up overall.
3. `DESK_LIVE_CONFIRM` is set to the exact phrase in `.env.example`.
4. You set a hard per-position dollar cap with `--max-notional`.

The live broker (`desk/brokers/live.py`) uses ccxt. On Hyperliquid it uses an **API wallet**, not your main key. The protective stop rests **on the exchange** as a reduce-only order, so a crash of this process never leaves a position unprotected. **It has not yet been run against a funded account.** Start with the smallest cap, and watch the first fills by hand.

Perpetual futures are restricted in some countries, including for US persons on most venues. Check that your venue is legal where you live.

## Layout

```
desk/thresholds.py   every number the desk believes (change things here, then re-run the backtest)
desk/filter.py       the gate order: kills → data → hard → scan → soft → size → jev
desk/engine.py       the desk (CHIEF + RISK); identical in backtest, paper and live
desk/backtest.py     replay with costs; in/out-of-sample, per-year, go-live gate
desk/research.py     sensitivity, ablation, take-profit, sizing, cost and Jev tables
desk/paper.py        the $50 ledger step
desk/jev.py          TypeSafe Jev client (POST api.typesafe.ai/v1/systemone)
desk/brokers/        sim.py (backtest + paper), live.py (ccxt)
desk/feeds/          hyperliquid.py (live), binance_vision.py (history)
desk/board.py        state → dashboard JSON (agent cards, bubbles, gates, thresholds)
dashboard/           the board: no build step, phone-friendly
```
