"""A local fake of Coinbase's Advanced Trade API (US futures) and the Coinbase App v2 reads the desk makes, on
127.0.0.1. Nothing here is Coinbase's: the answers are SYNTHETIC, shaped from Coinbase's Advanced Trade OpenAPI spec
(FCMBalanceSummary, FCMPosition, Order, Fill, NewOrderResponse, CancelOrderResponse, GetApiKeyPermissionsResponse,
Account, ...; its enums in tests/fixtures/coinbase_spec/enums.json, and every status, reason and session state this
fake answers is checked against them by tests/test_coinbase_spec.py) and the public answers recorded in
tests/fixtures/coinbase/.

What it checks and models, so the desk's client and broker are tested against Coinbase's documented behaviour:
  * auth: every private call's JWT is verified with the test key's public key (ES256 or EdDSA): kid = sub = the key
    name, iss "cdp", no aud, exp = nbf + 120 against the fake's own clock, uri = "METHOD api.coinbase.com/path" without
    the query, a 64-hex nonce never reused. Anything wrong: HTTP 401, text/plain "Unauthorized\\n" (what Coinbase
    answers, checked live without credentials). A key without the permission a call needs (view, trade, transfer):
    403 JSON "Missing required scopes".
  * orders: whole contracts as strings, $5 prices, client_order_id idempotency (a repeated id returns the first order),
    rejections as {success: false, error_response} with HTTP 200 or 400 (reject_status), market IOC fills at the ask
    or bid up to the book's liquidity (partial fills), stop-limits that trigger on the last trade and then fill only at
    their limit or better (a gap past the limit leaves them triggered and unfilled), and NO reduce-only: a stop
    resting with no position opens one when it fires, two stops can flip a position;
  * sessions: while closed nothing trades and no stop triggers, market orders are refused
    (FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE), stop-limits queue (QUEUED), cancels queue (CANCEL_QUEUED), and in the last
    30 s before a reopen cancels are refused (INVALID_CANCEL_FCM_TRADING_SESSION); session_state is the spec's
    FCM_TRADING_SESSION_STATE_OPEN / _CLOSE (any other can be set);
  * a cancel is a request: with cancel_delay_s, batch_cancel answers success and the order reads OPEN with
    pending_cancel until the delay has passed (it can still fill meanwhile);
  * List Orders returns only the RETAIL_ADVANCED placement source unless asked for another (the spec's default);
  * the Coinbase App (v2) reads refuse an Ed25519 token (401): Coinbase's key page says Ed25519 isn't supported there;
  * the account: spot USD (CBI, part of it maybe on an ACH hold), futures USD (CFM), P&L realised today and not yet
    settled (daily_realized_pnl), open P&L against a basis that re-bases at the daily settlement, money in transit
    between the two accounts (a sweep), holds, and the USD wallet's v2 transactions (deposits, withdrawals, USDC sold
    for margin, settlements), each with a status that can change;
  * trouble: any call can be made to answer 429, 5xx, something that isn't JSON, or nothing at all, before or after
    doing its work (a lost answer).
Every request is logged (method, path, query, body, key), so a test can prove nothing but GETs went out.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

PRODUCT = "BIP-20DEC30-CDE"
CS = 0.01
V3 = "/api/v3/brokerage"
T0 = 1_791_900_000.0        # 2026-10-13 14:00 UTC, a Tuesday
FIX = Path(__file__).parent / "fixtures" / "coinbase"
ZERO_ORG = "00000000-0000-0000-0000-000000000000"


def iso(s: float) -> str:
    return datetime.fromtimestamp(s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(t: str) -> float:
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- test keys (generated fresh, clearly fake)

@dataclass
class FakeKey:
    name: str
    alg: str
    private: object
    public: object
    view: bool = True
    trade: bool = False
    transfer: bool = False
    portfolio_uuid: str = "11111111-2222-3333-4444-555555555555"
    portfolio_type: str = "DEFAULT"
    private_text: str = ""

    def file_json(self, **extra) -> str:
        """The key file as the CDP portal's download looks (name + privateKey)."""
        return json.dumps({"name": self.name, "privateKey": self.private_text, **extra})


def dcid(label: str) -> str:
    """A client_order_id in the desk's form (a version-5 UUID): an order the tests treat as one the desk placed."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/kendallcoding202/btc-perp-desk/tests/{label}"))


def key_name(label: str = "test-key-not-real") -> str:
    return f"organizations/{ZERO_ORG}/apiKeys/{label}"


def make_ec_key(label: str = "test-key-not-real", **perms) -> FakeKey:
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption()).decode()
    return FakeKey(key_name(label), "ES256", k, k.public_key(), private_text=pem, **perms)


def make_ed_key(label: str = "test-ed-key-not-real", form: str = "raw64", **perms) -> FakeKey:
    k = ed25519.Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if form == "raw64":
        text = base64.b64encode(seed + pub).decode()
    elif form == "raw32":
        text = base64.b64encode(seed).decode()
    else:
        text = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode()
    return FakeKey(key_name(label), "EdDSA", k, k.public_key(), private_text=text, **perms)


# ---------------------------------------------------------------- the fake

@dataclass
class Rule:
    method: str
    path: str               # a prefix of the path
    status: int | None      # None: no answer at all (the connection drops)
    times: int = 1
    after: bool = False     # do the work first, then answer this (a lost answer)
    body: bytes | None = None


@dataclass
class Pos:
    side: str               # LONG / SHORT
    n: int
    entry: float
    basis: float            # what open P&L is measured from (the entry, or the last settlement price)

    @property
    def sign(self) -> int:
        return 1 if self.side == "LONG" else -1


class FakeCoinbase:
    def __init__(self, *, now_s: float = T0, price: float = 83_000.0, spread: float = 10.0, cbi: float = 1_000.0):
        self.lock = threading.RLock()
        self.now_s = now_s
        self.keys: dict[str, FakeKey] = {}
        self.nonces: set[str] = set()
        self.requests: list[dict] = []
        self.fail: list[Rule] = []
        # the market
        self.last = price
        self.last_trade_s = now_s
        self.history: list[tuple[float, float]] = [(now_s, price)]     # every trade: the candles
        self.spread = spread
        self.session_open = True
        self.no_cancel_phase = False
        self.trading_disabled = False
        self.product_edits: dict = {}
        self.liquidity: int | None = None       # the most contracts one market order finds
        self.band: float | None = None          # a stop-limit's limit further than this from the last trade: refused
        self.fee_per_contract = 0.20
        self.fee_rate = 0.0005
        self.reject_status = 200
        self.page_size = 50
        self.fill_units = "contracts"           # "btc": fills report their size in BTC (to test the unit check)
        self.amount_style = "object"            # balance amounts as {"value", "currency"} or plain "string"
        self.pending_field = True               # the balance answer has total_pending_transfers_amount
        self.stop_hold_per_contract = 0.0
        self.on_cancel = None                   # called as a cancel arrives (a stop that fills just then, say)
        self.max_stops = 0                      # the most stop orders ever resting on the product at once
        self.max_closing = 0                    # the most closing-side contracts ever resting beyond the position
        # the account
        self.cbi = cbi
        self.cbi_hold = 0.0
        self.cfm = 0.0
        self.realized = 0.0
        self.pending = 0.0
        self.usdc = 0.0
        self.positions: dict[str, Pos] = {}
        self.orders: dict[str, dict] = {}
        self.cids: dict[str, str] = {}
        self.fills: list[dict] = []
        self.transactions: list[dict] = []
        self.sweeps: list[dict] = []
        self.usd_account = "aaaaaaaa-0000-4000-8000-000000000001"
        self.extra_accounts: list[dict] = []    # more v3 accounts (another portfolio's, say)
        self.extra_v2_accounts: list[dict] = [] # more v2 accounts, listed first (v2 rows don't say their portfolio)
        self.usdc_account = "aaaaaaaa-0000-4000-8000-000000000002"
        self._seq = 0
        self._product = json.loads((FIX / "product.json").read_text())["calls"][0]["response"]
        self.realtime = False                   # True: the fake's clock keeps up with this machine's (the CLI's tests)
        self.funding_now = True                 # the product's funding_time is this hour (the recorded one is fixed)
        self.bars2h: dict[int, tuple] = {}      # TWO_HOUR candles: start (s) -> (open, high, low, close, volume)
        self.book_levels = 30                   # levels each side of the book, 100 contracts each, $5 apart
        self.book_404_when_closed = False       # Coinbase may answer no book at all while closed
        self.session_state: str | None = None   # set: the product answers this session_state (else OPEN / CLOSE)
        self.cancel_delay_s = 0.0               # > 0: a cancel completes this long after it's asked (pending_cancel)
        self.band_side_codes = True             # a price-band refusal answers the spec's ..._PRICE_BAND_ON_SELL/BUY
        self.band_fields = ("error", "new_order_failure_reason", "preview_failure_reason")   # where its code goes
        self.realized_in_cfm = False            # the other reading of the balance: today's P&L booked into cfm at once
        self.server: ThreadingHTTPServer | None = None

    # ---- lifecycle
    def __enter__(self) -> "FakeCoinbase":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

            def do_DELETE(self):
                fake._handle(self, "DELETE")

            def do_PUT(self):
                fake._handle(self, "PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # ---- the clock (shared with the client in tests: its sleeps move it)
    def clock(self) -> float:
        return self.now_s

    def clock_ms(self) -> int:
        return int(self.now_s * 1000)

    def sleep(self, s: float) -> None:
        with self.lock:
            self.now_s += max(0.0, float(s))
            self._tick()

    def _tick(self) -> None:
        """Cancels asked for cancel_delay_s ago complete now."""
        for o in self.orders.values():
            if o.get("pending_cancel") and o["status"] in ("OPEN", "QUEUED") and "_cancel_at" in o and \
                    self.now_s >= o["_cancel_at"]:
                o["pending_cancel"] = False
                o["status"] = "CANCELLED" if self.session_open else "CANCEL_QUEUED"

    def add_key(self, key: FakeKey) -> FakeKey:
        self.keys[key.name] = key
        return key

    def fail_next(self, method: str, path: str, status: int | None = 503, times: int = 1, after: bool = False,
                  body: bytes | None = None) -> None:
        self.fail.append(Rule(method, path, status, times, after, body))

    # ---- what the tests read
    def non_get(self) -> list[dict]:
        return [r for r in self.requests if r["method"] != "GET"]

    def resting(self, product: str = PRODUCT) -> list[dict]:
        return [o for o in self.orders.values() if o["product_id"] == product
                and o["status"] in ("OPEN", "PENDING", "QUEUED", "CANCEL_QUEUED")]

    def stops(self) -> list[dict]:
        return [o for o in self.resting() if "stop_limit_stop_limit_gtc" in o["order_configuration"]]

    def position(self) -> Pos | None:
        return self.positions.get(PRODUCT)

    # ---- money
    def _amt(self, v: float):
        s = f"{v:.2f}" if abs(v) >= 0.005 or v == 0 else f"{v:.6f}"
        return {"value": s, "currency": "USD"} if self.amount_style == "object" else s

    def upnl(self) -> float:
        return sum(p.sign * p.n * CS * (self.last - p.basis) for p in self.positions.values())

    def holds(self) -> float:
        return self.stop_hold_per_contract * sum(int(float(o["order_configuration"]["stop_limit_stop_limit_gtc"]
                                                           ["base_size"])) for o in self.stops())

    def equity(self) -> float:
        """The account's true value: what the desk's formula must come to."""
        return self.cbi + self.cfm + self.upnl() + (0.0 if self.realized_in_cfm else self.realized) + self.pending

    def _tx(self, kind: str, amount: float, status: str = "completed", created_s: float | None = None) -> dict:
        self._seq += 1
        t = created_s if created_s is not None else self.now_s
        tx = {"id": str(uuid.UUID(int=0xFEED0000 + self._seq)), "type": kind, "status": status,
              "amount": {"amount": f"{amount:.2f}", "currency": "USD"},
              "native_amount": {"amount": f"{amount:.2f}", "currency": "USD"},
              "description": None, "created_at": iso(t), "updated_at": iso(t), "resource": "transaction",
              "resource_path": f"/v2/accounts/{self.usd_account}/transactions/x"}
        self.transactions.append(tx)
        return tx

    def deposit(self, usd: float, status: str = "completed", credited: bool = True, hold: bool = False,
                dt: float = 60.0) -> dict:
        with self.lock:
            self.now_s += dt
            if credited:
                self.cbi += usd
            if hold:
                self.cbi_hold += usd
            return self._tx("fiat_deposit", usd, status)

    def release_hold(self) -> None:
        self.cbi_hold = 0.0

    def withdraw(self, usd: float, fee: float = 0.0, signed: bool = True, status: str = "completed",
                 dt: float = 60.0) -> dict:
        with self.lock:
            self.now_s += dt
            self.cbi -= usd + fee
            return self._tx("fiat_withdrawal", -(usd + fee) if signed else usd + fee, status)

    def usdc_sell(self, usd: float, dt: float = 60.0) -> dict:
        with self.lock:
            self.now_s += dt
            self.usdc -= usd
            self.cbi += usd
            return self._tx("fcm_futures_usdc_sell", usd)

    def spot_trade(self, usd: float, dt: float = 60.0) -> dict:
        with self.lock:
            self.now_s += dt
            self.cbi -= usd
            return self._tx("advanced_trade_fill", -usd)

    def set_status(self, tx: dict, status: str, credit: float = 0.0) -> None:
        with self.lock:
            tx["status"] = status
            tx["updated_at"] = iso(self.now_s)
            self.cbi += credit

    def charge_funding(self, rate: float) -> None:
        with self.lock:
            for p in self.positions.values():
                self.realized -= p.sign * p.n * CS * self.last * rate

    def settle(self, price: float | None = None) -> None:
        """Coinbase's daily settlement (3:00 AM ET per its help pages): P&L realised today and the open P&L to the
        settlement price move into the futures account; the open P&L re-bases there. The account's value is unchanged."""
        with self.lock:
            px = self.last if price is None else price
            for p in self.positions.values():
                self.realized += p.sign * p.n * CS * (px - p.basis)
                p.basis = px
            self.cfm += self.realized
            self.realized = 0.0

    def margin_transfer(self, usd: float) -> None:
        """Coinbase moving spot USD into the futures account for margin (inside the account)."""
        with self.lock:
            self.cbi -= usd
            self.cfm += usd

    def sweep_start(self, usd: float) -> None:
        """A sweep of excess futures cash back to spot, in transit."""
        with self.lock:
            self.cfm -= usd
            self.pending += usd

    def sweep_arrive(self) -> dict:
        with self.lock:
            amt, self.pending = self.pending, 0.0
            self.cbi += amt
            return self._tx("derivatives_settlement", amt)

    # ---- the market
    @property
    def bid(self) -> float:
        return self.last - self.spread / 2

    @property
    def ask(self) -> float:
        return self.last + self.spread / 2

    def close_market(self) -> None:
        with self.lock:
            self.session_open = False

    def reopen(self, price: float | None = None, dt: float = 60.0) -> None:
        with self.lock:
            self.session_open = True
            self.no_cancel_phase = False
            for o in self.orders.values():
                if o["status"] == "QUEUED":
                    o["status"] = "OPEN"
                elif o["status"] == "CANCEL_QUEUED":
                    o["status"] = "CANCELLED"
            self.trade(self.last if price is None else price, dt)

    def trade(self, price: float, dt: float = 60.0) -> None:
        """The last trade moves to `price` (one jump: a gap if it's far). While closed nothing trades."""
        with self.lock:
            self.now_s += dt
            if not self.session_open:
                return
            self.last = float(price)
            self.last_trade_s = self.now_s
            self.history.append((self.now_s, self.last))
            for o in sorted(self.orders.values(), key=lambda o: o["created_time"]):
                if o["status"] != "OPEN" or "stop_limit_stop_limit_gtc" not in o["order_configuration"]:
                    continue
                c = o["order_configuration"]["stop_limit_stop_limit_gtc"]
                stop, limit = float(c["stop_price"]), float(c["limit_price"])
                if o["trigger_status"] == "STOP_PENDING":
                    hit = self.last <= stop if o["side"] == "SELL" else self.last >= stop
                    if not hit:
                        continue
                    o["trigger_status"] = "STOP_TRIGGERED"
                left = int(float(c["base_size"])) - int(float(o["filled_size"]))
                px = self.bid if o["side"] == "SELL" else self.ask
                if (o["side"] == "SELL" and px < limit) or (o["side"] == "BUY" and px > limit):
                    continue        # the limit is passed: a stop-limit doesn't fill there
                n = min(left, self.liquidity or left)
                if n > 0:
                    self._fill(o, n, px)
                if int(float(o["filled_size"])) >= int(float(c["base_size"])):
                    o["status"] = "FILLED"

    # ---- fills (no reduce-only: a fill against no position opens one)
    def _fill(self, o: dict, n: int, px: float) -> None:
        sign = 1 if o["side"] == "BUY" else -1
        fee = max(self.fee_per_contract * n, self.fee_rate * n * CS * px)
        p = self.positions.get(o["product_id"])
        if p is None:
            self.positions[o["product_id"]] = Pos("LONG" if sign > 0 else "SHORT", n, px, px)
        elif p.sign == sign:
            tot = p.n + n
            p.entry = (p.entry * p.n + px * n) / tot
            p.basis = (p.basis * p.n + px * n) / tot
            p.n = tot
        else:
            closed = min(n, p.n)
            pnl = p.sign * closed * CS * (px - p.basis)
            self.realized += pnl
            if self.realized_in_cfm:
                self.cfm += pnl
            p.n -= closed
            rest = n - closed
            if p.n == 0:
                del self.positions[o["product_id"]]
            if rest > 0:                                    # through flat and out the other side
                self.positions[o["product_id"]] = Pos("LONG" if sign > 0 else "SHORT", rest, px, px)
        self.realized -= fee
        if self.realized_in_cfm:
            self.cfm -= fee
        self._seq += 1
        size = f"{n * CS:.2f}" if self.fill_units == "btc" else str(n)
        self.fills.append({
            "entry_id": f"e{self._seq:08d}", "trade_id": f"t{self._seq:08d}", "order_id": o["order_id"],
            "trade_time": iso(self.now_s), "trade_type": "FILL", "price": f"{px:.2f}", "size": size,
            "commission": f"{fee:.2f}", "product_id": o["product_id"], "sequence_timestamp": iso(self.now_s),
            "liquidity_indicator": "TAKER", "size_in_quote": False, "user_id": "u-test", "side": o["side"],
            "retail_portfolio_id": "11111111-2222-3333-4444-555555555555"})
        done = float(o["filled_size"]) + n
        avg = (float(o["average_filled_price"] or 0) * float(o["filled_size"]) + px * n) / done
        o.update(filled_size=str(int(done)), average_filled_price=f"{avg:.2f}",
                 total_fees=f"{float(o['total_fees']) + fee:.2f}", number_of_fills=str(int(o["number_of_fills"]) + 1),
                 last_fill_time=iso(self.now_s))
        if "stop_limit_stop_limit_gtc" in o["order_configuration"] and \
                int(o["filled_size"]) >= int(o["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"]):
            o["status"] = "FILLED"
        self._watch()

    def _order(self, body: dict, kind: str, cfg: dict, status: str) -> dict:
        self._seq += 1
        oid = str(uuid.UUID(int=0x0DE70000 + self._seq))
        o = {"order_id": oid, "product_id": body["product_id"], "user_id": "u-test", "order_configuration": cfg,
             "side": body["side"], "client_order_id": body["client_order_id"], "status": status,
             "time_in_force": "IMMEDIATE_OR_CANCEL" if kind == "market" else "GOOD_UNTIL_CANCELLED",
             "created_time": iso(self.now_s), "completion_percentage": "0", "filled_size": "0",
             "average_filled_price": "0", "number_of_fills": "0", "filled_value": "0", "pending_cancel": False,
             "size_in_quote": False, "total_fees": "0", "size_inclusive_of_fees": False,
             "total_value_after_fees": "0", "trigger_status": "STOP_PENDING" if kind == "stop" else
             "INVALID_ORDER_TYPE", "order_type": {"market": "MARKET", "stop": "STOP_LIMIT", "close": "MARKET"}[kind],
             "reject_reason": "", "settled": False, "product_type": "FUTURE", "reject_message": "",
             "cancel_message": "", "order_placement_source": "RETAIL_ADVANCED", "outstanding_hold_amount": "0",
             "is_liquidation": False, "last_fill_time": None, "leverage": "1", "margin_type": "CROSS",
             "retail_portfolio_id": "11111111-2222-3333-4444-555555555555"}
        self.orders[oid] = o
        self.cids[body["client_order_id"]] = oid
        return o

    # ---- the API
    ALL_FIELDS = ("error", "new_order_failure_reason", "preview_failure_reason")

    def _reject(self, code: str, message: str, cfg=None, preview: str | None = None, fields=None):
        """A refusal, as NewOrderErrorResponse: new_order_failure_reason (the current field), and the deprecated error
        and preview_failure_reason (`preview`, else UNKNOWN_PREVIEW_FAILURE_REASON). `fields`: which of the three
        carry their code; the others say UNKNOWN_..."""
        fields = self.ALL_FIELDS if fields is None else fields
        er = {"message": message, "error_details": message,
              "error": code if "error" in fields else "UNKNOWN_FAILURE_REASON",
              "new_order_failure_reason": code if "new_order_failure_reason" in fields else "UNKNOWN_FAILURE_REASON",
              "preview_failure_reason": (preview or "UNKNOWN_PREVIEW_FAILURE_REASON") if "preview_failure_reason" in
              fields else "UNKNOWN_PREVIEW_FAILURE_REASON"}
        return self.reject_status, {"success": False, "failure_reason": "UNKNOWN_FAILURE_REASON", "order_id": "",
                                    "error_response": er, "order_configuration": cfg or {}}

    def _ok_order(self, o: dict):
        return 200, {"success": True, "success_response": {"order_id": o["order_id"], "product_id": o["product_id"],
                                                           "side": o["side"], "client_order_id": o["client_order_id"]},
                     "order_configuration": o["order_configuration"]}

    def _post_order(self, body: dict):
        cid = body.get("client_order_id")
        if not cid:
            return 400, {"error": "INVALID_ARGUMENT", "error_details": "client_order_id is required",
                         "message": "client_order_id is required"}
        if cid in self.cids:                                # "the order corresponding with that ID will be returned"
            return self._ok_order(self.orders[self.cids[cid]])
        cfg = body.get("order_configuration") or {}
        if body.get("product_id") != PRODUCT:
            return self._reject("INVALID_PRODUCT_ID", "invalid product", cfg)
        if body.get("side") not in ("BUY", "SELL") or len(cfg) != 1:
            return self._reject("INVALID_REQUEST", "invalid request", cfg)
        (kind_key, c), = cfg.items()
        size = str(c.get("base_size", ""))
        if not re.fullmatch(r"[1-9][0-9]*", size):
            return self._reject("INVALID_SIZE_PRECISION", "size must be whole contracts", cfg,
                                "PREVIEW_INVALID_SIZE_PRECISION")
        n = int(size)
        if n > 5000:
            return self._reject("INVALID_REQUEST", "too large", cfg, "PREVIEW_INVALID_BASE_SIZE_TOO_LARGE")
        if self.trading_disabled:
            return self._reject("UNTRADABLE_PRODUCT", "trading disabled", cfg)
        if kind_key == "market_market_ioc":
            if not self.session_open:
                return self._reject("FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE", "market orders aren't accepted while "
                                    "the market is closed", cfg, "PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE")
            p = self.positions.get(PRODUCT)
            opening = p is None or (p.sign > 0) == (body["side"] == "BUY")
            need = n * CS * self.ask * 0.21
            avail = (self.cbi - self.cbi_hold) + self.cfm + self.upnl() + self.realized - self.holds()
            if opening and need > avail:
                return self._reject("INSUFFICIENT_FUNDS", "insufficient funds", cfg,
                                    "PREVIEW_INSUFFICIENT_FUNDS_FOR_FUTURES")
            o = self._order(body, "market", cfg, "OPEN")
            k = min(n, self.liquidity if self.liquidity is not None else n)
            if k > 0:
                self._fill(o, k, self.ask if body["side"] == "BUY" else self.bid)
            o["status"] = "FILLED" if k == n else "CANCELLED"
            return self._ok_order(o)
        if kind_key == "stop_limit_stop_limit_gtc":
            try:
                stop, limit = float(c["stop_price"]), float(c["limit_price"])
            except (KeyError, ValueError):
                return self._reject("INVALID_REQUEST", "stop_price and limit_price are required", cfg)
            if stop % 5 or limit % 5:
                return self._reject("INVALID_PRICE_PRECISION", "price must be a multiple of 5", cfg,
                                    "PREVIEW_INVALID_PRICE_PRECISION")
            want = "STOP_DIRECTION_STOP_DOWN" if body["side"] == "SELL" else "STOP_DIRECTION_STOP_UP"
            if c.get("stop_direction") != want:
                return self._reject("INVALID_REQUEST", "stop direction", cfg, "PREVIEW_INVALID_STOP_PRICE")
            if body["side"] == "SELL":
                if stop >= self.last:
                    return self._reject("INVALID_REQUEST", "stop above last", cfg,
                                        "PREVIEW_STOP_PRICE_ABOVE_LAST_TRADE_PRICE")
                if limit > stop:
                    return self._reject("INVALID_REQUEST", "limit above stop", cfg,
                                        "PREVIEW_STOP_PRICE_BELOW_LIMIT_PRICE")
            else:
                if stop <= self.last:
                    return self._reject("INVALID_REQUEST", "stop below last", cfg,
                                        "PREVIEW_STOP_PRICE_BELOW_LAST_TRADE_PRICE")
                if limit < stop:
                    return self._reject("INVALID_REQUEST", "limit below stop", cfg,
                                        "PREVIEW_STOP_PRICE_ABOVE_LIMIT_PRICE")
            if self.band is not None and abs(limit - self.last) / self.last > self.band:
                code = (f"LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_{body['side']}" if self.band_side_codes
                        else "LIMIT_PRICE_TOO_FAR_FROM_MARKET")
                return self._reject(code, "limit too far from market", cfg, "PREVIEW_" + code,
                                    fields=self.band_fields)
            o = self._order(body, "stop", cfg, "OPEN" if self.session_open else "QUEUED")
            self._watch()
            return self._ok_order(o)
        return self._reject("UNSUPPORTED_ORDER_CONFIGURATION", "unsupported", cfg)

    def _close_position(self, body: dict):
        cid = body.get("client_order_id")
        if not cid:
            return 400, {"error": "INVALID_ARGUMENT", "error_details": "client_order_id is required", "message": ""}
        if cid in self.cids:
            return self._ok_order(self.orders[self.cids[cid]])
        p = self.positions.get(body.get("product_id"))
        size = str(body.get("size") or "")
        if not size:
            return self._reject("INVALID_REQUEST", "market close with an empty size", {})
        if p is None:
            return self._reject("INVALID_REQUEST", "no open position", {})
        n = int(size)
        if n > p.n:
            return self._reject("INVALID_REQUEST", "size exceeds position", {})
        if not self.session_open:
            return self._reject("FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE", "closed", {},
                                "PREVIEW_FUTURES_AFTER_HOUR_INVALID_ORDER_TYPE")
        side = "SELL" if p.side == "LONG" else "BUY"
        cfg = {"market_market_ioc": {"base_size": str(n)}}
        o = self._order({**body, "side": side}, "close", cfg, "OPEN")
        k = min(n, self.liquidity if self.liquidity is not None else n)
        if k > 0:
            self._fill(o, k, self.bid if side == "SELL" else self.ask)
        o["status"] = "FILLED" if k == n else "CANCELLED"
        return self._ok_order(o)

    def _watch(self) -> None:
        """Keep the worst moment: how many stops rested at once, and by how many contracts the closing stops
        resting were more than the position (with no reduce-only, that much would open the other way)."""
        st = self.stops()
        self.max_stops = max(self.max_stops, len(st))
        p = self.positions.get(PRODUCT)
        held = p.n if p else 0
        closing = "SELL" if (p and p.side == "LONG") else "BUY"
        resting = sum(int(o["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"]) for o in st
                      if not p or o["side"] == closing)
        self.max_closing = max(self.max_closing, resting - held)

    def _batch_cancel(self, body: dict):
        if self.on_cancel is not None:
            hook, self.on_cancel = self.on_cancel, None
            hook()
        out = []
        for oid in body.get("order_ids") or []:
            o = self.orders.get(oid)
            if o is None:
                out.append({"success": False, "failure_reason": "UNKNOWN_CANCEL_ORDER", "order_id": oid})
            elif o["status"] == "FILLED":
                out.append({"success": False, "failure_reason": "ORDER_IS_FULLY_FILLED", "order_id": oid})
            elif o["status"] in ("CANCELLED", "EXPIRED", "FAILED", "CANCEL_QUEUED"):
                out.append({"success": False, "failure_reason": "DUPLICATE_CANCEL_REQUEST", "order_id": oid})
            elif self.no_cancel_phase:
                out.append({"success": False, "failure_reason": "INVALID_CANCEL_FCM_TRADING_SESSION",
                            "order_id": oid})
            elif not self.session_open:
                o["status"] = "CANCEL_QUEUED"
                out.append({"success": True, "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON", "order_id": oid})
            elif self.cancel_delay_s > 0:
                o["pending_cancel"] = True          # taken, not done: it still rests (and could fill) meanwhile
                o["_cancel_at"] = self.now_s + self.cancel_delay_s
                out.append({"success": True, "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON", "order_id": oid})
            else:
                o["status"] = "CANCELLED"
                out.append({"success": True, "failure_reason": "UNKNOWN_CANCEL_FAILURE_REASON", "order_id": oid})
        return 200, {"results": out}

    def _balance(self):
        a = self._amt
        upnl, holds = self.upnl(), self.holds()
        avail = (self.cbi - self.cbi_hold) + self.cfm + upnl + (0.0 if self.realized_in_cfm else self.realized) - holds
        im = sum(p.n * CS * self.last * 0.21 for p in self.positions.values())
        s = {"futures_buying_power": a(max(0.0, avail - im)), "total_usd_balance": a(self.cbi + self.cfm),
             "cbi_usd_balance": a(self.cbi), "cfm_usd_balance": a(self.cfm),
             "total_open_orders_hold_amount": a(holds), "unrealized_pnl": a(upnl),
             "daily_realized_pnl": a(self.realized), "initial_margin": a(im), "available_margin": a(avail),
             "liquidation_threshold": a(im / 2), "liquidation_buffer_amount": a(avail - im / 2),
             "liquidation_buffer_percentage": f"{(avail - im / 2) / avail * 100 if avail else 0:.2f}",
             "intraday_margin_window_measure": {"margin_window_type": "FCM_MARGIN_WINDOW_TYPE_INTRADAY",
                                                "margin_level": "MARGIN_LEVEL_TYPE_BASE", "initial_margin": "0",
                                                "maintenance_margin": "0", "liquidation_buffer": "0",
                                                "total_hold": "0", "futures_buying_power": "0"},
             "overnight_margin_window_measure": {"margin_window_type": "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                                                 "margin_level": "MARGIN_LEVEL_TYPE_BASE", "initial_margin": "0",
                                                 "maintenance_margin": "0", "liquidation_buffer": "0",
                                                 "total_hold": "0", "futures_buying_power": "0"}}
        if self.pending_field:
            s["total_pending_transfers_amount"] = a(self.pending)
            s["funding_pnl"] = a(0.0)
        return 200, {"balance_summary": s}

    def _position_row(self, pid: str, p: Pos) -> dict:
        return {"product_id": pid, "expiration_time": "2089-12-30T16:00:00Z", "side": p.side,
                "number_of_contracts": str(p.n), "current_price": f"{self.last:.2f}",
                "avg_entry_price": f"{p.entry:.2f}", "unrealized_pnl": f"{p.sign * p.n * CS * (self.last - p.basis):.2f}",
                "daily_realized_pnl": f"{self.realized:.2f}"}

    def _orders_list(self, q: dict):
        statuses = q.get("order_status") or []
        if "OPEN" in statuses and len(statuses) > 1:
            return 400, {"error": "INVALID_ARGUMENT", "error_details": "cannot pair OPEN orders with other order "
                                                                      "statuses", "message": "invalid"}
        rows = sorted(self.orders.values(), key=lambda o: o["created_time"], reverse=True)
        src = (q.get("order_placement_source") or ["RETAIL_ADVANCED"])[0]     # "By default, returns RETAIL_ADVANCED"
        rows = [o for o in rows if o.get("order_placement_source", "RETAIL_ADVANCED") == src]
        if statuses:
            rows = [o for o in rows if o["status"] in statuses]
        if q.get("product_ids"):
            rows = [o for o in rows if o["product_id"] in q["product_ids"]]
        if q.get("start_date") and "OPEN" not in statuses:
            t0 = from_iso(q["start_date"][0])
            rows = [o for o in rows if from_iso(o["created_time"]) >= t0]
        start = int((q.get("cursor") or ["0"])[0] or 0)
        size = min(int((q.get("limit") or [self.page_size])[0]), self.page_size)
        page = rows[start:start + size]
        more = start + size < len(rows)
        return 200, {"orders": [self._public_order(o) for o in page], "sequence": "0", "has_next": more,
                     "cursor": str(start + size) if more else ""}

    @staticmethod
    def _public_order(o: dict) -> dict:
        return {k: v for k, v in copy.deepcopy(o).items() if not k.startswith("_")}

    def _fills_list(self, q: dict):
        rows = list(reversed(self.fills))
        if q.get("product_ids"):
            rows = [f for f in rows if f["product_id"] in q["product_ids"]]
        if q.get("order_ids"):
            rows = [f for f in rows if f["order_id"] in q["order_ids"]]
        if q.get("start_sequence_timestamp"):
            t0 = from_iso(q["start_sequence_timestamp"][0])
            rows = [f for f in rows if from_iso(f["sequence_timestamp"]) >= t0]
        start = int((q.get("cursor") or ["0"])[0] or 0)
        size = min(int((q.get("limit") or [self.page_size])[0]), self.page_size)
        page = rows[start:start + size]
        more = start + size < len(rows)
        return 200, {"fills": copy.deepcopy(page), "cursor": str(start + size) if more else ""}

    def _product_answer(self) -> dict:
        d = copy.deepcopy(self._product)
        d["price"] = f"{self.last:.0f}"
        d["mid_market_price"] = f"{self.last:.0f}"
        d["trading_disabled"] = self.trading_disabled
        d["fcm_trading_session_details"]["is_session_open"] = self.session_open
        d["fcm_trading_session_details"]["session_state"] = self.session_state or (
            "FCM_TRADING_SESSION_STATE_OPEN" if self.session_open else "FCM_TRADING_SESSION_STATE_CLOSE")
        if self.funding_now:
            d["future_product_details"]["funding_time"] = iso(self.now_s - self.now_s % 3600)

        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v
        merge(d, self.product_edits)
        return d

    def _v2_page(self, rows: list, q: dict, path: str):
        limit = min(int((q.get("limit") or ["25"])[0]), 100)
        after = (q.get("starting_after") or [None])[0]
        start = 0
        if after:
            ids = [r["id"] for r in rows]
            start = ids.index(after) + 1 if after in ids else len(rows)
        page = rows[start:start + limit]
        more = start + limit < len(rows)
        nxt = f"{path}?limit={limit}&starting_after={page[-1]['id']}" if more and page else None
        return 200, {"pagination": {"ending_before": None, "starting_after": after, "limit": limit, "order": "desc",
                                    "previous_uri": None, "next_uri": nxt,
                                    "next_starting_after": page[-1]["id"] if more and page else None},
                     "data": copy.deepcopy(page)}

    PERM = {("POST", f"{V3}/orders"): "trade", ("POST", f"{V3}/orders/batch_cancel"): "trade",
            ("POST", f"{V3}/orders/close_position"): "trade", ("POST", f"{V3}/orders/edit"): "trade",
            ("POST", f"{V3}/orders/preview"): "view", ("POST", f"{V3}/cfm/intraday/margin_setting"): "trade",
            ("POST", f"{V3}/cfm/sweeps/schedule"): "transfer", ("DELETE", f"{V3}/cfm/sweeps"): "transfer"}

    def _route(self, method: str, path: str, q: dict, body: dict | None, key: FakeKey):
        need = self.PERM.get((method, path), "view")
        if not getattr(key, need):
            return 403, {"error": "PERMISSION_DENIED", "error_details": "Missing required scopes",
                         "message": "Missing required scopes"}
        if method == "POST":
            if path == f"{V3}/orders":
                return self._post_order(body or {})
            if path == f"{V3}/orders/close_position":
                return self._close_position(body or {})
            if path == f"{V3}/orders/batch_cancel":
                return self._batch_cancel(body or {})
            if path == f"{V3}/orders/edit":
                return 200, {"success": False, "errors": [{"edit_failure_reason": "CANNOT_EDIT_FUTURES_ORDER"}]}
            return 200, {"success": True}
        if method != "GET":
            return 200, {"success": True}
        if path == f"{V3}/key_permissions":
            return 200, {"can_view": key.view, "can_trade": key.trade, "can_transfer": key.transfer,
                         "portfolio_uuid": key.portfolio_uuid, "portfolio_type": key.portfolio_type}
        if path == f"{V3}/cfm/balance_summary":
            return self._balance()
        if path == f"{V3}/cfm/positions":
            return 200, {"positions": [self._position_row(k, p) for k, p in self.positions.items()]}
        m = re.fullmatch(rf"{V3}/cfm/positions/(.+)", path)
        if m:
            p = self.positions.get(m.group(1))
            return 200, {"position": self._position_row(m.group(1), p) if p else {}}
        if path == f"{V3}/cfm/sweeps":
            return 200, {"sweeps": copy.deepcopy(self.sweeps)}
        if path == f"{V3}/cfm/intraday/margin_setting":
            return 200, {"setting": "INTRADAY_MARGIN_SETTING_STANDARD"}
        if path == f"{V3}/cfm/intraday/current_margin_window":
            return 200, {"margin_window": {"margin_window_type": "FCM_MARGIN_WINDOW_TYPE_OVERNIGHT",
                                           "end_time": iso(self.now_s + 3600)},
                         "is_intraday_margin_killswitch_enabled": False,
                         "is_intraday_margin_enrollment_killswitch_enabled": False}
        if path == f"{V3}/orders/historical/batch":
            return self._orders_list(q)
        if path == f"{V3}/orders/historical/fills":
            return self._fills_list(q)
        m = re.fullmatch(rf"{V3}/orders/historical/(.+)", path)
        if m:
            o = self.orders.get(m.group(1))
            if o is None:
                return 404, {"error": "NOT_FOUND", "error_details": "order with this orderID was not found",
                             "message": "order with this orderID was not found"}
            return 200, {"order": self._public_order(o)}
        if path == f"{V3}/transaction_summary":
            return 200, {"total_volume": 0, "total_fees": 0, "fee_tier": {
                "pricing_tier": "Advanced 1", "usd_from": "0", "usd_to": "10000", "taker_fee_rate": "0.0005",
                "maker_fee_rate": "0.0002", "aop_from": "", "aop_to": "", "perps_vol_from": "", "perps_vol_to": ""},
                "margin_rate": {"value": "0.21"}, "goods_and_services_tax": None,
                "advanced_trade_only_volume": 0, "advanced_trade_only_fees": 0, "coinbase_pro_volume": 0,
                "coinbase_pro_fees": 0, "total_balance": "0", "has_promo_fee": False}
        if path == f"{V3}/accounts":
            pid = key.portfolio_uuid
            rows = [{"uuid": self.usd_account, "name": "Cash (USD)", "currency": "USD",
                     "available_balance": {"value": f"{self.cbi - self.cbi_hold:.2f}", "currency": "USD"},
                     "hold": {"value": f"{self.cbi_hold:.2f}", "currency": "USD"}, "default": True, "active": True,
                     "type": "ACCOUNT_TYPE_FIAT", "ready": True, "retail_portfolio_id": pid,
                     "platform": "ACCOUNT_PLATFORM_CONSUMER", "created_at": iso(T0 - 86400), "updated_at": iso(T0),
                     "deleted_at": None},
                    {"uuid": self.usdc_account, "name": "USDC Wallet", "currency": "USDC",
                     "available_balance": {"value": f"{self.usdc:.2f}", "currency": "USDC"},
                     "hold": {"value": "0", "currency": "USDC"}, "default": False, "active": True,
                     "type": "ACCOUNT_TYPE_CRYPTO", "ready": True, "retail_portfolio_id": pid,
                     "platform": "ACCOUNT_PLATFORM_CONSUMER", "created_at": iso(T0 - 86400), "updated_at": iso(T0),
                     "deleted_at": None},
                    {"uuid": "aaaaaaaa-0000-4000-8000-000000000003", "name": "Futures (USD)", "currency": "USD",
                     "available_balance": {"value": f"{self.cfm:.2f}", "currency": "USD"},
                     "hold": {"value": "0", "currency": "USD"}, "default": False, "active": True,
                     "type": "ACCOUNT_TYPE_FIAT", "ready": True, "retail_portfolio_id": pid,
                     "platform": "ACCOUNT_PLATFORM_CFM_CONSUMER", "created_at": iso(T0 - 86400),
                     "updated_at": iso(T0), "deleted_at": None}] + copy.deepcopy(self.extra_accounts)
            return 200, {"accounts": rows, "has_next": False, "cursor": "", "size": len(rows)}
        if path == f"{V3}/portfolios":
            return 200, {"portfolios": [{"name": "Default", "uuid": key.portfolio_uuid, "type": "DEFAULT",
                                         "deleted": False}]}
        if path == f"{V3}/products/{PRODUCT}":
            return 200, self._product_answer()
        if path == f"{V3}/product_book":
            return self._book_answer()
        if path == f"{V3}/products/{PRODUCT}/candles" and q["granularity"][0] == "TWO_HOUR":
            lo, hi = int(q["start"][0]), int(q["end"][0])
            rows = [(t, b) for t, b in sorted(self.bars2h.items(), reverse=True) if lo <= t <= hi][:300]
            return 200, {"candles": [{"start": str(t), "open": f"{b[0]:g}", "high": f"{b[1]:g}", "low": f"{b[2]:g}",
                                      "close": f"{b[3]:g}", "volume": f"{b[4]:g}"} for t, b in rows]}
        if path == f"{V3}/products/{PRODUCT}/candles":
            sec = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "ONE_HOUR": 3600}[q["granularity"][0]]
            lo, hi = int(q["start"][0]), int(q["end"][0])
            buckets: dict[int, list[float]] = {}
            for t, px in self.history:
                b = int(t) - int(t) % sec
                if lo <= b <= hi:
                    buckets.setdefault(b, []).append(px)
            return 200, {"candles": [{"start": str(b), "low": str(min(v)), "high": str(max(v)), "open": str(v[0]),
                                      "close": str(v[-1]), "volume": "1"} for b, v in sorted(buckets.items(),
                                                                                         reverse=True)]}
        if path == f"{V3}/products/{PRODUCT}/ticker":
            return 200, {"trades": [{"trade_id": "1", "product_id": PRODUCT, "price": f"{self.last:.0f}", "size": "1",
                                     "time": iso(self.last_trade_s), "side": "BUY", "bid": "", "ask": "",
                                     "exchange": "cde"}], "best_bid": f"{self.bid:.0f}", "best_ask": f"{self.ask:.0f}"}
        if path == f"{V3}/best_bid_ask":
            if not self.session_open:
                return 200, {"pricebooks": [{"product_id": PRODUCT, "bids": [], "asks": [],
                                             "time": iso(self.last_trade_s)}]}
            return 200, {"pricebooks": [{"product_id": PRODUCT, "bids": [{"price": f"{self.bid:.0f}", "size": "50"}],
                                         "asks": [{"price": f"{self.ask:.0f}", "size": "50"}],
                                         "time": iso(self.now_s)}]}
        if path == "/v2/accounts":
            rows = [{"id": self.usd_account, "name": "USD Wallet", "primary": True, "type": "fiat",
                     "currency": {"code": "USD", "name": "US Dollar"},
                     "balance": {"amount": f"{self.cbi:.2f}", "currency": "USD"}, "resource": "account"},
                    {"id": self.usdc_account, "name": "USDC Wallet", "primary": False, "type": "wallet",
                     "currency": {"code": "USDC", "name": "USDC"},
                     "balance": {"amount": f"{self.usdc:.2f}", "currency": "USDC"}, "resource": "account"}]
            return self._v2_page(copy.deepcopy(self.extra_v2_accounts) + rows, q, path)
        m = re.fullmatch(r"/v2/accounts/([^/]+)/transactions", path)
        if m:
            if m.group(1) != self.usd_account:
                return 404, {"errors": [{"id": "not_found", "message": "Not found"}]}
            return self._v2_page(list(reversed(self.transactions)), q, path)
        return 404, {"error": "NOT_FOUND", "error_details": "Not Found", "message": "Not Found"}

    def _book_answer(self):
        """The product's book: book_levels levels a side, 100 contracts each, $5 apart from the best bid and ask.
        Empty while the market is closed (Coinbase may answer none then)."""
        if not self.session_open:
            if self.book_404_when_closed:
                return 404, {"error": "NOT_FOUND", "error_details": "no pricebook found", "message": "no pricebook found"}
            return 200, {"pricebook": {"product_id": PRODUCT, "bids": [], "asks": [], "time": iso(self.now_s)}}
        bids = [{"price": f"{self.bid - 5 * k:.0f}", "size": "100"} for k in range(self.book_levels)]
        asks = [{"price": f"{self.ask + 5 * k:.0f}", "size": "100"} for k in range(self.book_levels)]
        return 200, {"pricebook": {"product_id": PRODUCT, "bids": bids, "asks": asks, "time": iso(self.now_s)}}

    PUBLIC = {f"{V3}/market/products/{PRODUCT}", f"{V3}/market/products/{PRODUCT}/candles", f"{V3}/market/product_book",
              f"{V3}/market/products/{PRODUCT}/ticker"}

    def _auth(self, method: str, path: str, header: str) -> FakeKey | None:
        if not header.startswith("Bearer "):
            return None
        tok = header[7:]
        try:
            head = jwt.get_unverified_header(tok)
            key = self.keys.get(head.get("kid"))
            if key is None or head.get("alg") != key.alg or head.get("typ") != "JWT":
                return None
            if not re.fullmatch(r"[0-9a-f]{64}", str(head.get("nonce") or "")) or head["nonce"] in self.nonces:
                return None
            claims = jwt.decode(tok, key.public, algorithms=[key.alg],
                                options={"verify_exp": False, "verify_nbf": False, "verify_aud": False,
                                         "require": ["sub", "iss", "nbf", "exp", "uri"]})
        except Exception:
            return None
        if claims["sub"] != key.name or claims["iss"] != "cdp" or "aud" in claims:
            return None
        if claims["exp"] - claims["nbf"] != 120 or not claims["nbf"] <= self.now_s + 1.0 < claims["exp"] + 1.0:
            return None
        if claims["uri"] != f"{method} api.coinbase.com{path}":
            return None
        self.nonces.add(head["nonce"])
        return key

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        u = urlsplit(h.path)
        path, q = u.path, parse_qs(u.query)
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        with self.lock:
            if self.realtime:
                import time

                self.now_s = max(self.now_s, time.time())
            rec = {"method": method, "path": path, "query": q, "body": body, "raw": raw, "key": None,
                   "status": None, "ts": self.now_s}
            self.requests.append(rec)
            if method == "GET" and path == f"{V3}/time":
                return self._send(h, rec, 200, {"iso": iso(self.now_s), "epochSeconds": str(int(self.now_s)),
                                                "epochMillis": str(int(self.now_s * 1000))})
            if method == "GET" and path in self.PUBLIC:        # the public market data paper reads, with no key
                status, out = self._route("GET", path.replace("/market", "", 1), q, None,
                                          FakeKey("public", "none", None, None))
                return self._send(h, rec, status, out)
            self._tick()
            key = self._auth(method, path, h.headers.get("Authorization", ""))
            if key is None or (path.startswith("/v2/") and key.alg == "EdDSA"):
                return self._send(h, rec, 401, b"Unauthorized\n")
            rec["key"] = key.name
            rule = next((r for r in self.fail if r.method == method and path.startswith(r.path) and r.times > 0),
                        None)
            if rule is not None:
                rule.times -= 1
                if not rule.after:
                    return self._fail(h, rec, rule)
            status, out = self._route(method, path, q, body, key)
            if rule is not None:
                return self._fail(h, rec, rule)
            return self._send(h, rec, status, out)

    def _fail(self, h, rec, rule: Rule) -> None:
        if rule.status is None:
            rec["status"] = "dropped"
            h.close_connection = True
            return
        if rule.body is not None:
            return self._send(h, rec, rule.status, rule.body, ctype="text/html")
        if rule.status == 429:
            return self._send(h, rec, 429, {"errors": [{"id": "rate_limit_exceeded", "message": "Too many requests"}]})
        return self._send(h, rec, rule.status, b"<html>upstream error</html>", ctype="text/html")

    def _send(self, h, rec, status: int, out, ctype: str | None = None) -> None:
        rec["status"] = status
        if isinstance(out, bytes):
            data, ctype = out, ctype or "text/plain; charset=utf-8"
        else:
            data, ctype = json.dumps(out).encode(), ctype or "application/json"
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)


def good_proof(now_ms: int, days: int = 20, portfolio_uuid: str = "11111111-2222-3333-4444-555555555555") -> dict:
    """A SHADOW record that passes every arming check (the tests' own: SHADOW itself writes the real one)."""
    from datetime import timedelta

    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    rows = {(today - timedelta(days=k)).isoformat(): {"runs": 90, "ok": 90, "funded": True} for k in range(days)}
    return {"started_ts": now_ms - days * 86400_000, "last_run_ts": now_ms - 600_000, "portfolio_type": "DEFAULT",
            "portfolio_uuid": portfolio_uuid, "deposits_reconciled": 1, "days": rows}


ARM_ENV = {"DESK_LIVE_CONFIRM": "I understand this trades real money and can lose all of it",
           "DESK_LIVE_COINBASE": PRODUCT}


# ---------------------------------------------------------------- the desk's side, wired to the fake

def view_rest(fake: FakeCoinbase, fk: FakeKey | None = None, **kw):
    """A read-only CoinbaseREST on the fake (the kind SHADOW holds)."""
    from desk.brokers.coinbase_auth import load_cdp_key
    from desk.brokers.coinbase_rest import CoinbaseREST

    fk = fake.add_key(fk or make_ec_key())
    kw.setdefault("clock", fake.clock)
    kw.setdefault("sleep", fake.sleep)
    kw.setdefault("min_gap_s", 0)
    return CoinbaseREST(load_cdp_key(fk.file_json()), base_url=fake.base_url, **kw)


def arm_rest(fake: FakeCoinbase, fk: FakeKey | None = None, *, cap: float = 1_000.0, live_state=None, **kw):
    """An order-capable CoinbaseREST on the fake, through coinbase_live.arm() and every one of its checks."""
    from desk import coinbase_live
    from desk.brokers.coinbase_auth import load_cdp_key

    fk = fake.add_key(fk or make_ec_key("test-trade-key-not-real", trade=True))
    out: list[str] = []
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), env=ARM_ENV, max_notional=cap,
                            gate=[("backtest: passes", True, "ok")], shadow_proof=good_proof(fake.clock_ms()),
                            live_state=live_state, base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep,
                            out=out.append, **kw)
    assert got is not None, "\n".join(out)
    got[0].min_gap_s = 0
    return got[0]


# ---------------------------------------------------------------- 2h candles for the desk to decide on

def seed_bars(fake: FakeCoinbase, bars) -> None:
    """The 2h candles the fake serves: desk Bars (ts in ms)."""
    fake.bars2h = {int(b.ts // 1000): (b.open, b.high, b.low, b.close, b.volume or 1.0) for b in bars}


def trend_bars(end_s: float, n: int = 1600, *, seed: int = 3, last: float = 83_000.0, breakout: bool = False):
    """n closed 2h candles ending at the last close before `end_s`: a steady uptrend with noise ending near `last`
    (the 100-EMA above the 400-EMA). breakout: the newest close takes out the prior 60-candle high by 1.5%."""
    import math
    import random

    from desk.models import Bar

    rnd = random.Random(seed)
    bar = 7200
    t_end = int(end_s) - int(end_s) % bar - bar          # the newest closed candle's open
    closes = [1.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * math.exp(rnd.gauss(0.0006, 0.009)))
    scale = last / closes[-1]
    out = []
    prev = closes[0] * scale
    for k, c in enumerate(closes):
        c *= scale
        o = prev
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.002)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        out.append(Bar((t_end - (n - 1 - k) * bar) * 1000, round(o), round(h), round(lo), round(c), 100.0))
        prev = c
    if breakout:
        hi = max(b.high for b in out[-61:-1])
        c = round(hi * 1.015)
        b = out[-1]
        out[-1] = Bar(b.ts, b.open, max(b.high, c + 20), b.low, c, 100.0)
    return out


def next_bar(bars, close: float, *, low: float | None = None, high: float | None = None):
    """One more 2h candle after `bars`, opening at the last close."""
    from desk.models import Bar

    b = bars[-1]
    o = b.close
    return bars + [Bar(b.ts + 7_200_000, o, max(o, close, high or 0), min(o, close, low or 1e18), close, 100.0)]
