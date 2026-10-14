"""Live broker — real orders through ccxt's unified API.

STATUS: written against ccxt's unified API (create_order with reduceOnly /
stopLossPrice, fetch_positions, fetch_balance, fetch_open_orders,
fetch_my_trades, fetch_funding_history) and tested against an in-process fake
that answers the way ccxt 4.5's hyperliquid class does (tests/test_live.py).
It has NOT been run against a funded account. Before trusting it:

  1. run `python -m desk paper` for the promotion period,
  2. run `python -m desk live --coin BTC --max-notional 50` (a $50 cap) and watch every fill,
  3. only then raise the cap.

Design rules:
  * each coin trades its own perp (SYMBOL_FORMATS), on that market's size step and
    minimum order; no market for the coin, a missing credential, an account that reads $0
    or a cap that can't buy one ticket = refuse to start. On Hyperliquid a sub-account is
    traded through vaultAddress (HL_SUBACCOUNT_ADDRESS), and ccxt's builder fee is off;
  * the protective stop lives ON THE EXCHANGE as a reduce-only stop-market order,
    so if this process dies the position is still protected. It counts as placed only
    once it is read back from the open orders, at its level and covering the whole
    position; it is retried with backoff, and if it still won't rest the position is
    closed and an alert goes out. Every sync checks one such stop is there, puts it back
    if not, and cancels any extra ones;
  * the exchange is the source of truth for the position; the desk's local copy
    (size, entry, best close, stop) is reconciled on every sync. A position is booked as
    one trade from every one of its closing fills since the entry (it may close in
    pieces), with their fees and the funding the exchange charged that coin, never at a
    later mid; fills that can't be read are re-read, and an exit booked without them is
    re-booked once they can be;
  * a desk close that doesn't go through is kept (close_wanted) and sent again at every
    check, across restarts, until the position is flat;
  * each coin's state is its own file (state/<COIN>/live_position.json);
  * any exception while placing an order leaves the desk in entry_kill;
  * money moved in or out of the account (a deposit, a withdrawal, a transfer between accounts, spot <-> perp) is
    read from the exchange's ledger with every balance read and handed to the runner as a flow, never as profit or
    loss (desk/flows.py): on Hyperliquid, userNonFundingLedgerUpdates through ccxt's fetch_ledger, the only one of
    ccxt's calls that sees every kind (fetch_deposits and fetch_withdrawals return bridge deposits and withdrawals
    only). Each flow is priced at the account's value when it happened, even when it's read later. A balance is used
    only once it's confirmed: read with a readable ledger, its move from the last confirmed one (saved, so across a
    restart too) explained by P&L, fees, funding and the flows. Until then the last confirmed one stands and the
    runner holds new candles; a move nothing explains is alerted (and entry_kill set) instead of a guess.

Coinbase (BTC on BIP-20DEC30-CDE, through desk/brokers/coinbase.py's CoinbaseExchange; every change below is gated
on that adapter, so Hyperliquid runs exactly as before):
  * Coinbase has no stop-market order and no reduce-only, so the protective stop is a stop-limit, and two full-size
    stops resting at once could both fire and open the opposite position. A stop is therefore moved CANCEL-FIRST: the
    old one is cancelled and confirmed gone, then the new one placed (about a second with no stop resting, longer if
    Coinbase is slow), never two at once; the adapter refuses any stop that would make the closing contracts resting
    more than the position. A close cancels the stop first, then sends close_position for the size Coinbase holds;
    after any failure the position is read again and a stop goes back only on what's still open. Flat with an order
    resting: it's cancelled now.
  * Every open order on the product is the desk's (LIVE starts flat with no orders). A position the desk doesn't know
    is adopted only if the order that opened it carries one of the desk's entry ids (rebuilt from the bar it was
    decided at); one opened by a stop, a close or an unknown order is closed at market and entry_kill is asked for.
  * A stop-limit can't fill once the price is past its limit (a gap): seen at a sync, the position is closed at market.
  * An entry's answer lost (a timeout) is resolved in the same check: if the position is there, it's adopted and gets
    its stop at once. Every order carries a client_order_id, so sending it again can't make a second one.
  * Entries are sized again just before they go out, at Coinbase's best bid or ask then (the desk decided at the
    close), on the part of the balance that can be margin; nothing goes while Coinbase is closed (market orders can't
    be queued), and no stop moves then either (a queued cancel and a queued new stop could both be live at the
    reopen). A missing stop is still placed: stop-limits can be queued.
  * Deposits and withdrawals come from the v2 USD wallet, each with a stable id: a pending one only explains a move
    that matches it to the cent; a completed one is counted once.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Order, Position, Side, Trade
from ..thresholds import Params

# ccxt's unified symbol for each coin's USD-margined perpetual, per venue. Hyperliquid's were checked against its
# public market list (ccxt.hyperliquid().load_markets(), no keys): BTC, ETH and SOL are "<COIN>/USDC:USDC", with
# size steps of 0.00001, 0.0001 and 0.01 and a $10 minimum order. The other venues follow ccxt's naming for linear
# perps and weren't reachable from here to check; LiveBroker refuses to start if the venue doesn't list the market.
SYMBOL_FORMATS = {
    "hyperliquid": "{coin}/USDC:USDC",
    "binanceusdm": "{coin}/USDT:USDT",
    "bybit": "{coin}/USDT:USDT",
    "okx": "{coin}/USDT:USDT",
    "krakenfutures": "{coin}/USD:USD",
}

TICK_SIZE = 4   # ccxt's precisionMode where precision["amount"] is the size step itself (not a count of decimals)


class LiveSetupError(RuntimeError):
    """Live trading refuses to start (no such market, a cap below the venue's minimum order, ...)."""


COINBASE_LOCKED = ("Coinbase has its own client and LIVE on it is locked: use `python -m desk shadow` (read only), or "
                   "once you arm it, `python -m desk live --coin BTC --once` (README: Going live on Coinbase)")


def market_symbol(venue: str, coin: str, exchange=None) -> str:
    """The perp this coin's desk trades on `venue`: the coin's own market, never another coin's. A venue with its own
    rules (BTC on Coinbase) is traded only through its own adapter (desk/brokers/coinbase.py), never ccxt."""
    if venue not in SYMBOL_FORMATS:
        from ..thresholds import VENUES

        if venue in VENUES:
            v = VENUES[venue]
            if getattr(exchange, "is_coinbase_adapter", False) and coin.upper() == "BTC":
                return v.product
            raise LiveSetupError(f"{coin} is set up for {v.display} ({v.product}, desk/thresholds.py). "
                                 f"{COINBASE_LOCKED}")
        raise LiveSetupError(f"no perp symbol known for venue {venue!r} (known: {', '.join(SYMBOL_FORMATS)})")
    return SYMBOL_FORMATS[venue].format(coin=coin.upper())


def exchange_config(venue: str, *, wallet: str | None = None, key: str | None = None, sub_account: str | None = None,
                    api_key: str | None = None, secret: str | None = None, password: str | None = None) -> dict:
    """ccxt's constructor config for `venue`. A missing credential refuses to start (it never comes back by
    retrying).

    Hyperliquid: `wallet` is the address of the account the API wallet (`key`) was approved for, never the API
    wallet's own address. A sub-account has no key of its own: every order and cancel for it must carry
    vaultAddress = the sub-account, and every read must ask for it. ccxt does both when the sub-account is given as
    options["subAccountAddress"]; without it, orders land on the main account while the desk reads the sub-account."""
    def need(value: str | None, name: str) -> str:
        if not (value or "").strip():
            raise LiveSetupError(f"{name} is not set (see .env.example): live can't connect to {venue}")
        return value.strip()

    if venue == "hyperliquid":
        cfg = {"walletAddress": need(wallet, "HL_WALLET_ADDRESS"), "privateKey": need(key, "HL_PRIVATE_KEY"),
               "options": {}}
        if (sub_account or "").strip():
            cfg["options"]["subAccountAddress"] = sub_account.strip()
    else:
        cfg = {"apiKey": need(api_key, "EXCHANGE_API_KEY"), "secret": need(secret, "EXCHANGE_API_SECRET")}
        if password:
            cfg["password"] = password
    cfg["enableRateLimit"] = True
    return cfg


def make_exchange(venue: str):
    from ..thresholds import VENUES

    if venue in VENUES:
        raise LiveSetupError(COINBASE_LOCKED)
    try:
        import ccxt  # imported lazily so backtest/paper never need it
    except ImportError:
        raise LiveSetupError("ccxt is not installed: pip install -r requirements.txt") from None

    cls = getattr(ccxt, venue, None)
    if cls is None:
        raise LiveSetupError(f"ccxt has no exchange called {venue!r}")
    env = os.environ.get
    ex = cls(exchange_config(venue, wallet=env("HL_WALLET_ADDRESS"), key=env("HL_PRIVATE_KEY"),
                             sub_account=env("HL_SUBACCOUNT_ADDRESS"), api_key=env("EXCHANGE_API_KEY"),
                             secret=env("EXCHANGE_API_SECRET"), password=env("EXCHANGE_API_PASSWORD")))
    if venue == "hyperliquid":
        _no_builder_fee(ex)
    ex.load_markets()
    return ex


def _no_builder_fee(ex) -> None:
    """ccxt's hyperliquid class (checked on 4.5.85) signs an approveBuilderFee and a setReferrer before its first
    order, cancel and market load. Once the approval goes through (a main key) it adds a 0.01% builder fee to every
    order, on top of the taker fee the desk's costs assume; when it is refused (an API wallet) it retries the refused
    approval before every order and cancel, an extra round trip in front of each stop placement and trail move.
    Neither is wanted: skip the approval (approvedBuilderFee stays unset, so no builder is attached to any order)
    and the referrer. This leans on ccxt internals; if they're gone, refuse rather than trade on unchecked fees."""
    if not callable(getattr(ex, "handle_builder_fee_approval", None)) or not isinstance(getattr(ex, "options", None),
                                                                                         dict):
        raise LiveSetupError("this ccxt version's hyperliquid class has no handle_builder_fee_approval: its builder "
                             "fee can't be switched off as it was on 4.5.85 (requirements.txt). Check it first")
    ex.handle_builder_fee_approval = lambda: True
    ex.options["refSet"] = True


# Venues whose reported account value already includes unrealised PnL (Coinbase: the adapter's balance adds it).
EQUITY_INCLUDES_UPNL = {"hyperliquid", "coinbase"}

# The protective stop: tries to get it resting on the exchange before giving up and closing the position,
# and the wait before each retry (doubling: 1 s, 2 s, 4 s).
STOP_ATTEMPTS = 4
STOP_BACKOFF_S = 1.0
FILL_LOOKBACK_MS = 60_000   # an order's own fills are looked up from a minute before it was sent (clock skew)
FUNDING_MAX_PAGES = 100     # 500 rows a page: years of hourly funding for a few coins
PROVISIONAL_TRIES = 240     # an exit booked without its fills is re-read at every check for about an hour (15 s polls)
LEDGER_PAGES = 20           # deposit/withdrawal history pages read per check (more only after a long time away)
RECONCILE_MIN_USD = 1.0     # a move in the account's value that nothing explains, beyond the larger of $1...
RECONCILE_FRACTION = 0.02   # ...and 2% of the account, is alerted (price marks, fees and funding stay well inside)
LEDGER_ALERT_EVERY = 40     # a ledger that can't be read alerts at the first failure and every 40th after (~10 min)
UNEXPLAINED_WAIT_MS = 120_000   # a move nothing explains waits this long for its ledger entry before it's alerted
FLOW_FRESH_MS = 120_000     # a flow read within this of when it happened is priced at the balance it was read with
FUNDING_GAP_MS = 3600_000   # explaining a longer stretch with a position held (a restart): the exchange's funding too
LEDGER_SKEW_MS = 10 * 60_000        # on a first start, the ledger is read from this long before now (clock skew)
FIRST_LOOKBACK_MS = 86400_000       # ...after marking everything on it from the last day as the start
# Coinbase (no reduce-only): the worst case, in seconds, of an order sequence that must not be cut off half way (a
# stop moved cancel-first: cancel, confirm, place and read back, four tries with backoff; a close: cancel the stop,
# close_position, read back, put a stop back on what's left). None starts unless it fits in the run's time left.
CB_MOVE_WORST_S = 45.0
CB_CLOSE_WORST_S = 45.0
CB_ADOPT_LOOKBACK_MS = 7 * 86400_000    # the fills read to find the order that opened a position the desk doesn't know
CB_ENTRY_BARS = 4                       # an entry id is rebuilt from the bars that closed this many bars before it


def _null():
    import contextlib

    return contextlib.nullcontext()


@dataclass
class _Read:
    """A ledger read not committed yet: the new flows, and the cursor and seen ids it would leave."""
    flows: list
    cursor: int
    seen: list
    candidates: list = field(default_factory=list)  # pending entries (Coinbase): used only if they match a move exactly


class UnknownLedgerEntry(ValueError):
    """An entry on the exchange's ledger that hl_flow doesn't know: alerted, never guessed at."""


# Hyperliquid's userNonFundingLedgerUpdates entries that don't move the perp account's value as money in or out:
# a liquidation is trading (booked with its trade); the rest happen on the spot side.
HL_NOT_FLOWS = {"liquidation", "spotTransfer", "spotGenesis", "cStakingTransfer"}


def hl_flow(info: dict, me: str) -> tuple[float, str] | None:
    """(dollars into (+) or out of (-) the perp account `me`, what it was) for one Hyperliquid
    userNonFundingLedgerUpdates entry ({"time", "hash", "delta": {"type", ...}}, ccxt's fetch_ledger info), None when
    it isn't money in or out of that account. Raises UnknownLedgerEntry for anything else: never a guess.

    ccxt 4.5.85's parse_ledger_entry gives every entry's "usdc" unsigned with no direction (and no amount at all for a
    vault withdrawal), so the direction is read here from the delta: who sent it, who got it, toPerp."""
    d = info.get("delta") or {}
    kind, me = d.get("type"), (me or "").lower()

    def usd(key: str = "usdc") -> float:
        try:
            return abs(float(d[key]))
        except (KeyError, TypeError, ValueError):
            raise UnknownLedgerEntry(f"{kind} entry without a readable {key}: {d}") from None

    sender, receiver = str(d.get("user") or "").lower(), str(d.get("destination") or "").lower()
    if kind == "deposit":
        return usd(), "deposit"
    if kind == "withdraw":
        return -usd(), "withdrawal"              # the withdrawal fee comes out of the amount withdrawn
    if kind in ("internalTransfer", "subAccountTransfer"):
        what = "sub-account transfer" if kind == "subAccountTransfer" else "transfer"
        if receiver == me and sender != me:
            return usd(), f"{what} in"
        if sender == me and receiver != me:
            fee = abs(float(d.get("fee") or 0.0))
            return -(usd() + fee), f"{what} out"
        raise UnknownLedgerEntry(f"{kind} that is neither to nor from {me or 'this account'}: {d}")
    if kind == "accountClassTransfer":
        return (usd(), "spot → perp") if d.get("toPerp") else (-usd(), "perp → spot")
    if kind == "vaultDeposit":
        return -usd(), "into a vault"
    if kind == "vaultWithdraw":
        return usd("netWithdrawnUsd"), "out of a vault"
    if kind in HL_NOT_FLOWS:
        return None
    raise UnknownLedgerEntry(f"unknown ledger entry type {kind!r}: {d}")


def _trigger(o: dict) -> float | None:
    """The trigger price of a ccxt order structure (None for a plain limit/market order)."""
    for k in ("triggerPrice", "stopLossPrice", "stopPrice"):
        if o.get(k) is not None:
            return float(o[k])
    return None


class LiveBroker:
    is_live = True

    def __init__(self, p: Params, venue: str | None = None, state_path: str | Path | None = None,
                 max_notional_usd: float | None = None, exchange=None, log=print, on_alert=None, sleep=time.sleep,
                 clock=None, price_at=None, allow_empty: bool = False, unexplained_wait_ms: int | None = None,
                 provisional_tries: int | None = None, rules=None, market_closed=None,
                 unexplained_as_flow: bool = False):
        self.p = p
        self.log = log
        self.on_alert = on_alert              # the runner puts alerts on the board and the phone
        self.sleep = sleep
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.venue = venue or p.market.venue
        self.coin = p.market.symbol.upper()
        self.symbol = market_symbol(self.venue, self.coin, exchange)
        self.ex = exchange or make_exchange(self.venue)
        # Coinbase (desk/brokers/coinbase.py): no reduce-only, the product is the desk's alone (module docstring)
        self._cb = bool(getattr(self.ex, "is_coinbase_adapter", False))
        if self._cb and getattr(self.ex, "on_alert", None) is None:
            self.ex.on_alert = self.alert
        self.unexplained_wait_ms = UNEXPLAINED_WAIT_MS if unexplained_wait_ms is None else unexplained_wait_ms
        self.provisional_tries = PROVISIONAL_TRIES if provisional_tries is None else provisional_tries
        self.rules = rules                    # venue rules (desk/venue.py): an entry is sized again just before it goes
        self.market_closed = market_closed    # () -> True while the venue is known to be closed (the runner's sessions)
        self.order_seq = 0                    # Coinbase: the next client_order_id's sequence number (saved before a POST)
        self.entry_kill_why: str | None = None  # something the runner must stop entries for (entry_kill)
        self.on_urgent = None                 # (kind, text): the runner's urgent push (a position with no stop, ...)
        # SHADOW (desk/brokers/coinbase_shadow.py): the orders are a simulated book, and the real account is never
        # traded, so a move in its value nothing explains is money in or out, never the desk's profit or loss
        self._simulated = bool(getattr(self.ex, "is_shadow", False))
        self.unexplained_as_flow = unexplained_as_flow
        self._said: set[str] = set()          # this process's once-only alerts
        self.alerts = []
        self._load_market()
        if max_notional_usd is not None and self.min_cost and max_notional_usd < self.min_cost:
            raise LiveSetupError(f"--max-notional ${max_notional_usd:,.2f} is below {self.venue}'s minimum order of "
                                 f"${self.min_cost:,.2f} on {self.symbol}: every entry would be refused")
        # one coin's position file (runner.live_paths): two coins never share one
        self.state_path = Path(state_path or f"state/{self.coin}/live_position.json")
        self.max_notional_usd = max_notional_usd
        self.position: Position | None = None
        self.pending: list[Order] = []
        self.trades: list[Trade] = []
        self.fees_total = 0.0
        self.funding_total = 0.0
        self.orders_submitted = 0
        self.orders_filled = 0
        self.last_fill: dict | None = None
        self.skipped: dict | None = None      # the last entry not sent: under the venue's minimum after rounding
        self.alerts: list[tuple[int, str]] = getattr(self, "alerts", [])     # (any raised loading the market)
        self.stop_order_id: str | None = None
        self.stop_px: float | None = None     # trigger of that resting stop, as the venue rounded it
        self.entry_order_id: str | None = None  # the desk's own entry order (an opening fill from any other is an add)
        self.close_wanted: str | None = None  # a desk close (its reason) not yet done on the exchange: sent again at
        self.close_failures = 0               # every check (retry_close) until it is, and kept across a restart
        self.provisional: list[dict] = []     # exits booked without their fills: re-booked once the fills read
        self.bar_ts: int | None = None        # the last bar whose decision this position has been through (the runner)
        self.replay_until: int | None = None  # bar_ts as saved when this process started
        self.last_price: float | None = None
        self.stops_to_clean = False           # a close left reduce-only stops behind that couldn't be cancelled yet
        self.defer_stops = False              # catching up missed bars: trail moves are kept, sent once at the end
        # Money in and out (desk/flows.py): every deposit, withdrawal or transfer read from the exchange's ledger, the
        # time read up to, the entries already seen, and the runner's hook that takes each one out of the P&L.
        self.flows: list[dict] = []
        self.flows_cursor: int | None = None
        self.flows_seen: list[str] = []
        self.on_flow = None
        self.price_at = price_at              # ts -> the coin's price then (the runner's feed): a flow read late
        if self.price_at is None and self._cb:
            self.price_at = self.ex.price_at  # Coinbase's own candles
        self.flow_gap: str | None = None      # an unexplained move in the account's value, for the runner (entry_kill)
        self.balance_confirmed = True         # False: the last balance read isn't explained yet; the runner waits
        self.unconfirmed_why: str | None = None
        self._recon: dict | None = None       # the last confirmed balance, price and position (saved), to explain the next
        self._unexplained_since: int | None = None
        self._alerted: set[str] = set()
        self._ledger_failures = 0
        self.account = str((getattr(self.ex, "options", None) or {}).get("subAccountAddress")
                           or getattr(self.ex, "walletAddress", "") or "").lower()
        self.flows_readable = bool((getattr(self.ex, "has", None) or {}).get("fetchLedger")) and \
            callable(getattr(self.ex, "fetch_ledger", None))
        self._load_local()
        if self.flows_cursor is None:         # first start: what funded the account so far is the start, not a flow
            self._first_ledger_read()
        bal = self._balance()
        # a restart starts from the balance it last confirmed (the first check confirms the new one, or not yet); a
        # first start from this one, read just after the ledger
        self.cash = bal if self._recon is None else self._recon["av"]
        if self._recon is None:
            self._recon = self._snapshot(None)
        if not self.flows_readable:
            self.alert(f"{self.venue}'s deposits and withdrawals can't be read here (no fetch_ledger): one would count as "
                       f"profit or loss, brakes included. A move nothing explains is still alerted")
        if bal <= 0 and self.position is None and not allow_empty:
            raise LiveSetupError(f"the {self.venue} account reads ${bal:,.2f}: nothing to trade with. On "
                                 f"Hyperliquid, HL_WALLET_ADDRESS (and HL_SUBACCOUNT_ADDRESS for a sub-account) must "
                                 f"name the account that holds the money, not the API wallet's own address")
        self.sync()

    # ---- the market ------------------------------------------------------
    def _load_market(self) -> None:
        """Refuse to start unless the venue lists this coin's perp; keep its size step, contract size and minimums."""
        markets = getattr(self.ex, "markets", None) or self.ex.load_markets()
        m = markets.get(self.symbol)
        if m is None or not (m.get("swap") or m.get("contract")) or (m.get("base") or self.coin).upper() != self.coin:
            raise LiveSetupError(f"{self.venue} lists no {self.coin} perpetual ({self.symbol}); refusing to trade "
                                 f"{self.coin} on another market")
        if m.get("active") is False:
            raise LiveSetupError(f"{self.symbol} is not active on {self.venue}")
        self.market = m
        self.contract_size = float(m.get("contractSize") or 1.0)    # coins per contract (1 on Hyperliquid)
        step = (m.get("precision") or {}).get("amount")
        if step is not None and getattr(self.ex, "precisionMode", TICK_SIZE) != TICK_SIZE:
            step = 10.0 ** -float(step)                                 # precision given as a number of decimals
        self.amount_step = float(step) if step else None
        limits = m.get("limits") or {}
        self.min_cost = float((limits.get("cost") or {}).get("min") or 0.0)
        self.min_amount = float((limits.get("amount") or {}).get("min") or 0.0)

    def _amount(self, qty: float) -> float:
        """Coins -> the order amount the venue takes (contracts), cut DOWN to the size step, never rounded up past
        what the desk (or the --max-notional cap) allowed. 0 when that leaves nothing."""
        n = qty / self.contract_size
        if self.amount_step:
            n = math.floor(n / self.amount_step + 1e-9) * self.amount_step
        if n <= 0:
            return 0.0
        try:
            return float(self.ex.amount_to_precision(self.symbol, n))
        except Exception:   # ccxt raises when a positive amount rounds to zero
            return 0.0

    def smallest_cap(self, price: float) -> float:
        """The smallest --max-notional with which an entry can go out at `price`. _open caps the size at
        cap / (price x (1 + slippage)), cuts it down to the size step, and doesn't send it if it is under the venue's
        minimum after slippage: so the cap must buy the first whole step above that minimum, cushion included."""
        s = self.p.costs.slippage_bps / 1e4
        unit = (self.amount_step or 0.0) * self.contract_size
        coins = max(self.min_cost / ((1 - s) * price), self.min_amount * self.contract_size)
        if unit:
            n = max(1, math.ceil(coins / unit - 1e-9))
            while n * unit * price * (1 - s) < self.min_cost:
                n += 1
            coins = n * unit
        return math.ceil(coins * price * (1 + s) * 100) / 100      # to the cent, up

    def check_cap(self, price: float | None) -> None:
        """At startup, with the market's price: refuse a --max-notional that can't buy a single ticket (at or just
        above the venue's minimum, every entry would be skipped)."""
        if self.max_notional_usd is None or not price:
            return
        need = self.smallest_cap(price)
        if self.max_notional_usd < need:
            raise LiveSetupError(f"--max-notional ${self.max_notional_usd:,.2f} can't buy the smallest ticket "
                                 f"{self.symbol} takes at {price:,.2f}: ${need:,.2f} ({self.venue}'s "
                                 f"${self.min_cost:,.2f} minimum after the size step {self.amount_step or 0:g} and the "
                                 f"{self.p.costs.slippage_bps:g} bps slippage cushion). Every entry would be skipped; "
                                 f"use a cap a little above that, as the minimum moves with the price")

    def _px(self, price: float) -> float:
        return float(self.ex.price_to_precision(self.symbol, price))

    def _closing_side(self, side: Side) -> str:
        return "sell" if side is Side.LONG else "buy"

    def alert(self, text: str) -> None:
        """Something a human must know about: logged, kept, and handed to the runner (board + phone)."""
        self.alerts = (self.alerts + [(self.clock(), text)])[-50:]
        self.log(f"ALERT {self.coin}: {text}")
        if self.on_alert is not None:
            try:
                self.on_alert(text)
            except Exception as exc:  # a failed notification must never stop the broker
                self.log(f"alert hook failed: {type(exc).__name__}: {exc}")

    def urgent(self, kind: str, text: str) -> None:
        """Something that may leave real money unprotected (a position with no stop, a stop with no position): handed
        to the runner's urgent push (once per kind per process; first, so the runner sends it instead of a plain
        alert), then alerted."""
        if kind not in self._said:
            self._said.add(kind)
            if self.on_urgent is not None:
                try:
                    self.on_urgent(kind, self._sim(text))
                except Exception as exc:  # a failed notification must never stop the broker
                    self.log(f"urgent hook failed: {type(exc).__name__}: {exc}")
        self.order_alert(text)

    def _sim(self, text: str) -> str:
        return f"Simulated book (nothing rests on Coinbase): {text}" if self._simulated else text

    def order_alert(self, text: str) -> None:
        """An alert about the orders and the position: on SHADOW's simulated book it says so."""
        self.alert(self._sim(text))

    # ---- exchange reads ------------------------------------------------
    def _balance(self) -> float:
        bal = self.ex.fetch_balance()
        quote = self.market.get("settle") or self.symbol.split(":")[-1]
        total = bal.get("total", {}).get(quote)
        if total is None:
            total = bal.get(quote, {}).get("total", 0.0)
        return float(total or 0.0)

    def _exchange_position(self) -> tuple[Side | None, float, float]:
        for pos in self.ex.fetch_positions([self.symbol]):
            if pos.get("symbol") not in (None, self.symbol):
                continue
            qty = float(pos.get("contracts") or 0.0) * float(pos.get("contractSize") or self.contract_size)
            if qty > 0:
                return Side(pos["side"]), qty, float(pos.get("entryPrice") or 0.0)
        return None, 0.0, 0.0

    def _fills_since(self, since: int) -> list[dict] | None:
        """This market's fills (ccxt trade structures) since `since`, oldest first; None if the venue can't say."""
        if not (getattr(self.ex, "has", None) or {}).get("fetchMyTrades", True):
            return None
        try:
            fills = self.ex.fetch_my_trades(self.symbol, since)
        except Exception as exc:
            self.log(f"{self.coin}: could not read fills: {type(exc).__name__}: {exc}")
            return None
        fills = [t for t in fills if t.get("symbol") in (None, self.symbol) and (t.get("timestamp") or since) >= since]
        return sorted(fills, key=lambda t: t.get("timestamp") or 0)

    def _sum_fills(self, fills: list[dict]) -> tuple[float, float, float | None, int | None]:
        """(coins, average price, fee or None if any fill's fee is unknown or not in the settle currency, last ts)."""
        amt = sum(float(t["amount"]) for t in fills)
        px = sum(float(t["price"]) * float(t["amount"]) for t in fills) / amt
        settle = self.market.get("settle")
        fees = [t.get("fee") or {} for t in fills]
        fee = None
        if all(f.get("cost") is not None and f.get("currency") in (None, settle) for f in fees):
            fee = sum(float(f["cost"]) for f in fees)
        return amt * self.contract_size, px, fee, max((t.get("timestamp") or 0) for t in fills) or None

    def _order_fill(self, res: dict, ts: int, ref: float, qty: float) -> tuple[float, float, str]:
        """(average price, fee, where they came from) of a market order the desk just sent: its fills on the
        exchange, else what the order's answer says, else the reference price and the configured taker fee."""
        fills = self._fills_since(ts - FILL_LOOKBACK_MS)
        mine = [t for t in fills or [] if str(t.get("order")) == str(res["id"])]
        if mine:
            _, px, fee, _ = self._sum_fills(mine)
        else:
            px, fee = float(res.get("average") or res.get("price") or ref), None
        if fee is not None:
            return px, fee, "fills"
        if (res.get("fee") or {}).get("cost") is not None:
            return px, float(res["fee"]["cost"]), "order"
        self.log(f"{self.coin}: order {res['id']}'s fee isn't readable from the exchange; using the "
                 f"{self.p.costs.taker_fee_bps} bps taker estimate")
        return px, qty * px * self.p.costs.taker_fee_bps / 1e4, "estimate"

    def _is_this_market(self, row: dict) -> bool:
        """Whether a funding row is this market's. ccxt's hyperliquid (4.5) labels every userFunding row with the
        symbol that was asked for, whatever coin it is for, so its own coin (info.delta.coin) decides when present."""
        delta = (row.get("info") or {}).get("delta")
        if isinstance(delta, dict) and delta.get("coin") is not None:
            name = (self.market.get("info") or {}).get("name") or self.market.get("baseName") or self.coin
            return str(delta["coin"]) == str(name)
        return row.get("symbol") in (None, self.symbol)

    def _exchange_funding(self, since: int, until: int | None = None) -> float | None:
        """Funding the exchange charged this market's position from `since` (to `until`), in the desk's sign
        (+ = paid); None if the venue can't say. Read page by page: Hyperliquid answers at most 500 rows per call,
        for every coin the account holds, and charges hourly."""
        if not (getattr(self.ex, "has", None) or {}).get("fetchFundingHistory", True):
            return None
        rows, seen, start = [], set(), since
        for _ in range(FUNDING_MAX_PAGES):
            try:
                page = self.ex.fetch_funding_history(self.symbol, start)
            except Exception as exc:
                self.log(f"{self.coin}: could not read funding: {type(exc).__name__}: {exc}")
                return None
            new = []
            for r in page:
                key = json.dumps(r.get("info"), sort_keys=True, default=str) if r.get("info") else \
                    (r.get("timestamp"), r.get("symbol"), r.get("amount"))
                if key not in seen:
                    seen.add(key)
                    new.append(r)
            if not new:
                break
            rows += new
            # the next page starts at the last time seen (not after it: other coins' rows share each hour's time)
            start = max(start, max(int(r.get("timestamp") or start) for r in new))
            if until is not None and start > until:
                break
        else:
            self.log(f"{self.coin}: funding history still going after {FUNDING_MAX_PAGES} pages; the trade's funding "
                     f"may be short")
        return -sum(float(r.get("amount") or 0.0) for r in rows if self._is_this_market(r)
                    and (r.get("timestamp") or since) >= since and (until is None or (r.get("timestamp") or 0) <= until))

    def _resting_stops(self, side: str | None = None) -> list[dict]:
        """Reduce-only trigger orders resting on this market (on `side`, if given). On Coinbase, which has no
        reduce-only, every trigger order on the product (all of them are the desk's), but not one whose cancel is
        queued for the reopen (it doesn't protect: it's going)."""
        out = []
        for o in self.ex.fetch_open_orders(self.symbol):
            if o.get("symbol") not in (None, self.symbol) or _trigger(o) is None:
                continue
            if self._cb:
                if o.get("status") == "canceling":
                    continue
            elif o.get("reduceOnly") is False:
                continue
            if side is None or o.get("side") in (None, side):
                out.append(o)
        return out

    def equity(self, price: float) -> float:
        if self.venue in EQUITY_INCLUDES_UPNL or self.position is None:
            return self.cash
        return self.cash + self.position.unrealized(price)

    def sync(self, price: float | None = None) -> None:
        """Reconcile with the exchange: the exchange's position is the truth. If the stop filled while we were away,
        record the trade. While a position is open, a stop must be resting on the exchange: if it's gone, put it back."""
        if self._cb:
            return self._cb_sync(price)
        if price is not None:
            self.last_price = price
        side, qty, entry = self._exchange_position()
        if side is None and self.position is not None:
            # the exchange closed us — almost certainly the resting stop
            self._book_exchange_exit()
        elif side is not None and (self.position is None or side is not self.position.side):
            # a position we don't know about: adopt it. A stop already resting for it is kept; otherwise the
            # runner gives it one straight away.
            if self.position is not None:
                self.alert(f"the exchange holds a {side.value} {qty:g} {self.coin}, not the desk's "
                           f"{self.position.side.value}: adopting the exchange's position")
            self.position = Position(side, qty, entry, self.clock(), stop=0.0, best_close=entry)
            self.stop_order_id = self.stop_px = self.entry_order_id = None
            stops = self._resting_stops(self._closing_side(side))
            if stops:
                self.position.stop = self.stop_px = _trigger(stops[0])
                self.stop_order_id = str(stops[0]["id"])
        elif side is not None:
            self._follow_size(qty, entry)
        if self.position is not None and self.position.stop:
            self._check_stop_resting()
        elif self.position is None and self.stops_to_clean:
            self._cancel_stops(keep=None)
        if self.provisional:
            self._resolve_provisional()
        self.cash = self._refresh_cash()
        self._save_local()

    # ---- money in and out ----------------------------------------------------
    #
    # Every balance the desk acts on is a confirmed one: read together with the exchange's ledger, every move in it
    # explained by the position's P&L, the trades, funding and the deposits, withdrawals and transfers on that ledger.
    # Until then the last confirmed balance stands (balance_confirmed is False and the runner holds new candles), so a
    # deposit is never mistaken for profit, or a withdrawal for a loss, by the brakes. Each flow is priced at the
    # account's value at its own time, not when it was read.

    def _first_ledger_read(self) -> None:
        """First start (no state file yet): everything on the ledger so far funded the account, so it's the start, not
        a flow. Read it before the starting balance and mark it seen; read again from a little before now, so a clock
        behind the exchange's can't skip an entry made after the start. Raises if it can't be read: starting without
        knowing which money is the start would count a deposit twice (the runner retries the startup)."""
        if not self.flows_readable:
            self.flows_cursor = self.clock()
            return
        got = self._read_flows(_Read([], self.clock() - FIRST_LOOKBACK_MS, []), alert=False)
        if got is None:
            raise RuntimeError("the exchange's deposit and withdrawal history can't be read yet")
        self.flows_seen = got.seen
        self.flows_cursor = max(got.cursor, self.clock() - LEDGER_SKEW_MS)

    def _read_flows(self, base: "_Read | None" = None, alert: bool = True) -> "_Read | None":
        """The deposits, withdrawals and transfers on the exchange's ledger since `base` (default: what's committed),
        oldest first, as a _Read that commits nothing: the caller commits it once the balance it goes with is
        confirmed. None if the ledger couldn't be read, all of it: a read that fails part way through is dropped
        whole, so the next one starts again from the same point."""
        base = base or _Read([], self.flows_cursor, list(self.flows_seen))
        if not self.flows_readable:
            return base
        if getattr(self.ex, "ledger_ids_stable", False):
            return self._read_flows_stable(base, alert)
        seen, seen_list, out, since = set(base.seen), list(base.seen), list(base.flows), base.cursor
        for _ in range(LEDGER_PAGES):
            try:
                page = self.ex.fetch_ledger(None, since)
            except Exception as exc:
                self._ledger_failures += 1
                text = (f"could not read deposits and withdrawals ({type(exc).__name__}: {exc}); the brakes keep the "
                        f"last confirmed balance and new candles wait until it reads again")
                if alert and (self._ledger_failures == 1 or self._ledger_failures % LEDGER_ALERT_EVERY == 0):
                    self.alert(text)
                else:
                    self.log(f"{self.coin}: {text}")
                return None
            fresh = []
            for e in sorted(page, key=lambda e: e.get("timestamp") or 0):
                info = e.get("info") or {}
                fid = "exchange-" + hashlib.sha1(json.dumps(info, sort_keys=True, default=str).encode()).hexdigest()[:16]
                if fid not in seen:
                    seen.add(fid)
                    seen_list.append(fid)
                    fresh.append((fid, int(e.get("timestamp") or info.get("time") or since), info))
            if not fresh:
                break
            for fid, ts, info in fresh:
                try:
                    got = hl_flow(info, self.account)
                except UnknownLedgerEntry as exc:
                    if alert and fid not in self._alerted:
                        self._alerted.add(fid)
                        self.alert(f"{exc}. Not recorded as a deposit or withdrawal: check it on the exchange; if "
                                   f"it moved the account, it's alerted as a move nothing explains")
                    continue
                if got is not None:
                    out.append({"id": fid, "ts": ts, "amount": round(got[0], 6), "what": got[1],
                                "type": (info.get("delta") or {}).get("type")})
            since = max(since, max(ts for _, ts, _ in fresh))
        self._ledger_failures = 0
        return _Read(out, since, seen_list[-1000:])

    def _px_now(self) -> float | None:
        if self.last_price is not None:
            return self.last_price
        if self.price_at is not None:
            try:
                return self.price_at(self.clock())
            except Exception as exc:
                self.log(f"{self.coin}: could not read the price: {type(exc).__name__}: {exc}")
        return None

    def _trades_since(self, snap: dict) -> list:
        k = snap.get("last_entry")
        if k is None:
            return list(self.trades)
        idx = next((i for i in range(len(self.trades) - 1, -1, -1) if self.trades[i].entry_ts == k), None)
        return self.trades[idx + 1:] if idx is not None else list(self.trades)

    def _unexplained(self, bal: float, flows: list[dict], px: float | None) -> float | None:
        """How much of the move in the account's value since the last confirmed balance (the saved one, across a
        restart) the price on the position held, the trades booked, funding and `flows` don't explain. None when
        there is nothing to compare with (the first check), or it needs a price that can't be read."""
        prev = self._recon
        if prev is None:
            return None
        pos, held, new_trades = self.position, prev.get("pos"), self._trades_since(prev)
        expected = 0.0
        if held is not None:
            sign, qty, _, ets = held
            if pos is not None and pos.entry_ts == ets and pos.side.sign == sign:
                if px is None or prev.get("px") is None:
                    return None
                expected += sign * qty * (px - prev["px"])                   # the move on what was held
            else:
                t = next((t for t in new_trades if t.entry_ts == ets), None)
                if t is not None:
                    if prev.get("px") is None:
                        return None
                    expected += sign * t.qty * (t.exit_price - prev["px"])
            if self.clock() - prev["ts"] > FUNDING_GAP_MS:      # a long gap (a restart): the funding charged meanwhile
                expected -= self._exchange_funding(prev["ts"]) or 0.0
        expected += sum(t.net_pnl for t in new_trades if held is None or t.entry_ts != held[3])
        if pos is not None and (held is None or pos.entry_ts != held[3]):
            if px is None:
                return None
            expected += pos.unrealized(px) - pos.fees_paid
        return (bal - prev["av"]) - sum(f["amount"] for f in flows) - expected

    def _tolerance(self, bal: float) -> float:
        prev = (self._recon or {}).get("av", bal)
        return max(RECONCILE_MIN_USD, RECONCILE_FRACTION * max(abs(prev), abs(bal)))

    def _unconfirmed(self, why: str) -> float:
        if self.balance_confirmed:
            self.log(f"{self.coin}: balance not confirmed ({why}); the brakes keep ${self.cash:,.2f}")
        self.balance_confirmed, self.unconfirmed_why = False, why
        return self.cash

    def _refresh_cash(self) -> float:
        """The account's value, confirmed against the exchange's ledger (see above), or else the last confirmed one.

        Ledger, balance, and the move between this balance and the last confirmed one explained. Not explained: the
        ledger is read once more (a deposit that landed between the two reads), and if that's still not it, the
        balance waits UNEXPLAINED_WAIT_MS for an entry to show up before it's alerted (flow_gap: the runner sets
        entry_kill) and taken as it is. Each new flow is priced at its own time and handed to the runner (on_flow)
        before anything uses the new balance."""
        read = self._read_flows()
        if read is None:
            return self._unconfirmed("the ledger can't be read")
        bal, px = self._balance(), self._px_now()
        left = self._unexplained(bal, read.flows, px)
        if left is not None and abs(left) > self._tolerance(bal):
            more = self._read_flows(read)
            if more is None:
                return self._unconfirmed("the ledger can't be read")
            if len(more.flows) > len(read.flows):
                read, bal = more, self._balance()
                left = self._unexplained(bal, read.flows, px)
            elif self._cb:
                read = more         # the latest pending entries
        if left is not None and abs(left) > self._tolerance(bal) and read.candidates:
            pick = self._match_candidates(left, read.candidates)
            if pick:        # pending entries that explain the move to the cent: they're it
                ids = [c["id"] for c in pick]
                read = _Read(read.flows + pick, read.cursor, (read.seen + ids)[-1000:],
                             [c for c in read.candidates if c["id"] not in ids])
                left -= sum(c["amount"] for c in pick)
        if left is not None and abs(left) > self._tolerance(bal):
            self._unexplained_since = self._unexplained_since or self.clock()
            if self.clock() - self._unexplained_since < self.unexplained_wait_ms:
                return self._unconfirmed(f"{left:+,.2f} of a move to ${bal:,.2f} isn't explained yet; reading the "
                                         f"ledger again")
            prev_av = self._recon["av"]
            if self.unexplained_as_flow:
                # SHADOW never trades the real account: what moved it is money in or out, booked as such (and kept
                # on the board), never profit or loss
                read = _Read(read.flows + [{
                    "id": f"unexplained-{self._unexplained_since}", "ts": int(self._unexplained_since),
                    "amount": round(left, 2), "type": "unexplained",
                    "what": "a move in the real account no deposit or withdrawal the desk can read explains"}],
                    read.cursor, read.seen, read.candidates)
                self.flow_gap = (f"the real account's value moved {bal - prev_av:+,.2f} (to ${bal:,.2f}) and "
                                 f"{left:+,.2f} of it isn't explained by a deposit or withdrawal the desk can read. "
                                 f"SHADOW never trades the real account, so it's booked as money "
                                 f"{'in' if left > 0 else 'out'}, not profit or loss. Check the account's history; "
                                 f"entry_kill is on")
            else:
                self.flow_gap = (f"the account's value moved {bal - prev_av:+,.2f} (to ${bal:,.2f}) and {left:+,.2f} "
                                 f"of it isn't explained by the position's P&L, fees, funding or a recorded deposit or "
                                 f"withdrawal. Not guessed at: the brakes now count it as "
                                 f"{'profit' if left > 0 else 'a loss'}. Check the exchange's history; entry_kill is "
                                 f"on")
            self.alert(self.flow_gap)
        self._unexplained_since = None
        self.flows_cursor, self.flows_seen = read.cursor, read.seen
        self.cash, self.balance_confirmed, self.unconfirmed_why = bal, True, None
        self._take_flows(read.flows, bal, px)
        self._recon = self._snapshot(px)
        return bal

    def _pnl_since(self, ts: int, px: float | None) -> float | None:
        """What the account's trading made from `ts` until now; None if it needs a price that can't be read."""
        then: list[float | None] = []

        def at_ts() -> float | None:
            if not then:
                try:
                    then.append(self.price_at(ts) if self.price_at is not None else None)
                except Exception as exc:
                    self.log(f"{self.coin}: could not read the price at {ts}: {type(exc).__name__}: {exc}")
                    then.append(None)
            return then[0]

        out = 0.0
        for t in self.trades:
            if t.exit_ts <= ts:
                continue
            if t.entry_ts > ts:
                out += t.net_pnl
            elif at_ts() is None:
                return None
            else:   # held across it: the move from then to its exit, less the exit's fee
                out += t.side.sign * t.qty * (t.exit_price - at_ts()) - \
                    t.qty * t.exit_price * self.p.costs.taker_fee_bps / 1e4
        pos = self.position
        if pos is not None:
            if px is None:
                return None
            if pos.entry_ts > ts:
                out += pos.unrealized(px) - pos.fees_paid
            elif at_ts() is None:
                return None
            else:
                out += pos.side.sign * pos.qty * (px - at_ts())
        return out

    def _take_flows(self, flows: list[dict], bal: float, px: float | None) -> None:
        """Price each new flow at the account's value just before it happened, keep it, and hand it to the runner.
        Read soon after it happened, that's the balance now less it and the flows after it; read later (a restart,
        a sleep, a ledger that couldn't be read), what the trading made since is taken out too, at the price at its
        minute. When that can't be worked out it's priced now, alerted, and the runner sets entry_kill."""
        now, after = self.clock(), sum(f["amount"] for f in flows)
        for fl in flows:
            before = bal - after
            after -= fl["amount"]
            fl["read_ts"], fl["priced"] = now, "when read"
            if now - fl["ts"] > FLOW_FRESH_MS:
                made = self._pnl_since(fl["ts"], px)
                if made is None:
                    fl["priced"] = "approximately"
                    self.flow_gap = (f"a {fl['what']} of ${abs(fl['amount']):,.2f} was read "
                                     f"{(now - fl['ts']) / 60_000:,.0f} min after it happened, with a position open, "
                                     f"and its price then can't be read: it's booked at the account's value now, so "
                                     f"the performance figure and the brakes are off by the position's move since. "
                                     f"Check it; entry_kill is on")
                    self.alert(self.flow_gap)
                else:
                    before -= made
                    fl["priced"] = "at its time"
            fl["equity_before"], fl["equity_after"] = before, before + fl["amount"]
            self.flows = (self.flows + [fl])[-500:]
            self.log(f"{self.coin}: {'deposit' if fl['amount'] > 0 else 'withdrawal'} of ${abs(fl['amount']):,.2f} "
                     f"({fl['what']}) read from the exchange, priced {fl['priced']}")
        if flows:
            self._save_local()
        for fl in flows:
            if self.on_flow is not None:
                self.on_flow(fl)

    def _snapshot(self, px: float | None) -> dict:
        pos = self.position
        return {"av": self.cash, "px": px, "ts": self.clock(),
                "last_entry": self.trades[-1].entry_ts if self.trades else None,
                "pos": None if pos is None else [pos.side.sign, pos.qty, pos.entry_price, pos.entry_ts]}

    def _follow_size(self, qty: float, entry: float) -> None:
        """The exchange holds the desk's side at another size: something closed part of it (by hand, or a close
        that only partly filled) or added to it. Follow the exchange's size, so the stop and the next close cover
        exactly what is open. Nothing is booked yet: when the position closes, the trade is booked from every one of
        its closing fills since the entry, the part that went early included."""
        pos = self.position
        if math.isclose(qty, pos.qty, rel_tol=1e-9, abs_tol=1e-12):
            return
        if qty < pos.qty:
            self.alert(f"the exchange holds {qty:g} {self.coin}, less than the desk's {pos.qty:g}: part of the "
                       f"position was closed outside the desk. Following the exchange; the whole trade is booked from "
                       f"its fills when it closes")
        else:
            self.alert(f"the exchange holds {qty:g} {self.coin}, more than the desk's {pos.qty:g}: something added to "
                       f"the position. Following the exchange (average entry {entry:,.2f}) and resizing the stop")
            if entry > 0:
                pos.entry_price = entry
        pos.qty = qty

    def _covers(self, o: dict) -> bool:
        """Whether a resting stop is big enough for the whole open position (a stop sized for less leaves the rest
        unprotected when it fires)."""
        amt = o.get("remaining") if o.get("remaining") is not None else o.get("amount")
        return amt is None or float(amt) >= self._amount(self.position.qty) * (1 - 1e-9)

    def _check_stop_resting(self) -> None:
        """Every sync while a position is open: the desk's stop must be resting on the exchange, sized for the whole
        position."""
        pos = self.position
        stops = self._resting_stops(self._closing_side(pos.side))
        want = self._px(pos.stop)
        at_stop = [o for o in stops if math.isclose(_trigger(o), want, rel_tol=1e-9)]
        good = [o for o in at_stop if self._covers(o)]
        if good:
            ids = [str(o["id"]) for o in good]
            if self.stop_order_id not in ids:
                self.stop_order_id = ids[0]
            self.stop_px = want
            if len(stops) > 1:      # left by tries whose read-back failed, or an old level: one stop, the desk's
                self.log(f"{self.coin}: {len(stops) - 1} more stop(s) resting beside the desk's: cancelling them")
                self._cancel_stops(keep=self.stop_order_id)
            return
        if self._cb:
            # Coinbase: one full-size stop tighter than the desk's saved level (a trail that went through on Coinbase
            # after the state last saved, or one tightened by hand) is kept as the desk's stop: never loosened.
            tight = [o for o in stops if self._covers(o) and (_trigger(o) - want) * pos.side.sign > 0]
            if tight and not at_stop:
                o = max(tight, key=lambda o: _trigger(o) * pos.side.sign)
                pos.stop = self.stop_px = _trigger(o)
                self.stop_order_id = str(o["id"])
                self.log(f"{self.coin}: a stop at {pos.stop:,.2f} rests, tighter than the saved {want:,.2f}: kept as "
                         f"the desk's stop")
                if len(stops) > 1:
                    self._cancel_stops(keep=self.stop_order_id)
                self._save_local()
                return
        if at_stop:     # at the desk's level but too small: the position grew. Put a full-size one on.
            self.log(f"{self.coin}: the resting stop covers {at_stop[0].get('amount')} of the open "
                     f"{self._amount(pos.qty):g}: resizing it")
            self._ensure_stop(pos.stop, self.last_price, self.clock(), keep_old=True)
            return
        if stops:   # a stop rests, just not at the desk's level: a trail move that didn't go through. Try again.
            self.log(f"{self.coin}: resting stop at {_trigger(stops[0]):,.2f}, desk's stop {want:,.2f}: moving it")
            self._ensure_stop(pos.stop, self.last_price, self.clock(), keep_old=True)
            return
        self.alert(f"no stop resting on the exchange for the open {pos.side.value} {pos.qty:g} {self.coin}: "
                   f"placing it at {want:,.2f}")
        self._ensure_stop(pos.stop, self.last_price, self.clock())

    def _exit_fills(self, pos: Position, fills: list[dict]) -> tuple[list[dict], list[dict]]:
        """(every closing fill, every add) among this market's fills since the position's entry. The position may
        have been closed in pieces (by hand, a close that only partly filled, then the stop): its trade is all of
        them. An add is an opening fill that isn't the desk's own entry order."""
        closing = [t for t in fills if t.get("side") == self._closing_side(pos.side)]
        if self.entry_order_id is None and pos.fees_paid:
            return closing, []      # an entry whose order id isn't known: its own fill can't be told from an add
        adds = [t for t in fills if t.get("side") != self._closing_side(pos.side)
                and str(t.get("order")) != str(self.entry_order_id)]
        return closing, adds

    def _book_exchange_exit(self) -> Trade:
        """The exchange closed the position while the desk wasn't looking (its stop, or a liquidation): book it at
        the exchange's own fills (price and fee) since the entry, every closing fill of it. If the venue can't say,
        ask for the stop order itself; failing that, book it at the stop level with the configured fee and alert,
        so a human checks the venue's trade history."""
        pos = self.position
        closing, adds = [], []
        if (getattr(self.ex, "has", None) or {}).get("fetchMyTrades", True):
            for attempt in range(STOP_ATTEMPTS):     # a rate limit, or fills not visible yet: back off and look again
                if attempt:
                    self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
                closing, adds = self._exit_fills(pos, self._fills_since(pos.entry_ts) or [])
                if closing:
                    return self._book_fills(pos, closing, adds, self._exit_reason(closing))
        est_fee = lambda px: pos.qty * px * self.p.costs.taker_fee_bps / 1e4   # noqa: E731
        reason, px, fee, ts = "stop (exchange)", None, None, None
        if self.stop_order_id and (getattr(self.ex, "has", None) or {}).get("fetchOrder", True):
            try:
                o = self.ex.fetch_order(self.stop_order_id, self.symbol)
                if o.get("status") == "closed" and o.get("average"):
                    px = float(o["average"])
                    fee = float((o.get("fee") or {}).get("cost") or est_fee(px))
                    ts = o.get("lastTradeTimestamp") or o.get("lastUpdateTimestamp") or o.get("timestamp")
            except Exception as exc:
                self.log(f"{self.coin}: could not read stop order {self.stop_order_id}: {type(exc).__name__}: {exc}")
        if px is not None:
            return self._book(pos, px, fee, int(ts or self.clock()), reason)
        # Provisional: at the stop level (or, for a desk close whose answer was lost, the last price), with the
        # configured fee. Every check reads the fills again and re-books the trade from them once they come back.
        px = (self.last_price if self.close_wanted else pos.stop) or pos.stop or self.last_price or pos.entry_price
        fee = est_fee(px)
        rec = {"entry_ts": pos.entry_ts, "side": pos.side.value, "entry_price": pos.entry_price,
               "entry_fee": pos.fees_paid, "entry_order_id": self.entry_order_id, "stop_order_id": self.stop_order_id,
               "close_wanted": self.close_wanted, "tries": 0}
        t = self._book(pos, px, fee, self.clock(), f"{self.close_wanted or 'stop'} (exchange, fill not found)")
        self.provisional = (self.provisional + [{**rec, "booked_ts": t.exit_ts}])[-20:]
        self.alert(f"the exchange closed the {pos.side.value} {pos.qty:g} {self.coin} but its fill couldn't be "
                   f"read: booked for now at {px:,.2f} with an estimated fee. It is re-booked from the exchange's "
                   f"fills as soon as they can be read; if this alert isn't followed by a re-booking, check the "
                   f"venue's trade history.")
        return t

    def _resolve_provisional(self) -> None:
        """Re-book each exit that was booked without its fills, once the exchange's fills for it can be read: the
        same closing fills since its entry that a normal exit is booked from (up to when it was booked)."""
        for rec in list(self.provisional):
            fills = self._fills_since(rec["entry_ts"])
            until = rec["booked_ts"] + FILL_LOOKBACK_MS
            if self.position is not None and self.position.entry_ts > rec["entry_ts"]:
                until = min(until, self.position.entry_ts - 1)        # the next position's fills aren't this one's
            side = Side(rec["side"])
            window = [t for t in fills or [] if (t.get("timestamp") or 0) <= until]
            closing = [t for t in window if t.get("side") == self._closing_side(side)]
            k = next((j for j in range(len(self.trades) - 1, -1, -1) if self.trades[j].entry_ts == rec["entry_ts"]
                      and self.trades[j].side is side and "fill not found" in self.trades[j].reason), None)
            if k is None:
                self.provisional.remove(rec)
                continue
            if not closing:
                rec["tries"] += 1
                if rec["tries"] >= self.provisional_tries:
                    self.provisional.remove(rec)
                    self.alert(f"the {rec['side']} {self.coin} exit booked without its fills still can't be read "
                               f"after {self.provisional_tries} tries: it stays at {self.trades[k].exit_price:,.2f}. Check "
                               f"the venue's trade history.")
                continue
            old = self.trades[k]
            adds = [] if rec["entry_order_id"] is None and rec["entry_fee"] else [
                t for t in window if t.get("side") != self._closing_side(side)
                and str(t.get("order")) != str(rec["entry_order_id"])]
            qty, px, fee, ts = self._sum_fills(closing)
            if fee is None:
                fee = qty * px * self.p.costs.taker_fee_bps / 1e4
            if adds:
                aq, ap, af, _ = self._sum_fills(adds)
                fee += af if af is not None else aq * ap * self.p.costs.taker_fee_bps / 1e4
            ts = int(ts or old.exit_ts)
            funding = self._exchange_funding(rec["entry_ts"], until=ts)
            funding = old.funding if funding is None else funding
            if any((t.get("info") or {}).get("liquidationMarkPx") or (t.get("info") or {}).get("liquidation")
                   for t in closing):
                reason = "liquidation"
            elif rec["close_wanted"] and not any(str(t.get("order")) == str(rec["stop_order_id"]) for t in closing):
                reason = rec["close_wanted"]
            else:
                reason = "stop (exchange)"
            new = Trade(side, qty, old.entry_ts, old.entry_price, ts, px, reason,
                        side.sign * qty * (px - old.entry_price), rec["entry_fee"] + fee, funding)
            self.fees_total += new.fees - old.fees
            self.funding_total += new.funding - old.funding
            self.trades[k] = new
            self.provisional.remove(rec)
            self.alert(f"re-booked the {rec['side']} {self.coin} exit from the exchange's fills: {px:,.2f} (booked for "
                       f"now at {old.exit_price:,.2f}), net {new.net_pnl:+,.2f} (was {old.net_pnl:+,.2f})")

    def _exit_reason(self, closing: list[dict]) -> str:
        """Why the exchange is flat: a liquidation, the stop, or the desk's own close whose answer was lost (it went
        through, but the call failed) when none of the fills is the stop's."""
        if any((t.get("info") or {}).get("liquidationMarkPx") or (t.get("info") or {}).get("liquidation")
               for t in closing):
            return "liquidation"
        by_stop = self.stop_order_id and any(str(t.get("order")) == self.stop_order_id for t in closing)
        if self.close_wanted and not by_stop:
            return self.close_wanted
        return "stop (exchange)"

    def _book_fills(self, pos: Position, closing: list[dict], adds: list[dict], reason: str) -> Trade:
        """One trade for the whole position, from the exchange's fills: every closing fill's size, average price and
        fee, plus the fee of anything added after the entry."""
        qty, px, fee, ts = self._sum_fills(closing)
        if fee is None:
            fee = qty * px * self.p.costs.taker_fee_bps / 1e4
            self.log(f"{self.coin}: exit fee not readable; using the {self.p.costs.taker_fee_bps} bps estimate")
        if adds:
            add_qty, add_px, add_fee, _ = self._sum_fills(adds)
            fee += add_fee if add_fee is not None else add_qty * add_px * self.p.costs.taker_fee_bps / 1e4
        if len(closing) > 1:
            self.log(f"{self.coin}: the exit is {len(closing)} closing fills, {qty:g} in all, average {px:,.2f}")
        return self._book(pos, px, fee, int(ts or self.clock()), reason, qty=qty)

    def _book(self, pos: Position, px: float, fee: float, ts: int, reason: str, qty: float | None = None) -> Trade:
        """Record the closed trade, with the funding the exchange actually charged when it can say. `qty` is what
        the exit fills add up to (the whole position, even if part of it was closed earlier); the desk's size if
        the fills couldn't be read."""
        qty = pos.qty if qty is None else qty
        funding = self._exchange_funding(pos.entry_ts)
        if funding is None:
            funding = pos.funding_paid
            self.log(f"{self.coin}: funding not readable from the exchange; the trade carries the desk's estimate")
        self.funding_total += funding - pos.funding_paid
        self.fees_total += fee
        gross = pos.side.sign * qty * (px - pos.entry_price)
        t = Trade(pos.side, qty, pos.entry_ts, pos.entry_price, ts, px, reason, gross, pos.fees_paid + fee,
                  funding)
        self.trades.append(t)
        self.position = None
        self.stop_order_id = self.stop_px = self.entry_order_id = self.close_wanted = None
        self.close_failures = 0
        self.stops_to_clean = True
        self.last_fill = {"action": "close", "side": pos.side.value, "qty": qty, "price": px, "fee": fee,
                          "ts": ts, "reason": reason, "net_pnl": t.net_pnl}
        return t

    # ---- orders ----------------------------------------------------------
    def submit(self, order: Order) -> None:
        self.pending.append(order)
        self.orders_submitted += 1

    def execute_now(self, price: float, ts: int) -> None:
        self.last_price = price
        pending, self.pending = self.pending, []
        for o in pending:
            if o.action == "open" and self.position is None:
                self._open(o, price, ts)
            elif o.action == "close" and self.position is not None:
                self._close(price, ts, o.reason)

    fill_pending = execute_now

    @staticmethod
    def _accepted(res: dict | None, what: str) -> dict:
        if not res or res.get("status") == "rejected" or not res.get("id"):
            raise RuntimeError(f"{what} not accepted by the exchange: {res}")
        return res

    def _open(self, o: Order, price: float, ts: int) -> None:
        if self._cb:
            return self._cb_open(o, price, ts)
        qty = o.qty
        if self.max_notional_usd is not None:
            # the cap holds after the fill's slippage too
            qty = min(qty, self.max_notional_usd / (price * (1 + self.p.costs.slippage_bps / 1e4)))
        amount = self._amount(qty)
        qty = amount * self.contract_size
        if qty <= 0 or qty * price * (1 - self.p.costs.slippage_bps / 1e4) < self.min_cost or amount < self.min_amount:
            self.skipped = {"ts": ts, "reason": o.reason, "qty": qty, "notional": qty * price,
                            "min_cost": self.min_cost, "step": self.amount_step}
            text = (f"entry not sent: {qty:g} {self.coin} (${qty * price:,.2f}) after the size step "
                    f"{self.amount_step or 0:g} and the cap is under {self.venue}'s ${self.min_cost:,.2f} minimum")
            if self.max_notional_usd is not None and self.max_notional_usd < self.smallest_cap(price):
                self.alert(f"{text}: --max-notional ${self.max_notional_usd:,.2f} is below the smallest ticket at "
                           f"{price:,.2f} (${self.smallest_cap(price):,.2f}), so no entry can go out")
            else:
                self.log(text)
            return
        side = "buy" if o.side is Side.LONG else "sell"
        res = self._accepted(self.ex.create_order(self.symbol, "market", side, amount, price), "entry order")
        if res.get("filled") is not None:
            qty = float(res["filled"]) * self.contract_size
        else:   # the venue didn't say how much filled: ask for the position
            ex_side, ex_qty, _ = self._exchange_position()
            qty = ex_qty if ex_side is o.side else 0.0
        if qty <= 0:
            self.alert(f"entry order {res['id']} for {amount:g} {self.coin} did not fill; no position")
            return
        fill, fee, _ = self._order_fill(res, ts, price, qty)
        self.fees_total += fee
        self.position = Position(o.side, qty, fill, ts, stop=o.stop or 0.0, best_close=fill, fees_paid=fee,
                                 setup=o.reason)
        self.entry_order_id = str(res["id"])
        self.orders_filled += 1
        self.last_fill = {"action": "open", "side": o.side.value, "qty": qty, "price": fill, "fee": fee, "ts": ts,
                          "reason": o.reason}
        self._save_local()
        if not o.stop:
            self.alert(f"entry {res['id']} came with no stop: closing it rather than holding it unprotected")
            self._close(price, ts, "no_stop")
            return
        self._ensure_stop(o.stop, price, ts)

    def _close(self, price: float | None, ts: int, reason: str, known: dict | None = None) -> Trade | None:
        """Close at market (reduce-only). The close goes first and the stops are cancelled after it's done, so a
        close that fails leaves the stop in place. (Coinbase, with no reduce-only: _cb_close.)"""
        if self._cb:
            return self._cb_close(price, ts, reason, known)
        pos = self.position
        if pos is None:
            return None
        ex_side, ex_qty, _ = self._exchange_position()
        if ex_side is None:     # already flat on the exchange (the stop got there first): book that instead
            n = len(self.trades)
            self.sync(price)
            return self.trades[-1] if len(self.trades) > n else None
        # Wanted until it's done: if this call fails (or the answer is lost after the exchange took it), the next
        # check sends it again, and a restart picks it up from the state file.
        self.close_wanted = reason
        self._save_local()
        ref = price or self.last_price or pos.entry_price
        side = self._closing_side(pos.side)
        try:
            res = self._accepted(self.ex.create_order(self.symbol, "market", side, self._amount(ex_qty), ref,
                                                      {"reduceOnly": True}), f"close ({reason})")
        except Exception as exc:
            self.close_failures += 1
            if self.close_failures == 1 or self.close_failures % 20 == 0:
                self.alert(f"could not close the {pos.side.value} {pos.qty:g} {self.coin} ({reason}): "
                           f"{type(exc).__name__}: {exc}. The stop stays in place; the close is sent again at every "
                           f"check until it goes through")
            raise
        after_side, after_qty, _ = self._exchange_position()
        if after_side is not None:
            pos.qty = after_qty
            self._save_local()
            raise RuntimeError(f"close ({reason}) left {after_qty:g} {self.coin} open; its stop stays in place and "
                               f"the rest is sent again at the next check")
        t = self._book_desk_close(pos, res, ts, ref, reason)
        self._cancel_stops(keep=None)
        self.orders_filled += 1
        self.cash = self._refresh_cash()
        self._save_local()
        return t

    def _book_desk_close(self, pos: Position, res: dict, ts: int, ref: float, reason: str) -> Trade:
        """The desk's own close went through: book the whole position from its closing fills since the entry (an
        earlier part may have gone by hand, or in a close that only partly filled). If this order's own fills aren't
        readable (yet), its answer stands in for them: its average, else the reference price, and its fee, else the
        configured taker fee."""
        fills = self._fills_since(pos.entry_ts)
        closing, adds = self._exit_fills(pos, fills or [])
        if not any(str(t.get("order")) == str(res["id"]) for t in closing):
            amount = float(res.get("filled") or 0.0) or pos.qty / self.contract_size
            px = float(res.get("average") or res.get("price") or ref)
            fee = (res.get("fee") or {}).get("cost")
            if fee is None:
                fee = amount * self.contract_size * px * self.p.costs.taker_fee_bps / 1e4
                self.log(f"{self.coin}: close {res['id']}'s fills and fee aren't readable from the exchange; using its "
                         f"answer and the {self.p.costs.taker_fee_bps} bps taker estimate")
            closing = closing + [{"order": str(res["id"]), "side": self._closing_side(pos.side), "amount": amount,
                                  "price": px, "timestamp": ts,
                                  "fee": {"cost": float(fee), "currency": self.market.get("settle")}}]
        return self._book_fills(pos, closing, adds, reason)

    def close_now(self, price: float, ts: int, reason: str) -> Trade | None:
        self.pending = []
        return self._close(price, ts, reason)

    def retry_close(self, price: float, ts: int) -> Trade | None:
        """Send again a desk close that hasn't gone through (an exchange error, a partial fill, a restart). Paper
        can't fail a close, so a desk that wanted out must keep trying: a daily-loss flatten, say, is decided only
        once a day."""
        if self.close_wanted is None:
            return None
        if self.position is None:
            self.close_wanted = None
            self._save_local()
            return None
        return self._close(price, ts, self.close_wanted)

    # ---- the protective stop ------------------------------------------------
    def _place_stop(self, stop: float, known: dict | None = None) -> str:
        pos = self.position
        trig = self._px(stop)
        params = {"stopLossPrice": trig, "reduceOnly": True}
        if self._cb:    # a stop-limit (the adapter); its id is the stop's, the position's and a sequence number's
            params = {"stopLossPrice": trig,
                      "clientOrderId": self.ex.client_order_id("stop", pos.entry_ts, trig, self.order_seq)}
            if known is not None:
                params["known"] = known
        res = self._accepted(self.ex.create_order(self.symbol, "market", self._closing_side(pos.side),
                                                  self._amount(pos.qty), trig, params), "stop order")
        if self._cb:
            self.order_seq += 1     # the next stop, even at the same level, is a new order
        return str(res["id"])

    def _find_stop(self, oid: str | None, stop: float) -> dict | None:
        """The resting stop with this id, or else one resting at this trigger (an order whose call failed after
        the exchange took it): never place a second one on top of it."""
        stops = self._resting_stops(self._closing_side(self.position.side))
        want = self._px(stop)
        for o in stops:
            if oid is not None and str(o["id"]) == oid:
                return o
        for o in stops:
            if math.isclose(_trigger(o), want, rel_tol=1e-9) and self._covers(o):
                return o
        return None

    def _ensure_stop(self, stop: float, price: float | None, ts: int, keep_old: bool = False) -> bool:
        """Get a stop at `stop` resting on the exchange, confirmed by reading it back from the open orders. Tries
        STOP_ATTEMPTS times with backoff. If it never shows up, the position is closed (reduce-only market) and an
        alert goes out: never a position without a stop. With `keep_old` (moving a trail), an older stop that is
        still resting keeps protecting the position instead, and the next sync tries the move again. (Coinbase, with
        no reduce-only: a move is cancel-first, _cb_replace_stop.)"""
        if self._cb:
            return self._cb_replace_stop(stop) if keep_old else self._cb_ensure_stop(stop, price, ts)
        pos = self.position
        if pos is None:
            return True
        if price is not None and (price - stop) * pos.side.sign <= 0:
            # the market is already through the stop: that's the stop being hit
            self.alert(f"{self.coin} at {price:,.2f} is already through the stop {stop:,.2f}: closing at market")
            self._close(price, ts, "stop")
            return False
        err = "not on the exchange's open orders after placing it"
        for attempt in range(STOP_ATTEMPTS):
            if attempt:
                self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
            oid = None
            try:
                oid = self._place_stop(stop)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            try:
                found = self._find_stop(oid, stop)
            except Exception as exc:
                found, err = None, f"{type(exc).__name__}: {exc}"
            if found is not None:
                self.stop_order_id, self.stop_px = str(found["id"]), _trigger(found)
                self._cancel_stops(keep=self.stop_order_id)     # the old stop (a trail move), or a duplicate
                self._save_local()
                return True
            self.log(f"{self.coin}: stop at {stop:,.2f}, try {attempt + 1}/{STOP_ATTEMPTS}: {err}")
        if keep_old:
            try:
                found = self._find_stop(None, stop)     # one of the tries took after all: it's the stop
                old = self._resting_stops(self._closing_side(pos.side))
            except Exception:
                found, old = None, []
            if found is not None:
                self.stop_order_id, self.stop_px = str(found["id"]), _trigger(found)
                self._cancel_stops(keep=self.stop_order_id)
                self._save_local()
                return True
            if old:
                self.alert(f"could not move the stop to {stop:,.2f} after {STOP_ATTEMPTS} tries ({err}); the stop at "
                           f"{_trigger(old[0]):,.2f} still protects the position, trying again at the next sync")
                return False
        self.alert(f"could not get a stop resting on the exchange after {STOP_ATTEMPTS} tries ({err}): closing the "
                   f"{pos.side.value} {pos.qty:g} {self.coin} at market rather than hold it unprotected")
        self._close(price, ts, "no_stop")
        return False

    def _cancel_stops(self, keep: str | None) -> None:
        """Cancel every reduce-only stop on this market but `keep`. A stop left behind after a close would close the
        next position at the wrong level, so a cancel that fails is retried at the next sync."""
        try:
            stops = self._resting_stops()
        except Exception as exc:
            self.log(f"{self.coin}: could not list resting stops to cancel: {type(exc).__name__}: {exc}")
            if keep is None:
                self.stops_to_clean = True
            return
        failed = False
        for o in stops:
            if str(o["id"]) == keep:
                continue
            try:
                self.ex.cancel_order(o["id"], self.symbol)
            except Exception as exc:  # already filled or gone, or a blip: the next sync looks again
                failed = True
                self.log(f"{self.coin}: cancel of stop {o['id']} failed: {type(exc).__name__}: {exc}")
        if keep is None:
            self.stop_order_id = self.stop_px = None
            self.stops_to_clean = failed

    def update_stop(self, stop: float) -> None:
        """Move the resting stop: the new one is placed and confirmed first, then the old one is cancelled (never
        unprotected). If the new one won't go on, the old one stays and the next sync tries again. While catching
        up missed bars (defer_stops) the move is only kept in memory: flush_stop() sends the final level once and saves
        it. Until then the state file keeps the stop that is resting on the exchange, so a process killed mid catch-up
        restarts from the bar it had saved, with that bar's stop, and replays the gap as it was."""
        if self.position is None:
            return
        self.position.stop = stop
        if self.defer_stops:
            return
        self._save_local()
        self._ensure_stop(stop, self.last_price, self.clock(), keep_old=True)
        if self.position is not None:
            self._save_local()

    def flush_stop(self) -> None:
        """After a catch-up: put the desk's stop on the exchange if the resting one isn't at it."""
        pos = self.position
        if pos is None or not pos.stop or (self.stop_px is not None and math.isclose(self.stop_px, self._px(pos.stop),
                                                                                     rel_tol=1e-9)):
            return
        self._ensure_stop(pos.stop, self.last_price, self.clock(), keep_old=True)

    def missed_stop(self, bar) -> bool:
        """Catching up: did `bar` go through a trail the desk moved during the gap but never sent? The stop that was
        resting on the exchange did its own job (sync books it); this is the tighter level paper would have held.

        Never for a bar the saved position had already been through when this process started (replay_until: a restart
        after its stop was saved but before the desk file was): its stop is that later bar's, not the one paper held
        on the earlier bar."""
        pos = self.position
        if pos is None or not pos.stop or self.stop_px is None:
            return False
        if self.replay_until is not None and bar.ts <= self.replay_until:
            return False
        if (self._px(pos.stop) - self.stop_px) * pos.side.sign <= 0:     # the resting stop is the desk's (as rounded)
            return False
        return bar.low <= pos.stop if pos.side is Side.LONG else bar.high >= pos.stop

    def check_stop_price(self, price: float, ts: int):
        """The exchange holds the stop; just reconcile."""
        self.sync(price)

    def apply_funding(self, rate: float, price: float) -> None:
        """Funding is settled by the exchange; keep an estimate for the board."""
        if self.position:
            paid = self.position.side.sign * self.position.qty * price * rate
            self.position.funding_paid += paid
            self.funding_total += paid

    # ---- Coinbase: no reduce-only, one product that is the desk's alone (module docstring) --------------------
    @property
    def bar_ms(self) -> int:
        from ..thresholds import INTERVAL_SECONDS

        return INTERVAL_SECONDS[self.p.market.interval] * 1000

    def _closed_now(self) -> bool:
        """Coinbase is known to be closed now: the runner's sessions say so, or this run's product answer did. Not
        knowing isn't closed (a close or a stop that Coinbase then refuses is harmless; holding a position isn't)."""
        if self.market_closed is not None:
            try:
                if self.market_closed():
                    return True
            except Exception as exc:
                self.log(f"{self.coin}: could not tell whether the market is closed: {type(exc).__name__}")
        return self._cb and getattr(self.ex, "session_open", lambda: None)() is False

    def _say_once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.alert(text)

    def _cb_sync(self, price: float | None = None) -> None:
        if price is not None:
            self.last_price = price
        side, qty, entry = self._exchange_position()
        if self.position is not None and (side is None or side is not self.position.side):
            self._book_exchange_exit()      # its stop, a liquidation, by hand (or a stop that went through, then more)
        if side is not None and self.position is None:
            self._cb_adopt(side, qty, entry)
        elif side is not None:
            self._follow_size(qty, entry)
        if self.position is not None and self.position.stop:
            self._check_stop_resting()
        if self.position is not None:
            self._cb_limit_passed()
        else:
            self._cb_clear_strays()
        if self.provisional:
            self._resolve_provisional()
        foreign = getattr(self.ex, "foreign", None) or {}
        if foreign.get("positions") or foreign.get("orders"):
            what = [f"a {p['side']} {p['contracts']} on {p['product_id']}" for p in foreign.get("positions") or []] + \
                   [f"order {o['id']} on {o['product_id']}" for o in foreign.get("orders") or []]
            text = (f"the account holds {', '.join(what)}, not the desk's: no entries while it does. The desk trades "
                    f"only {self.symbol} and never touches anything else: close or cancel it yourself, or move it to "
                    f"another portfolio")
            on_product = [o for o in foreign.get("orders") or [] if o.get("product_id") == self.symbol]
            if on_product and not self._simulated:
                # one on the desk's own product: with no reduce-only its closing contracts count against the position
                self.urgent("foreign", f"{text}. One rests on {self.symbol} itself: with no reduce-only it could open "
                                       f"a position, or stop the desk from placing its own stop")
            else:
                self._say_once("foreign", text)
        self.cash = self._refresh_cash()
        self._save_local()

    def _is_desk_entry(self, order: dict | None) -> bool:
        """Whether `order` is one of the desk's entries: its client_order_id is the one the desk gives an entry
        decided at one of the bars that closed just before it was placed (rebuilt, no saved state needed)."""
        if not order or not order.get("clientOrderId"):
            return False
        bar = self.bar_ms
        t = int(order.get("timestamp") or self.clock())
        bars = {(t // bar - 1 - k) * bar for k in range(CB_ENTRY_BARS)}
        if self.bar_ts is not None:
            bars.add(int(self.bar_ts))
        return any(self.ex.client_order_id("entry", b) == order["clientOrderId"] for b in bars)

    def _cb_adopt(self, side: Side, qty: float, entry: float) -> None:
        """A position on the product this desk's state doesn't know. The desk's own entry (its state was lost after
        the order went): adopted, with the stop resting for it if there is one (else the runner gives it one now).
        Anything else (opened by a stop, a close, by hand): closed at market, and entry_kill is asked for."""
        try:
            order = self.ex.opened_by(side.value, self.clock() - CB_ADOPT_LOOKBACK_MS)
            known = True
        except Exception as exc:
            order, known = None, False
            self.log(f"{self.coin}: could not read what opened the position: {type(exc).__name__}: {exc}")
        mine = self._is_desk_entry(order)
        ts = int((order or {}).get("timestamp") or self.clock())
        self.position = Position(side, qty, entry, ts, stop=0.0, best_close=entry,
                                 setup="adopted" if mine or not known else "not the desk's")
        self.stop_order_id = self.stop_px = None
        self.entry_order_id = str(order["id"]) if order else None
        stops = self._resting_stops(self._closing_side(side))
        if stops:
            self.position.stop = self.stop_px = _trigger(stops[0])
            self.stop_order_id = str(stops[0]["id"])
        if mine:
            self.order_alert(f"Coinbase holds a {side.value} {qty:g} {self.coin} the desk opened (its entry order "
                             f"{order['id']}) that its saved state didn't have: adopted, "
                             + (f"with its stop at {self.stop_px:,.2f}" if self.stop_px else
                                "with no stop resting: the desk places one this run"))
            self._save_local()
            return
        if not known:
            self.entry_kill_why = (f"Coinbase holds a {side.value} {qty:g} {self.coin} the desk's state doesn't know, "
                                   f"and its fills can't be read to tell who opened it: kept and given a stop, "
                                   f"entry_kill is on. Check it in Coinbase")
            self.order_alert(self.entry_kill_why)
            self._save_local()
            return
        what = f"order {order['id']} ({order.get('type')})" if order else "no order the fills show"
        self.entry_kill_why = (f"Coinbase holds a {side.value} {qty:g} {self.coin} opened by {what}, not one of the "
                               f"desk's entries (a stop left resting, a close, or by hand): closing it at market. "
                               f"entry_kill is on")
        self.order_alert(self.entry_kill_why)
        self._save_local()
        try:
            self._close(self.last_price, self.clock(), "not_the_desks")
        except Exception as exc:     # kept as close_wanted: sent again at the next check
            self.log(f"{self.coin}: closing the position that isn't the desk's failed: {type(exc).__name__}: {exc}")

    def _cb_limit_passed(self) -> None:
        """A stop-limit the price is past the limit of can't fill (a gap): close at market rather than wait."""
        pos, px = self.position, self.last_price
        if pos is None or px is None:
            return
        for o in self._resting_stops(self._closing_side(pos.side)):
            lim = o.get("price")
            if lim is None:
                continue
            if (px - float(lim)) * pos.side.sign < 0:
                self.order_alert(f"the stop at {_trigger(o):,.0f} is past its limit {float(lim):,.0f} ({self.coin} at "
                                 f"{px:,.2f}): a stop-limit can't fill there, so the position is closed at market")
                self._close(px, self.clock(), "stop")
                return

    def _cb_clear_strays(self) -> None:
        """Flat: nothing of the desk's may rest on the product. Coinbase has no reduce-only, so a stop left behind would
        open a position if it fired. Cancelled now, retried; only if that fails, the urgent alert. An order that isn't
        the desk's is never touched: pushed urgently (cancel it yourself)."""
        from .coinbase import StopAlreadyFilled

        try:
            orders = [o for o in self.ex.fetch_open_orders(self.symbol) if o.get("status") == "open"]
        except Exception as exc:
            self.log(f"{self.coin}: could not list the orders resting with no position: {type(exc).__name__}: {exc}")
            self.stops_to_clean = True
            return
        others = getattr(self.ex, "foreign_on_product", lambda: [])()
        if others and not self._simulated:
            ids = ", ".join(str(o["id"]) for o in others)
            self.urgent("stray-foreign", f"STOP WITH NO POSITION: order {ids} rests on {self.symbol} with no "
                                         f"position, and it isn't the desk's (the desk never touches it): Coinbase has "
                                         f"no reduce-only, so it would open a position if it fired. Cancel order {ids} "
                                         f"in Coinbase now")
        if orders and getattr(self.ex, "cancels_refused", lambda: False)():
            self.log(f"{self.coin}: {len(orders)} order(s) rest with no position and Coinbase isn't taking cancels "
                     f"now; cancelled at the next check")
            self.stops_to_clean = True
            return
        failed = []
        for o in orders:
            err = ""
            for attempt in range(STOP_ATTEMPTS):
                if attempt:
                    self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
                try:
                    self.ex.cancel_order(o["id"], self.symbol)
                    break
                except StopAlreadyFilled:
                    break       # it filled with no position: the next sync finds what it opened
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
            else:
                failed.append((o, err))
        self.stops_to_clean = bool(failed)
        if failed:
            ids = ", ".join(str(o["id"]) for o, _ in failed)
            self.entry_kill_why = (f"STOP WITH NO POSITION: an order is resting on {self.symbol} with no position "
                                   f"(order {ids}): Coinbase has no reduce-only, so it would open a position if it "
                                   f"fired. Cancel order {ids} in Coinbase now. ({failed[0][1]})")
            self.urgent("stray", self.entry_kill_why)
        self.stop_order_id = self.stop_px = None

    def _cb_order_state(self, oid: str) -> str | None:
        """What a stop reads after a cancel with no clear answer: 'gone', 'open', 'filled', or None (unreadable)."""
        try:
            o = self.ex.fetch_order(oid, self.symbol)
        except Exception:
            try:
                rest = [x for x in self.ex.fetch_open_orders(self.symbol) if str(x["id"]) == str(oid)]
            except Exception:
                return None
            if not rest:
                return "gone"
            o = rest[0]
        st = o.get("status")
        if st == "closed":
            return "filled"
        if st == "canceled" or str(o.get("coinbase_status")) == "CANCEL_QUEUED":
            return "gone"
        return "open" if st == "open" else "canceling"

    def _cb_no_stop(self, why: str) -> None:
        """The position may have no stop resting on Coinbase and the desk can't fix it in this run: entry_kill, and the
        urgent push."""
        if (self.entry_kill_why or "").startswith("NO STOP"):
            self.log(f"{self.coin}: still no stop: {why}")      # said already this check
            return
        pos = self.position
        what = (f"the {pos.side.value} {pos.qty:g} {self.coin} on {self.symbol}" if pos else
                f"the position on {self.symbol}")
        self.entry_kill_why = (f"NO STOP: {what} may have no stop resting on Coinbase ({why}). Place its stop or close "
                               f"it in Coinbase now; the desk tries again at the next check. entry_kill is on")
        self.urgent("no_stop", self.entry_kill_why)

    def _cb_cancel_closing(self) -> tuple[bool | None, list]:
        """Cancel every desk order resting on the closing side and confirm they're gone. (True, the desk's orders read
        after): gone. (False, []): one stays, or may (alerted). (None, []): one had already filled (sync to book it).
        A cancel with no clear answer is read back before anything is decided: "still protects" only when it reads
        open."""
        from .coinbase import CancelPending, StopAlreadyFilled

        closing = self._closing_side(self.position.side)
        try:
            stops = [o for o in self.ex.fetch_open_orders(self.symbol) if o.get("side") == closing]
        except Exception as exc:
            self.log(f"{self.coin}: could not list the stops: {type(exc).__name__}: {exc}")
            return False, []
        for o in stops:
            if o.get("status") == "canceling":
                continue
            try:
                self.ex.cancel_order(o["id"], self.symbol)
            except StopAlreadyFilled:
                return None, []
            except CancelPending as exc:
                self._cb_no_stop(f"the cancel of its stop {o['id']} was taken but isn't done ({exc}): the old stop is "
                                 f"going, and a new one isn't placed beside it")
                return False, []
            except Exception as exc:
                state = self._cb_order_state(str(o["id"]))
                if state == "gone":
                    continue
                if state == "filled":
                    return None, []
                if state == "open":
                    self.order_alert(f"could not cancel the stop {o['id']} ({type(exc).__name__}: {exc}); it reads "
                                     f"open, so it stays and still protects the position. Tried again at the next "
                                     f"check")
                    return False, []
                self._cb_no_stop(f"the cancel of its stop {o['id']} had no clear answer ({type(exc).__name__}) and the "
                                 f"stop can't be read back")
                return False, []
        try:
            after = self.ex.fetch_open_orders(self.symbol)
        except Exception as exc:
            # every cancel above was read back done: the closing side is clear (the placement reads again itself)
            self.log(f"{self.coin}: could not confirm the stop's cancel: {type(exc).__name__}: {exc}")
            after = []
        left = [o for o in after if o.get("side") == closing]
        if left:
            self._cb_no_stop(f"the stop {left[0]['id']}'s cancel isn't confirmed (it reads "
                             f"{left[0].get('coinbase_status') or left[0].get('status')}): no second stop on top of it")
            return False, []
        self.stop_order_id = self.stop_px = None
        return True, after

    def _last_trade_px(self) -> float | None:
        try:
            got = self.ex.last_trade()
        except Exception as exc:
            self.log(f"{self.coin}: could not read the last trade: {type(exc).__name__}: {exc}")
            return None
        return got[0] if got else None

    def _protecting(self):
        import contextlib

        p = getattr(self.ex, "protecting", None)
        return p() if p is not None else contextlib.nullcontext()

    def _cb_replace_stop(self, stop: float) -> bool:
        """Move the stop CANCEL-FIRST: the old one is cancelled and confirmed gone, then the new one placed. Never
        while Coinbase is closed, never without the time for the whole sequence (the move, and a close if the new stop
        won't rest), and if the last trade is already through the new level, that's the stop being hit: closed at
        market. With nothing resting on the closing side there's nothing to move: the stop is just placed (a stop-limit
        queues while Coinbase is closed). After the cancel, the placement goes on past the run's deadline (protecting)
        with the position and orders already read as its fallback; if no stop rests in the end, the urgent push."""
        pos = self.position
        if pos is None:
            return True
        closing = self._closing_side(pos.side)
        try:
            resting = [o for o in self.ex.fetch_open_orders(self.symbol) if o.get("side") == closing]
        except Exception as exc:
            self.log(f"{self.coin}: could not list the stops before moving one: {type(exc).__name__}: {exc}")
            return False
        if not resting:
            return self._cb_ensure_stop(stop, self.last_price, self.clock())
        if self._closed_now():
            self.log(f"{self.coin}: Coinbase is closed: the stop stays at {self.stop_px or 0:,.0f}; the move to "
                     f"{stop:,.0f} waits for the reopen")
            return False
        if not self.ex.fits(CB_MOVE_WORST_S + CB_CLOSE_WORST_S):
            self.log(f"{self.coin}: not enough of the run left to move the stop safely; it stays, moved next run")
            return False
        last = self._last_trade_px()
        if last is not None and (last - stop) * pos.side.sign <= 0:
            self.order_alert(f"{self.coin} last traded {last:,.2f}, already through the stop's new level {stop:,.2f}: "
                             f"closing at market")
            self._close(last, self.clock(), "stop")
            return False
        gone, after = self._cb_cancel_closing()
        if gone is None:            # the old stop filled first: book it
            self.sync(self.last_price)
            return False
        if not gone:
            return False
        with self._protecting():
            try:
                try:
                    side, qty, entry = self._exchange_position()
                except Exception as exc:    # the position read before the cancel stands (the cancel left it as it was)
                    self.log(f"{self.coin}: could not read the position after cancelling the stop: "
                             f"{type(exc).__name__}")
                    side, qty, entry = pos.side, pos.qty, pos.entry_price
                if side is None or side is not pos.side:
                    self.sync(self.last_price)
                    return False
                self._follow_size(qty, entry)
                known = {"side": pos.side.value, "contracts": self._amount(pos.qty), "orders": after}
                ok = self._cb_ensure_stop(stop, last or self.last_price, self.clock(), known=known)
            except Exception as exc:
                if self.position is not None and not self.stop_order_id:
                    self._cb_no_stop(f"its old stop was cancelled to move it and the new one couldn't be placed "
                                     f"({type(exc).__name__})")
                raise
        return ok

    def _cb_place_until_rests(self, stop: float, known: dict | None = None) -> tuple[bool, str]:
        """Place the stop and read it back, STOP_ATTEMPTS times with backoff. (rested, last error). Raises
        StopThroughMarket (the price is already through it)."""
        from .coinbase import OrderRefused, StaleClientOrderId, StopThroughMarket

        err = "not on Coinbase's open orders after placing it"
        closing = self._closing_side(self.position.side)
        for attempt in range(STOP_ATTEMPTS):
            if attempt:
                self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
            oid = None
            try:
                oid = self._place_stop(stop, known)
            except StopThroughMarket:
                raise
            except StaleClientOrderId as exc:
                self.order_seq += 1
                self._save_local()
                err = str(exc)
            except OrderRefused as exc:
                err = str(exc)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            try:
                found = self._find_stop(oid, stop)
                if found is None:       # a stop resting at another level (a race): it protects; moved next sync
                    found = next((o for o in self._resting_stops(closing) if self._covers(o)), None)
            except Exception as exc:
                found = None
                err = err if oid is None else f"{type(exc).__name__}: {exc}"
                if oid is not None:     # placed, its read-back failed: it rests (the adapter read it back as open)
                    self.stop_order_id, self.stop_px = oid, self._px(stop)
                    self._save_local()
                    return True, ""
            if found is not None:
                self.stop_order_id, self.stop_px = str(found["id"]), _trigger(found)
                self._save_local()
                return True, ""
            try:
                side, _, _ = self._exchange_position()
            except Exception:
                side = self.position.side
            if side is None:
                return False, "flat"
            self.log(f"{self.coin}: stop at {stop:,.2f}, try {attempt + 1}/{STOP_ATTEMPTS}: {err}")
        return False, err

    def _cb_ensure_stop(self, stop: float, price: float | None, ts: int, known: dict | None = None) -> bool:
        """Get the stop resting (no other stop rests: the adapter refuses one that would). If it never rests the
        position is closed at market, past the run's deadline if need be, and entry_kill is asked for: the next entry
        would meet the same refusal. A close that doesn't go through either: the urgent push, never "closed"."""
        from .coinbase import StopThroughMarket

        pos = self.position
        if pos is None:
            return True
        if getattr(self.ex, "halted", lambda: False)():
            self._cb_no_stop("Coinbase's session reads HALTED: nothing can be sent until it reopens")
            return False
        if price is not None and (price - stop) * pos.side.sign <= 0:
            self.order_alert(f"{self.coin} at {price:,.2f} is already through the stop {stop:,.2f}: closing at market")
            self._close(price, ts, "stop")
            return False
        with self._protecting():
            try:
                ok, err = self._cb_place_until_rests(stop, known)
            except StopThroughMarket as exc:
                self.order_alert(f"{exc}: closing at market")
                self._close(self.last_price, ts, "stop")
                return False
            if ok:
                return True
            if err == "flat" or self.position is None:
                self.sync(self.last_price)
                return False
            why = f"could not get a stop resting on Coinbase after {STOP_ATTEMPTS} tries ({err})"
            self.entry_kill_why = f"{why}. entry_kill is on"
            try:
                self._close(price, ts, "no_stop", known=known)
            except Exception as exc:
                self._cb_no_stop(f"{why}, and the close at market didn't go through ({type(exc).__name__}: {exc})")
                raise
            if self.position is None:
                self.order_alert(f"{why}: the {pos.side.value} {pos.qty:g} {self.coin} was closed at market rather "
                                 f"than held unprotected. entry_kill is on")
            else:
                self._cb_no_stop(f"{why}, and the close at market wasn't sent or didn't go through")
            return False

    def _cb_protect_remainder(self) -> None:
        """After a close that failed or went part way: a stop back on what Coinbase still holds (never on more), with
        no close-at-market fallback (a close is what just failed). Never resting: the urgent alert."""
        from .coinbase import StopThroughMarket

        pos = self.position
        if pos is None or not pos.stop:
            return
        with self._protecting():
            try:
                ok, err = self._cb_place_until_rests(pos.stop)
            except StopThroughMarket as exc:
                ok, err = False, str(exc)
        if not ok and err != "flat":
            self._cb_no_stop(f"a close didn't go through and its stop couldn't be put back ({err})")

    PROTECTIVE = ("no_stop", "stop", "not_the_desks")

    def _cb_close(self, price: float | None, ts: int, reason: str, known: dict | None = None) -> Trade | None:
        """Close on Coinbase: cancel the stop first (confirmed gone), then close_position for the size Coinbase holds.
        After any failure the position is read again: a stop goes back only on what's still open; flat, nothing may
        rest. Nothing is sent while Coinbase is closed (market orders can't be queued). A close the desk decided
        (a trend exit, flat_kill) waits for the next check without the time for the whole sequence; a protective one
        (no stop would rest, the stop is hit or gapped past) goes now, past the run's deadline if need be."""
        from .coinbase import StaleClientOrderId
        from .coinbase_rest import CoinbaseUnavailable

        pos = self.position
        if pos is None:
            return None
        protective = reason in self.PROTECTIVE
        with (self._protecting() if protective else _null()):
            try:
                ex_side, ex_qty, entry = self._exchange_position()
            except CoinbaseUnavailable:
                if not (protective and known):
                    raise
                ex_side, ex_qty, entry = pos.side, pos.qty, pos.entry_price      # read just before (known)
            if ex_side is None:
                n = len(self.trades)
                self.sync(price)
                return self.trades[-1] if len(self.trades) > n else None
            self.close_wanted = reason
            self._save_local()
            if self._closed_now():
                self.log(f"{self.coin}: Coinbase is closed: market orders can't be queued, so the {reason} close goes "
                         f"at the reopen (the stop stays meanwhile)")
                return None
            if not protective and not self.ex.fits(CB_CLOSE_WORST_S):
                self.log(f"{self.coin}: not enough of the run left to close safely; the {reason} close goes next run")
                return None
            ref = price or self.last_price or pos.entry_price
            side = self._closing_side(pos.side)
            # After a cancel-first move whose new stop wouldn't rest (known), the closing side was just confirmed clear;
            # anything a try left resting after all is cancelled once the position is flat (_cb_clear_strays).
            gone, _ = (True, []) if known is not None else self._cb_cancel_closing()
            if gone is None:            # the stop filled first
                n = len(self.trades)
                self.sync(price)
                if self.position is None:
                    return self.trades[-1] if len(self.trades) > n else None
                pos = self.position     # it filled only in part: whatever of it still rests goes before the close
                gone, _ = self._cb_cancel_closing()
                ex_side, ex_qty, entry = self._exchange_position()
                if ex_side is None:
                    self.sync(price)
                    return self.trades[-1] if len(self.trades) > n else None
            if not gone:
                self.close_failures += 1
                raise RuntimeError(f"close ({reason}) not sent: the stop couldn't be cancelled first, and with no "
                                   f"reduce-only a close with the stop resting could leave it to open a position. The "
                                   f"close is sent again at the next check")
            res = None
            for _ in range(2):
                cid = self.ex.client_order_id("close", pos.entry_ts, self.order_seq)
                self.order_seq += 1
                self._save_local()      # written before it's sent: the next one never reuses this id
                params = {"reduceOnly": True, "clientOrderId": cid}
                if known is not None:
                    params["known"] = known
                try:
                    res = self._accepted(self.ex.create_order(self.symbol, "market", side, self._amount(ex_qty), ref,
                                                              params), f"close ({reason})")
                    break
                except StaleClientOrderId:
                    continue
                except Exception as exc:
                    self.close_failures += 1
                    self._cb_after_failed_close()
                    if self.position is None:       # it went through after all, and is booked
                        return self.trades[-1] if self.trades else None
                    if self.close_failures == 1 or self.close_failures % 20 == 0:
                        self.order_alert(f"could not close the {pos.side.value} {pos.qty:g} {self.coin} ({reason}): "
                                         f"{type(exc).__name__}: {exc}. "
                                         + ("A stop is back on the position" if self.stop_order_id else
                                            "No stop could be put back on it") +
                                         "; the close is sent again at every check until it goes through")
                    raise
            if res is None:
                self._cb_after_failed_close()
                raise RuntimeError(f"close ({reason}): Coinbase kept answering with an old order for its client id; "
                                   f"the close is sent again at the next check")
            try:
                after_side, after_qty, after_entry = self._exchange_position()
            except CoinbaseUnavailable:
                if float(res.get("filled") or 0) + 1e-9 < self._amount(ex_qty):
                    raise
                after_side = None       # the close's own answer says it all filled
            if after_side is not None:
                self._follow_size(after_qty, after_entry)
                self._save_local()
                self._cb_protect_remainder()
                raise RuntimeError(f"close ({reason}) left {after_qty:g} {self.coin} open; "
                                   + ("a stop is back on it" if self.stop_order_id else "no stop could be put back")
                                   + " and the rest is sent again at the next check")
            t = self._book_desk_close(pos, res, ts, ref, reason)
            try:
                self._cb_clear_strays()
            except Exception as exc:
                self.log(f"{self.coin}: could not check for orders left resting: {type(exc).__name__}")
                self.stops_to_clean = True
            self.orders_filled += 1
            try:
                self.cash = self._refresh_cash()
            except Exception as exc:    # the trade is booked; the balance is confirmed at the next check
                self.log(f"{self.coin}: the balance after the close couldn't be read: {type(exc).__name__}")
            self._save_local()
            return t

    def _cb_after_failed_close(self) -> None:
        try:
            side, qty, entry = self._exchange_position()
        except Exception as exc:
            self._cb_no_stop(f"a close failed and the position can't be read back ({type(exc).__name__})")
            return
        if side is None:
            self.sync(self.last_price)      # the close went through: booked from its fills; nothing left resting
            return
        self._follow_size(qty, entry)
        self._cb_protect_remainder()

    def _cb_skip(self, o: Order, ts: int, why: str, qty: float | None = None, loud: bool = False) -> None:
        self.skipped = {"ts": ts, "reason": o.reason, "qty": o.qty if qty is None else qty, "why": why}
        if loud:
            self._say_once(f"skip:{why}", f"entry not sent: {why}")
        else:
            self.log(f"{self.coin}: entry not sent: {why}")

    def _cb_open(self, o: Order, price: float, ts: int) -> None:
        """An entry on Coinbase: sized again at the best bid/ask now (not the close the desk decided at), on the part
        of the balance that can be margin, whole contracts; sent with the entry's client_order_id; its stop placed
        and read back before this returns (past the run's deadline if need be). Its answer lost, or refused only after
        a try that got no answer: the position is read, adopted and protected now."""
        from .coinbase import OrderRefused, SessionClosed

        why = self.ex.entry_block()
        if why:
            return self._cb_skip(o, ts, why, loud=True)
        if self._closed_now():
            return self._cb_skip(o, ts, "Coinbase is closed now (market orders can't be queued)")
        if not self.ex.fits(CB_MOVE_WORST_S + CB_CLOSE_WORST_S):
            return self._cb_skip(o, ts, "not enough of the run left to place the entry and its stop safely")
        if not o.stop:
            return self._cb_skip(o, ts, "it came with no stop")
        try:
            bba = self.ex.best_bid_ask()
        except Exception as exc:
            bba = None
            self.log(f"{self.coin}: could not read the best bid/ask: {type(exc).__name__}: {exc}")
        if bba is None:
            return self._cb_skip(o, ts, "no fresh best bid/ask from Coinbase to size it at")
        ref = bba[1] if o.side is Side.LONG else bba[0]
        sign, qty = o.side.sign, o.qty
        dist = (ref - o.stop) * sign
        if dist <= 0:
            return self._cb_skip(o, ts, f"{self.coin} at {ref:,.2f} is already through its stop {o.stop:,.2f}")
        close_px = (o.meta or {}).get("notional", 0.0) / o.qty if o.qty else 0.0
        stop_atr = (self.p.strategy.short_stop_atr if sign < 0 and self.p.strategy.short_stop_atr
                    else self.p.strategy.stop_atr)
        atr = abs(close_px - o.stop) / stop_atr if close_px and stop_atr else None
        if atr and dist < 0.5 * atr:
            return self._cb_skip(o, ts, f"{self.coin} at {ref:,.2f} is within half an ATR of its stop {o.stop:,.2f}")
        usable = getattr(self.ex, "usable", None)
        sizing = min(self.cash, usable) if usable is not None else self.cash
        if self.rules is not None:      # the desk's own sizing (filter.py SIZE) at this price, then whole contracts
            z = self.p.sizing
            want = sizing * z.risk_per_trade / dist if sizing > 0 else 0.0
            want = min(want, sizing * z.max_leverage / ref) if sizing > 0 else 0.0
            q2, gates = self.rules.size(want, ref, o.stop, sizing, sign, z.risk_per_trade)
            if q2 <= 0:
                return self._cb_skip(o, ts, f"sized again at {ref:,.2f}: {gates[-1].note if gates else 'nothing fits'}")
            qty = min(qty, q2)
        if self.max_notional_usd is not None:
            qty = min(qty, self.max_notional_usd / (ref * (1 + self.p.costs.slippage_bps / 1e4)))
        amount = self._amount(qty)
        if amount < max(1.0, self.min_amount):
            return self._cb_skip(o, ts, f"under one contract at {ref:,.2f} after the cap and the size step", qty)
        qty = amount * self.contract_size
        side = "buy" if o.side is Side.LONG else "sell"
        bar = self.bar_ts if self.bar_ts is not None else (ts // self.bar_ms - 1) * self.bar_ms
        cid = self.ex.client_order_id("entry", bar)
        try:
            res = self._accepted(self.ex.create_order(self.symbol, "market", side, amount, ref,
                                                      {"clientOrderId": cid}), "entry order")
        except (OrderRefused, SessionClosed) as exc:     # refused before anything was done
            return self._cb_skip(o, ts, str(exc), loud=True)
        except Exception as exc:
            # a lost answer, or a refusal after a try that got none, or any other: read before deciding anything
            with self._protecting():
                if self._cb_recover_entry(o, cid, ts, ref, exc):
                    return
            raise
        with self._protecting():
            filled = float(res.get("filled") or 0.0)
            if filled <= 0:
                ex_side, ex_qty, _ = self._exchange_position()
                filled = ex_qty / self.contract_size if ex_side is o.side else 0.0
            if filled <= 0:
                self.order_alert(f"entry order {res['id']} for {amount:g} contracts did not fill; no position")
                return
            qty = filled * self.contract_size
            fill, fee, _ = self._order_fill(res, ts, ref, qty)
            self.fees_total += fee
            self.position = Position(o.side, qty, fill, ts, stop=o.stop, best_close=fill, fees_paid=fee,
                                     setup=o.reason)
            self.entry_order_id = str(res["id"])
            self.orders_filled += 1
            self.last_fill = {"action": "open", "side": o.side.value, "qty": qty, "price": fill, "fee": fee, "ts": ts,
                              "reason": o.reason}
            self._save_local()
            self._cb_check_units(res, filled)
            self._ensure_stop(o.stop, ref, ts)

    def _cb_check_units(self, res: dict, filled: float) -> None:
        """The entry's fills, its order's filled size and the position Coinbase reports must agree, in contracts. If
        they don't (sizes in BTC somewhere, say), nothing is guessed: alerted, and entry_kill is asked for."""
        try:
            fills = [t for t in self._fills_since(int(res.get("timestamp") or self.clock()) - FILL_LOOKBACK_MS) or []
                     if str(t.get("order")) == str(res["id"])]
            side, qty, _ = self._exchange_position()
        except Exception as exc:
            self.log(f"{self.coin}: could not check the entry's sizes: {type(exc).__name__}: {exc}")
            return
        held = qty / self.contract_size
        summed = sum(float(t["amount"]) for t in fills) if fills else filled
        if abs(summed - filled) > 1e-6 or abs(held - filled) > 1e-6:
            self.entry_kill_why = (f"the entry's sizes don't agree: its fills add up to {summed:g}, the order says "
                                   f"{filled:g} filled, and Coinbase holds {held:g} contracts. The units may not be "
                                   f"what the desk thinks (contracts vs BTC): not guessed at, entry_kill is on")
            self.order_alert(self.entry_kill_why)

    def _cb_recover_entry(self, o: Order, cid: str, ts: int, ref: float, exc: Exception) -> bool:
        """The entry went out but its answer didn't come back (or a retry was refused after a try that got no answer).
        If Coinbase holds the position, it's this entry: adopt it and put its stop on now, in this same check (the
        next run is 15 minutes or more away)."""
        try:
            side, qty, entry = self._exchange_position()
        except Exception as e2:
            self.entry_kill_why = (f"an entry's answer was lost ({type(exc).__name__}) and the position can't be read "
                                   f"back ({type(e2).__name__}): it may be open with no stop. Check {self.symbol} in "
                                   f"Coinbase now")
            self.urgent("no_stop", self.entry_kill_why)
            return False
        if side is None:
            return False
        order = None
        try:
            order = self.ex.find_order(cid, ts - FILL_LOOKBACK_MS)
        except Exception as e2:
            self.log(f"{self.coin}: could not look the entry order up: {type(e2).__name__}: {e2}")
        if side is not o.side:
            return False
        fee, px = qty * entry * self.p.costs.taker_fee_bps / 1e4, entry or ref
        if order is not None:
            px = float(order.get("average") or px)
            if (order.get("fee") or {}).get("cost") is not None:
                fee = float(order["fee"]["cost"])
        self.fees_total += fee
        self.position = Position(o.side, qty, px, ts, stop=o.stop, best_close=px, fees_paid=fee, setup=o.reason)
        self.entry_order_id = str(order["id"]) if order else None
        self.orders_filled += 1
        self.last_fill = {"action": "open", "side": o.side.value, "qty": qty, "price": px, "fee": fee, "ts": ts,
                          "reason": o.reason}
        self._save_local()
        self.order_alert(f"the entry's answer was lost or unclear ({type(exc).__name__}), but Coinbase holds the "
                         f"{o.side.value} {qty:g} {self.coin}: adopted as the desk's entry, and its stop goes on now")
        self._ensure_stop(o.stop, ref, ts)
        return True

    def _read_flows_stable(self, base: "_Read", alert: bool = True) -> "_Read | None":
        """The exchange's ledger when each entry keeps its id through its status changes (Coinbase's v2 transactions):
        one read covers the window. A completed entry is a flow, counted once (its id is then seen). A pending one is
        only a candidate: it explains a move only if it matches it to the cent (_refresh_cash), and isn't marked seen
        until it's used or completed. Canceled, failed and expired ones never count. A flow's time is when it was
        made, or the last read before this one if that's later: it landed after the last balance that didn't have it.
        The cursor is this read's time."""
        try:
            page = self.ex.fetch_ledger(None, base.cursor)
        except Exception as exc:
            self._ledger_failures += 1
            text = (f"could not read deposits and withdrawals ({type(exc).__name__}: {exc}); the brakes keep the "
                    f"last confirmed balance and new candles wait until it reads again")
            if alert and (self._ledger_failures == 1 or self._ledger_failures % LEDGER_ALERT_EVERY == 0):
                self.alert(text)
            else:
                self.log(f"{self.coin}: {text}")
            return None
        now = self.clock()
        seen, seen_list, out = set(base.seen), list(base.seen), list(base.flows)
        cands = []
        for e in sorted(page, key=lambda e: e.get("timestamp") or 0):
            fid = str(e["id"])
            if fid in seen:
                continue
            status = e.get("status") or self.ex.flow_status(e.get("info") or {})
            if status == "void":
                continue
            if status not in ("final", "candidate"):
                if alert and fid not in self._alerted:
                    self._alerted.add(fid)
                    self.alert(f"a Coinbase transaction ({fid}) with a status the desk doesn't know: not recorded; "
                               f"if it moved the account, it's alerted as a move nothing explains")
                continue
            try:
                got = self.ex.parse_flow(e.get("info") or {})
            except UnknownLedgerEntry as exc:
                if status == "final":
                    seen.add(fid)
                    seen_list.append(fid)
                if alert and fid not in self._alerted:
                    self._alerted.add(fid)
                    self.alert(f"{exc}. Not recorded as a deposit or withdrawal: check it on Coinbase; if it moved the "
                               f"account, it's alerted as a move nothing explains")
                continue
            if got is None:     # inside the account (spot <-> futures)
                if status == "final":
                    seen.add(fid)
                    seen_list.append(fid)
                continue
            rec = {"id": fid, "ts": max(int(e.get("timestamp") or 0), int(base.cursor or 0)),
                   "amount": round(got[0], 6), "what": got[1], "type": e.get("type")}
            if status == "final":
                seen.add(fid)
                seen_list.append(fid)
                out.append(rec)
            else:
                cands.append(rec)
        self._ledger_failures = 0
        return _Read(out, now, seen_list[-1000:], cands)

    @staticmethod
    def _match_candidates(left: float, cands: list[dict]) -> list[dict] | None:
        """The pending entries that explain `left` to the cent: one of them, or all of them together."""
        for c in cands:
            if abs(c["amount"] - left) < 0.005:
                return [c]
        if len(cands) > 1 and abs(sum(c["amount"] for c in cands) - left) < 0.005:
            return list(cands)
        return None

    # ---- local mirror ------------------------------------------------------
    def _save_local(self) -> None:
        """Written whole or not at all (a temporary file, then a rename): a process killed mid-write leaves the last
        complete state, never half of one."""
        pos = self.position
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps({
            "coin": self.coin, "venue": self.venue, "symbol": self.symbol,
            "stop_order_id": self.stop_order_id, "stop_px": self.stop_px, "stops_to_clean": self.stops_to_clean,
            "entry_order_id": self.entry_order_id, "close_wanted": self.close_wanted, "provisional": self.provisional,
            "bar_ts": self.bar_ts,
            "flows": self.flows, "flows_cursor": self.flows_cursor, "flows_seen": self.flows_seen,
            "recon": self._recon, "unexplained_since": self._unexplained_since, "order_seq": self.order_seq,
            "position": None if pos is None else {
                "side": pos.side.value, "qty": pos.qty, "entry_price": pos.entry_price, "entry_ts": pos.entry_ts,
                "stop": pos.stop, "best_close": pos.best_close, "fees_paid": pos.fees_paid,
                "funding_paid": pos.funding_paid, "setup": pos.setup},
            "trades": [t.as_dict() for t in self.trades[-500:]],
        }))
        tmp.replace(self.state_path)

    def _load_local(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text())
        if d.get("symbol", self.symbol) != self.symbol:
            raise LiveSetupError(f"{self.state_path} holds the {d['symbol']} desk's position, not {self.symbol}'s")
        self.stop_order_id = d.get("stop_order_id")
        self.stop_px = d.get("stop_px")
        self.stops_to_clean = bool(d.get("stops_to_clean", False))
        self.entry_order_id = d.get("entry_order_id")
        self.close_wanted = d.get("close_wanted")
        self.provisional = list(d.get("provisional") or [])
        self.bar_ts = self.replay_until = d.get("bar_ts")
        self.flows = list(d.get("flows") or [])
        self.flows_cursor = d.get("flows_cursor")
        self.flows_seen = list(d.get("flows_seen") or [])
        self._recon = d.get("recon")
        if self._cb:        # a one-shot run (Coinbase on Actions) waits across runs, so the wait's start is kept
            self._unexplained_since = d.get("unexplained_since")
        self.order_seq = int(d.get("order_seq") or 0)
        pd = d.get("position")
        if pd:
            self.position = Position(Side(pd["side"]), pd["qty"], pd["entry_price"], pd["entry_ts"], pd["stop"],
                                     pd["best_close"], pd.get("fees_paid", 0.0), pd.get("funding_paid", 0.0),
                                     setup=pd.get("setup", ""))
        self.trades = [Trade(Side(t["side"]), t["qty"], t["entry_ts"], t["entry_price"], t["exit_ts"],
                             t["exit_price"], t["reason"], t["gross_pnl"], t["fees"], t["funding"])
                       for t in d.get("trades", [])]
