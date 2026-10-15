# Connecting the desk to your Coinbase account

This page has two parts:

- **Part 1, now: SHADOW.** A **View-only** key lets the desk read your real Coinbase account. It runs the same decisions it would make with real money and records the orders it *would* send. **It sends no order.** The key can't trade or move money, and the desk's client refuses every order request before anything leaves GitHub.
- **Part 2, later and only if you decide to: LIVE.** A separate trade key, and deliberate arming steps. **LIVE is not armed.** Nothing in this repository arms it, and nothing in Part 1 does either.

ETH, SOL and VT stay paper accounts. Paper trading (all four accounts, their ledgers and boards) is unaffected by anything here.

The menu names below are quoted from Coinbase's own pages: its API key page (https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication) and its key guide for agents (https://docs.cdp.coinbase.com/coinbase-for-agents/skill.md), both read on 2026-10-14. The two pages name the last button differently (step 8), so both names are given. The portal itself couldn't be clicked through from here, and Coinbase renames things: if a label differs, look for the same idea nearby.

---

## Part 1: SHADOW (do this now)

### A. Create a View-only API key

1. In a browser, sign in at **portal.cdp.coinbase.com/api-keys/secret** (the Coinbase Developer Platform) with your usual Coinbase login. Use the same account that was approved for derivatives on 2026-10-13. Coinbase creates a project for you the first time you sign in.
2. Under the **API Keys** tab, select **Secret API Keys**, then click **Create API key**.
3. Enter an API key nickname, `btc-perp-desk shadow (view only)`, then expand **API restrictions** and **Advanced Settings**.
4. Under **Advanced Settings** → **Coinbase App & Advanced Trade**, under **Accounts**, select your **Default** portfolio. Coinbase: "US Derivatives (Futures) only work in your **default** portfolio".
5. In the same section, leave the permissions at **View** only. View is enabled by default; leave **Trade** and **Transfer** off.
   - SHADOW refuses a key that can trade or move money. A mix-up stops it instead of risking anything.
6. Check **Allow requests from any IP address**, and configure no IP allowlist. GitHub's runners use changing addresses, so an allowlist would block every run.
7. **Required: set the signature algorithm to ECDSA.** Coinbase's API key page says: "Do NOT select Ed25519 as it is not supported by Coinbase App APIs". The desk reads your deposits and withdrawals through those APIs, so it refuses an Ed25519 key before calling Coinbase, and says so.
8. Click **Create & Download**. If your page shows **Create API key** instead, click it, then click **Download API key** in the window that opens: Coinbase no longer downloads the key file on its own.
9. Save the downloaded JSON key file. You only need it for the next steps.
10. Open the downloaded `.json` file in a plain text editor (TextEdit in plain-text mode, Notepad) and copy **all of it**, braces included. It looks like this (yours is longer):

    ```
    {
       "name": "organizations/…/apiKeys/…",
       "privateKey": "-----BEGIN EC PRIVATE KEY-----\n…\n-----END EC PRIVATE KEY-----\n"
    }
    ```

### B. Put it in GitHub, as two secrets

11. On github.com, open **kendallcoding202/btc-perp-desk** → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
12. **Name:** `COINBASE_CDP_KEY`. **Secret:** paste the whole file. Click **Add secret**.
13. Click **New repository secret** again.
    - **Name:** `DESK_STATE_KEY`.
    - **Secret:** a random password of **32 characters or more**, from your password manager. Save it in the password manager too.
    - Why: this repository and its Pages site are **public**. DESK_STATE_KEY encrypts SHADOW's state and board (AES-256-GCM), so the state and the board can only be opened with it. You type it once on each device to open the BTC SHADOW page.
    - Phone pushes are a separate channel: they go to ntfy.sh, where **anyone who knows your `NTFY_TOPIC` name can read them**. So SHADOW's pushes leave every amount out (they say "…" and point to the SHADOW page). To get amounts on your phone, reserve a topic on ntfy.sh, make an access token for it, and store it as the secret `NTFY_TOKEN`. Either way, use a long random topic name.
    - If you lose it, SHADOW starts over from scratch. No money is involved.
14. Delete the downloaded key file from your Downloads folder and empty the trash. Never paste the key anywhere else: not in a chat, an email, an issue or a commit.
15. In Coinbase, keep **only the desk's money** as **USD in the Default portfolio**:
    - SHADOW counts all USD there as the account.
    - USDC there can be pulled into futures margin. Move USDC, and any money that isn't the desk's, to another portfolio.
    - Deposit from your bank straight into the Default portfolio's USD.
    - A transfer between your portfolios, a Coinbase subscription charge, a payout or a clawback is recorded as **money in or out**, never as profit or loss.
    - Anything else that moves the account and that the desk can't read (spot trading in that portfolio, a type Coinbase adds later) waits 3 hours for an entry that explains it. Then SHADOW books it as money in or out (SHADOW never trades your real account, so it can't be profit or loss), lists it on the board as "nothing the desk can read explains it", alerts, and stops new simulated entries until you've looked.
    - Don't place orders on BIP-20DEC30-CDE yourself (in the app or on the website) while SHADOW or LIVE runs. The desk never touches an order it didn't place, but it stops new entries while one rests. On LIVE it pushes urgently, and if your order stands where its own stop must go (with no reduce-only, two full-size stops could flip the position), it closes its position at market rather than hold it without a stop of its own.

### C. What happens next

16. Wait up to 15 minutes, or go to **Actions** → **paper desk** → **Run workflow** (branch `main`) → **Run workflow**.
17. Open the run. The `step` job (paper) runs first, then the `shadow` job.
    - Its log says one line, like `SHADOW BTC: run 1 · key View only · reads ok · 0 closes processed · 0 orders it would send · state saved`.
    - It shows no amounts, on purpose: the logs are public.
    - A SHADOW failure shows as a red `shadow` job, but the run as a whole stays green and paper is never touched.
18. Your phone (ntfy, if `NTFY_TOPIC` is set) gets **BTC SHADOW: connected (read only)** with the checklist of every read.
19. On the dashboard, a **BTC SHADOW** bar appears under the account tabs. It shows the public status: updated when, OK or not, the desk's decision, and no amounts.
    - Tap it to open the SHADOW page, and enter DESK_STATE_KEY.
    - It stays on that device for that tab. Tick "remember" only on your own phone or computer.
20. What the SHADOW page shows:
    - **Money:** your real Coinbase balance and, beside it, the **SHADOW balance**: the real one plus the estimated P&L of the orders LIVE would have sent, labelled as an estimate.
    - **The desk:** what it decided at the last 2h close. If the account holds too little to buy one contract (0.01 BTC, about $830), it says how much would be needed. It also shows, clearly marked **WHAT-IF, not your account**, what it would do with the planned $500.
    - **Orders it would send:** each exact request LIVE would POST to Coinbase, with its simulated fill. **None was sent.**
    - Every read this run, the simulated book, anything your real account holds that the desk didn't place, events, alerts, and the run's private log.
    - Whether its decision matched the paper BTC account's at the same close.
21. **Your first deposit opens the account.** On day one at $0 the desk reads and decides but sizes nothing. The first bank deposit it reads is the account's **start**: money in, not profit. Later deposits, withdrawals and transfers are recorded as money in or out at the account's value then, never as profit or loss.
22. If the phone says **not working**, the reason follows:
    - **401 Unauthorized:** Coinbase gives no reason. Re-create the key as ECDSA, check that the secret is the whole file (name and privateKey), and check that the key wasn't deleted.
    - **lacks the permission:** the key isn't View-enabled.
    - **can trade / can move money:** you made a trade or transfer key. Make a View-only key for SHADOW.
    - **DESK_STATE_KEY doesn't open the saved state:** the secret changed. Put the old password back, or start over (below).
    - **DESK_STATE_KEY is too short**, or **is not set**: fix the secret. The SHADOW bar on the dashboard says the same, with no amount.
    - **an Ed25519 key, not ECDSA:** create the key again with ECDSA (step 7).
    - **kills.json can't be read** (a typo, say), or **looks like a kill switch but isn't BTC/kills.json**: see step 24.
    - **NOT SAVED:** the save check would refuse this run's state (you deleted `BTC/status.json` on the branch, say). The reason follows; put back what you changed on the `coinbase-state` branch. Until then the SHADOW page and the status bar stop moving.
    - **Coinbase didn't answer:** a Coinbase or network outage. The clock check reads "not checked", not failed. The next run tries again.
23. **To stop SHADOW:** delete the `COINBASE_CDP_KEY` secret. The job then skips and stays green. You can also revoke the key in the CDP portal.
24. **Kill switches:** edit `BTC/kills.json` on the `coinbase-state` branch (`{"entry_kill": true}` or `{"flat_kill": true}`), or set the repository **variable** `DESK_COINBASE_KILL` to `entry` or `flat` (Settings → Secrets and variables → Actions → Variables; `flat_kill`, `close` and `flatten` mean flat too). Any other value of the variable is read as `entry` and alerted: delete the variable to turn it off. In SHADOW they stop the simulated trading, as they would stop LIVE's.
    - **If the desk can't read the file** (it isn't JSON, like `{"flat_kill": true,}` with its trailing comma; a key it doesn't know; a switch that isn't `true` or `false`, like `"true"` in quotes or `1`; a key written twice), it fails closed. It reads **entry_kill on** (no new entries), and every switch the file names is **on** unless its value says off (`false`, `null`, `0`, `"false"`, `"no"`, `"off"`, or a word starting with f or n, like the typo `fasle`). So `{"flat_kill": true,}` still closes the position, and `{"flat_kill": false, "entry_kill": true,}` closes nothing. Nothing else in the file is acted on, `reset_halt_at` included. The SHADOW page, the status bar and your phone say so, naming `flat_kill` when the file does.
    - The run is still saved, and **the save keeps the file exactly as you wrote it**, so every run reads it the same way, and says so (the phone at most every 6 hours), until you fix it. For a close you can be sure of, set the variable `DESK_COINBASE_KILL` to `flat`.
    - **Other files you add on the branch** (notes, say) are kept as they are at every save, and never read. One whose name has "kill" in it but isn't exactly `BTC/kills.json` (`BTC/Kills.json`, `BTC/kills.json.orig`) turns **entry_kill on**, and the phone says so, until you rename it to `BTC/kills.json` or delete it. A `flat_kill` in it isn't acted on from there.
    - **To clear a drawdown halt** once you've looked: add `"reset_halt_at": "2026-11-02"` (any new text, a date is easiest) to the same `BTC/kills.json`. The next run clears the halt once, restarts the peak from the balance then, and says so on the board. Use a new text for the next reset.
25. **To start SHADOW over:** delete the `coinbase-state` branch. The next run opens a new state.

What SHADOW can't tell you yet: whether Coinbase fills, fees and price bands match the simulation. Its fills are at Coinbase's best bid or ask when it looked, with the fee **estimate** (max $0.20 a contract, 5 bps). The real fee tier is read and shown, but Coinbase's per-contract futures fee isn't in it.

---

## Part 2: the trade key and LIVE (later; not armed)

**LIVE is locked, and nothing here arms it.** Don't create the trade key early: nothing uses it, and an unused key is still a key to lose.

LIVE places real orders on `BIP-20DEC30-CDE`:
- entries as whole contracts at market;
- a protective stop-limit resting on Coinbase at all times (Coinbase has no stop-market and no reduce-only);
- the stop trailed at each run, cancel first, then the new one (two full-size stops at once could open a short);
- exits by cancelling the stop, then closing the position.

It has only ever run against the tests' local fake Coinbase.

It refuses, with every check printed as PASS or FAIL and not one order request sent, unless all of the checks below pass. Some are checked only at the **first LIVE start** (the first run that arms). Once LIVE has armed, the later runs don't check them again.

At the first LIVE start only:

1. The go-live gate passes: the backtest report, and 14+ days of paper that are up time-weighted. The BTC paper ledger restarted on 2026-10-13, so not before about 2026-10-27.
2. SHADOW's record on this account, read from its encrypted state:
   - 14+ days of runs, the last under 2 hours ago;
   - over the last 7 days: no move nothing explained, no unknown ledger entry, no balance that didn't add up, no run that couldn't read deposits, no auth failure;
   - at least one bank deposit reconciled to the cent;
   - money in the account on 7 of the last 14 days;
   - the Default portfolio, with its id recorded.
3. The account is flat, with no open order on any product.

At every LIVE run:

4. `DESK_LIVE_CONFIRM` is set to exactly `I understand this trades real money and can lose all of it`.
5. The variable `DESK_LIVE_COINBASE` is exactly `BIP-20DEC30-CDE`.
6. `--max-notional` is given and buys at least one contract (about $830 at $83,000, plus slippage). Raise it by hand as BTC rises.
7. The key is **View + Trade and NOT Transfer**, on the same portfolio SHADOW read.
8. The clock is within 30 s of Coinbase's, the balance adds up, and there's no USDC in the portfolio.

**A LIVE run that refuses before LIVE has ever armed saves nothing.** It writes no LIVE state, board, status or kill switch, so the `coinbase-state` branch and SHADOW's state stay as they were, and the next LIVE run is a first start again. Your phone gets **BTC LIVE: not armed** with the reason, a missing or short `DESK_STATE_KEY` included: at every such run while SHADOW's last run is under 2 hours old, then once every 6 hours. (Without a `DESK_STATE_KEY` that opens SHADOW's record, the age comes from SHADOW's public status.)

The LIVE step replaces SHADOW's in the workflow (step 4 below), so SHADOW stops running when you switch. Its last run has to be under 2 hours old at the first LIVE start, so the first start has to arm within 2 hours of SHADOW's last run. If it doesn't, put the SHADOW run step back until SHADOW has run again, then switch again.

Once LIVE has armed, it counts as running on this account: SHADOW refuses to run beside it. From then on a LIVE run that refuses (after you delete `DESK_LIVE_CONFIRM` to disarm, say) is still saved. If the save check would ever refuse a LIVE run's save, that run pushes **BTC LIVE: NOT SAVED** urgently, at every run until it's fixed: the orders and the stop stay on Coinbase, but what LIVE remembers between runs (the stop's level, the halt, the daily loss, the cooldown) isn't saved.

To arm it later, deliberately, when you choose:

1. Create a new key in the same portal, as in Part 1 A:
   - nickname `btc-perp-desk live`;
   - under **Advanced Settings** → **Coinbase App & Advanced Trade**: under **Accounts**, the **Default** portfolio; **View** (on by default) and **Trade**, with **Transfer** off (the desk never moves money, and LIVE refuses a key that can);
   - **Allow requests from any IP address**, signature algorithm **ECDSA**, then **Create & Download** (or **Create API key**, then **Download API key**).
2. Store it as a **new** repository secret, `COINBASE_CDP_TRADE_KEY`. Don't overwrite `COINBASE_CDP_KEY`: SHADOW refuses a key that can trade, so a mix-up stops instead of trading.
3. Add:
   - the secret `DESK_LIVE_CONFIRM` (the exact phrase);
   - the variables `DESK_LIVE_COINBASE` (`BIP-20DEC30-CDE`) and `DESK_LIVE_MAX_NOTIONAL` (for example `1000`).
4. Edit `.github/workflows/paper.yml` as README "Going live on Coinbase (locked)" shows. The edit swaps the shadow job's run step for the LIVE step, `python -m desk live --coin BTC --once --max-notional "$DESK_LIVE_MAX_NOTIONAL" --state-dir "$RUNNER_TEMP/cb"`, with `COINBASE_CDP_KEY: ${{ secrets.COINBASE_CDP_TRADE_KEY }}`. Put those secrets in a GitHub Environment limited to the `main` branch.
5. Watch the first fills by hand, in the Coinbase app.

To disarm:

1. Set the variable `DESK_COINBASE_KILL=flat`. The next run closes the position and stays flat.
2. Then delete `DESK_LIVE_CONFIRM`. Every later run refuses.
3. If you close the position yourself in the app, **cancel its stop too**. Coinbase has no reduce-only: a stop left resting with no position opens one when it fires. A refusing run still looks and pushes **STOP WITH NO POSITION**, but it doesn't cancel anything.

What every LIVE run does first, before it reads the product, the candles or the balance (any of which can fail): it reads the position and the open orders, puts a stop back on a position that has none, cancels any desk order left resting with no position, and honours `flat_kill`. A run that stops part way after that looks once more and pushes **NO STOP** or **STOP WITH NO POSITION** urgently. A stop the desk can't get to rest is never reported as closed unless the close actually went through.

The biggest real-money risks:

- GitHub runs the job about every 15 minutes, best effort, late or skipped at busy times. Between runs, only the stop resting on Coinbase protects the position.
- That stop is a stop-limit with its limit 5% past the trigger. A gap past the limit leaves it unfilled until the next run closes the position at market. The loss can then be bigger than the board's "5% risk": the board shows both figures.
- Each trailing move cancels the old stop before it places the new one, which leaves a gap with no stop resting. It is usually a second or two. If Coinbase is slow it is much longer. The new stop is tried up to 4 times, 1 s, 2 s and then 4 s apart, and read back after each try. Each try is one order request that waits up to 15 s for an answer and is sent up to 3 times, 2 s and then 4 s apart: about 50 s a try. So it can take about 3½ minutes before the desk gives up on the stop and closes the position at market. If Coinbase stops answering altogether, the desk keeps trying until 60 s past the run's deadline, which can be up to about 5½ minutes after the cancel (the run's 4½ minutes plus that minute), then pushes **NO STOP** urgently.
