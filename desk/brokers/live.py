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
    minimum order; no market for the coin = refuse to start;
  * the protective stop lives ON THE EXCHANGE as a reduce-only stop-market order,
    so if this process dies the position is still protected. It counts as placed only
    once it is read back from the open orders; it is retried with backoff, and if it
    still won't rest the position is closed and an alert goes out. Every sync checks
    it is still there and puts it back if not;
  * the exchange is the source of truth for the position; the desk's local copy
    (entry, best close, stop) is reconciled on every sync. An exit the exchange made
    is booked at its own fills, fees and funding, never at a later mid;
  * each coin's state is its own file (state/<COIN>/live_position.json);
  * any exception while placing an order leaves the desk in entry_kill.
"""

from __future__ import annotations

import json
import math
import os
import time
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


def market_symbol(venue: str, coin: str) -> str:
    """The perp this coin's desk trades on `venue`: the coin's own market, never another coin's."""
    if venue not in SYMBOL_FORMATS:
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


# Venues whose reported account value already includes unrealised PnL.
EQUITY_INCLUDES_UPNL = {"hyperliquid"}

# The protective stop: tries to get it resting on the exchange before giving up and closing the position,
# and the wait before each retry (doubling: 1 s, 2 s, 4 s).
STOP_ATTEMPTS = 4
STOP_BACKOFF_S = 1.0
FILL_LOOKBACK_MS = 60_000   # an order's own fills are looked up from a minute before it was sent (clock skew)
FUNDING_MAX_PAGES = 100     # 500 rows a page: years of hourly funding for a few coins
PROVISIONAL_TRIES = 240     # an exit booked without its fills is re-read at every check for about an hour (15 s polls)


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
                 clock=None):
        self.p = p
        self.log = log
        self.on_alert = on_alert              # the runner puts alerts on the board and the phone
        self.sleep = sleep
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.venue = venue or p.market.venue
        self.coin = p.market.symbol.upper()
        self.symbol = market_symbol(self.venue, self.coin)
        self.ex = exchange or make_exchange(self.venue)
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
        self.alerts: list[tuple[int, str]] = []
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
        self.cash = self._balance()
        self._load_local()
        if self.cash <= 0 and self.position is None:
            raise LiveSetupError(f"the {self.venue} account reads ${self.cash:,.2f}: nothing to trade with. On "
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
        """Reduce-only trigger orders resting on this market (on `side`, if given)."""
        out = []
        for o in self.ex.fetch_open_orders(self.symbol):
            if o.get("symbol") not in (None, self.symbol) or _trigger(o) is None or o.get("reduceOnly") is False:
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
        self.cash = self._balance()
        self._save_local()

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
                if rec["tries"] >= PROVISIONAL_TRIES:
                    self.provisional.remove(rec)
                    self.alert(f"the {rec['side']} {self.coin} exit booked without its fills still can't be read "
                               f"after {PROVISIONAL_TRIES} tries: it stays at {self.trades[k].exit_price:,.2f}. Check "
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

    def _close(self, price: float | None, ts: int, reason: str) -> Trade | None:
        """Close at market (reduce-only). The close goes first and the stops are cancelled after it's done, so a
        close that fails leaves the stop in place."""
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
        self.cash = self._balance()
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
    def _place_stop(self, stop: float) -> str:
        pos = self.position
        trig = self._px(stop)
        res = self._accepted(self.ex.create_order(self.symbol, "market", self._closing_side(pos.side),
                                                  self._amount(pos.qty), trig,
                                                  {"stopLossPrice": trig, "reduceOnly": True}), "stop order")
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
        still resting keeps protecting the position instead, and the next sync tries the move again."""
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

    # ---- local mirror ------------------------------------------------------
    def _save_local(self) -> None:
        pos = self.position
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "coin": self.coin, "venue": self.venue, "symbol": self.symbol,
            "stop_order_id": self.stop_order_id, "stop_px": self.stop_px, "stops_to_clean": self.stops_to_clean,
            "entry_order_id": self.entry_order_id, "close_wanted": self.close_wanted, "provisional": self.provisional,
            "bar_ts": self.bar_ts,
            "position": None if pos is None else {
                "side": pos.side.value, "qty": pos.qty, "entry_price": pos.entry_price, "entry_ts": pos.entry_ts,
                "stop": pos.stop, "best_close": pos.best_close, "fees_paid": pos.fees_paid,
                "funding_paid": pos.funding_paid, "setup": pos.setup},
            "trades": [t.as_dict() for t in self.trades[-500:]],
        }))

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
        pd = d.get("position")
        if pd:
            self.position = Position(Side(pd["side"]), pd["qty"], pd["entry_price"], pd["entry_ts"], pd["stop"],
                                     pd["best_close"], pd.get("fees_paid", 0.0), pd.get("funding_paid", 0.0),
                                     setup=pd.get("setup", ""))
        self.trades = [Trade(Side(t["side"]), t["qty"], t["entry_ts"], t["entry_price"], t["exit_ts"],
                             t["exit_price"], t["reason"], t["gross_pnl"], t["fees"], t["funding"])
                       for t in d.get("trades", [])]
