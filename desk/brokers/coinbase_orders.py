"""Coinbase order requests and answers, as pure functions: no I/O. LIVE sends exactly these bodies, and SHADOW
records exactly these bodies as what LIVE would have sent, so the two can't drift apart.

Facts this rests on (Coinbase's public product answer for BIP-20DEC30-CDE, read 2026-10-13, and the request and
response types generated from Coinbase's API spec, retail-public-api-spec.json, in its sample TypeScript SDK 0.3.0):
  * an order's size is a whole number of contracts, written as a string: base_increment "1", base_min_size "1",
    base_max_size "5000"; one contract is 0.01 BTC (future_product_details.contract_size);
  * prices are multiples of $5 (price_increment "5"): sent as whole-dollar strings;
  * there is no stop-market order and no documented reduce_only field, so the protective stop is a stop-limit
    (stop_limit_stop_limit_gtc: base_size, stop_price, limit_price, stop_direction). Its limit sits 5% past the
    trigger, the offset Coinbase itself gives a bracket's stop leg ("a limit price 5% higher for BUYS and 5% lower
    for SELLS", TriggerBracketGtc). A stop-limit whose limit the price gaps past doesn't fill;
  * entries are market IOC (market_market_ioc.base_size); exits are POST /orders/close_position with the size always
    given (the documented sample says a market close with an empty size fails);
  * client_order_id makes placing safe to retry: "If the ID provided is not unique, the order will not be created and
    the order corresponding with that ID will be returned instead" (NewOrderRequest). The desk's ids are UUIDv5s
    (Coinbase's format rules for the field weren't confirmed; a UUID is the safest form) over what the order is for.

Every parser accepts an amount as a plain string or as {"value", "currency"}: sources disagree on which the futures
answers use.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from ..thresholds import COINBASE
from .coinbase_rest import CoinbaseDataError

PRODUCT = COINBASE.product
CONTRACT_SIZE = COINBASE.contract_size
PRICE_STEP = 5
MAX_CONTRACTS = COINBASE.max_contracts
STOP_LIMIT_OFFSET = 0.05            # the stop-limit's limit, past its trigger (Coinbase's own bracket offset)
STOP_LIMIT_FALLBACKS = (0.05, 0.02, 0.01)   # tighter limits tried if a price band refuses the 5% one
CID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/kendallcoding202/btc-perp-desk/coinbase-orders")

# OrderExecutionStatus (the spec) -> the desk's view. EDIT_QUEUED: an edit waiting for the reopen (futures orders can't
# be edited, so the desk never asks; it still rests). UNKNOWN_ORDER_STATUS isn't here: parse_order raises on it.
OPEN_STATUSES = {"OPEN": "open", "PENDING": "open", "QUEUED": "open", "EDIT_QUEUED": "open",
                 "CANCEL_QUEUED": "canceling", "FILLED": "closed", "CANCELLED": "canceled", "EXPIRED": "canceled",
                 "FAILED": "canceled"}
DESK_SOURCE = "RETAIL_ADVANCED"     # order_placement_source of orders sent through the Advanced Trade API


def is_desk_cid(cid) -> bool:
    """Whether a client_order_id has the desk's form (a version-5 UUID, client_order_id() below). An order placed in the
    Coinbase app or by hand carries another (or none): never the desk's, whatever product it's on."""
    try:
        return uuid.UUID(str(cid)).version == 5
    except (TypeError, ValueError, AttributeError):
        return False


def client_order_id(kind: str, coin: str, *parts) -> str:
    """A deterministic 36-character id: the same order asked again carries the same id, so Coinbase returns the
    first one instead of making a second."""
    return str(uuid.uuid5(CID_NS, ":".join(str(x) for x in (kind, coin.upper(), *parts))))


def ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        if "." in s:                    # Coinbase gives microseconds or more; fromisoformat takes up to six digits
            head, rest = s.split(".", 1)
            digits = "".join(ch for ch in rest if ch.isdigit())
            tz = rest[len(digits):]
            s = f"{head}.{(digits + '000000')[:6]}{tz}"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except (TypeError, ValueError):
        raise CoinbaseDataError("Coinbase gave a time the desk can't read") from None


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------- prices and sizes

def round_price(px: float) -> int:
    """The nearest multiple of $5 (a tie goes up)."""
    return int(math.floor(px / PRICE_STEP + 0.5)) * PRICE_STEP


def floor5(px: float) -> int:
    return int(math.floor(px / PRICE_STEP + 1e-9)) * PRICE_STEP


def ceil5(px: float) -> int:
    return int(math.ceil(px / PRICE_STEP - 1e-9)) * PRICE_STEP


def contracts(n) -> str:
    """A whole-contract size as Coinbase takes it."""
    k = int(round(float(n)))
    if abs(float(n) - k) > 1e-9 or not 1 <= k <= MAX_CONTRACTS:
        raise ValueError(f"an order is 1 to {MAX_CONTRACTS} whole contracts, not {n}")
    return str(k)


def _side(side: str) -> str:
    s = str(side).upper()
    if s not in ("BUY", "SELL"):
        raise ValueError(f"an order's side is BUY or SELL, not {side!r}")
    return s


# ---------------------------------------------------------------- requests

def market(cid: str, side: str, n) -> dict:
    """An entry: market IOC for `n` whole contracts. No leverage or margin_type: what they mean on CFM orders isn't
    known, so Coinbase's defaults stand."""
    return {"client_order_id": cid, "product_id": PRODUCT, "side": _side(side),
            "order_configuration": {"market_market_ioc": {"base_size": contracts(n)}}}


def stop_limit_prices(closing_side: str, trigger: float, offset: float = STOP_LIMIT_OFFSET) -> tuple[int, int, str]:
    """(stop price, limit price, stop_direction) of the protective stop for the side that closes the position. A sell
    (protecting a long) triggers on the way down, its limit `offset` below, rounded down to $5; a buy (protecting a
    short) triggers on the way up, its limit `offset` above, rounded up."""
    stop = round_price(trigger)
    if _side(closing_side) == "SELL":
        return stop, floor5(stop * (1 - offset)), "STOP_DIRECTION_STOP_DOWN"
    return stop, ceil5(stop * (1 + offset)), "STOP_DIRECTION_STOP_UP"


def stop_limit(cid: str, closing_side: str, n, trigger: float, offset: float = STOP_LIMIT_OFFSET) -> dict:
    stop, limit, direction = stop_limit_prices(closing_side, trigger, offset)
    return {"client_order_id": cid, "product_id": PRODUCT, "side": _side(closing_side),
            "order_configuration": {"stop_limit_stop_limit_gtc": {
                "base_size": contracts(n), "stop_price": str(stop), "limit_price": str(limit),
                "stop_direction": direction}}}


def close_position(cid: str, n) -> dict:
    return {"client_order_id": cid, "product_id": PRODUCT, "size": contracts(n)}


def batch_cancel(ids) -> dict:
    ids = [str(i) for i in ids]
    if not ids:
        raise ValueError("nothing to cancel")
    return {"order_ids": ids}


# ---------------------------------------------------------------- answers

def amount(x, what: str = "amount", required: bool = True) -> float | None:
    """A number Coinbase sent as "1.5" or {"value": "1.5", "currency": "USD"}."""
    if isinstance(x, dict):
        x = x.get("value")
    if x is None or x == "":
        if required:
            raise CoinbaseDataError(f"Coinbase's answer has no {what}")
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise CoinbaseDataError(f"Coinbase's {what} isn't a number") from None
    if not math.isfinite(v):
        raise CoinbaseDataError(f"Coinbase's {what} isn't a finite number")
    return v


def parse_balance(resp: dict) -> dict:
    """GET /cfm/balance_summary. Field meanings from Coinbase's API spec (FCMBalanceSummary):
        total_usd_balance  USD across the futures account (CFM) and the spot account (CBI)
        unrealized_pnl     open P&L across all positions
        daily_realized_pnl P&L realised on the current trade date, not yet settled into cfm_usd_balance (P&L, fees
                           and funding settle at 3:00 AM ET the next business day, per Coinbase's help pages)
        available_margin   "includes your CBI spot USD, CFM futures USD, and Futures PnL, less any holds"
    The account's value (equity) = total + unrealized + realised-not-settled + money in transit between the two
    accounts (total_pending_transfers_amount, in newer answers). It's checked against available margin + holds +
    that transit (cross): money in the spot account that can't be margin yet (a deposit's ACH hold) shows as
    equity above cross; a formula that misses something shows as cross above equity."""
    s = resp.get("balance_summary") if isinstance(resp, dict) else None
    if not isinstance(s, dict):
        raise CoinbaseDataError("Coinbase's balance answer has no balance_summary")
    f = {k: amount(s.get(k), k) for k in ("total_usd_balance", "cbi_usd_balance", "cfm_usd_balance", "unrealized_pnl",
                                          "daily_realized_pnl", "available_margin", "total_open_orders_hold_amount")}
    opt = {k: amount(s.get(k), k, required=False) for k in (
        "futures_buying_power", "liquidation_buffer_amount", "liquidation_threshold", "initial_margin",
        "total_pending_transfers_amount", "funding_pnl")}
    pending = opt["total_pending_transfers_amount"] or 0.0
    equity = f["total_usd_balance"] + f["unrealized_pnl"] + f["daily_realized_pnl"] + pending
    cross = f["available_margin"] + f["total_open_orders_hold_amount"] + pending

    def window(m) -> dict | None:
        if not isinstance(m, dict):
            return None
        return {"type": m.get("margin_window_type"), "level": m.get("margin_level"),
                **{k: amount(m.get(k), k, required=False) for k in ("initial_margin", "maintenance_margin",
                                                                     "liquidation_buffer", "total_hold",
                                                                     "futures_buying_power")}}

    return {"equity": equity, "cross": cross, "check_diff": equity - cross,
            "parts_diff": f["total_usd_balance"] - f["cbi_usd_balance"] - f["cfm_usd_balance"],
            "total": f["total_usd_balance"], "cbi": f["cbi_usd_balance"], "cfm": f["cfm_usd_balance"],
            "upnl": f["unrealized_pnl"], "realized": f["daily_realized_pnl"], "pending": pending,
            "available_margin": f["available_margin"], "holds": f["total_open_orders_hold_amount"],
            "buying_power": opt["futures_buying_power"], "liquidation_buffer": opt["liquidation_buffer_amount"],
            "liquidation_buffer_pct": amount(s.get("liquidation_buffer_percentage"), "liquidation_buffer_percentage",
                                             required=False),
            "funding_pnl": opt["funding_pnl"],
            "intraday": window(s.get("intraday_margin_window_measure")),
            "overnight": window(s.get("overnight_margin_window_measure"))}


def parse_position(p: dict) -> dict | None:
    """One FCMPosition: size in contracts ("The size of your position in contracts"), side LONG/SHORT. None when it
    holds nothing. A size that isn't a whole number means the units aren't what the desk thinks: raises."""
    n = amount(p.get("number_of_contracts"), "number_of_contracts")
    if n == 0:
        return None
    if abs(n - round(n)) > 1e-9 or n < 0:
        raise CoinbaseDataError(f"a {p.get('product_id')} position of {n:g} isn't a whole number of contracts: the "
                                f"desk won't guess its units")
    side = str(p.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        raise CoinbaseDataError(f"a {p.get('product_id')} position with side {side or 'missing'}: not acted on")
    return {"product_id": p.get("product_id"), "side": side.lower(), "contracts": int(round(n)),
            "entry": amount(p.get("avg_entry_price"), "avg_entry_price", required=False),
            "price": amount(p.get("current_price"), "current_price", required=False),
            "upnl": amount(p.get("unrealized_pnl"), "unrealized_pnl", required=False),
            "realized": amount(p.get("daily_realized_pnl"), "daily_realized_pnl", required=False)}


def parse_order(o: dict) -> dict:
    """One order (GET /orders/historical/...), as a ccxt-shaped dict LiveBroker reads. Stop-limits carry triggerPrice
    (stop_price) and price (limit_price); reduceOnly is None, never False: Coinbase has no such field, so it says
    nothing either way. CANCEL_QUEUED (a cancel waiting for the market to reopen), and an open order whose cancel has
    been asked for and isn't done yet (pending_cancel, "Whether a cancel request has been initiated for the order, and
    not yet completed"), are 'canceling': still resting, not counted as protection, and not gone either (it can still
    fill until its cancel completes). UNKNOWN_ORDER_STATUS raises."""
    raw = str(o.get("status") or "")
    if raw not in OPEN_STATUSES:
        raise CoinbaseDataError(f"order {o.get('order_id')} has status {raw or 'missing'}: not acted on")
    status = OPEN_STATUSES[raw]
    if status == "open" and o.get("pending_cancel") is True:
        status = "canceling"
    cfg = o.get("order_configuration") or {}
    typ, size, trig, limit, direction = "other", None, None, None, None
    if "market_market_ioc" in cfg or "market_market_fok" in cfg:
        c = cfg.get("market_market_ioc") or cfg.get("market_market_fok") or {}
        typ, size = "market", amount(c.get("base_size"), "base_size", required=False)
    elif "stop_limit_stop_limit_gtc" in cfg or "stop_limit_stop_limit_gtd" in cfg:
        c = cfg.get("stop_limit_stop_limit_gtc") or cfg.get("stop_limit_stop_limit_gtd") or {}
        typ, size = "stop_limit", amount(c.get("base_size"), "base_size", required=False)
        trig, limit = amount(c.get("stop_price"), "stop_price"), amount(c.get("limit_price"), "limit_price")
        direction = c.get("stop_direction")
    elif "limit_limit_gtc" in cfg or "limit_limit_gtd" in cfg:
        c = cfg.get("limit_limit_gtc") or cfg.get("limit_limit_gtd") or {}
        typ, size, limit = "limit", amount(c.get("base_size"), "base_size", required=False), \
            amount(c.get("limit_price"), "limit_price", required=False)
    filled = amount(o.get("filled_size"), "filled_size", required=False) or 0.0
    avg = amount(o.get("average_filled_price"), "average_filled_price", required=False)
    fee = amount(o.get("total_fees"), "total_fees", required=False)
    return {"id": str(o.get("order_id")), "clientOrderId": o.get("client_order_id"), "symbol": o.get("product_id"),
            "side": str(o.get("side") or "").lower() or None, "type": typ, "status": status,
            "coinbase_status": raw + (" (cancel pending)" if status == "canceling" and raw != "CANCEL_QUEUED" else ""),
            "placement": o.get("order_placement_source"), "amount": size, "filled": filled,
            "remaining": None if size is None else max(0.0, size - filled),
            "average": avg if avg else None, "price": limit, "triggerPrice": trig, "stopDirection": direction,
            "triggerStatus": o.get("trigger_status"), "reduceOnly": None,
            "fee": {"cost": fee, "currency": "USD"} if fee is not None else None,
            "timestamp": ms(o.get("created_time")), "lastTradeTimestamp": ms(o.get("last_fill_time")),
            "rejectReason": o.get("reject_reason"), "info": o}


def parse_fill(f: dict) -> dict | None:
    """One fill (GET /orders/historical/fills), ccxt-shaped: amount in contracts, the commission as the USD fee. Its
    id is entry_id (trade_id isn't unique for adjusted fills). None for a REVERSAL, CORRECTION or SYNTHETIC row: the
    caller alerts it, it isn't a fill."""
    kind = str(f.get("trade_type") or "FILL").upper()
    if kind != "FILL":
        return None
    return {"id": str(f.get("entry_id") or f.get("trade_id")), "order": str(f.get("order_id")),
            "symbol": f.get("product_id"), "side": str(f.get("side") or "").lower(),
            "amount": amount(f.get("size"), "size"), "price": amount(f.get("price"), "price"),
            "fee": {"cost": amount(f.get("commission"), "commission", required=False), "currency": "USD"},
            "timestamp": ms(f.get("trade_time") or f.get("sequence_timestamp")),
            "takerOrMaker": str(f.get("liquidity_indicator") or "").lower() or None, "info": f}


def parse_permissions(resp: dict) -> dict:
    """GET /key_permissions: {can_view, can_trade, can_transfer, portfolio_uuid, portfolio_type}. A missing flag is
    read as the riskier answer for trade and transfer (True) and the safer one for view (False)."""
    if not isinstance(resp, dict):
        raise CoinbaseDataError("Coinbase's key permissions answer isn't an object")
    return {"can_view": resp.get("can_view") is True, "can_trade": resp.get("can_trade") is not False,
            "can_transfer": resp.get("can_transfer") is not False, "portfolio_uuid": resp.get("portfolio_uuid"),
            "portfolio_type": resp.get("portfolio_type")}


# ---------------------------------------------------------------- money in and out (the v2 USD wallet)

# The Coinbase App's transaction types (https://docs.cdp.coinbase.com/coinbase-app/track-apis/transactions). An amount
# "can be positive (credit) or negative (debit)", but which types carry a sign isn't pinned down, so the direction
# comes from the type and the size from abs(amount): +1 money in, -1 money out, 0 inside the account. A transfer
# ("Transfer funds between two of your own accounts": another portfolio, say) can go either way, so its direction is
# its amount's sign (None below). None of these is trading: the desk's own P&L never comes through the USD wallet.
FLOW_TYPES = {
    "fiat_deposit": (+1, "bank deposit"),
    "fiat_withdrawal": (-1, "bank withdrawal"),
    "fcm_futures_usdc_sell": (+1, "USDC turned into USD for margin: money in from your USDC, not profit"),
    "fcm_futures_usdc_sell_additional_encumberment_rollup":
        (+1, "USDC turned into USD for margin: money in from your USDC, not profit"),
    "derivatives_settlement": (0, "daily transfer between the futures and spot accounts"),
    "transfer": (None, "transfer between your own Coinbase accounts or portfolios"),
    "subscription": (-1, "Coinbase subscription charge (not trading)"),
    "subscription_rebate": (+1, "Coinbase subscription rebate (not trading)"),
    "clawback": (-1, "Coinbase clawback of money paid out before (not trading)"),
    "incentives_shared_clawback": (-1, "Coinbase incentive clawback (not trading)"),
    "earn_payout": (+1, "Coinbase earn payout (not trading)"),
    "incentives_rewards_payout": (+1, "Coinbase rewards payout (not trading)"),
}
FINAL = {"completed"}
CANDIDATE = {"pending", "waiting_for_clearing"}
VOID = {"canceled", "cancelled", "failed", "expired"}


def flow_status(tx: dict) -> str:
    """'final' (completed), 'candidate' (pending: it may already be in the balance, or not yet), 'void' (canceled,
    failed, expired: never counted), or 'unknown'."""
    s = str(tx.get("status") or "").lower()
    return "final" if s in FINAL else "candidate" if s in CANDIDATE else "void" if s in VOID else "unknown"


def parse_flow(tx: dict) -> tuple[float, str] | None:
    """(dollars into (+) or out of (-) the account, what it was) for one v2 USD-wallet transaction; None when it moves
    money inside the account (spot <-> futures). Raises UnknownLedgerEntry for anything else, never a guess: a type it
    doesn't know, a currency other than USD, a credit type given a negative amount, a native amount that disagrees."""
    from .live import UnknownLedgerEntry

    kind = str(tx.get("type") or "")
    if kind not in FLOW_TYPES:
        raise UnknownLedgerEntry(f"a Coinbase transaction of type {kind or 'missing'!r} the desk doesn't know")
    sign, what = FLOW_TYPES[kind]
    a = tx.get("amount") if isinstance(tx.get("amount"), dict) else {}
    if str(a.get("currency") or "").upper() != "USD":
        raise UnknownLedgerEntry(f"a Coinbase {kind} not in USD")
    try:
        v = float(a.get("amount"))
    except (TypeError, ValueError):
        raise UnknownLedgerEntry(f"a Coinbase {kind} without a readable amount") from None
    if not math.isfinite(v):
        raise UnknownLedgerEntry(f"a Coinbase {kind} without a readable amount")
    if sign is None:            # a transfer: either way, as its amount says
        if v == 0:
            raise UnknownLedgerEntry(f"a Coinbase {kind} of zero: its direction isn't clear")
        sign = 1 if v > 0 else -1
    if sign > 0 and v < 0:
        raise UnknownLedgerEntry(f"a Coinbase {kind} (money in) with a negative amount: its direction isn't clear")
    na = tx.get("native_amount") if isinstance(tx.get("native_amount"), dict) else {}
    if str(na.get("currency") or "").upper() == "USD":
        try:
            nv = float(na.get("amount"))
        except (TypeError, ValueError):
            nv = None
        if nv is not None and abs(abs(nv) - abs(v)) > 0.005:
            raise UnknownLedgerEntry(f"a Coinbase {kind} whose amount and native amount disagree")
    if sign == 0:
        return None
    return round(sign * abs(v), 6), what
