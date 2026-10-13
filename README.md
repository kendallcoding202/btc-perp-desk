# BTC Perp Desk (now BTC · ETH · SOL)

A multi-agent trading desk for **BTC, ETH and SOL perpetual futures** (three separate $150 paper accounts), modelled on the "GROKBOT + JEV" dashboard, with one difference: every number it trades on has been backtested, with fees and funding charged. A fourth paper account, **VT**, runs a different, slower strategy on the same three coins ([below](#vt-the-vol-trend-basket-a-4th-paper-account)).

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
| Total return | **+1,475%** ($150 → $2,363) | +1,059% |
| Per year | +50% | |
| Worst drop from a peak | **28%** | 77% |
| Out-of-sample (2024-01 → 2026-09; settings were picked on 2020–23) | +95%, profit factor 1.44 | +103% |
| Trades | 118 (≈17 a year), 42% winners, avg win 2.4× avg loss | |

By year: 2020 +128% · 2021 +10% · 2022 −5% · 2023 +231% · 2024 +69% · 2025 +33% · **2026 so far −11%**.

The honest read:
- **This is the aggressive profile you asked for.** Each trade risks 5% of the account. Leverage is set by the stop distance: a tight setup gets more (median 1.7×, max 4×). The worst losing streak so far was 8 trades in a row. At 2% risk the same strategy made +21%/yr with a 12% worst drop.
- **The backtest flatters it.** Many variants were tried to get here, and the 2h/5% choice was made after seeing them. 2026 has been a losing year. The paper experiment is the real test.
- **Whole-period profit isn't the result of one lucky setting.** Every nearby setting tested was profitable, and tripling fees and slippage still leaves it profitable.
- **$150 → $100,000 is a 667× goal.** At the whole backtest's +50%/yr that's about 16 years, but that pace includes 2020–23, where the settings were picked. At the 2024–26 out-of-sample pace, +28%/yr, it's about 26 years. The board projects every account at its own backtest's 2024–26 pace ("at 2024–26 backtest pace"), and once an account is 30 days old, at the pace it has actually made too. For ETH and SOL that period is only partly unseen: their 2% risk was picked after seeing ETH's 2024, and ETH's shorts were switched on after missing their own bar ([`reports/edge-hunt-2026-10.md`](reports/edge-hunt-2026-10.md)).

### ETH and SOL: same desk, smaller bets

ETH and SOL run the exact BTC strategy (nothing was re-tuned for them), at **2% risk per trade instead of 5%**. ETH also shorts, as a paper experiment (below). Reports: [`reports/backtest-ETH.md`](reports/backtest-ETH.md), [`reports/backtest-SOL.md`](reports/backtest-SOL.md).

| | ETH (2020-01 → 2026-09) | SOL (2020-09 → 2026-09) |
|---|---|---|
| Total return at 2% risk, as traded (ETH with its breakdown shorts) | +100% ($150 → $300) | +16% ($150 → $174) |
| Worst drop from a peak | 19% | 19% |
| First 60% of the period / last 40% | +53% / +31% | +1% / +15% |
| Buy & hold, same period | +1,956% (worst drop 81%) | +3,407% (worst drop 97%) |

Why 2%: at the BTC account's 5% risk, both hit the 40% drawdown halt in the backtest. ETH stopped in 2024 and SOL in 2021, which would have frozen the account. At 2% neither came close. The edge here is much thinner than on BTC: ETH about +11% a year with its shorts, SOL about +2.5%, and both lost money in 2024 (ETH −7%, SOL −12.5%). A wider 4 ATR stop looked better for both, but it was found after looking at these results, so it isn't trusted and isn't used.

### Design decisions the data made
- **2h candles, not 1h or 4h.** 2h beat 4h on 2020–2023 (+157% vs +104% at equal risk) and held up afterwards. 1h was worse: noise and fees.
- **3 of the 4 "soft" gates were removed** (trend strength, efficiency, extension). Tested on 2020–2023 only, each one cost money. They still show on the board as information.
- **Longs only.** Three short designs were tested on top of the longs: breakdowns, failed rallies, and bull traps (13 versions in all). The shorts' winnings never clearly beat their losses (0.64–1.12× in 2020–23, at most 1.00× after). Every version made the total worse and hit the 40% drawdown halt. Shorts lost even in 2022, when BTC fell 64%. The setups stay in the code (`allow_shorts`, `short_mode`) but off.
- **No shorts on ETH or SOL either.** The same 7 short versions were run on both at 2% risk. The pass rule was set first: the shorts must win at least $1.15 per $1 lost in both 2020–23 and 2024+. None passed.
  - **ETH breakdown shorts came closest.** They made money in both periods ($1.34, then $1.10 per $1 lost), and adding them lifted the backtest from +65% to +104% with a smaller worst drop (19% vs 26%). The edge since 2024 is thin, though: +$3.53 over almost 3 years.
  - **ETH shorts are on anyway, as a paper experiment you chose.** When the 100-candle EMA is below the 400-candle EMA, ETH shorts a 2h close below the 60-candle low: 3 ATR stop, trailing, covered when the trend flips back up. That adds about 15 trades a year. The board marks it as an experiment and shows a SHORT LINE while ETH is in a downtrend; alerts say SHORTED / COVERED SHORT. [`reports/backtest-ETH.md`](reports/backtest-ETH.md) now includes the shorts: +104% total, 19% worst drop, 2024 −7%. It's the only setting here that was switched on after missing its bar, so judge it on its paper record.
  - **SOL shorts worked in 2020–23 and then stopped.** Every version lost money from 2024 on ($0.63–0.89 per $1 lost).
- **No fixed take-profit.** The trailing stop already sells winners. Adding a profit target cut total return from +1,475% to between +213% and +898%, depending on the level.
- **Jev is advisory.** All 118 historical entries were replayed through Jev (with dates and prices stripped out). Its answers' correlation with trade outcome was about zero (−0.05 to +0.06). Its "follow-through" answer did split winners from losers somewhat (−$81 below its median, +$819 above), which is worth watching on paper, but it's too weak to give it a veto yet.

## Quick start

```bash
python3 -m pip install -r requirements.txt   # only needed for live trading and tests
cp .env.example .env                         # optional: JEV_API_KEY, NTFY_TOPIC

python3 -m desk backtest --research          # download history, write reports/backtest.md
python3 -m desk backtest --coin ETH          # same for ETH (or SOL): reports/backtest-ETH.md
python3 -m desk vt-backtest                  # the VT basket: reports/backtest-VT.md
python3 -m desk replay                       # watch it trade 2024→now at http://127.0.0.1:8787
python3 -m desk paper                        # a $150 paper account (add --coin ETH), live board at :8787
python3 -m desk step                         # one step for every account (BTC, ETH, SOL, VT), the way the Action runs it
python3 -m pytest                            # 112 tests, incl. "paper ledger == backtest" for the desk and for VT,
                                             # and live mode against a fake exchange (tests/test_live.py)
```

## The paper experiments

**Three $150 paper accounts** (BTC, ETH, SOL; $450 in all) trade live Hyperliquid prices, plus the **$500 VT basket** ([below](#vt-the-vol-trend-basket-a-4th-paper-account)). Each account is separate, with its own cash, positions and record; the three desk accounts also have their own kill switches. A GitHub Actions job ([`.github/workflows/paper.yml`](.github/workflows/paper.yml)) runs `python -m desk step` every 15 minutes, so the experiments keep running with no computer on. If one account's step fails (say, a rate-limited API call), the others still run and save, and the failed one catches up next run.

**Why $150, not $50.** Hyperliquid won't take an order under $10. The desk's ETH and SOL orders come to about 0.3–0.5× the account (BTC's about 1.7×), so at $50 some of their trades get skipped, and more after a losing streak. From every historical entry: BTC clears $10 on every trade from about $17 of equity; ETH and SOL need about $85–90, so $150 keeps them trading through a 40% drawdown. The accounts were restarted at $150 on 2026-10-12; the $50 runs (opened 10-09 and 10-10) had made no trades, and their ledgers are kept in `ledger/archive/` on the paper-ledger branch. One thing this changed: at $150 SOL takes 13 trades the $50 account had to skip (12 of them in 2021), and 12 of those 13 lost. Skipping them had flattered SOL's old report (+40%, now +16% at $150; 2024 onwards is the same either way).

- **The record** lives on the [`paper-ledger`](../../tree/paper-ledger/ledger) branch, saved whenever something happens, so the history is permanent and public:
  - `ledger/<COIN>/paper.json` (`BTC`, `ETH`, `SOL`): the account, open position, every trade, and the desk's memory
  - `ledger/<COIN>/kills.json`: that account's kill switches. Create or edit it on that branch, e.g. `ledger/SOL/kills.json` with `{"entry_kill": true}`.
  - `ledger/VT/paper.json`: the VT basket's cash, its three positions, every fill and closed position, and each coin's last 70 daily candles. A candle missing from one Hyperliquid answer is filled in from those, so the 50-day average never shifts. A candle no answer has had yet is waited for: it's a day with no candle, which that coin sits out as in the backtest, only once two answers have lacked it and it closed 3 days ago. VT has no kill switches.
- **The board** for your phone is published to GitHub Pages every run: **https://kendallcoding202.github.io/btc-perp-desk/** (open an account directly with `#ETH`, `#SOL` or `#VT`). It is read-only; kill switches go through `kills.json`.
  - Tabs at the top switch accounts and show all four balances, plus the total.
  - **Between runs, your browser pulls prices straight from Hyperliquid every 5 seconds.** The price, open-position P&L, balance and the candle that's still forming move live. Decisions, fills and the record change only when the desk runs: every 2h candle close, picked up within 15 minutes.
- **Locally:** `python -m desk paper` runs the same step every minute, with a live board and working kill buttons at http://127.0.0.1:8787. Don't run it against the same ledger as the Action, or the two will diverge.

Fills follow the backtest's rules exactly (a test enforces it):
- Decisions are made at each 2h close (every even UTC hour).
- Entries fill at the next candle's open.
- Stops are checked against each candle's range as it forms.
- Funding is charged every hour, as on Hyperliquid.

Each `step` processes whatever has closed since the last run, so a missed run never loses or double-counts a candle.

**Phone notifications:** set `NTFY_TOPIC`, install the [ntfy](https://ntfy.sh) app, and subscribe to the same topic. You'll get a push for every buy, sell, stop, or halt, and for every VT fill (bought, added, trimmed, sold).

## VT: the vol-trend basket (a 4th paper account)

**A paper experiment. It has no live mode, and `python -m desk live` won't take it.** It's a separate $500 account on the same Hyperliquid prices, saved and published with the others.

**What it does.** One account holds three sleeves, BTC, ETH and SOL, a third of the book each. Once a day, at the 00:00 UTC daily close, each coin gets a size:
- **Trend:** a coin is held only while its close is above its 50-day average. Below it, the sleeve goes to cash.
- **Size:** 0.40 ÷ the coin's 10-day volatility (annualised), at most 2 "units". One unit is 0.65 ÷ 3 of the account, so a calm coin gets more and a wild one less.
- **Band:** the size only changes when the new one is more than 25% away from what's held. Entries and exits always go through.
- **Fills:** at the next daily open, with the same fees, slippage and funding as the desk. Long only, no stops.

The numbers live in `thresholds.VolTrend` and are frozen. `desk/voltrend.py` is the only code that trades them, for both the backtest and the paper ledger, and a test holds the two equal.

**Why it was added.** The October 2026 edge hunt ([`research/edge-hunt-2026-10/`](research/edge-hunt-2026-10/), write-up in [`reports/edge-hunt-2026-10.md`](reports/edge-hunt-2026-10.md)) tested well over a thousand variants against a pass bar written down before any result was seen: settings chosen on 2020–23 only, then judged once on 2024-01 → 2026-09. This one passed, and then two independent skeptics re-checked it (a code review for look-ahead, cost and data errors, and a re-implementation from the rules alone). Most of the other ideas failed or are still waiting for evidence.

**What the backtest says** ([`reports/backtest-VT.md`](reports/backtest-VT.md), Binance perp daily bars, $500 start, the $10 minimum on):

| | VT | Buy & hold, a third each |
|---|---|---|
| 2020-01 → 2026-09 | **+519%** (+31%/yr), Sharpe 1.41, worst drop 26% | +2,210%, Sharpe 0.99, worst drop 88% |
| 2020–23 (where the rules were chosen) | +275%, Sharpe 1.84 | +1,746%, Sharpe 1.30 |
| 2024-01 → 2026-09 (out-of-sample) | **+65%, Sharpe 0.90**, worst drop 22% | +44%, Sharpe 0.52, worst drop 62% |

By year: 2020 +41% · 2021 +77% · **2022 −17%** · 2023 +81% · 2024 +19% · 2025 +18% · 2026 so far +18%.

**What to expect.** Something modest:
- **Expect less than the backtest.** The skeptics expect a Sharpe nearer 0.5–0.7 than 0.90. The board labels it as an experiment for that reason.
- **It loses in long bear markets.** In 2022 it lost 17% (holding the three coins lost 75%). 26 of the 28 positions it closed that year lost money: rallies inside the falling market pulled it back in, then failed.
- **Its edge over simply holding the coins isn't proven.** It beat buy & hold since 2024 on return, Sharpe and drawdown, but that is one stretch of under three years, and it mostly works as a slow trend filter: in the market for the rallies, out for most of the falls. A few big rally days carry much of the result: since 2024, without its 5 best days the +65% becomes +9%.
- **It moves with the desk more than against it.** Daily-return correlation with the three desk accounts is 0.51 since 2020 and 0.60 since 2024. It spreads the risk a little; it doesn't hedge it.

**Why $500.** Hyperliquid won't take an order under $10, and VT's orders are small: from every historical order, a new position is about 0.14× the account and a resize about 0.06×. The table shows fresh accounts opened on 2024-01-01 with the minimum on (orders blocked = entries and resizes it skipped):

| start | orders blocked since 2024 | return | Sharpe |
|---|---|---|---|
| $50 | 351 (most ETH and SOL orders: 115 of 141 and 171 of 200) | +91% | 1.14 |
| $150 | 52 | +69% | 0.95 |
| $250 | 28 | +63% | 0.89 |
| **$500** | **3** | **+62%** | **0.87** |
| $1,000 | 0 | +62% | 0.87 |
| no minimum | 0 | +62% | 0.87 |

$500 trades almost exactly the tested strategy, and $250 is close. The $50 account looks better only because the minimum keeps it out of most of its trades: that result is luck, not the strategy. The research simulation counted more blocked orders (4, 30 and 417) because it applied the 25% band to one continuous series instead of the sizes actually held; the report explains the difference. The start size is `thresholds.VolTrend.starting_equity`; it's yours to change before the account opens.

**On the board** (`#VT`), the VT tab shows the bank, P&L, goal and equity curve like the others, and instead of the price chart a holdings table: for each coin, how far its last close is above or below its 50-day average, its 10-day vol, the target and held sizes, what's held and its unrealised P&L. It also shows when the next decision is (every 00:00 UTC). Between runs, the bank and P&L move live with Hyperliquid's prices.

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

This is for the BTC, ETH and SOL desk accounts only. VT has no live mode.

`python -m desk live --coin BTC --max-notional 50` refuses to start unless **all** of these hold:
1. That coin's backtest report (`reports/backtest.json` for BTC, `reports/backtest-ETH.json`, `reports/backtest-SOL.json`) passes the go-live gate: 3+ years, profitable out-of-sample with profit factor ≥ 1.15, max drawdown ≤ 35%, never halted.
2. That coin's paper ledger has **14+ days** of trading and is up overall.
3. `DESK_LIVE_CONFIRM` is set to the exact phrase in `.env.example`.
4. You set a hard per-position dollar cap with `--max-notional`.

`--force-unproven` overrides 1 and 2: it prints a warning and carries on. 3 and 4 always hold. Don't use it.

It also refuses to start if:
- a credential is missing, or the account reads $0. On Hyperliquid, `HL_WALLET_ADDRESS` is the account the API wallet was approved for, never the API wallet's own address.
- the venue lists no perp for that coin.
- `--max-notional` can't buy the smallest ticket at the current price. That is the venue's minimum order ($10 on Hyperliquid) after the size step and the 3 bps slippage cushion: $10.80 on BTC at 83,000. It moves with the price, by up to one size step (about $1 of BTC at 100,000), so start at $12 or more.
- live state from before it was kept per coin is still in `state/`, or another live process is already running that coin.

A test holds each of these refusals (`tests/test_live.py`). If the exchange errors while live starts (a rate limit, a timeout), it keeps trying every 15 s rather than exiting, sets `entry_kill` and alerts your phone.

The live broker (`desk/brokers/live.py`) uses ccxt, pinned to 4.5.85 in `requirements.txt`. On Hyperliquid it uses an **API wallet**, not your main key, and switches off ccxt's builder fee and referrer, which the desk's costs don't include. The protective stop rests **on the exchange** as a reduce-only order, so it holds even if this process stops. **It has never been run against a funded account.** It has only been run against fakes: the tests' in-process exchange, and a fake Hyperliquid server behind the real ccxt class.

To run two coins, give each its own Hyperliquid sub-account: set `HL_SUBACCOUNT_ADDRESS` for that process (`HL_SUBACCOUNT_ADDRESS=0x... python -m desk live --coin ETH ...`), and give its board its own `--port`. Orders and cancels then carry the sub-account as `vaultAddress`, and every read asks for it. Two coins on one account would each size from the whole account's value. Don't keep orders of your own on a market the desk trades: it cancels any other stop it finds resting there.

**The five live-only bugs the October 2026 audit found ([`reports/edge-hunt-2026-10.md`](reports/edge-hunt-2026-10.md)) are fixed**, each with tests that fail on the old code:
- **ETH and SOL orders went to BTC**, at 30× the cap or more. Each coin now trades its own perp (`<COIN>/USDC:USDC` on Hyperliquid). Sizes are cut down to that market's size step and kept under the cap, and a ticket under the $10 minimum isn't sent.
- **A failed stop after an entry left the position unprotected.** A stop now counts as placed only once it's read back from the exchange's open orders, at its level and covering the whole position. It's tried 4 times (waiting 1, 2 and 4 s). If it still won't rest, the position is closed at market and an alert goes out: logged, on the board, and to your phone if `NTFY_TOPIC` is set. Every check (every 15 s) makes sure one stop is resting, puts it back if it isn't, and cancels any extra ones.
- **A stop that filled on the exchange was booked at the next price check.** It's now booked at the exchange's own fills, with their price and fee and the funding the exchange charged that coin. A position closed in pieces (part by hand, or a close that only partly filled, then the stop) is one trade, booked from all of its closing fills. If the fills can't be read, the read is retried. If they still can't be read, the exit is booked for now at the stop level with an alert, and re-booked from the fills once they can be read.
- **After a gap, live processed only the newest candle.** It now goes through every missed candle in order, and makes paper's decision on each as long as it holds what paper holds. Exits and trailing stops that paper would have acted on are acted on now. An entry from a missed candle isn't taken, because paper filled it at a price that has passed. So from then on live can be flat where paper is in the trade, and their decisions differ until paper is flat again. Sizing at missed candles uses the account's current value. The newest candle counts as fresh only for 5 minutes after its close; an entry from an older one isn't taken either. If the gap is longer than the 1,500-candle history window, live stops rather than guess.
- **State files were shared across coins.** Each coin's live files are now in `state/<COIN>/`. If an old shared file is still in `state/`, live refuses until you move it. The old code sent every coin's orders to BTC-PERP, so any position the file describes is on BTC.

A review of those fixes found more live-only problems, also fixed with tests (see the report):
- A sub-account's orders went to the main account. `HL_SUBACCOUNT_ADDRESS` now puts them on the sub-account.
- A desk close that failed once was dropped until the desk decided again, which for a daily-loss flatten meant the next UTC day. It's now sent again at every check until it goes through, and after a restart.
- A restart that hit one exchange error exited, even when it had to put a missing stop back. A restart in the middle of a catch-up could stop itself out on a candle paper held through.
- A trade's funding included the other coins' funding on the account; ccxt's builder fee would have added 1 bp to every order; a cap just above the minimum started and then never traded; `reset-halt --mode live` said "halt cleared" under a running live process, which wrote the halt back. Now `reset-halt` refuses until that coin's live process is stopped.

What is still open:
- Live has never placed an order on a real exchange. How the real venue shapes orders and fills, how fast new orders show up in its open orders, and its rate limits are unverified. Start with the smallest cap, and watch the first fills by hand.
- Only Hyperliquid was checked, against ccxt's code and Hyperliquid's public market list. The other venues' symbols follow ccxt's naming but weren't checked. Those venues may also need extra parameters before their stop orders show up in `fetch_open_orders`. Without them, the stop check would fail and close every new position.
- Any exchange error while placing an order still sets `entry_kill`, even when a retry then gets the order through. Clear it from the board once you've checked.
- The go-live gate still rests on 14 days of paper, which the audit calls noise.

Paper trading is not affected by any of this. The backtests (BTC, ETH, SOL, VT) and paper steps on copies of the real ledgers give byte-for-byte the same output before and after these fixes, apart from each report's generated time.

Perpetual futures are restricted in some countries, including for US persons on most venues. Check that your venue is legal where you live.

## Layout

```
desk/thresholds.py   every number the desk believes (change things here, then re-run the backtest);
                     COINS and COIN_OVERRIDES set which coins trade and their risk
desk/filter.py       the gate order: kills → data → hard → scan → soft → size → jev
desk/engine.py       the desk (CHIEF + RISK); identical in backtest, paper and live
desk/backtest.py     replay with costs; in/out-of-sample, per-year, go-live gate
desk/research.py     sensitivity, ablation, take-profit, sizing, cost and Jev tables
desk/voltrend.py     the VT basket: its rule, its book, and its backtest (one code path for backtest and paper)
desk/paper.py        the paper ledger step (one ledger per account under ledger/<COIN>/, VT's in ledger/VT/)
desk/jev.py          TypeSafe Jev client (POST api.typesafe.ai/v1/systemone)
desk/brokers/        sim.py (backtest + paper), live.py (ccxt)
desk/feeds/          hyperliquid.py (live), binance_vision.py (history)
desk/board.py        state → dashboard JSON (agent cards, bubbles, gates, thresholds; the VT holdings)
dashboard/           the board: no build step, phone-friendly
dashboard/spider.js  the walking CRAWLER: visits whatever the desk just did (gate cards in the order the desk
                     checks them, the newest candle, the trigger line, new fills) and curls up when the board
                     is stale. Decoration only; it changes no rule
```
