# Coinbase LIVE: where it stands

**LIVE is locked in code, and stays locked until a review comes back clean.** LIVE is the mode that would place real orders on `BIP-20DEC30-CDE`. In `desk/coinbase_live.py`, `LIVE_REVIEWED = False`. While it's False:

- Every LIVE attempt is refused first, before any other check, with: *LIVE isn't finished: its last review found open safety problems, see docs/coinbase-live-status.md*. No key is loaded, no request goes to Coinbase, no order is sent, and nothing is saved on the `coinbase-state` branch. Your phone gets **BTC LIVE: not armed** with that reason.
- Nothing you can set gets past it: not a secret, a repository variable, a command-line flag (`--force-unproven`, `--local`), `BTC/kills.json` or a saved state. Only a change to the code does.
- **SHADOW is unaffected.** It never arms anything, its key can't trade, and its client can't send an order. It keeps running every 15 minutes, and its record (the 14 days LIVE will need) keeps counting.

LIVE has only ever run against the tests' local fake Coinbase.

## Why it's locked

LIVE has had six rounds of review. Each one found new ways a real position could be left without a working stop. The last round found these six, and they're still open:

1. **Two desk stops, one of them gapped.** The position has two desk stops, and the price has gapped through one: it triggered and can't fill. LIVE keeps the stop at the saved level, which is the gapped one, and cancels the other, which still works. The position is left with no stop that can fill. If the safety pass couldn't read the position first, nothing urgent is pushed. If Coinbase is open and the close that follows fails, the stop goes back only at the saved level, which the price is already through.
2. **A resize that cancels one stop, then can't cancel the next.** To resize two part-size stops (after the position grew in the app, say), LIVE cancels them one by one before placing the new one. If the second cancel is refused, the first is already gone. Part of the position then has no stop, the alert says the remaining stop "still protects the position", and nothing urgent is pushed. This repeats at every run while Coinbase keeps refusing that cancel.
3. **A position grown in the app while Coinbase is closed.** If the safety pass can't read the position, LIVE sees that its stop is too small but waits for the reopen to resize it. The added contracts have no stop until the first run after the reopen. Nothing urgent is pushed, and the alert says "resizing the stop", which it isn't doing.
4. **`flat_kill` while Coinbase is closed.** A full LIVE run with `flat_kill` on holds the close back while Coinbase is closed (a market order can't be queued). It pushes only a high-priority TRADE note, not the urgent **FLAT_KILL NOT DONE** that a protect-only run sends. The stop stays meanwhile.
5. **A stop whose cancel is already queued.** While Coinbase is closed, a desk stop left with no position gets its cancel queued, and Coinbase cancels it at the reopen, before anything trades. The watchdog still pushes it urgently as **STOP WITH NO POSITION** ("cancel it in Coinbase now"), at every refused or protect-only run until the reopen. It's a false alarm, but an urgent one.
6. **Coinbase's position read lagging an entry.** If Coinbase answers "no position" for 4 reads or more right after an entry fills, LIVE takes that as its stop having filled. It books an exit that never happened and never places the stop. The position stays open with no stop until a later run reads it (15 minutes or more later), and nothing urgent is pushed.

Items 1, 2, 3 and 6 could leave real money without a working stop. None of them is fixed yet: LIVE's hardening continues separately.

## What unlocks it

A review of LIVE's stop handling that comes back clean, with each problem above fixed and held by a test. Then a code change sets `LIVE_REVIEWED = True`. The tests already run LIVE with the lock open (`tests/conftest.py`), and `tests/test_coinbase_live_lock.py` checks that it's shut in the code as it stands.

Until then, [Part 2 of the setup guide](coinbase-setup.md#part-2-the-trade-key-and-live-later-not-armed) is for later. Don't create the trade key yet.
