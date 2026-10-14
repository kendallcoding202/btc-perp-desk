"""The exact Coinbase request bodies LIVE sends and SHADOW records (desk/brokers/coinbase_orders.py), and the parsers
for Coinbase's answers. Pure functions: no network."""

from __future__ import annotations

import re

import pytest

from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase_rest import CoinbaseDataError
from desk.brokers.live import UnknownLedgerEntry

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def test_entry_body_is_a_whole_contract_market_ioc():
    assert co.market("cid-1", "BUY", 3) == {
        "client_order_id": "cid-1", "product_id": "BIP-20DEC30-CDE", "side": "BUY",
        "order_configuration": {"market_market_ioc": {"base_size": "3"}}}
    for bad in (0, 0.5, 1.5, 5001, -1):
        with pytest.raises(ValueError):
            co.market("c", "BUY", bad)
    with pytest.raises(ValueError):
        co.market("c", "LONG", 1)


def test_a_long_s_stop_is_a_sell_stop_limit_down_with_its_limit_5pct_below_floored_to_5():
    body = co.stop_limit("cid-2", "SELL", 1, 80_782.0)
    assert body == {"client_order_id": "cid-2", "product_id": "BIP-20DEC30-CDE", "side": "SELL",
                    "order_configuration": {"stop_limit_stop_limit_gtc": {
                        "base_size": "1", "stop_price": "80780", "limit_price": "76740",
                        "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}}
    # 80,780 x 0.95 = 76,741 -> floored to 76,740


def test_a_short_s_stop_is_a_buy_stop_limit_up_with_its_limit_5pct_above_ceiled_to_5():
    c = co.stop_limit("cid-3", "BUY", 2, 85_003.0)["order_configuration"]["stop_limit_stop_limit_gtc"]
    assert c == {"base_size": "2", "stop_price": "85005", "limit_price": "89260",
                 "stop_direction": "STOP_DIRECTION_STOP_UP"}            # 85,005 x 1.05 = 89,255.25 -> 89,260
    assert co.stop_limit_prices("SELL", 80_000, 0.02) == (80_000, 78_400, "STOP_DIRECTION_STOP_DOWN")


def test_every_price_sent_is_a_multiple_of_5_and_every_size_a_whole_number():
    for px in (80_000.0, 80_002.4, 80_002.5, 80_004.99, 83_141.0, 99_999.9):
        for side in ("SELL", "BUY"):
            c = co.stop_limit("c", side, 1, px)["order_configuration"]["stop_limit_stop_limit_gtc"]
            assert int(c["stop_price"]) % 5 == 0 and int(c["limit_price"]) % 5 == 0
            assert re.fullmatch(r"[1-9][0-9]*", c["base_size"])
    assert co.round_price(80_002.4) == 80_000 and co.round_price(80_002.5) == 80_005


def test_close_position_always_carries_its_size_and_batch_cancel_its_ids():
    assert co.close_position("cid-4", 2) == {"client_order_id": "cid-4", "product_id": "BIP-20DEC30-CDE",
                                             "size": "2"}
    assert co.batch_cancel(["a", "b"]) == {"order_ids": ["a", "b"]}
    with pytest.raises(ValueError):
        co.batch_cancel([])


def test_client_order_ids_are_deterministic_uuid5s():
    a = co.client_order_id("entry", "btc", 1_791_900_000_000)
    assert UUID.fullmatch(a) and len(a) == 36
    assert a == co.client_order_id("entry", "BTC", 1_791_900_000_000)
    assert a != co.client_order_id("entry", "BTC", 1_791_907_200_000)
    assert a != co.client_order_id("stop", "BTC", 1_791_900_000_000)


def _bal(**over):
    base = {"total_usd_balance": "1000", "cbi_usd_balance": "400", "cfm_usd_balance": "600", "unrealized_pnl": "25",
            "daily_realized_pnl": "-40", "available_margin": "985", "total_open_orders_hold_amount": "0",
            "liquidation_buffer_percentage": "88.5"}
    base.update(over)
    return {"balance_summary": base}


def test_the_balance_is_total_plus_open_plus_unsettled_pnl_plus_transit_in_either_amount_shape():
    b = co.parse_balance(_bal())
    assert (b["equity"], b["cross"], b["check_diff"], b["parts_diff"]) == (985.0, 985.0, 0.0, 0.0)
    obj = co.parse_balance({"balance_summary": {k: {"value": v, "currency": "USD"} if k != "liquidation_buffer_"
                                                "percentage" else v for k, v in _bal()["balance_summary"].items()}})
    assert obj["equity"] == b["equity"] and obj["cross"] == b["cross"]
    t = co.parse_balance(_bal(cfm_usd_balance="500", total_usd_balance="900", available_margin="885",
                              total_pending_transfers_amount={"value": "100", "currency": "USD"}))
    assert t["equity"] == 985.0 and t["cross"] == 985.0 and t["pending"] == 100.0
    with pytest.raises(CoinbaseDataError, match="no daily_realized_pnl"):
        co.parse_balance({"balance_summary": {k: v for k, v in _bal()["balance_summary"].items()
                                              if k != "daily_realized_pnl"}})
    with pytest.raises(CoinbaseDataError, match="isn't a number"):
        co.parse_balance(_bal(unrealized_pnl="n/a"))


def test_positions_are_whole_contracts_and_an_unknown_side_or_fractional_size_raises():
    p = co.parse_position({"product_id": "BIP-20DEC30-CDE", "side": "LONG", "number_of_contracts": "2",
                           "avg_entry_price": "83000", "current_price": "83500", "unrealized_pnl": "10"})
    assert (p["side"], p["contracts"], p["entry"]) == ("long", 2, 83000.0)
    assert co.parse_position({"product_id": "X", "side": "UNKNOWN", "number_of_contracts": "0"}) is None
    with pytest.raises(CoinbaseDataError, match="whole number of contracts"):
        co.parse_position({"product_id": "X", "side": "LONG", "number_of_contracts": "0.02"})
    with pytest.raises(CoinbaseDataError, match="side UNKNOWN"):
        co.parse_position({"product_id": "X", "side": "UNKNOWN", "number_of_contracts": "1"})


def _order(status="OPEN", **over):
    o = {"order_id": "o1", "client_order_id": "c1", "product_id": "BIP-20DEC30-CDE", "side": "SELL", "status": status,
         "order_configuration": {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "80780",
                                                               "limit_price": "76740",
                                                               "stop_direction": "STOP_DIRECTION_STOP_DOWN"}},
         "filled_size": "0", "average_filled_price": "0", "total_fees": "0", "trigger_status": "STOP_PENDING",
         "created_time": "2026-10-13T12:00:00.123456789Z"}
    o.update(over)
    return o


def test_orders_parse_with_their_trigger_limit_and_no_reduce_only_claim():
    o = co.parse_order(_order())
    assert (o["status"], o["side"], o["triggerPrice"], o["price"], o["amount"], o["remaining"]) == \
        ("open", "sell", 80780.0, 76740.0, 1.0, 1.0)
    assert o["reduceOnly"] is None and o["timestamp"] == 1_791_892_800_123
    assert co.parse_order(_order("CANCEL_QUEUED"))["status"] == "canceling"
    assert co.parse_order(_order("QUEUED"))["status"] == "open"
    assert co.parse_order(_order("PENDING"))["status"] == "open"
    assert co.parse_order(_order("FILLED"))["status"] == "closed"
    for s in ("CANCELLED", "EXPIRED", "FAILED"):
        assert co.parse_order(_order(s))["status"] == "canceled"
    with pytest.raises(CoinbaseDataError, match="UNKNOWN_ORDER_STATUS"):
        co.parse_order(_order("UNKNOWN_ORDER_STATUS"))
    m = co.parse_order(_order("FILLED", order_configuration={"market_market_ioc": {"base_size": "2"}},
                              filled_size="2", average_filled_price="83005", total_fees="0.83"))
    assert (m["type"], m["filled"], m["average"], m["fee"]) == ("market", 2.0, 83005.0, {"cost": 0.83,
                                                                                          "currency": "USD"})


def test_fills_key_on_entry_id_and_adjusted_rows_are_not_fills():
    f = co.parse_fill({"entry_id": "e1", "trade_id": "t1", "order_id": "o1", "trade_type": "FILL", "price": "83000",
                       "size": "1", "commission": "0.42", "product_id": "BIP-20DEC30-CDE", "side": "BUY",
                       "trade_time": "2026-10-13T12:00:00Z", "liquidity_indicator": "TAKER"})
    assert (f["id"], f["order"], f["amount"], f["fee"]["cost"], f["side"]) == ("e1", "o1", 1.0, 0.42, "buy")
    for kind in ("REVERSAL", "CORRECTION", "SYNTHETIC"):
        assert co.parse_fill({"entry_id": "e2", "trade_type": kind}) is None


def _tx(kind, amount, cur="USD", native=None):
    return {"id": "t", "type": kind, "status": "completed", "amount": {"amount": amount, "currency": cur},
            "native_amount": {"amount": native if native is not None else amount, "currency": "USD"}}


@pytest.mark.parametrize("kind,amount,want", [
    ("fiat_deposit", "500.00", 500.0),
    ("fiat_withdrawal", "-100.00", -100.0),          # signed
    ("fiat_withdrawal", "100.00", -100.0),           # unsigned: the type says which way
    ("fcm_futures_usdc_sell", "25.00", 25.0),
    ("fcm_futures_usdc_sell_additional_encumberment_rollup", "5.00", 5.0)])
def test_money_in_and_out_takes_its_direction_from_the_type(kind, amount, want):
    got = co.parse_flow(_tx(kind, amount))
    assert got[0] == want
    if kind.startswith("fcm"):
        assert "not profit" in got[1]


def test_internal_moves_are_not_flows_and_anything_unclear_is_refused():
    assert co.parse_flow(_tx("derivatives_settlement", "40.00")) is None
    for tx in (_tx("advanced_trade_fill", "-10"), _tx("send", "-1"), _tx("tx", "3"), _tx("brand_new_type", "1"),
               _tx("fiat_deposit", "-500.00"),                       # a credit with a debit's sign
               _tx("fiat_deposit", "500.00", cur="USDC"),
               _tx("fiat_deposit", "500.00", native="400.00"),
               {"id": "t", "type": "fiat_deposit", "amount": {"amount": "x", "currency": "USD"}}):
        with pytest.raises(UnknownLedgerEntry):
            co.parse_flow(tx)
    assert [co.flow_status({"status": s}) for s in ("completed", "pending", "waiting_for_clearing", "canceled",
                                                     "failed", "expired", "weird")] == \
        ["final", "candidate", "candidate", "void", "void", "void", "unknown"]


def test_key_permissions_read_a_missing_flag_as_the_riskier_answer():
    assert co.parse_permissions({"can_view": True}) == {"can_view": True, "can_trade": True, "can_transfer": True,
                                                        "portfolio_uuid": None, "portfolio_type": None}
    assert co.parse_permissions({"can_view": True, "can_trade": False, "can_transfer": False,
                                 "portfolio_type": "DEFAULT"})["can_trade"] is False
