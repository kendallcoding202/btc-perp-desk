# Connecting the desk to your Coinbase account

This page has two parts:

- **Part 1, now: SHADOW.** A **View-only** key lets the desk read your real Coinbase account. It runs the same decisions it would make with real money and records the orders it *would* send. **It sends no order.** The key can't trade or move money, and the desk's client refuses every order request before anything leaves GitHub.
- **Part 2, later and only if you decide to: LIVE.** A separate trade key, and deliberate arming steps. **LIVE is locked in code** until a review of its stop handling comes back clean ([`coinbase-live-status.md`](coinbase-live-status.md) says what's open). Nothing in this repository arms it, and nothing in Part 1 does either.

Every time on the SHADOW page and in the phone pushes is **Mountain Time** (MT: MDT in summer, MST from the first Sunday of November), like "10:08 am MT".

ETH, SOL and VT stay paper accounts. Paper trading (all four accounts, their ledgers and boards) is unaffected by anything here.

The menu names below are quoted from Coinbase's own pages: its API key page (https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication) and its key guide for agents (https://docs.cdp.coinbase.com/coinbase-for-agents/skill.md), both read on 2026-10-14. The two pages name the last button differently (step 8), so both names are given. The portal itself couldn't be clicked through from here, and Coinbase renames things: if a label differs, look for the same idea nearby.

---

## Part 1: SHADOW (do this now)

### A. Create a View-only API key

1. In a browser, sign in at **portal.cdp.coinbase.com/api-keys/secret** (the Coinbase Developer Platform) with your usual Coinbase login. Use the same account that was approved for derivatives on 2026-10-13. Coinbase creates a project for you the first time you sign in.
2. Under the **API Keys** tab, select **Secret API Keys**, then click **Create API key**.
3. Enter an API key nickname, `btc-perp-desk shadow (view only)`, then expand **API restrictions** and **Advanced Settings**.
4. Under **Advanced Settings** → **Coinbase App & Advanced Trade**, under **Accounts**, select your **Default** portfolio. Coinbase: "US Derivatives (Futures) only work in your **default** portfolio". SHADOW refuses a key made on any other portfolio before it reads any money (it would read that portfolio's money as the account's).
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
    - Paste it with **no line break** at the end (some password managers copy one). The SHADOW page's password field can't hold a line break, so SHADOW refuses a `DESK_STATE_KEY` with one in it, and says so. It also refuses one that looks like the key file (the two secrets swapped), or that holds the same text as `COINBASE_CDP_KEY`.
    - Why: this repository and its Pages site are **public**. DESK_STATE_KEY encrypts SHADOW's state and board (AES-256-GCM), so the state and the board can only be opened with it. You type it once on each device to open the BTC SHADOW page.
    - Phone pushes are a separate channel: they go to ntfy.sh, where **anyone who knows your `NTFY_TOPIC` name can read them**. So SHADOW's pushes leave every amount out (they say "…" and point to the SHADOW page). To get amounts on your phone, reserve your `NTFY_TOPIC` topic on ntfy.sh (with an ntfy.sh account), make an access token for it, and store it as the secret `NTFY_TOKEN`. Either way, use a long random topic name.
    - If you reserve it, set everyone's access to **"I can publish and subscribe, everyone can publish"** (write-only). ntfy.sh's default, "Only I can publish and subscribe", refuses every push sent without your token: paper's pushes never use `NTFY_TOKEN`, so they would stop (paper's log only says "notify failed"), and so would SHADOW's resend without the token when the token is refused (step 22). Then log in to your ntfy.sh account in the phone app, or it stops receiving the reserved topic.
    - Keep it the same from now on. Losing or changing it is step 26.
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
    - If either secret, `COINBASE_CDP_KEY` or `DESK_STATE_KEY`, isn't set at all, the `shadow` job is skipped and stays green. It sends no push and writes no status, so no SHADOW bar appears: only the skipped job's log line says "SHADOW skipped - set the COINBASE_CDP_KEY and DESK_STATE_KEY secrets". If nothing arrives after step 16, check that both secrets are there.
18. Your phone (ntfy, if `NTFY_TOPIC` is set) gets **BTC SHADOW: connected (read only)** with the checklist of every read.
19. On the dashboard, a **BTC SHADOW** bar appears under the account tabs. It shows the public status: updated when, OK or not, the desk's decision, and no amounts.
    - It runs one run behind: the `step` job publishes the dashboard before the `shadow` job runs, so the bar shows the run before. After the first run (step 16) it appears with the next run, and after you fix something the old reason stays one more run.
    - Tap it to open the SHADOW page, and enter DESK_STATE_KEY.
    - It stays on that device for that tab. Tick "remember" only on your own phone or computer.
20. What the SHADOW page shows:
    - **Money:** your real Coinbase balance and, beside it, the **SHADOW balance**: the real one plus the estimated P&L of the orders LIVE would have sent, labelled as an estimate.
    - **The desk:** what it decided at the last 2h close. If the account holds too little to buy one contract (0.01 BTC, about $830), it says how much would be needed. It also shows, clearly marked **WHAT-IF, not your account**, what it would do with the planned $500.
    - **Orders it would send:** each exact request LIVE would POST to Coinbase, with its simulated fill. **None was sent.**
    - Every read this run, the simulated book, anything your real account holds that the desk didn't place, events, alerts, and the run's private log.
    - Whether its decision matched the paper BTC account's at the same close.
    - Coinbase closes once a week, **Fridays 3:00 to 4:00 pm MT**, and for maintenance windows it lists ahead. No entry is decided into a closure, and a stop isn't checked while Coinbase is closed.
21. **Your first deposit opens the account.** On day one at $0 the desk reads and decides but sizes nothing. The first bank deposit it reads is the account's **start**: money in, not profit. Later deposits, withdrawals and transfers are recorded as money in or out at the account's value then, never as profit or loss.
    - If the Default portfolio already holds USD at SHADOW's first run that works, that balance is the start instead ("the balance when SHADOW started"). Runs refused or stopped part way before that leave no state, so they don't change it. Until then the SHADOW page says "the balance hasn't been read yet", not "no money in it". Money that shows up later with no deposit SHADOW can read (it started at $0) is the start when it's found ("the balance found", with a **BTC SHADOW: account opened** push). Deposits after the start are money in.
22. If the phone says **BTC SHADOW: not working**, the reason follows. It comes at the first such run, and at once whenever the reason changes (a 401, then a key that can trade, say). While it lasts, it comes again:
    - **once SHADOW has saved a run that works**, for a run that stops part way (its state is still saved): 6 hours after the last push;
    - **before that, or for a run refused before it can save its state** (a secret, a deleted state): in the first run after 12 am, 6 am, 12 pm and 6 pm MDT (11 pm, 5 am, 11 am and 5 pm MST);
    - **for a refusal that can't even write the status** (a LIVE state SHADOW can't read, the state and the status deleted): it has nothing to remember a push in, so "at once" means the first run after the branch last changed (the run before saved, or your commit, in the 25 minutes before the run; the run after may send it once more, and a commit made on your own computer counts from when you made it). Then it comes in each run in the first 35 minutes after those times.

    **BTC SHADOW: working again** says when SHADOW ran and saved again, what the runs before had stopped over, and any read that still failed.

    The reasons, as the push and the dashboard's SHADOW bar say them:
    - **401 Unauthorized:** Coinbase gives no reason. Re-create the key as ECDSA, check that the secret is the whole file (name and privateKey), and check that the key wasn't deleted.
    - **isn't a Coinbase API key file:** the secret isn't the whole downloaded file (a partial paste, say). Open the file again and paste all of it, braces included (step 10).
    - **lacks the permission:** the key isn't View-enabled.
    - **can trade / can move money:** you made a trade or transfer key. Make a View-only key for SHADOW.
    - **is on the … portfolio, not Default:** the key was made on another portfolio. Create the View key again with your **Default** portfolio under **Accounts** (step 4). Nothing was read from that portfolio's money.
    - **DESK_STATE_KEY looks like the key file (the two secrets are swapped):** put the whole key file in `COINBASE_CDP_KEY` and the password in `DESK_STATE_KEY` (steps 12 and 13).
    - **DESK_STATE_KEY looks like the key file (the key file is in both secrets):** keep the key file in `COINBASE_CDP_KEY`, and put the password in `DESK_STATE_KEY` (step 13).
    - **DESK_STATE_KEY has a line break in it:** set the secret again, without the line break at the end (step 13).
    - **DESK_STATE_KEY doesn't open the saved state:** the secret changed after SHADOW had run. Put the old password back, or start over (step 26).
    - **DESK_STATE_KEY is too short:** fix the secret. The SHADOW bar on the dashboard says the same, with no amount. A secret that isn't set at all sends nothing: the job is skipped (step 17).
    - **an Ed25519 key, not ECDSA:** create the key again with ECDSA (step 7).
    - **Coinbase didn't answer** (at the clock check, or part of the run): a Coinbase or network outage. No usable answer came in time. The clock check reads "not checked", not failed. The next run tries again.
    - **Coinbase's answer couldn't be read:** Coinbase did answer, but in a shape the desk doesn't know (Coinbase changed something, say). That includes its answer about the key's permissions: a field missing or not true or false is said this way, never as a key that can't View or can trade, so don't make a new key for it. The push names the read and where the run stopped. The SHADOW page shows the run's log, with Coinbase's own words. The next run tries again. If it keeps coming, the desk's code needs a fix for the new answer.
    - **Coinbase's answer was incomplete, or its market was empty:** Coinbase answered in the shape the desk knows, but short: candles missing, an order book empty on one side, or no funding rate yet. The push says which. That's Coinbase's market or answer for the moment, not a change the desk's code needs to follow: the next run tries again.
    - **Coinbase refused a read:** Coinbase answered with a refusal (an HTTP 4xx, say). The push names it, and the SHADOW page's log has the details. The next run tries again.
    - **SHADOW hit an error of its own, not Coinbase's:** a bug in the desk's code. The push names the error's type and where it happened: the step (like "while reading the fee tier, sweeps and fills") and the desk's function, with no amounts. The SHADOW page shows the run's log, with the error's own text. The next run tries again. If it keeps happening, it needs a fix in the code: pass on what the push says (it holds no key or amount).
    - **this machine's clock is … off Coinbase's:** that's GitHub's runner, not your computer. Nothing to fix: the next run tries again on another runner.
    - **COINBASE_CDP_KEY and DESK_STATE_KEY hold the same text:** the password went into both. Put the whole key file in `COINBASE_CDP_KEY` (step 12) and keep the password in `DESK_STATE_KEY`.
    - Whatever the first runs were refused over, fix it and SHADOW starts at the next run, and the dashboard's old reason goes away. Until SHADOW has had a run that works, a run that's refused or stops part way saves its status, never a state (the key file, a Trade key, a 401, an Ed25519 key, the wrong portfolio, the clock, the two secrets swapped, Coinbase not answering at the key check or later in the run, an answer it can't read, an error of its own), so you can change `DESK_STATE_KEY` while you fix them. Once the key has passed, or for an answer it couldn't read or an error, it saves its board too: the run's checks and its log, for the SHADOW page. The runs never open that board: after you change `DESK_STATE_KEY`, the page opens with the new password from the next run on. Once SHADOW has had a run that works, keep it (step 26).
    - **No push arrives at all, though `NTFY_TOPIC` is set:** if you added `NTFY_TOKEN`, ntfy.sh may be refusing it (a wrong or expired token, or one for another topic). Each push is then sent again without the token, its amounts left out, ending "NTFY_TOKEN was refused by ntfy.sh": fix the token, or delete the secret. A push that still doesn't go out is listed on the SHADOW page (its alerts and log) when that run saves its board; a run refused before it opens its state keeps no record of it. If the token is fine but nothing arrives, check the topic's access on ntfy.sh (step 13: write-only for everyone) and that the phone app is logged in.

    Two other pushes come with their own titles:
    - **BTC SHADOW: ALERT** (each kind at most every 6 hours): something SHADOW noticed while it ran, said in plain words. Among them: **kills.json can't be read** (a typo, say), a file that **looks like a kill switch but isn't BTC/kills.json**, and **the desk set entry_kill itself**: see step 24.
    - **BTC SHADOW: NOT SAVED** (at once, then every 6 hours): the save check would refuse this run's state. The reason follows.
      - **`BTC/status.json` deleted** on the `coinbase-state` branch (with `README.md` or anything else): the next run that opens the saved state writes it back from it and saves nothing else (that's the NOT SAVED), and the run after saves as usual (**working again**). A run refused over a secret before that (`DESK_STATE_KEY` too short, say) writes nothing (no status without the state's counts is saved), and its **not working** push, every 6 hours while it lasts, says the status is missing too: fix the secret, and the next run writes it back. Meanwhile the dashboard's SHADOW bar goes, and the SHADOW page says the status is missing.
      - **`BTC/status.json` edited** into one the save check would refuse (it isn't JSON any more, it has a key the desk doesn't write, its `mode` isn't SHADOW (set to LIVE, say, which arms nothing), or its start or its run count no longer match the state): the same as a deleted one. The next run that opens the saved state writes it back from it (NOT SAVED), and the run after saves as usual. An edit the check takes anyway (fewer runs, say) is left as it is, and the next save replaces it.
      - **`BTC/state-shadow.enc.json` deleted** between two runs: every run is refused until you put it back as it was (revert the commit that deleted it), or delete the branch to start over (step 25). Until then SHADOW's state isn't saved and the SHADOW page stops moving. Deleted while a run is under way, it's put back by that run's save.
      - **`BTC/state-shadow.enc.json` and `BTC/status.json` both deleted**, with `BTC/state-shadow.prev.enc.json` (the state before the last) still there: every run is refused, reading nothing and writing nothing (a run refused over a secret as well writes nothing either, and its push names both), with **not working** at the first run after your commit, then in the 6-hourly runs above, until you put both back as they were (revert the commit that deleted them), or delete the branch to start over (step 25). The save check refuses any save beside the state before the last, so no new state ever starts over the record. Meanwhile the SHADOW page says the status is missing, or, with the board deleted too, that there's no status.
      - **Every SHADOW file deleted** (the whole branch emptied, `state-shadow.prev.enc.json` included): that's starting over, as deleting the branch is. The next run opens a new state. Its push is **BTC SHADOW: connected, started over (read only)** while the branch still shows SHADOW had worked there (its board, or the desk's `BTC/kills.desk.json`); with nothing left to show it, it's **BTC SHADOW: connected (read only)**, saying SHADOW's record starts from this run. Either way SHADOW's record for LIVE (its 14 days) starts again. A branch you made yourself before SHADOW's first run (only your `BTC/kills.json` on it, say) is a plain first run: **connected (read only)**.
      - A revert only works until a run saves: each save replaces the branch's one commit, and your commit and what it deleted leave the branch's history with it.
23. **To stop SHADOW:** delete the `COINBASE_CDP_KEY` secret. The job then skips and stays green. You can also revoke the key in the CDP portal. (Once you've switched to LIVE, Part 2, the job runs on `COINBASE_CDP_TRADE_KEY` instead and the View key no longer starts or stops anything.)

    **Leave the `coinbase-state` branch alone**, apart from `BTC/kills.json` (step 24). Don't edit, rename or delete SHADOW's own files there (the state, `BTC/status.json`, the boards): the desk copes with most such edits, but one combined with a mistyped secret can leave SHADOW pushing **not working** at every run, or quiet for a few hours. To start over, delete the whole branch (step 25).

24. **Kill switches:** edit `BTC/kills.json` on the `coinbase-state` branch (`{"entry_kill": true}` or `{"flat_kill": true}`), or set the repository **variable** `DESK_COINBASE_KILL` (Settings → Secrets and variables → Actions → Variables). Both read the same words, anywhere in the text: **flat**, **flatten**, **close** (`flat_kill`, `close_all`, `close_position` too) mean flat_kill (close the position); **entry** (`entry_kill`, `no_entries`) means entry_kill (no new entries); one value can say both (`flat entry`). A variable with neither word is read as `entry` and alerted: delete the variable to turn it off. In SHADOW they stop the simulated trading, as they would stop LIVE's.
    - **If the desk can't read the file** (it isn't JSON, like `{"flat_kill": true,}` with its trailing comma; a key it doesn't know, like `{"flat": true}`; a switch that isn't `true` or `false`, like `"true"` in quotes or `1`; a key written twice), it fails closed. It reads **entry_kill on** (no new entries), and every switch the file names is **on** unless its value is one of the words for off: `false`, `null`, `0`, `""`, `"false"`, `"f"`, `"no"`, `"n"`, `"off"`, `"none"`, `"nil"`, or a near-typo of false, null, off or no (`fasle`, `nul`, `noo`). Any other value is on: `"now"`, `"flat"` and `"force"` close the position. So `{"flat_kill": true,}` and `{"close": true}` still close the position, and `{"flat_kill": false, "entry_kill": true,}` closes nothing. Nothing else in the file is acted on, `reset_halt_at` included. The SHADOW page, the status bar and your phone say so, naming `flat_kill` when the file does.
    - The run is still saved, and **the save keeps the file exactly as you wrote it**, so every run reads it the same way, and says so (the phone at most every 6 hours), until you fix it. For a close you can be sure of, set the variable `DESK_COINBASE_KILL` to `flat`.
    - **A kill the desk sets itself** (after a move nothing explains, say) is written into `BTC/kills.json` (`"entry_kill": true`), with its reason on the SHADOW page, and recorded in `BTC/kills.desk.json` (the desk's file: don't edit it). It stays on until **you set it to `false` in `BTC/kills.json`** once you've looked. If the desk set it while it couldn't read your file, your typo is kept and the kill stays on anyway: fixing the typo doesn't turn it off. The next save writes it into your fixed file, and the phone says so; then set it to `false` there.
    - **Deleting `BTC/kills.json`** (or renaming it) turns your switches off, but never a kill the desk set: that stays on, the next save writes it into a new `BTC/kills.json`, and the phone says so. A `BTC/kills.json` you delete while a run is under way turns your switches off: that run acted on the copy it loaded, and its save writes a fresh `BTC/kills.json` with every switch false (a kill the desk set stays on in it).
    - **Other files you add on the branch** (notes, say) are kept as they are at every save, and never read. One whose name has "kill" in it anywhere on the branch, but isn't exactly `BTC/kills.json` (`kills.json` at the top, `btc/kills.json`, `BTC/Kills.json`, `BTC/kills.json.orig`), turns **entry_kill on**, and the phone says so, until you move it to `BTC/kills.json` or delete it. A `flat_kill` in it is named, but only `BTC/kills.json` closes the position. Deleting one of your files while a run is under way is fine: that run's save goes through, without it. A `.gitattributes` on the branch is dropped at the next save (it could change how git stores the desk's files); a `.gitignore` is kept and never hides the desk's files; a `.github/` folder is kept exactly as it is, and the desk never changes it.
    - **To clear a drawdown halt** once you've looked: add `"reset_halt_at": "2026-11-02"` (any new text, a date is easiest) to the same `BTC/kills.json`. The next run clears the halt once, restarts the peak from the balance then, and says so on the board. Use a new text for the next reset.
25. **To start SHADOW over:** delete the `coinbase-state` branch (or every file on it). The next run opens a new state.
26. **Losing or changing `DESK_STATE_KEY`.** SHADOW never starts a new state over one the password doesn't open: every run is refused with **DESK_STATE_KEY doesn't open the saved state** (nothing is read or sent; the state is kept) until the old password is back.
    - To change it on purpose (you typed it on a shared device, say): set the new secret, then delete the `coinbase-state` branch (step 25). That loses what's on the branch: SHADOW's record (its 14 days for LIVE start again), the kill switches and any file of yours there. Put your `BTC/kills.json` back after the first new run if you had one. Then, on each device, click **forget the key on this device** at the foot of the SHADOW page and type the new password.
    - Never change it once LIVE has armed (Part 2): a LIVE run whose `DESK_STATE_KEY` doesn't open its state sends nothing, so only the stop resting on Coinbase protects the position. Disarm and close the position first.
    - A key on another Coinbase login's Default portfolio is another account: SHADOW closes the old one's period, opens the new one from its balance and starts its 14 days again (said on the phone), rather than reading the change as money moving.

What SHADOW can't tell you yet: whether Coinbase fills, fees and price bands match the simulation. Its fills are at Coinbase's best bid or ask when it looked, with the fee **estimate** (max $0.20 a contract, 5 bps). The real fee tier is read and shown, but Coinbase's per-contract futures fee isn't in it.

---

## Part 2: the trade key and LIVE (later; not armed)

**LIVE is locked in code, and nothing here arms it.** Six rounds of review kept finding new money-safety problems in how LIVE handles its stops, so `desk/coinbase_live.py` refuses every LIVE attempt, first of all, until a review comes back clean: *LIVE isn't finished: its last review found open safety problems, see docs/coinbase-live-status.md*. No secret, variable, flag or `kills.json` gets past it, and a refused attempt sends nothing and saves nothing. **[`coinbase-live-status.md`](coinbase-live-status.md) lists what's still open.** SHADOW (Part 1) is unaffected. The steps below are for the day that lock is lifted, which is only after the open items on that page are fixed. What follows says how LIVE is meant to work; where it doesn't yet (a position left without a working stop, with nothing urgent pushed), that page says so. Don't create the trade key early: nothing uses it, and an unused key is still a key to lose.

LIVE places real orders on `BIP-20DEC30-CDE`:
- entries as whole contracts at market;
- a protective stop-limit resting on Coinbase at all times (Coinbase has no stop-market and no reduce-only);
- the stop trailed at each run, cancel first, then the new one (two full-size stops at once could open a short);
- exits by cancelling the stop, then closing the position.

It has only ever run against the tests' local fake Coinbase.

Once the lock is lifted, it refuses, with every check printed as PASS or FAIL and not one order request sent, unless all of the checks below pass. Some are checked only at the **first LIVE start** (the first run that arms). Once LIVE has armed, the later runs don't check them again.

At the first LIVE start only:

1. The go-live gate passes: the backtest report, and 14+ days of paper that are up time-weighted. The BTC paper ledger restarted on 2026-10-13, so not before about 2026-10-27.
2. SHADOW's record on this account, read from its encrypted state:
   - 14+ days of runs, the last under 2 hours ago;
   - over the last 7 days: no move nothing explained, no unknown ledger entry, no balance that didn't add up, no run that couldn't read deposits, no auth failure;
   - at least one bank deposit reconciled to the cent;
   - money in the account on 7 of the last 14 days;
   - the Default portfolio, with its id recorded.
3. The account is flat, with no open order on any product.

At every LIVE run, what any order needs:

4. The key is **View + Trade and NOT Transfer**, on the same portfolio SHADOW read, and the clock is within 30 s of Coinbase's.
5. `DESK_STATE_KEY` opens LIVE's saved state, and it's for `BIP-20DEC30-CDE`. Never change `DESK_STATE_KEY` once LIVE has armed (Part 1, step 26).

And what only new trades need:

6. `DESK_LIVE_CONFIRM` is set to exactly `I understand this trades real money and can lose all of it`.
7. The variable `DESK_LIVE_COINBASE` is exactly `BIP-20DEC30-CDE`.
8. `--max-notional` is given and buys at least one contract (about $830 at $83,000, plus slippage). Raise it by hand as BTC rises. Write it as a plain number of USD: `1000`, `$1000` and `1,000` all mean $1,000 (commas only between whole thousands). Anything else (`1,5`, `1000,50`, `10 00`), or a deleted or blank `DESK_LIVE_MAX_NOTIONAL`, is a missing `--max-notional`, never a cap read 10 or 100 times too large and never a run that doesn't start: once LIVE has armed, a protect-only run; at the first start, a refusal.
9. The balance adds up, and there's no USDC in the portfolio.

**Once LIVE has armed, a run that fails only checks 6 to 9 is PROTECT ONLY.** It sends no entry, adds nothing and moves no stop, but it still protects the position it holds: it honours `flat_kill` (closes at market), puts a stop back on a position that has none (never wider than the saved one), tops up at the saved level a stop that covers only part of the position, cancels desk stops that cover more than the position (keeping first the ones that can still fill), and closes at market a stop the price gapped past (a stop-limit that triggered and didn't fill). Only a stop counts as cover: a take-profit limit of yours doesn't, and if it leaves no room for a stop, NO STOP is pushed. The order client itself refuses anything else, before it's sent: it reads the position and every open order on the product first (queued ones, and ones placed in the app, included), and cancels only the desk's own orders. Your phone gets **BTC LIVE: PROTECT ONLY** urgently at every such run, naming the check and what the desk did, as it happened. A read that fails while a check is made is named as that ("Coinbase's balance couldn't be read"), never as the check failing ("the balance doesn't add up"). **Deleting `DESK_LIVE_CONFIRM` stops new trades; the desk keeps protecting a position that is already open. To close it, set `flat_kill`.**

A run that fails check 4 or 5 sends nothing at all (it can't safely place an order). It still looks, read only, and pushes urgently at every run what can't wait: **NO STOP** (none, or stops covering only part of the position), **STOP WITH NO POSITION**, **STOP DIDN'T FILL** (a gapped stop: close the position in Coinbase), **FLAT_KILL NOT DONE** (`flat_kill` is on but nothing can close the position: fix the check, or close it in Coinbase), and **STOPS COVER MORE THAN THE POSITION**. That look is signed on Coinbase's clock, so a run refused over the clock still makes it. If Coinbase can't be read at all, a `flat_kill` that's on, with the position the desk last saved, is still pushed (**FLAT_KILL NOT DONE**), and the not-working push says LIVE "didn't arm this run" (it is still armed).

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
4. Edit `.github/workflows/paper.yml` as README "Going live on Coinbase (locked)" shows. The edit swaps the shadow job's run step for the LIVE step, `python -m desk live --coin BTC --once --max-notional "$DESK_LIVE_MAX_NOTIONAL" --state-dir "$RUNNER_TEMP/cb"`, with `COINBASE_CDP_KEY: ${{ secrets.COINBASE_CDP_TRADE_KEY }}`, and changes the job's `SHADOW_ON` line to check `COINBASE_CDP_TRADE_KEY` instead of `COINBASE_CDP_KEY`. From then on the View key no longer starts or stops the job: deleting it skips nothing, and keeping it isn't needed. Put those secrets in a GitHub Environment limited to the `main` branch.
5. Watch the first fills by hand, in the Coinbase app.

To disarm:

1. Set the variable `DESK_COINBASE_KILL=flat`. The next run closes the position and stays flat.
2. Then delete `DESK_LIVE_CONFIRM`. Every later run is protect only: no new trades, and with no position, nothing to send but a cancel of a desk order left resting.
3. Only once Coinbase shows no position and no order on `BIP-20DEC30-CDE` (the PROTECT ONLY push says "No position is open"), stop the job: delete the `COINBASE_CDP_TRADE_KEY` secret (the job then skips, green, and no longer looks at the account), or revoke the trade key in the CDP portal. Never with a position open: the job is what watches it. Deleting `COINBASE_CDP_KEY`, the View key, does nothing once you've switched.
4. If you close the position yourself in the app, **cancel its stop too**. Coinbase has no reduce-only: a stop left resting with no position opens one when it fires. A refusing run still looks and pushes **STOP WITH NO POSITION**, but it doesn't cancel anything.

What every LIVE run does first, right after arming and before it reads the product, the candles or the balance (any of which can fail; the clock and the key's permissions aren't read again, arming just read them): it reads the position and the open orders, puts a stop back on a position that has none or tops up one that covers only part of it, closes at market a stop the price gapped past, cancels any desk order left resting with no position, and honours `flat_kill`. Desk stops covering more than the position are cut back to it, and a stop that can still fill is meant never to be cancelled to keep one that can't (not yet when one of two desk stops has gapped: item 1 on [`coinbase-live-status.md`](coinbase-live-status.md)). A close cancels the stop first (no reduce-only), so it never starts unless it can go through: not while Coinbase reads closed (its weekly break, Fridays 3:00 to 4:00 pm MT, or maintenance), or when its product can't be read to tell (market orders can't be queued: the close waits and the stop stays); not when an order that isn't yours covers the closing side; and not without the time, at the speed Coinbase is answering, to cancel, close and put the stop back if the close fails (the urgent **FLAT_KILL NOT DONE** says so, the stop kept). A close refused after the cancel puts the stop back at its trigger; one that fills only in part is closed again or gets a stop on the rest. A run that stops part way after that looks once more and pushes **NO STOP** or **STOP WITH NO POSITION** urgently. A position is never reported as closed unless Coinbase reads it flat, and never as protected unless the safety pass ran and left it protected (the PROTECT ONLY push says so when it didn't run: check Coinbase).

The biggest real-money risks:

- GitHub runs the job about every 15 minutes, best effort, late or skipped at busy times. Between runs, only the stop resting on Coinbase protects the position.
- That stop is a stop-limit with its limit 5% past the trigger. A gap past the limit leaves it unfilled until the next run closes the position at market. The loss can then be bigger than the board's "5% risk": the board shows both figures.
- Each trailing move cancels the old stop before it places the new one, which leaves a gap with no stop resting. It is usually a second or two. If Coinbase is slow it is much longer. The new stop is tried up to 4 times, 1 s, 2 s and then 4 s apart, and read back after each try. Each try is one order request that waits up to 15 s for an answer and is sent up to 3 times, 2 s and then 4 s apart: about 50 s a try. So it can take about 3½ minutes before the desk gives up on the stop and closes the position at market. If Coinbase stops answering altogether, the desk keeps trying until 60 s past the run's deadline, which can be up to about 5½ minutes after the cancel (the run's 4½ minutes plus that minute), then pushes **NO STOP** urgently.
