"""The desk's Coinbase codes and the fake's answers against Coinbase's own Advanced Trade OpenAPI spec.

tests/fixtures/coinbase_spec/enums.json holds the spec's enums (OrderExecutionStatus, NewOrderFailureReason,
PreviewFailureReason, CancelOrderFailureReason, FcmTradingSessionState, OrderPlacementSource, ...), copied from
https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/advanced-trade-spec.yaml with its date and
sha256. The code was first written from an older sample SDK's types and missed documented values (a price band's
LIMIT_ORDER_PRICE_EXCEEDS_PRICE_BAND_ON_SELL, EDIT_QUEUED, HALTED), and the fake spoke the same old values, so its tests
passed. Here: every code the desk acts on is documented, every documented one of a kind the desk handles is handled,
and the fake answers only documented values. Re-fetch the spec and regenerate the fixture before LIVE is armed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk.brokers import coinbase as cb
from desk.brokers import coinbase_orders as co
from fake_coinbase import PRODUCT, V3, FakeCoinbase, arm_rest, dcid

SPEC = json.loads((Path(__file__).parent / "fixtures" / "coinbase_spec" / "enums.json").read_text())
E = {k: set(v or ()) for k, v in SPEC["enums"].items()}
ORDER_CODES = E["NewOrderFailureReason"] | E["PreviewFailureReason"]


def test_the_fixture_says_where_it_came_from():
    assert SPEC["source"].startswith("https://docs.cdp.coinbase.com/") and SPEC["fetched"] == "2026-10-14"
    assert len(SPEC["spec_sha256"]) == 64 and all(E[k] for k in ("OrderExecutionStatus", "NewOrderFailureReason",
                                                                  "CancelOrderFailureReason", "FcmTradingSessionState"))


def test_every_order_status_is_handled_and_unknown_raises():
    assert set(co.OPEN_STATUSES) == E["OrderExecutionStatus"] - {"UNKNOWN_ORDER_STATUS"}
    with pytest.raises(Exception):
        co.parse_order({"order_id": "x", "status": "UNKNOWN_ORDER_STATUS"})


def test_the_refusal_codes_the_desk_acts_on_are_documented_and_complete():
    for name in ("BAND_CODES", "THROUGH_CODES", "SESSION_CODES"):
        assert set(getattr(cb, name)) <= ORDER_CODES, name
    band = {c for c in ORDER_CODES if "PRICE_BAND" in c or "TOO_FAR_FROM_MARKET" in c or
            c.endswith("STOP_TRIGGER_PRICE_OUT_OF_BOUNDS")}
    through = {c for c in ORDER_CODES if "STOP_PRICE_ABOVE_LAST_TRADE" in c or "STOP_PRICE_BELOW_LAST_TRADE" in c or
               c.endswith("STOP_TRIGGERED")}
    session = {c for c in ORDER_CODES if "FCM_TRADING_SESSION" in c or "FUTURES_AFTER_HOUR" in c}
    assert band <= set(cb.BAND_CODES) and through <= set(cb.THROUGH_CODES) and session <= set(cb.SESSION_CODES)
    # error and preview_failure_reason are Deprecated in NewOrderErrorResponse: the desk reads every field
    assert set(SPEC["new_order_error_response_deprecated"]) == {"error", "preview_failure_reason"}


def test_cancel_reasons_and_session_states_and_sources_are_documented():
    assert set(cb.CANCEL_CODES) <= E["CancelOrderFailureReason"]
    assert {cb.SESSION_OPEN_STATE, cb.SESSION_HALTED, cb.SESSION_NO_CANCEL} <= E["FcmTradingSessionState"]
    assert {co.DESK_SOURCE, *cb.OTHER_SOURCES} <= E["OrderPlacementSource"]
    assert SPEC["list_orders_order_placement_source"]["default"] == co.DESK_SOURCE


def _walk(x, out):
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "status" and "order_id" not in x:
                continue            # a product's or a transaction's status, not an order's
            if isinstance(v, str):
                out.setdefault(k, set()).add(v)
            else:
                _walk(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk(v, out)


FIELD_ENUM = {"status": "OrderExecutionStatus", "failure_reason": None, "new_order_failure_reason": "orders",
              "preview_failure_reason": "orders", "session_state": "FcmTradingSessionState",
              "order_placement_source": "OrderPlacementSource", "platform": "AccountPlatform",
              "portfolio_type": "PortfolioType", "trigger_status": "StopTriggerStatus", "order_type": "OrderType",
              "time_in_force": "TimeInForceType", "stop_direction": "StopPriceDirection", "margin_type": "MarginType",
              "product_type": "ProductType"}


def test_the_fake_answers_only_documented_values():
    """Drive the fake through what the tests make it do (orders, a band refusal, a stop through the market, cancels
    open, queued and pending, a closed session) and check every enum-valued field it answered."""
    seen: dict = {}
    with FakeCoinbase(cbi=1_000.0) as fake:
        real_send = fake._send

        def send(h, rec, status, out, ctype=None):
            if isinstance(out, dict) and rec["path"].startswith(V3) and rec["path"] != f"{V3}/key_permissions":
                _walk(out, seen)
            return real_send(h, rec, status, out, ctype)
        fake._send = send
        rest = arm_rest(fake)
        ex = cb.CoinbaseExchange(rest, on_alert=lambda t: None)
        ex.load_markets()
        ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": dcid("e")})
        with pytest.raises(cb.StopThroughMarket):
            ex.create_order(PRODUCT, "market", "sell", 1, 84_000, {"clientOrderId": dcid("t"), "stopLossPrice": 84_000})
        fake.band = 0.06
        s = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": dcid("s"), "stopLossPrice": 80_000})
        fake.cancel_delay_s = 2.0
        ex.cancel_order(s["id"])
        fake.cancel_delay_s = 0.0
        s2 = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": dcid("s2"),
                                                                    "stopLossPrice": 80_000})
        fake.close_market()
        ex.load_markets(reload=True)
        with pytest.raises(cb.SessionClosed):
            ex.create_order(PRODUCT, "market", "sell", 1, 83_000, {"clientOrderId": dcid("c"), "reduceOnly": True})
        ex.cancel_order(s2["id"])
        fake.reopen()
        ex.fetch_open_orders(PRODUCT)
        ex.wallets()
        rest.get(f"{V3}/orders/historical/batch", {"order_status": ["CANCELLED", "FILLED"]})
    checked = 0
    for field, enum in FIELD_ENUM.items():
        for v in seen.get(field, ()):
            if field == "failure_reason":
                ok = v in E["CancelOrderFailureReason"] or v in ORDER_CODES or v == "UNKNOWN_FAILURE_REASON"
            elif enum == "orders":
                ok = v in ORDER_CODES or v in ("UNKNOWN_FAILURE_REASON", "UNKNOWN_PREVIEW_FAILURE_REASON")
            else:
                ok = v in E[enum]
            assert ok, f"the fake answers {field} = {v!r}, which Coinbase's spec doesn't list"
            checked += 1
    assert checked >= 15 and "FCM_TRADING_SESSION_STATE_CLOSE" in seen["session_state"]
    assert {"CANCELLED", "FILLED", "OPEN"} <= seen["status"]
