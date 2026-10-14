"""The lock on Coinbase LIVE: the Armed token, and arm(), the one function that makes one.

CoinbaseREST (desk/brokers/coinbase_rest.py) refuses every POST, before a socket opens, unless it was built with a valid
Armed token; this module is the only place one is made (tests/test_coinbase_lock.py scans desk/ for any other
"Armed(" or "armed="). A token names the product and the cap it was armed for and lives 15 minutes: one run.

arm() makes one only after every check passes, and prints each as PASS or FAIL. The local ones come first, before any
network call; any FAIL means no token, no order client, and not one non-GET request:

  local (no network)
    the go-live gate        the backtest report passes, paper has 14+ days and is up time-weighted
                            (__main__.promotion_status); --force-unproven overrides these two, nothing else
    DESK_LIVE_CONFIRM       exactly "I understand this trades real money and can lose all of it"
    DESK_LIVE_COINBASE      exactly BIP-20DEC30-CDE, the product being armed
    --max-notional          given (it must buy one contract: checked at the price, below)
    SHADOW's proof          (shadow_proof, read by the caller from SHADOW's encrypted state) 14+ days of SHADOW runs on
                            this account, the last under 2 hours ago; over the last 7 days no move nothing explained,
                            no unknown ledger entry, no balance that didn't add up, no run that couldn't read the
                            deposits, no auth failure; at least one bank deposit reconciled to the cent; at least 7 of
                            the last 14 days with money in the account and the balance adding up; the Default
                            portfolio seen, and its id recorded
  network (GET only)
    the clock               within 30 s of Coinbase's
    the key                 View + Trade, and NOT Transfer (the desk never moves money)
    the portfolio           Default, and the same one SHADOW read (the trade key is a different key)
    the cap                 --max-notional buys one contract (0.01 BTC) at the price now, slippage included
    the balance             adds up (coinbase_orders.parse_balance)
    no USDC                 in the portfolio (Coinbase can pull it into futures margin)
    flat                    at the first LIVE start only: no futures position and no open order on any product

The gate and SHADOW's proof are checked at the first LIVE start (live_state has no armed_at yet); the rest every run.
Nothing in this repository calls arm() outside the tests' fake Coinbase: no workflow arms it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .brokers.coinbase_auth import CdpKey
from .brokers.coinbase_rest import DEFAULT_BASE, CoinbaseError, CoinbaseREST
from .thresholds import COINBASE

PRODUCT = COINBASE.product
ARM_TTL_S = 15 * 60
LIVE_CONFIRM_PHRASE = "I understand this trades real money and can lose all of it"    # = desk/__main__.py's
SHADOW_DAYS = 14
SHADOW_CLEAN_DAYS = 7
SHADOW_FRESH_MS = 2 * 3600_000
SHADOW_FUNDED_DAYS = 7
DAY_MS = 86400_000
# What a SHADOW proof holds (the caller reads it from SHADOW's state): started_ts, last_run_ts, portfolio_type,
# portfolio_uuid, deposits_reconciled (completed bank deposits that reconciled to the cent), and days: {"YYYY-MM-DD":
# {runs, ok, flows_unreadable, unexplained, unknown_entries, equity_mismatch, auth_fail, funded (money in the account
# and the balance adding up at some run that day)}}.
DIRTY = ("unexplained", "unknown_entries", "equity_mismatch", "flows_unreadable", "auth_fail")

_SEAL = object()


class Armed:
    """Proof, for one run, that arm() passed every check. Only arm() can make one."""
    __slots__ = ("_seal", "product", "cap", "checks", "made_s", "expires_s")

    def __init__(self, seal, product: str, cap: float, checks: tuple, now_s: float):
        if seal is not _SEAL:
            raise TypeError("an Armed token is made only by coinbase_live.arm(), after every arming check passes")
        self._seal, self.product, self.cap, self.checks = seal, product, float(cap), tuple(checks)
        self.made_s, self.expires_s = now_s, now_s + ARM_TTL_S

    def valid(self, now_s: float) -> bool:
        return self._seal is _SEAL and self.product == PRODUCT and self.made_s <= now_s < self.expires_s

    def __reduce__(self):
        raise TypeError("an Armed token can't be copied or saved")

    def __reduce_ex__(self, protocol):
        raise TypeError("an Armed token can't be copied or saved")

    def __repr__(self) -> str:
        return f"Armed({self.product}, cap ${self.cap:,.0f})"


def shadow_checks(proof: dict | None, now_ms: int) -> list[tuple[str, bool, str]]:
    """SHADOW's record on this account, as arming checks."""
    if not proof:
        return [("SHADOW has run on this account (its state opens with DESK_STATE_KEY)", False, "no SHADOW record")]
    days = proof.get("days") or {}
    started, last = int(proof.get("started_ts") or now_ms), int(proof.get("last_run_ts") or 0)
    ran = (last - started) / DAY_MS
    out = [(f"SHADOW ran {SHADOW_DAYS}+ days on this account", ran >= SHADOW_DAYS, f"{ran:.1f} days"),
           ("SHADOW's last run is under 2 hours old", 0 <= now_ms - last < SHADOW_FRESH_MS,
            f"{(now_ms - last) / 3600_000:.1f} h ago" if last else "never")]
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    week = [(today - timedelta(days=k)).isoformat() for k in range(SHADOW_CLEAN_DAYS)]
    fortnight = [(today - timedelta(days=k)).isoformat() for k in range(SHADOW_DAYS)]
    for k in DIRTY:
        n = sum(int((days.get(d) or {}).get(k) or 0) for d in week)
        label = {"unexplained": "no move nothing explained", "unknown_entries": "no unknown ledger entry",
                 "equity_mismatch": "no balance that didn't add up", "flows_unreadable": "no run that couldn't read "
                 "the deposits", "auth_fail": "no auth failure"}[k]
        out.append((f"SHADOW, last {SHADOW_CLEAN_DAYS} days: {label}", n == 0, f"{n}"))
    dep = int(proof.get("deposits_reconciled") or 0)
    out.append(("SHADOW reconciled a bank deposit to the cent", dep >= 1, f"{dep}"))
    funded = sum(1 for d in fortnight if (days.get(d) or {}).get("funded"))
    out.append((f"SHADOW, {SHADOW_FUNDED_DAYS} of the last {SHADOW_DAYS} days with money in the account and the "
                f"balance adding up", funded >= SHADOW_FUNDED_DAYS, f"{funded} days"))
    out.append(("SHADOW saw the Default portfolio", proof.get("portfolio_type") == "DEFAULT",
                str(proof.get("portfolio_type"))))
    out.append(("SHADOW recorded the portfolio's id", bool(proof.get("portfolio_uuid")), "recorded"
                if proof.get("portfolio_uuid") else "missing"))
    return out


def arm(key: CdpKey, *, env, max_notional: float | None, gate: list, force_unproven: bool = False,
        shadow_proof: dict | None = None, live_state: dict | None = None, base_url: str = DEFAULT_BASE,
        clock=time.time, sleep=time.sleep, out=print, deadline_s: float | None = None, slippage_bps: float = 3.0,
        emit=print):
    """Every arming check (module docstring). All pass: (an order-capable CoinbaseREST, its Armed token). Any
    fails: None, with every check printed and the first failure said; not one non-GET request was made."""
    from .brokers.coinbase import CoinbaseExchange

    first = not (live_state or {}).get("armed_at")
    lines: list[tuple[str, bool, str]] = []
    gate_ok = True
    if first:
        for label, ok, val in gate:
            lines.append((label, ok, val))
            gate_ok = gate_ok and ok
    local = [(f'DESK_LIVE_CONFIRM is the exact phrase ("{LIVE_CONFIRM_PHRASE}")',
              env.get("DESK_LIVE_CONFIRM") == LIVE_CONFIRM_PHRASE, "set" if env.get("DESK_LIVE_CONFIRM") else "not set"),
             (f"DESK_LIVE_COINBASE is exactly {PRODUCT}, the product being armed",
              env.get("DESK_LIVE_COINBASE") == PRODUCT, repr(env.get("DESK_LIVE_COINBASE") or "")),
             ("--max-notional is given", bool(max_notional and max_notional > 0),
              f"${max_notional:,.2f}" if max_notional else "missing")]
    if first:
        local += shadow_checks(shadow_proof, int(clock() * 1000))
    lines += local

    def report(failed: str | None) -> None:
        for label, ok, val in lines:
            out(f"  {'PASS' if ok else 'FAIL'}  {label} — {val}")
        if failed:
            out(f"\n  Not going live on Coinbase: {failed}")

    if first and not gate_ok:
        if not force_unproven:
            report("the desk hasn't proven itself on paper yet (the go-live gate)")
            return None
        out("  --force-unproven: overriding the backtest and paper checks only. This is your call, not the desk's.")
    bad = next((label for label, ok, _ in local if not ok), None)
    if bad:
        report(bad)
        return None

    # ---- network: reads only, on a client that can't send an order
    rest = CoinbaseREST(key, base_url=base_url, clock=clock, sleep=sleep, deadline_s=deadline_s, emit=emit)
    ex = CoinbaseExchange(rest, on_alert=lambda text: None)

    def check(label: str, fn) -> bool:
        try:
            ok, val = fn()
        except CoinbaseError as exc:
            ok, val = False, f"{type(exc).__name__}: {exc}"
        lines.append((label, ok, val))
        return ok

    def skew():
        s = rest.server_skew()
        return True, f"{-s:+.1f} s"

    def perms():
        p = ex.key_permissions()
        ok = p["can_view"] and p["can_trade"] and not p["can_transfer"]
        why = "View + Trade, no Transfer" if ok else (
            "this key can move money (Transfer). LIVE only runs with View + Trade; create a key without Transfer"
            if p["can_transfer"] else "this key can't trade (it needs View + Trade)")
        return ok, why

    def portfolio():
        p = ex.permissions or {}
        same = not shadow_proof or not shadow_proof.get("portfolio_uuid") or \
            p.get("portfolio_uuid") == shadow_proof.get("portfolio_uuid")
        ok = p.get("portfolio_type") == "DEFAULT" and same
        return ok, (f"{p.get('portfolio_type')}" + ("" if same else ", not the portfolio SHADOW read: the trade key "
                                                                   "must be on the same account and portfolio"))

    def cap():
        ex.load_markets()
        bba = ex.best_bid_ask()
        px = bba[1] if bba else (ex.details or {}).get("price")
        if not px:
            return False, "no price to check it at"
        need = COINBASE.contract_size * px * (1 + slippage_bps / 1e4)
        return max_notional >= need, f"one contract is ${need:,.2f} at {px:,.0f}; the cap is ${max_notional:,.2f}"

    def balance():
        ex.fetch_balance()
        return ex.balance_block is None, ex.balance_block or "adds up"

    def usdc():
        w = ex.wallets()
        return not w["usdc"], "none" if not w["usdc"] else "USDC is in the portfolio: move it to another one"

    def flat():
        pos, orders = ex._fresh()
        f = ex.foreign
        n = (1 if pos else 0) + len(orders) + len(f["positions"]) + len(f["orders"])
        return n == 0, "flat, no orders" if n == 0 else "LIVE starts flat: close or cancel them yourself first"

    steps = [("this machine's clock is within 30 s of Coinbase's", skew), ("the key is View + Trade, not Transfer", perms),
             ("the key's portfolio is Default (and SHADOW's)", portfolio), ("--max-notional buys one contract", cap),
             ("Coinbase's balance adds up", balance), ("no USDC in the portfolio", usdc)]
    if first:
        steps.append(("the account is flat with no open order (first LIVE start)", flat))
    for label, fn in steps:
        if not check(label, fn):
            report(label)
            return None
    report(None)
    token = Armed(_SEAL, PRODUCT, max_notional, tuple(label for label, _, _ in lines), clock())
    client = CoinbaseREST(key, armed=token, base_url=base_url, clock=clock, sleep=sleep, deadline_s=deadline_s,
                          emit=emit)
    client.skew_s = rest.skew_s
    return client, token
