# Connecting the desk to your Coinbase account

This page has two parts:

- **Part 1, now: SHADOW.** A **View-only** key lets the desk read your real Coinbase account. It runs the same decisions it would make with real money and records the orders it *would* send. **It sends no order.** The key can't trade or move money, and the desk's client refuses every order request before anything leaves GitHub.
- **Part 2, later and only if you decide to: LIVE.** A separate trade key, and deliberate arming steps. **LIVE is not armed.** Nothing in this repository arms it, and nothing in Part 1 does either.

ETH, SOL and VT stay paper accounts. Paper trading (all four accounts, their ledgers and boards) is unaffected by anything here.

The menu names below come from Coinbase's own pages and other projects' guides, read through search. They couldn't be checked click by click from here, and Coinbase renames things. If a label differs, look for the same idea nearby.

---

## Part 1: SHADOW (do this now)

### A. Create a View-only API key

1. In a browser, sign in at **portal.cdp.coinbase.com** (the Coinbase Developer Platform) with your usual Coinbase login. Use the same account that was approved for derivatives on 2026-10-13.
2. Open **API Keys**, choose the **Secret API Keys** tab, then **Create API key**.
3. Name it `btc-perp-desk shadow (view only)`.
4. Under **API restrictions** (permissions), tick **View** only. Leave **Trade** and **Transfer** unticked.
   - SHADOW refuses a key that can trade or move money. A mix-up stops it instead of risking anything.
5. **Portfolio:** choose your **Default** (Primary) portfolio. US futures only live there.
6. **IP allowlist:** leave it empty. GitHub's runners use changing addresses, so an allowlist would block every run.
7. Open **Advanced settings** and set the signature algorithm to **ECDSA**.
   - The portal's default is Ed25519, and sources disagree on whether Ed25519 keys work with the Advanced Trade API. ECDSA works everywhere. The desk accepts both, but use ECDSA.
8. Click **Create**, and confirm with your 2FA.
9. Click **Download** to save the key file. Coinbase no longer downloads it on its own, and the private key is shown only once.
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
    - Why: this repository and its Pages site are **public**. DESK_STATE_KEY encrypts SHADOW's state and board (AES-256-GCM), so only you can read your balances. You type it once on each device to open the BTC SHADOW page.
    - If you lose it, SHADOW starts over from scratch. No money is involved.
14. Delete the downloaded key file from your Downloads folder and empty the trash. Never paste the key anywhere else: not in a chat, an email, an issue or a commit.
15. In Coinbase, keep **only the desk's money** as **USD in the Default portfolio**:
    - SHADOW counts all USD there as the account.
    - USDC there can be pulled into futures margin. Move USDC, and any money that isn't the desk's, to another portfolio.
    - Deposit from your bank straight into the Default portfolio's USD. Transfers between portfolios show up as entries the desk doesn't know: it alerts on them and stops new entries until you've looked.

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
21. **Your first deposit opens the account.** On day one at $0 the desk reads and decides but sizes nothing. The first bank deposit it reads is the account's **start**: money in, not profit. Later deposits and withdrawals are recorded as money in or out at the account's value then, never as profit or loss.
22. If the phone says **not working**, the reason follows:
    - **401 Unauthorized:** Coinbase gives no reason. Re-create the key as ECDSA, check that the secret is the whole file (name and privateKey), and check that the key wasn't deleted.
    - **lacks the permission:** the key isn't View-enabled.
    - **can trade / can move money:** you made a trade or transfer key. Make a View-only key for SHADOW.
    - **DESK_STATE_KEY doesn't open the saved state:** the secret changed. Put the old password back, or start over (below).
23. **To stop SHADOW:** delete the `COINBASE_CDP_KEY` secret. The job then skips and stays green. You can also revoke the key in the CDP portal.
24. **Kill switches:** edit `BTC/kills.json` on the `coinbase-state` branch (`{"entry_kill": true}` or `{"flat_kill": true}`), or set the repository **variable** `DESK_COINBASE_KILL` to `entry` or `flat` (Settings → Secrets and variables → Actions → Variables). In SHADOW they stop the simulated trading, as they would stop LIVE's.
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

It refuses, with every check printed as PASS or FAIL and not one order request sent, unless **all** of these hold:

1. The go-live gate passes: the backtest report, and 14+ days of paper that are up time-weighted. The BTC paper ledger restarted on 2026-10-13, so not before about 2026-10-27.
2. `DESK_LIVE_CONFIRM` is set to exactly `I understand this trades real money and can lose all of it`.
3. The variable `DESK_LIVE_COINBASE` is exactly `BIP-20DEC30-CDE`.
4. `--max-notional` is given and buys at least one contract (about $830 at $83,000, plus slippage). Raise it by hand as BTC rises.
5. SHADOW's record on this account, read from its encrypted state:
   - 14+ days of runs, the last under 2 hours ago;
   - over the last 7 days: no move nothing explained, no unknown ledger entry, no balance that didn't add up, no run that couldn't read deposits, no auth failure;
   - at least one bank deposit reconciled to the cent;
   - money in the account on 7 of the last 14 days;
   - the Default portfolio, with its id recorded.
6. The key is **View + Trade and NOT Transfer**, on the same portfolio SHADOW read.
7. The clock is within 30 s of Coinbase's, the balance adds up, and there's no USDC in the portfolio.
8. At the first start, the account is flat with no open order on any product.

To arm it later, deliberately, when you choose:

1. Create a new key in the same portal:
   - name `btc-perp-desk live`;
   - permissions **View + Trade**, **NOT Transfer** (the desk never moves money, and LIVE refuses a key that can);
   - Default portfolio, no IP allowlist, ECDSA.
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

The biggest real-money risks:

- GitHub runs the job about every 15 minutes, best effort, late or skipped at busy times. Between runs, only the stop resting on Coinbase protects the position.
- That stop is a stop-limit with its limit 5% past the trigger. A gap past the limit leaves it unfilled until the next run closes the position at market. The loss can then be bigger than the board's "5% risk": the board shows both figures.
- Each trailing move leaves a few seconds with no stop resting. Up to about 30 s if Coinbase is slow.
