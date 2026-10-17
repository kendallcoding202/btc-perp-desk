# BTC Perp Desk (now BTC · ETH · SOL)

A multi-agent trading desk for **BTC, ETH and SOL perpetual futures** (three separate paper accounts: BTC $500, ETH and SOL $150 each), modelled on the "GROKBOT + JEV" dashboard, with one difference: every number it trades on has been backtested, with fees and funding charged. The BTC account simulates **Coinbase's US-regulated BTC perp**, the venue it's planned to trade with real money: whole 0.01 BTC contracts, its margin, sessions and fees ([below](#btc-on-coinbase)). A fourth paper account, **VT**, runs a different, slower strategy on the same three coins ([below](#vt-the-vol-trend-basket-a-4th-paper-account)).

```
CHIEF    never takes a position     runs the gates in order, every 2h close
SCAN     finding what moves         60-bar (5-day) Donchian breakout, 100/400 EMA trend filter
VET      is the move real           efficiency ratio, volatility band       (asks Jev)
CROWD    who's on the other side    funding pressure: don't join a crowd paying 50%+ APR (asks Jev)
SIZE     how much                   5% of equity at risk per trade (aggressive), ≤5x (BTC: whole Coinbase contracts, ≤4x)
FILLS    execution                  taker in, taker out; fees + slippage always charged
RISK     the stop                   3 ATR stop, trails every new high, never loosens; kill switches
CRAWLER  reads first, then trades   Coinbase (BTC) and Hyperliquid live data; Binance archive for 6+ years of history
JEV      second opinion             TypeSafe AI's decision model, advisory (see why below)
```

> **Not financial advice. This can lose money.** Nothing here is a promise of profit. The results below are what history says. The future doesn't have to agree.

## What the backtest says (2020-01 → 2026-09, BTC on Coinbase rules, 2h candles, 5% risk per trade)

Full report with every table: [`reports/backtest.md`](reports/backtest.md). Re-run it any time with `python -m desk backtest --research`. The history is Binance's BTCUSDT perp (Coinbase's BTC perp only listed in July 2025), traded under Coinbase's rules: whole contracts of 0.01 BTC at each candle's price, at most 4x, the overnight margin check, its sessions and an estimate of its fees ([BTC on Coinbase](#btc-on-coinbase)).

| | Desk, Coinbase rules | Fractional, as before (Hyperliquid) | Buy & hold BTC |
|---|---|---|---|
| Total return | **+1,126%** ($500 → $6,128) | +1,475% ($500 → $7,876) | +1,059% |
| Per year | +45% | +50% | |
| Worst drop from a peak | **28%** | 28% | 77% |
| Out-of-sample (2024-01 → 2026-09; settings were picked on 2020–23) | +86% (+26%/yr), profit factor 1.42 | +95%, 1.44 | +103% |
| Trades | 118 (≈17 a year), 39% winners, avg win 2.6× avg loss | the same 118 | |

By year: 2020 +103% · 2021 +8% · 2022 −5% · 2023 +210% · 2024 +64% · 2025 +31% · **2026 so far −12%**.

The honest read:
- **This is the aggressive profile you asked for.** Each trade risks up to 5% of the account. Leverage is set by the stop distance: a tight setup gets more (median 1.7×, max 3.8×). The worst losing streak so far was 8 trades in a row. At 2% risk it made +18%/yr with a 12% worst drop, but on whole contracts one contract often risks more than 2% of a small account: it took 94 of the 118 trades (59 entries skipped for size, a median of 1 contract).
- **Coinbase's rules cost about a quarter of the profit** over the whole history: +1,126% against +1,475% fractional. The report adds them one at a time: Coinbase's fee rate (5 bps against 4.5) cost 32 points (the 4x cap never bound), whole contracts 84, the $0.20-a-contract fee minimum 158 (it binds while BTC is under $40,000: 45 of the 118 trades, 2020–23), and the sessions 76, nearly all from one Friday entry that fell in the weekly break and went a bar later at a worse price (with whole contracts, one trade's difference also changes the sizes of the ones after it). Since 2024 it's +86% against +95%.
- **The backtest flatters it.** Many variants were tried to get here, and the 2h/5% choice was made after seeing them. 2026 has been a losing year. The paper experiment is the real test.
- **Whole-period profit isn't the result of one lucky setting.** Every nearby setting with the 3 or 4 ATR stop was profitable over the whole period, though three of the faster ones (20 and 30 bars with 3 ATR, 20 with 4) hit the 40% drawdown halt in 2026, and tripling fees and slippage still leaves it profitable. Under Coinbase's rules every 2 ATR stop hit the 40% drawdown halt in October 2020, after 19–30 trades: the tighter stop sizes 7–22 contracts at 2020's prices (one was worth $70–120), and at the $0.20-a-contract fee minimum each round trip cost $2.80–8.80, up to about 2% of a $500 account. The report's sensitivity table marks each halt.
- **$500 → $100,000 is a 200× goal.** At the whole backtest's +45%/yr that's about 14 years, but that pace includes 2020–23, where the settings were picked. At the 2024–26 out-of-sample pace, +26%/yr, it's about 23 years. With the planned $100 a month for 5 years it's about 14 years at that pace; a fresh $500 account opened in 2024 and fed $100 a month only made +22%/yr, time-weighted, because at $500 whole contracts are coarse, and at that pace it's about 16 years (about $11,700 after the 5 years, $6,500 of it deposited). These are backtest paces, not forecasts. The board projects every account at its own backtest's 2024–26 pace ("at 2024–26 backtest pace"; for BTC the pace of a fresh $500 account fed $100 a month in that backtest, +22%/yr, as that's its own situation), and once an account is 30 days old, at the pace it has actually made too. For ETH and SOL that period is only partly unseen: their 2% risk was picked after seeing ETH's 2024, and ETH's shorts were switched on after missing their own bar ([`reports/edge-hunt-2026-10.md`](reports/edge-hunt-2026-10.md)).

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
python3 -m desk paper                        # a $500 BTC paper account ($150 with --coin ETH or SOL), board at :8787
python3 -m desk step                         # one step for every account (BTC, ETH, SOL, VT), the way the Action runs it
python3 -m pytest                            # every test, incl. "paper ledger == backtest" for the desk, for BTC on
                                             # Coinbase rules and for VT, live mode against a fake exchange
                                             # (tests/test_live.py), and SHADOW and the locked Coinbase LIVE path
                                             # against a local fake Coinbase (tests/test_coinbase_shadow.py)
```

## The paper experiments

**Three desk paper accounts** (BTC $500, ETH and SOL $150 each; $800 in all) trade live prices, BTC Coinbase's ([below](#btc-on-coinbase)) and ETH and SOL Hyperliquid's, plus the **$500 VT basket** ([below](#vt-the-vol-trend-basket-a-4th-paper-account)). Each account is separate, with its own cash, positions and record; the three desk accounts also have their own kill switches. A GitHub Actions job ([`.github/workflows/paper.yml`](.github/workflows/paper.yml)) runs `python -m desk step` every 15 minutes, so the experiments keep running with no computer on. If one account's step fails (say, a rate-limited API call), the others still run and save, and the failed one catches up next run.

**Why BTC is $500.** BTC is the account planned for real money first, on Coinbase's US-regulated nano BTC perp (Hyperliquid isn't available in the US). Coinbase trades whole contracts of 0.01 BTC, about $830 at $83,000, with about 21% overnight margin, so a $150 account can't hold even one contract. The paper account mirrors the planned $500 start. It was restarted at $500 on 2026-10-13; the $150 run (opened 2026-10-12, no trades) is kept in `ledger/archive/` on the paper-ledger branch. The BTC paper account now simulates Coinbase's rules, whole contracts included ([BTC on Coinbase](#btc-on-coinbase)); the $500 Hyperliquid run opened that day is refused rather than converted, for you to archive.

**Why $150 for ETH and SOL, not $50.** Hyperliquid won't take an order under $10. The desk's ETH and SOL orders come to about 0.3–0.5× the account (BTC's about 1.7×), so at $50 some of their trades get skipped, and more after a losing streak. From every historical entry: BTC clears $10 on every trade from about $17 of equity; ETH and SOL need about $85–90, so $150 keeps them trading through a 40% drawdown. The accounts were restarted at $150 on 2026-10-12; the $50 runs (opened 10-09 and 10-10) had made no trades, and their ledgers are kept in `ledger/archive/` on the paper-ledger branch. One thing this changed: at $150 SOL takes 13 trades the $50 account had to skip (12 of them in 2021), and 12 of those 13 lost. Skipping them had flattered SOL's old report (+40%, now +16% at $150; 2024 onwards is the same either way).

- **The record** lives on the [`paper-ledger`](../../tree/paper-ledger/ledger) branch, saved whenever something happens, so the history is permanent and public:
  - `ledger/<COIN>/paper.json` (`BTC`, `ETH`, `SOL`): the account, open position, every trade, and the desk's memory. BTC's also says `"venue": "coinbase"` and keeps Coinbase's funding log, margin rates and maintenance windows
  - `ledger/<COIN>/kills.json`: that account's kill switches. Create or edit it on that branch, e.g. `ledger/SOL/kills.json` with `{"entry_kill": true}`.
  - `ledger/VT/paper.json`: the VT basket's cash, its three positions, every fill and closed position, and each coin's last 70 daily candles. A candle missing from one Hyperliquid answer is filled in from those, so the 50-day average never shifts. A candle no answer has had yet is waited for: it's a day with no candle, which that coin sits out as in the backtest, only once two answers have lacked it and it closed 3 days ago. VT has no kill switches.
- **The board** for your phone is published to GitHub Pages every run: **https://kendallcoding202.github.io/btc-perp-desk/** (open an account directly with `#ETH`, `#SOL` or `#VT`). It is read-only; kill switches go through `kills.json`.
  - Tabs at the top switch accounts and show all four balances, plus the total.
  - It's drawn as a comic lab notebook: a day edition on cream paper and a night edition on dark paper, which follows your phone's light or dark setting; `day · night · auto` in the footer picks one for that device. A gain carries ▲, a loss ▼ (in magenta, not red), zero neither; on the chart, entries are L / S pins and up candles are hollow. Nothing sits over the chart: each agent's note is on its card, and while you hover a card or tab to it, the chart marks what the note is about, when the chart draws it: SCAN's 60-bar line with the price its note gives (and the next close's, when the line steps at the candle still forming), VET's fast and slow averages, RISK's and SIZE's stop and entry while there's a position, FILLS' fills. On a phone a tap marks it for 3 s; when none of the chart is on screen, the tap brings the chart into view first, marks it a second longer, and then goes back to the card. A mark stays on its own tab: switching accounts ends it. The gold live line stays on top of a mark, and no name sits on it. The spider's web is drawn faintly behind the candles.
  - **Every time on it is Mountain Time** (America/Denver, labelled `MT`), whatever zone your phone is set to: the run's time and date in the header, `LAST RUN`, the countdown (`NEXT 2H CLOSE 6:00 PM MT IN 0:42:10`), the chart's days, the desk log and its tape, the footer, Coinbase's closures (its weekly break is 3:00–4:00 pm MT all year) and VT's daily decision. The desk's day still turns at 00:00 UTC: that's 6:00 pm MT on daylight time and 5:00 pm MT from 2026-11-01, by the zone's own rules, never a fixed offset. The phone pushes say MT too. The ledgers keep their timestamps in UTC and their own words; only what's shown changes.
  - **Between runs, your browser pulls prices straight from Hyperliquid every 5 seconds.** The price, open-position P&L, balance and the candle that's still forming move live. So do the account cards in the tab row and the ALL total: each card is re-marked at the same price its own tab uses (cash plus the open position's move), so it reads the same whichever tab is open. Decisions, fills and the record change only when the desk runs: every 2h candle close, picked up within 15 minutes.
  - **The BTC tab takes Coinbase's own price, about once a second,** from Coinbase's public market-data feed (`wss://advanced-trade-ws.coinbase.com`, its ticker channel for the board's product, no key). A page can't call Coinbase's REST API (it sends no CORS header), but a WebSocket isn't bound by CORS. The feed counts as fresh while a heartbeat came in the last 5 seconds (Coinbase sends one a second) and a ticker message has come on this connection in the last minute, timed by when each arrives (never by Coinbase's own stamp, so one stamped in the future can't hold the price back, and a phone clock that's set back doesn't keep an old price fresh). Coinbase documents a ticker message for each trade; measured, one also comes when the best bid or ask moves, about two a second in all. Its price is Coinbase's last trade, so it stays the right price through a quiet spell with no trade. A minute with no ticker message at all, outside a closure the board knows of, means trading has stopped or the subscription was lost: the tab falls back to the proxy and the page opens a new connection. After that, and after any closure, it takes a new update (a trade or the book moving), not the snapshot of the price from before, to bring Coinbase's price back. While fresh, the price, P&L, balance, the chart title and the BTC card in the tab row use it (on the other tabs too, from the first load: the index carries BTC's product and closures), the tab says `LIVE (COINBASE)` and `Price (Coinbase live)`, the chart shows it as a solid gold line ending in one `● LIVE 82,290` tag on the price at the right edge, its dot beating once for each new price (ETH's and SOL's charts mark Hyperliquid's price the same way while it's under 20 seconds old; the proxy's price, a stale one or a closed market never get the marker), and the footer says `live price 0s ago from Coinbase`. When it isn't fresh (the feed refused, gone quiet or reconnecting), the tab falls back to Hyperliquid's BTC as a proxy and says so, as before (`LIVE (HYPERLIQUID PROXY)`, `Price (HL proxy)`, `(HL proxy)` in the chart title); the two are never mixed in one number. If Hyperliquid hasn't answered for 20 seconds either, the tab says `STALE (HYPERLIQUID PROXY)` instead of live, as the desk monitor says `no live price`, and the footer says `stale price` with its age. The BTC card in the tab row says whose price it shows in a small word over it: `cb`, `hl proxy` (`hl stale` once that's 20 seconds old), `closed`, or `last run` before any live price. While Coinbase is closed, as the last run found it or inside a closure on the board's schedule (its weekly break or a maintenance window), the tab isn't re-marked at all (`CLOSED (COINBASE)`), and the BTC card on the other tabs shows the board's price too. A scheduled closure the last run found stays closed past its end until Coinbase's own feed shows it trading again or a run finds it open; one Coinbase reported outside its schedule stays closed until a run finds it open. The page opens the socket only while the BTC account is on screen and the page is visible, and retries a dropped one after 2 s, doubling to at most a minute. VT keeps Hyperliquid's BTC, like its other coins. The desk itself still marks BTC at Coinbase's price at every run.
  - **BTC's chart ends with the 2h candle that's still forming, on Coinbase's prices only.** Each run already reads it from Coinbase (paper fills an order at its open and checks the stop against it); the board now carries it as `chart.forming` with the run's time, and the chart draws it as the newest candle. While Coinbase's feed is on, the page carries it on: its close is the newest price, its high and low widen with each price the feed sends. The page only sees prices from when it opened, so a high or a low between the run and then can be missing, and once the window rolls the page knows only what it saw. A candle built that way is drawn **provisional**: a dashed outline in both editions, `newest candle ▯ provisional` in the chart's legend, and in the footer `newest candle: provisional, from Coinbase's live feed; the desk's own candle replaces it at the next run`. The run's own candle, as the run answered it, is provisional too whenever nothing newer has carried it on (the feed down and Hyperliquid's price the proxy, Coinbase closed since the run, or its window closed): the footer says `newest candle: provisional, the desk's as of 9:03 pm MT`, and its close tag on the price axis is outlined in dashes and reads `RUN 81,995`, never filled as if it were the current price. It never uses Coinbase's candles channel (measured incomplete) and never Hyperliquid's candle; the desk's decisions never read it. ETH and SOL keep Hyperliquid's forming candle.
  - **A page left open updates itself to new code.** Each deploy is stamped with the commit it was built from (`tools/stamp_site.py`: `version.json`, and a `?v=` on the page's own scripts and styles so no cached copy is kept). When the page comes back to the foreground, and every 10 minutes while it shows, it reads `version.json` and on a new build reloads once, on the same tab and back where it was scrolled to, keeping the day / night choice and the SHADOW key; never while the SHADOW key is being typed, and each page at most once every 15 minutes (GitHub's CDN can serve the old page for a few minutes after a deploy; the reload's address carries `?b=` and the new build, which the page takes off as it loads, so that holds even where the browser loses the tab's storage). The footer shows the build (`build e94d763`) and says `this page updated itself to the newest version at 9:12 am MT` after it did. Runs that only bring new boards keep the same build. A page opened before this was deployed needs one reload by hand.
- **Locally:** `python -m desk paper` runs the same step every minute, with a live board and working kill buttons at http://127.0.0.1:8787. Don't run it against the same ledger as the Action, or the two will diverge.

Fills follow the backtest's rules exactly (a test enforces it):
- Decisions are made at each 2h close (every even UTC hour).
- Entries fill at the next candle's open.
- Stops are checked against each candle's range as it forms.
- Funding is charged every hour, as on Hyperliquid and Coinbase (BTC: at the nearest rate Coinbase's funding log has, an estimate).
- BTC trades whole Coinbase contracts, and no entry goes into Coinbase's weekly break or a maintenance window.

Each `step` processes whatever has closed since the last run, so a missed run never loses or double-counts a candle.

**Phone notifications:** set `NTFY_TOPIC`, install the [ntfy](https://ntfy.sh) app, and subscribe to the same topic. You'll get a push for every buy, sell, stop, or halt, and for every VT fill (bought, added, trimmed, sold).

## BTC on Coinbase

The BTC account is planned to trade real money on **Coinbase's US-regulated BTC perp** (Coinbase Financial Markets: "BTC PERP", product `BIP-20DEC30-CDE`, nano BTC perp-style futures; Hyperliquid isn't available in the US). So its paper account and its backtest simulate Coinbase as far as Coinbase's public data allows. The strategy is BTC's as before: the same signals, stop, trailing and exits. These are the venue's rules on top of it, in `desk/venue.py`, with the numbers in `thresholds.COINBASE` (read from Coinbase's public API on 2026-10-13). The backtest and the paper account use the same code, and a test holds them equal on the same candles.

**What's simulated**
- **Whole contracts of 0.01 BTC.** The desk's risk-based size (5% of the account lost at the stop) is cut down to whole contracts at the candle's price, never rounded up, and at most 4x the account (Coinbase's overnight margin, about 21%, would allow about 4.76x). If that comes to 0 contracts the entry is skipped, and the board and your phone say why, once: `SIZE: skipped — one contract ($832) would risk 7.1% at this stop (budget 5%)`. At $83,000 a contract is about $830, so a $500 account's $25 budget buys one while the stop is within about $2,500 (3%) of the price, and two within 1.5%: at $500 most trades risk less than the 5%, and a wide stop skips the trade. Deposits make the sizes finer.
- **The overnight margin check.** Equity minus the loss at the stop must cover the contracts' notional × Coinbase's overnight long margin rate (21.025% on 2026-10-13; paper reads it every run and falls back to the last one it saved). If it doesn't, contracts are cut until it does, and with none left the entry is skipped. At 4x and 5% risk it always fits at today's rate.
- **Sessions.** Coinbase's BTC perp trades around the clock except for a weekly break, Friday 17:00–18:00 New York time (21:00–22:00 UTC in summer, 22:00–23:00 UTC in winter; Coinbase's own 5-minute candles show it), and maintenance windows Coinbase lists ahead (about quarterly, a Saturday; paper records each one it reads, and the ones seen since July 2025 are in `thresholds.COINBASE`). No entry is decided while its fill would fall in a closure, nor while Coinbase says its session is closed (that flag speaks for a close decided on time; a close a missed run decides late goes by the schedule, as in the backtest), nor while Coinbase's answer has no session details at all (said once, on the board and your phone). Coinbase has no candles while it's closed, so a stop isn't checked then, and a stop the price gapped through fills at the reopen. The backtest leaves out the Binance candles that fall wholly inside a known window, to match. The weekly hour sits inside one 2h candle, so the backtest can't tell which part of that candle's range traded: it checks the stop against all of it.
- **Coinbase's own market data** for the paper account: 2h candles (paged 300 at a time to the 1,500 the desk needs), the hourly funding rate, the book (spread, and depth as contracts × 0.01 BTC × price within 10 bps), open interest, margin rates and the session, from Coinbase's public API with no key (`desk/feeds/coinbase.py`). A short, failed or odd answer (a contract size that isn't 0.01 BTC, order-size rules that changed, a page that isn't JSON after its retries, say) stops BTC's step for that run; the next run catches up. While Coinbase is closed it may have no book at all: that's expected then, and the step goes on without a spread or depth. A candle Coinbase should have (no closure explains it) that an answer lacks is waited for: the bars from it on aren't processed until an answer has it, so a stop or a signal on it is never lost. Once two saved runs' answers lacked it and it closed 6 hours ago, it's a candle Coinbase never published: a desk event says so and the account goes on without it. More than 12 such candles in one answer is a short answer.

**What's estimated** (the board marks the fees and funding "est.", and its rules line says stops are simulated)
- **Fees.** Coinbase's all-in fee per contract isn't confirmed. The desk charges max($0.20 a contract, 0.05% of the notional) per side, plus the usual 3 bps of slippage: the higher of the two schedules Coinbase has published (2025 marketing says "as low as 0.02%, $0.15 minimum"; an older footnote, 0.05% and $0.20). At $83,000 the 0.05% binds ($0.42 a contract). The minimum bound while BTC was under $40,000 (2020–23 in the backtest). Replace both (`fee_min_per_contract` in `thresholds.COINBASE`, and BTC's `costs.taker_fee_bps`) with the real tier before any real money.
- **Funding.** Coinbase funds hourly but publishes no funding history. Each paper run writes the current hourly rate to a funding log in the ledger (8 days kept, or back to what a catch-up after a longer outage still needs; a run that logs a new hour saves the ledger, so that's about hourly), and each hour is charged the nearest rate the log has. The 3-day funding gate reads the same log. If Coinbase stops answering a rate, paper says so once after 3 hours (the board shows since when) and keeps charging the last one; after a day, no entry is judged on it (the `funding_known` gate waits). The backtest uses Binance's funding prints as the stand-in.
- **Stop fills.** Coinbase has no stop-market order: the stop will be a stop-limit. The simulation fills it at the stop (or at the open, gapped through), which is what a stop-limit does when its limit is met. In a fast market a stop-limit can be left unfilled; 2h candles can't show that.
- **History.** Coinbase's BTC perp listed on 2025-07-18, so the backtest's prices are Binance's BTCUSDT perp's. Fills aren't rounded to Coinbase's $5 tick (under 1 bp).

**What it does to the numbers** ([`reports/backtest.md`](reports/backtest.md)): +1,126% over 2020-01 → 2026-09 against +1,475% fractional, +86% against +95% since 2024 (the table at the top, and the report's waterfall of each rule). The go-live gate still passes.

**The pace to $100,000 with $500 + $100 a month, under Coinbase rules.** A fresh $500 account opened 2024-01-01 with $100 put in each month (as the paper account will get them) made +22%/yr time-weighted (fractional sizing from the same start: +28%/yr), $4,442 by 2026-10 with $3,700 put in: at $500 whole contracts are coarse. At that pace the plan reaches $100,000 in about 16 years, about $11,700 after the 5 years ($6,500 of it deposited). That's the pace the board projects the BTC account at, because it's this account's situation. The board counts the plan from its first deposit (2026-11-01); until then it shows about 26 years, the pace without it. The full run's 2024–26 out-of-sample pace, +26%/yr, would make it about 14 years, but that account already held about $3,300 in January 2024, where contracts are much finer. Backtest paces, not forecasts.

**The old Hyperliquid BTC ledger is refused, never converted.** A BTC ledger whose meta doesn't say `coinbase` (the $500 Hyperliquid run opened 2026-10-13) stops BTC's step with `LedgerRefused`, before any request: the Actions run fails, the board shows why above the tabs (no BTC board is published while it lasts), your phone gets it every 6 hours, and ETH, SOL and VT keep running. To archive it, on the `paper-ledger` branch move `ledger/BTC/paper.json` to `ledger/archive/BTC-hyperliquid-500usd-from-2026-10-13.json`, as the earlier runs were archived. The next run opens a fresh $500 Coinbase account (`"venue": "coinbase"` in its meta), and the save step's guard lets it in because there's no BTC ledger left to replace.

**On the board** the BTC tab is titled `COINBASE · BTC PERP (0.01 BTC contracts)`, with `PAPER · SIMULATED ON COINBASE RULES · NO ORDERS SENT` under it, and its phone pushes say `BTC paper (Coinbase rules, simulated)`. It shows the contracts held and the margin they use, the funding (est.) and fees (est.), what a whole contract actually risks at the stop (often less than the 5% budget at $500), the next closure (or why it's closed now, and until when), a candle it's waiting for, and any entry the venue is skipping, with why. Its events say fills in contracts (`BOUGHT 2 contracts (0.02 BTC) @ ...`).

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

**On the board** (`#VT`), the VT tab shows the bank, P&L, goal and equity curve like the others, and instead of the price chart a holdings table: for each coin, how far its last close is above or below its 50-day average, its 10-day vol, the target and held sizes, what's held and its unrealised P&L. It also shows when the next decision is (every 00:00 UTC: 6:00 pm MT in summer, 5:00 pm MT in winter). Between runs, the bank and P&L move live with Hyperliquid's prices.

## Deposits and withdrawals

Money you put into an account, or take out, is never counted as profit or loss. Every paper account reads [`deposits.json`](deposits.json) at the repo root (on `main`) at each run. BTC's plan is set: $100 on the 1st of every month, 60 deposits from 2026-11-01 to 2031-10-01.

```json
{
  "BTC": {
    "monthly": {"amount": 100, "day": 1, "first": "2026-11-01", "count": 60},
    "extra": [
      {"date": "2026-12-15", "amount": 250, "note": "bonus"},
      {"date": "2027-03-01", "amount": -50, "note": "took some out"}
    ]
  }
}
```

- `monthly`: `amount` dollars on `day` (1 to 28) of each month, starting on `first` (which must fall on that day), `count` times.
- `extra`: one-off amounts on a date. **A negative amount is a withdrawal.** `note` is optional. An extra dated more than a year ahead is taken for a typo in the year (2062 for 2026) and stops the step; if it really is planned that far ahead, add `"future": true` to it.
- Any account works the same way: `"ETH"`, `"SOL"` and `"VT"` can each have their own `monthly` and `extra`.
- Keys starting with `_` are comments (text only).

To change the monthly amount later, make `monthly` a list of two plans: end the first one with `count` set to the months it ran, and start the second the month after. From $100 to $150 in May 2027, say (the first plan ran November 2026 to April 2027, 6 deposits):

```json
    "monthly": [
      {"amount": 100, "day": 1, "first": "2026-11-01", "count": 6},
      {"amount": 150, "day": 1, "first": "2027-05-01", "count": 54}
    ],
```

### Adding a deposit or a withdrawal from your phone

1. In the GitHub app or your phone's browser at github.com, open the repo, then `deposits.json` on the `main` branch, and tap the pencil (Edit).
2. BTC's `extra` list is open, with nothing in it yet:

   ```json
       "extra": [
       ]
   ```

   Type your entry on its own line between the `[` and the `]`:

   ```json
       "extra": [
         {"date": "2026-12-15", "amount": 250, "note": "bonus"}
       ]
   ```

   For the next one, put a comma after the entry that's already there, and add yours below it (new entries always go at the end, and the last one has no comma):

   ```json
       "extra": [
         {"date": "2026-12-15", "amount": 250, "note": "bonus"},
         {"date": "2027-03-01", "amount": -50, "note": "took some out"}
       ]
   ```

3. Tap **Commit changes** and commit to `main`.

**The slips a phone makes:** straight quotes only (`"`, not `“ ”`: turn off Smart Punctuation in iOS Settings > General > Keyboard if it changes them); amounts as plain numbers (`1000`, not `$1,000`; `-50`, not `- 50`); no comma after the last entry. If you get one wrong, the error message starts with how to fix it.

The next run (within about 15 minutes) records it once its date has come, at 00:00 UTC on that date; a date already past is recorded at the next run. With `NTFY_TOPIC` set, your phone gets `DEPOSIT $250.00 recorded (extra: bonus, 2026-12-15) — money in, not profit`.

The step checks the file strictly and never guesses:
- A mistake in one account's section stops that account's step until you fix it; the other accounts keep running. If the file isn't valid JSON at all, or names an account that doesn't exist, every account waits. Each paused account's error is shown above the board on GitHub Pages (its own board isn't published while it's paused, so the page shows another account's), the Actions run fails, and with `NTFY_TOPIC` set your phone gets `BTC paused: deposits.json` with the message: at the first run after the edit, then every 6 hours until it's fixed. Each step then catches up every missed candle, so no trading is lost.
- **Never edit or delete an entry that's already been recorded** (its date has passed). Fix a mistake with another `extra`; a negative one takes money back out. A recorded entry that's changed or missing stops that account until it's put back as it was. Add new extras at the end of the list: two on the same date are told apart by their order.
- **A withdrawal the open position still needs waits**, as an exchange holds back the margin a position uses: the realised balance must stay above $0 and the position within the account's maximum leverage (5x; BTC on Coinbase 4x; VT: the most its rules ever hold) of what's left. The board and your phone say so once (`WITHDRAWAL $1,000.00 waiting ...`); it goes out at the first bar open where it fits, usually once the position has closed. Entries dated after it wait with it. A withdrawal bigger than the whole account stops the step.

**How it's counted.** Like a fund: the account starts with one unit per dollar, and a deposit buys units at what a unit is worth at that moment (a withdrawal sells them). So:
- **The performance %** is the units' value, time-weighted: a deposit or a withdrawal never moves it. Once an account has had one, the board's ticker shows it as `PERF +x% TIME-WEIGHTED` next to the dollar P&L (the two can differ a lot: $100 a month into a small account), each tab shows `+x% perf · P&L $y`, and your phone says `(+x% time-weighted)`.
- **P&L in dollars** is the balance, minus the start, minus what was deposited net of withdrawals.
- **When it goes in.** A run on time (the usual 00:04 UTC run on the 1st) puts the deposit in at that run's price, the one the board marks the account at, so the performance % reads the same just before and just after it. A run that comes late and catches up candles it missed puts it in at the open of the first missed candle on or after 00:00 UTC on its date, before that candle's fills.
- **The brakes read the same percentage just before and just after a flow.** At each one, the 40% drawdown halt and the 10% daily loss limit are rescaled with the balance. A deposit during a 35% drawdown still shows a 35% drawdown. A withdrawal never trips the halt.
- **Trading sees a deposit only as a bigger account to size the next trade from.** 5% of a bigger account is a bigger bet, as it would be with real money. A position already open isn't topped up.
- **The go-live gate's "up overall"** is the realised balance over the units (the realised performance %), and the dollar P&L must be up too.
- **On the board:** the plan's deposits are ticks on the equity chart's bottom edge, and extras and withdrawals are dashed lines with their amount. Each one is in the events with its date, and the deposited total is next to the P&L. The goal panel shows what's been deposited (and withdrawn) so far, and counts the deposits still planned in its years to $100,000. The ALL card's P&L leaves deposits out. The ledger's commit message reads, for example, `BTC $600.00 (+$100.00 deposited, not profit)`.
- **Until the first flow is recorded** (BTC's is 2026-11-01), every ledger and board is byte for byte what it was before deposits existed. That includes the goal panel: it starts counting the plan at the first deposit.

**Live.** `deposits.json` is for the paper accounts only; live never applies it. Live reads real deposits, withdrawals and transfers from Hyperliquid's own ledger at every check: bridge deposits and withdrawals, sub-account transfers, transfers between accounts, spot ↔ perp, and vaults. It books them the same way, brakes included, each at the account's value at the moment it happened (if the desk only reads it later, after a restart or while the ledger couldn't be read, the open position's move since then is taken out at that minute's price; if that can't be done it alerts and sets `entry_kill`). (It uses ccxt's `fetch_ledger`; `fetch_deposits` and `fetch_withdrawals` only see the bridge.) An entry of a kind it doesn't know is alerted, never guessed at. While the ledger can't be read, the balance isn't trusted: the brakes keep the last one confirmed and the desk waits to decide on new candles (the exchange's stop still protects any position), then catches them up. If the account's value moves by more than the larger of $1 and 2% of the account beyond what the position's P&L, funding, the trades booked and the recorded flows explain, including across a restart, it re-reads the ledger, waits about two minutes for an entry to show up, and then alerts your phone and sets `entry_kill`.

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
2. That coin's paper ledger has **14+ days** of trading and is up overall, time-weighted: deposits and withdrawals don't count either way ([above](#deposits-and-withdrawals)).
3. `DESK_LIVE_CONFIRM` is set to the exact phrase in `.env.example`.
4. You set a hard per-position dollar cap with `--max-notional`.

`--force-unproven` overrides 1 and 2: it prints a warning and carries on. 3 and 4 always hold. Don't use it.

**BTC on Coinbase has its own path: SHADOW now, LIVE locked in code.** `python -m desk live --coin BTC` refuses every attempt: LIVE isn't finished, and [`docs/coinbase-live-status.md`](docs/coinbase-live-status.md) says what's still open ([Going live on Coinbase (locked)](#going-live-on-coinbase-locked)). What's below is the Hyperliquid (and ccxt) live loop.

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
- Deposits on live are read from Hyperliquid's ledger as ccxt 4.5.85 returns it, with the fields Hyperliquid documents. That has only been checked against ccxt's code and the fakes, not a funded account. Two details are assumed: a withdrawal's fee is part of the amount taken out, and a transfer's fee is paid by the sender. If either is wrong, the reconciliation alert catches it. Hyperliquid's newer "send" entries aren't parsed yet; they're alerted. A deposit read late is priced from Hyperliquid's 1-minute candle at its minute (1-hour candles once it's older than about 3 days); funding over that stretch is left out of its price.

Paper trading is not affected by any of this. The backtests (BTC, ETH, SOL, VT) and paper steps on copies of the real ledgers give byte-for-byte the same output before and after these fixes, apart from each report's generated time.

Perpetual futures are restricted in some countries, including for US persons on most venues. Check that your venue is legal where you live.

## Coinbase SHADOW (the real account, read only)

**SHADOW reads your real Coinbase account and sends no order.** It runs with a View-only key, a GitHub job after the paper step every 15 minutes. It makes the same decisions LIVE would make, on Coinbase's own 2h candles with paper's venue rules, and records the exact orders LIVE would have sent. To set it up, follow [`docs/coinbase-setup.md`](docs/coinbase-setup.md): one secret for the key file (`COINBASE_CDP_KEY`) and one password (`DESK_STATE_KEY`).

- **It can't send an order, three ways over.**
  - The key is View only: SHADOW checks Coinbase's key permissions every run and refuses a key that can trade or move money, or that's on a portfolio other than Default (it would read that portfolio's money as the account's). A key on another login's Default portfolio is another account: the old one's period is closed and the new one opens from its balance, its 14 days of SHADOW started again.
  - Its client (`desk/brokers/coinbase_rest.py`) refuses every POST before a socket opens unless it holds an Armed token, and only `desk/coinbase_live.py`'s `arm()` makes one. A test scans the code to keep it that way.
  - Its exchange (`desk/brokers/coinbase_shadow.py`) records each order instead of sending it.
- **The money is real; the trades are simulated.** SHADOW's balance is your real Coinbase balance (spot + futures + open and unsettled P&L, cross-checked against available margin and holds) plus the estimated P&L of the orders LIVE would have sent. The board shows the two side by side, the second marked as an estimate.
  - Simulated fills are at Coinbase's best ask (buys) or bid (sells) when the run looked, with the fee estimate max($0.20 a contract, 5 bps). The real fee tier is read and shown, but Coinbase's per-contract futures fee isn't in it.
  - Stops are simulated from Coinbase's 5-minute candles as stop-limits. One fills at the stop, or at the open past it, only within its limit; a gap past the limit leaves it unfilled, as on Coinbase, and the next run closes at market.
  - Funding on the simulated position is an estimate from the hourly rates SHADOW logs (Coinbase publishes no funding history).
- **Deposits and withdrawals are read from Coinbase** (the transactions of the USD account in the key's own portfolio, found through v3 `/accounts`): money in or out, never profit or loss. `deposits.json` drives the paper accounts only.
  - The first deposit opens the account: it is the start, not profit. On day one at $0 the desk reads and decides but sizes nothing, and says how big an account the current setup needs for one contract.
  - A withdrawal that empties the account closes it, with no drawdown halt; the next deposit opens it again.
  - USDC turned into USD for margin counts as money in from your USDC. A transfer between your portfolios, a Coinbase subscription charge, a payout or a clawback is money in or out too.
  - Every run checks that the balance moved by what P&L, fees, funding and those flows explain. A move nothing explains (a type Coinbase adds later, spot trading in the portfolio) waits 3 hours for an entry, then is booked as **money in or out**: SHADOW never trades the real account, so a move in it can't be SHADOW's profit or loss. It is listed on the board as unexplained, alerted, and sets `entry_kill`, as LIVE would.
  - A drawdown halt can be cleared from the branch: add `"reset_halt_at": "<any new text>"` to `BTC/kills.json` on `coinbase-state` (docs/coinbase-setup.md).
  - A `BTC/kills.json` the desk can't read (a typo) fails closed: `entry_kill` on, and every switch it names on unless its value is one of the words for off (`false`, `null`, `0`, `"no"`, `"off"` and the like, or a near-typo of false, null, off or no), so `{"flat_kill": true,}` and `{"flat_kill": "now"}` still close the position. The board, the status bar and your phone say so, and the run is still saved, the file kept exactly as you wrote it, so every run reads it the same way until you fix it (`coinbase_state.kills_read`, the one reading the runs and the save share). `DESK_COINBASE_KILL` and the file read the same words anywhere in them: flat, flatten or close (`close_all`, `close_position`) for `flat_kill`, entry (`no_entries`) for `entry_kill`, both at once if both are there; a variable with neither is `entry`, alerted.
  - A kill the desk sets itself (a move nothing explains, say) is written into `BTC/kills.json` and kept in `BTC/kills.desk.json` until you set it to `false` in `BTC/kills.json`: set while your file had a typo, it stays on after you fix the typo, and the next save writes it there; deleting or renaming `BTC/kills.json` doesn't clear it either (the next save writes it into a new one).
  - Other files you add on the branch are kept as they are and never read; one named like a kill switch anywhere on it that isn't `BTC/kills.json` (`kills.json` at the top, `BTC/Kills.json`) turns `entry_kill` on, said so. Deleting one while a run is under way never stops that run's save; a `.gitattributes` is dropped, a `.gitignore` never hides the desk's files, `.github/` is never changed. A save the save step would refuse is pushed by the run that made it (`NOT SAVED`; urgent on LIVE), and one of the desk's own files deleted on the branch is pushed at once: a `BTC/status.json` deleted (with `README.md` or not) or edited into one the save check refuses (its `mode` set to LIVE included) is written back by the next run that opens the saved state, from it (a run refused over a secret before that writes nothing, so no status without the state's counts is ever saved). The state and the status deleted together, beside the state before the last, refuse every run, writing nothing (a run refused over a secret as well included), until you put them back or delete the branch, and the save check refuses any save beside the state before the last; a refusal that writes nothing is pushed at the first run after your commit, its time read from the commit. Every SHADOW file deleted is a start over, said so ("started over" while the branch still shows SHADOW had worked there; otherwise its record starts from that run); a branch you made yourself before SHADOW's first run is a plain first run. Until SHADOW has had a run that works, a run that's refused or stops part way saves its status, never a state: whatever the first runs stopped over (the key file, a Trade key, the wrong portfolio, the two secrets swapped, a `DESK_STATE_KEY` with a line break, Coinbase not answering, an answer it can't read, an error of its own) recovers by itself once it's fixed, whatever `DESK_STATE_KEY` held meanwhile. Once the key has passed, or for an answer it can't read or an error, it saves its board too (its checks and its log, for the SHADOW page). The public status says what stopped it as it happened: "Coinbase didn't answer" only when no answer came, "Coinbase's answer couldn't be read" when one came in a shape the desk doesn't know (a key-permissions answer with a flag missing included), "Coinbase's answer was incomplete, or its market was empty" for candles missing, a book empty on one side or no funding rate yet, "SHADOW hit an error of its own" for a bug, and "a secret isn't set right" only for a secret.
- **It's compared with paper.** The paper BTC account's board is handed to the SHADOW job. At the same close, the gates that rest on the candles alone (the setup, the trend, the volatility band) must agree; a disagreement is alerted. The gates that differ by design are named: the account's own balance and brakes, the moment each read the book, each one's own funding log.
- **Entries.** An entry goes out at most 30 minutes after its close (runs land 7 to 9 minutes after a close on Actions), at market, sized again at the best ask then, and only while Coinbase is open. The weekly break and maintenance come from paper's rules and Coinbase's own session: open only when `is_session_open` is true and `session_state` is `FCM_TRADING_SESSION_STATE_OPEN` (HALTED, PRE_OPEN, PRE_OPEN_NO_CANCEL and the rest read as closed).
- **Orders the desk didn't place.** Coinbase lists only API (RETAIL_ADVANCED) orders unless asked, so the desk also reads the app's (RETAIL_SIMPLE). An order is the desk's only if it carries one of its client ids; any other order on BIP-20DEC30-CDE is never touched, blocks entries, and counts against the position when the desk places its own stop (no reduce-only). Don't place BIP orders yourself while SHADOW or LIVE runs.
- **The repository is public, so:**
  - SHADOW's state and board are encrypted (PBKDF2 + AES-256-GCM, `desk/statebox.py`) and saved on their own branch, `coinbase-state`, never `paper-ledger`.
  - The public `BTC/status.json` has no amounts and no ids. A run refused over its secrets (`DESK_STATE_KEY` too short, the key file, a line break in it, or not opening the saved state), or refused or stopped before SHADOW ever worked, still writes it, naming the reason, so the dashboard says what's wrong (not while `BTC/status.json` is missing beside a saved state: the first run that opens that state writes it back).
  - The Actions log gets one fixed line plus GitHub's masks for the key: the run captures everything else it prints.
  - The dashboard's **BTC SHADOW** bar links to `shadow.html`, which opens the encrypted board on your device with `DESK_STATE_KEY` (WebCrypto; nothing is sent). It's never one of the paper tabs or in the ALL total, and nothing shows until SHADOW has published a status.
- **Times** on the SHADOW page, in the pushes and in the run's log are Mountain Time ("10:08 am MT"; MDT in summer, MST in winter), so Coinbase's weekly break reads Fridays 3:00 to 4:00 pm MT. What's stored stays UTC, and paper's boards are unchanged.
- **Pushes** (ntfy): `BTC SHADOW: connected (read only)` once (`connected, started over` when every SHADOW file on the branch was deleted); `would BUY (no order sent)`, `would SELL (no order sent)` and `would SELL (stop, simulated)` (only when a simulated stop filled); `DEPOSIT`, `WITHDRAWAL` and `account opened`; `ALERT` (each kind at most every 6 hours; an alert about the simulated book starts "Simulated book (nothing rests on Coinbase)"); `not working` (the first failure and each new reason at once, then every 6 hours; it names where the run stopped, the step and the desk's function beside the error's type) and `working again` when it's fixed (naming any read that still failed); `NOT SAVED` (the save check would refuse the run's state: the first, then every 6 hours). A run refused before it opens its state has no memory of its own: the public `BTC/status.json` it loaded tells it whether that refusal is news.
  - **They go to ntfy.sh in plain text: anyone who knows your topic's name can read them.** So they leave every amount out ("…") unless the secret `NTFY_TOKEN` holds an ntfy access token for a reserved topic. The encrypted board has the amounts. A push ntfy.sh refuses with `NTFY_TOKEN` (a wrong or expired token) is sent again without it, its amounts left out, saying the token was refused; a push that still doesn't go out is listed on the board and in the run's log when that run saves its board (a run refused before it opens its state keeps no record of it).
  - Paper's pushes never use `NTFY_TOKEN`. If you reserve the topic, set everyone's access to "I can publish and subscribe, everyone can publish" (write-only): ntfy.sh's default ("Only I can publish and subscribe") refuses paper's pushes and SHADOW's resend without the token alike. Then log in to your ntfy.sh account in the phone app, or it stops receiving (docs/coinbase-setup.md, step 13).
- **The job can't hurt paper.**
  - It runs after the paper job (`needs: step`) and installs its two pinned, hashed libraries (PyJWT 2.14.0, cryptography 50.0.1) only for itself.
  - It holds only `contents: write` and pushes only `coinbase-state`, one commit replaced each save with `--force-with-lease`, after a check that refuses a younger or plain-text state (`desk/coinbase_state.py`).
  - It's `continue-on-error`: a SHADOW failure never fails the run.
  - Without both secrets it skips, green.
  - Its `NTFY_TOKEN` is its own: paper's step never reads it. Reserving the topic on ntfy.sh is the one setting that can stop paper's pushes (see Pushes above).
- **Checked against Coinbase's current spec.** The Advanced Trade OpenAPI spec's enums (order statuses, refusal codes, session states, placement sources) are in `tests/fixtures/coinbase_spec/enums.json`, dated, and `tests/test_coinbase_spec.py` checks the code and the tests' fake against them. Re-fetch it before LIVE is armed.
- **The key must be ECDSA.** Coinbase's API key page: "Do NOT select Ed25519 as it is not supported by Coinbase App APIs" (the deposits and withdrawals are read through those). The desk refuses an Ed25519 key before calling Coinbase.
- **Not yet known** (checked at runtime, never assumed):
  - whether Coinbase's balance fields mean exactly what the formula assumes (the daily settlement). If the balance comes to more than available margin and holds, that's an ACH hold only when the USD account's own hold says so; otherwise entries stop;
  - how quickly deposits show up in the v2 transactions for a View key;
  - which placement source Coinbase gives API orders (assumed RETAIL_ADVANCED; any other is learned from the desk's own orders and read too).
  
  SHADOW exists to find these out before any money moves. Every inferred fact is checked at runtime: SHADOW refuses or alerts rather than guess.

## Going live on Coinbase (locked)

**LIVE on Coinbase is locked in code, and nothing in this repository arms it.** Six rounds of review kept finding new money-safety problems in how LIVE handles its stops, so `desk/coinbase_live.py` has `LIVE_REVIEWED = False`, and `arm()` checks it before anything else. Every LIVE attempt is refused with one reason, *LIVE isn't finished: its last review found open safety problems, see docs/coinbase-live-status.md*. No key is loaded, no request is made, nothing is saved on `coinbase-state`, and your phone gets `BTC LIVE: not armed`. No secret, variable, flag (`--force-unproven`, `--local`), `kills.json` or saved state gets past it; only a code change does, once a review comes back clean. **SHADOW is unaffected.** What's still open: [`docs/coinbase-live-status.md`](docs/coinbase-live-status.md). The rest of this section is how LIVE is meant to work once that lock is lifted, which is only after the open items there are fixed: where LIVE doesn't do yet what's said below, that page says so.

`python -m desk live --coin BTC --once --max-notional N` runs one check, with the same code as SHADOW, on the client `coinbase_live.arm()` unlocks.
- Entries go out as whole contracts at market.
- A protective stop-limit rests on Coinbase at all times, its limit 5% past the trigger; Coinbase has no stop-market and no reduce-only.
- The stop is trailed cancel-first at each run: two full-size stops at once could open a short.
- Exits cancel the stop, then close the position.

Each LIVE run starts with a **safety pass**, right after arming and before the product, candles or balance are read: the position and open orders, read fresh; a stop put back on a position with none (at the saved level, else the stop distance from the entry at the last ATR seen); desk stops covering more than the position cut back to it; a stop the price gapped past (triggered, unfilled) closed at market; any desk order left resting with no position cancelled; `flat_kill` honoured. Its close cancels the stop first, so it starts only when it can go through: never while Coinbase reads closed or its product can't be read to tell (the close waits, the stop stays), nor without the time, at the speed Coinbase is answering, to cancel, close and put the stop back (`FLAT_KILL NOT DONE`, urgent, the stop kept); a close refused after the cancel puts the stop back at its trigger, and a part fill is closed again or gets a stop on the rest. A stop moved cancel-first starts only with the time for the move and a fallback close; once the old stop is cancelled, the new one (and if it won't rest, the close at market) goes on past the run's deadline, falling back to what it read just before if a read fails. A cancel or an entry whose answer is lost is read back before anything is decided. Anything that leaves a position without a stop, or a stop without a position, is meant to be pushed urgently (`BTC LIVE: NO STOP`, `BTC LIVE: STOP WITH NO POSITION`): items 1, 2, 3 and 6 in [`docs/coinbase-live-status.md`](docs/coinbase-live-status.md) are cases where a position is left without a working stop and nothing urgent is pushed yet. A run's "not working" push says how many order requests it sent.

It has only ever run against the tests' local fake Coinbase (`tests/fake_coinbase.py`). `arm()` prints every check as PASS or FAIL and sends nothing unless all of them pass: the go-live gate, `DESK_LIVE_CONFIRM`, `DESK_LIVE_COINBASE=BIP-20DEC30-CDE`, a `--max-notional` that buys one contract, SHADOW's 14 clean days with a deposit reconciled to the cent, a View + Trade key that can't move money on SHADOW's portfolio, no USDC, and a flat start. The go-live gate, SHADOW's record and the flat start are checked at the first LIVE start only (the first run that arms); the rest at every run.

**Once LIVE has armed, a run that fails only a check on new risk is PROTECT ONLY.** Those checks are `DESK_LIVE_CONFIRM`, `DESK_LIVE_COINBASE`, `--max-notional` and what it buys, the balance adding up, and no USDC. Such a run sends no entry, no add and no stop move; its safety pass still honours `flat_kill` (a close at market), puts a missing stop back no wider than the saved one, tops up at the saved level a stop that covers only part of the position, and closes at market a stop the price gapped past (keeping every stop that can still fill while that close can't go). `arm()` hands it a protect-only token, and the order client checks every order against the position it reads just before: only cancels of the desk's own orders, and closes and stops that can only shrink the position, get through. Whatever the token, the client sends nothing for another product and no entry over the cap it was armed with. It pushes `BTC LIVE: PROTECT ONLY` urgently at every run, naming the check and what it did. **Deleting `DESK_LIVE_CONFIRM` stops new trades; the desk keeps protecting a position that is already open. To close it, set `flat_kill`.** A run that fails what any order needs (the key, its portfolio, the clock, `DESK_STATE_KEY` opening the state) sends nothing; it still looks, read only (signed on Coinbase's clock, so a refusal over the clock looks too), and pushes urgently at every run `NO STOP`, `STOP WITH NO POSITION`, `STOP DIDN'T FILL` (a gapped stop), `FLAT_KILL NOT DONE` or `STOPS COVER MORE THAN THE POSITION`. A read that fails while a new-risk check is made is said as that ("Coinbase's balance couldn't be read"), never as the check failing. `--max-notional` is a plain number of USD (`1000`, `$1000`, or `1,000` with commas only between whole thousands); anything else (`1,5`, `1000,50`, `10 00`), or a blank one, is a missing one, never a cap read 10 or 100 times too large: the run still starts, protect only once armed (a refusal at a first start). Off GitHub Actions it also needs `--local`. The steps to arm it, and to disarm it, are in [`docs/coinbase-setup.md`](docs/coinbase-setup.md#part-2-the-trade-key-and-live-later-not-armed). This is the workflow edit they describe, for when you decide, not before. In the `shadow` job, add `environment: coinbase-live` (a GitHub Environment limited to the `main` branch, holding the secrets below); change its `SHADOW_ON` line to the trade key, so the View key no longer starts or skips the job:

```yaml
      SHADOW_ON: ${{ secrets.COINBASE_CDP_TRADE_KEY != '' && secrets.DESK_STATE_KEY != '' }}
```

and replace the SHADOW run step with this one, never with `--force-unproven`:

```yaml
      - name: LIVE run (real orders)
        id: shadow
        if: env.SHADOW_ON == 'true' && steps.load.outcome == 'success'
        timeout-minutes: 6
        env:
          COINBASE_CDP_KEY: ${{ secrets.COINBASE_CDP_TRADE_KEY }}
          DESK_STATE_KEY: ${{ secrets.DESK_STATE_KEY }}
          DESK_LIVE_CONFIRM: ${{ secrets.DESK_LIVE_CONFIRM }}
          DESK_LIVE_COINBASE: ${{ vars.DESK_LIVE_COINBASE }}
          DESK_LIVE_MAX_NOTIONAL: ${{ vars.DESK_LIVE_MAX_NOTIONAL }}
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
          DESK_COINBASE_KILL: ${{ vars.DESK_COINBASE_KILL }}
        run: |
          # paper's ledger, for the go-live gate (read at the first start only): never a reason this step doesn't run
          timeout 60 git fetch --depth=1 origin paper-ledger && git checkout FETCH_HEAD -- ledger/BTC/paper.json || true
          python -m desk live --coin BTC --once --max-notional "$DESK_LIVE_MAX_NOTIONAL" --state-dir "$RUNNER_TEMP/cb"
```

LIVE keeps its own encrypted state (`BTC/state-live.enc.json`) and its own board (`board-live.enc.json`), which the dashboard doesn't open yet: that's a follow-up for the day it's armed. It saves them only once it has armed. A LIVE run that refuses before LIVE ever armed (a mistyped `DESK_LIVE_CONFIRM`, or a missing `DESK_STATE_KEY`, say) saves nothing, so the `coinbase-state` branch and SHADOW's state stay as they were, and the next LIVE run is a first start again; it pushes `BTC LIVE: not armed` at every such run while SHADOW's last run is under 2 hours old, then every 6 hours. Once LIVE has armed, SHADOW refuses to run beside it. The LIVE step replaces SHADOW's, so SHADOW stops running at the switch, and the first start must arm within 2 hours of SHADOW's last run.

What a real-money run risks, beyond the strategy's own losses:
- The job runs about every 15 minutes, best effort: late, or skipped, at busy times. Between runs only the stop resting on Coinbase protects the position.
- The stop is a stop-limit. The board shows two figures, "risk at the stop trigger" (the 5% budget) and "if filled at the stop-limit's limit" (more). A gap past the limit has no bound until the next run closes at market.
- Each trailing move cancels the old stop before it places the new one, which leaves a gap with no stop resting. It is usually a second or two, and much longer if Coinbase is slow: the new stop is tried up to 4 times (1, 2, 4 s apart), each try one order request that waits up to 15 s and is sent up to 3 times, about 50 s a try, so about 3½ minutes before the desk gives up on the stop and closes the position at market. If Coinbase stops answering, it keeps trying until 60 s past the run's deadline, up to about 5½ minutes after the cancel, then pushes NO STOP urgently.
- If you close the position yourself, cancel its stop too: a stop with no position opens one when it fires.

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
desk/venue.py        a venue's own rules (BTC on Coinbase): whole contracts, margin, sessions, fees, the funding log
desk/brokers/        sim.py (backtest + paper; VenueSimBroker for a venue's fees), live.py (ccxt; and Coinbase's
                     adapter: coinbase.py, coinbase_rest.py, coinbase_auth.py, coinbase_orders.py, and SHADOW's
                     coinbase_shadow.py: the real money, a virtual book, the orders it would send)
desk/coinbase_run.py SHADOW and LIVE on Coinbase, one check per call (python -m desk shadow / live --once)
desk/coinbase_live.py the lock: arm() and its Armed token, the only way to an order-capable client
desk/statebox.py     the encrypted state and board (the repo is public); coinbase_state.py: the save checks
docs/coinbase-setup.md  click by click: the View-only key, the two secrets, what to expect; the trade key, later
desk/feeds/          hyperliquid.py (live), coinbase.py (live, BTC), binance_vision.py (history)
desk/board.py        state → dashboard JSON (agent cards and their notes, gates, thresholds; the VT holdings)
dashboard/           the board: no build step, phone-friendly; shadow.html opens SHADOW's encrypted board
dashboard/spider.js  the walking CRAWLER: visits whatever the desk just did (gate cards in the order the desk
                     checks them, the newest candle, the trigger line, new fills) and curls up when the board
                     is stale. Decoration only; it changes no rule
dashboard/theme-comic.css  the comic lab-notebook look, day and night editions (edition.js: the footer's switch)
```
