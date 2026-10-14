"""CoinbaseExchange (desk/brokers/coinbase.py), the ccxt-shaped adapter LiveBroker drives, against the local fake
Coinbase: the product check, the balance and its cross-check, positions in contracts, orders, fills, deposits and
withdrawals, and the order guards that stand in for Coinbase's missing reduce-only."""

from __future__ import annotations

import pytest

from desk.brokers import coinbase as cb
from desk.brokers.coinbase_rest import CoinbaseDataError, OrdersLocked
from desk.brokers.live import LiveSetupError
from fake_coinbase import PRODUCT, FakeCoinbase, Pos, arm_rest, view_rest


@pytest.fixture
def fake():
    with FakeCoinbase() as f:
        yield f


def adapter(rest, alerts=None):
    return cb.CoinbaseExchange(rest, on_alert=(alerts.append if alerts is not None else None))


# ---------------------------------------------------------------- the product

def test_the_market_is_the_perp_in_whole_contracts_of_001_btc_with_5_dollar_ticks(fake):
    ex = adapter(view_rest(fake))
    m = ex.load_markets()[PRODUCT]
    assert (m["contractSize"], m["precision"], m["limits"]["amount"]["min"], m["limits"]["cost"]["min"]) == \
        (0.01, {"amount": 1.0, "price": 5.0}, 1.0, 0.0)
    assert m["settle"] == "USD" and m["swap"] and ex.session_open() is True
    assert ex.amount_to_precision(PRODUCT, 2.999) == "2" and ex.price_to_precision(PRODUCT, 80_782) == "80780"
    with pytest.raises(ValueError):
        ex.amount_to_precision(PRODUCT, 0.6)
    assert fake.requests[-1]["path"] == f"/api/v3/brokerage/products/{PRODUCT}"     # authenticated, not the CDN copy


@pytest.mark.parametrize("edit,field", [
    ({"future_product_details": {"contract_size": "0.1"}}, "contract size"),
    ({"base_increment": "0.5"}, "order sizes"),
    ({"product_type": "SPOT"}, "product_type"),
    ({"product_venue": "INTX"}, "product_venue"),
    ({"future_product_details": {"funding_rate": "", "perpetual_details": {"funding_rate": ""}}}, "no funding rate"),
    ({"price_increment": "1"}, "price_increment")])
def test_a_changed_product_refuses_with_the_field_named(fake, edit, field):
    fake.product_edits = edit
    with pytest.raises(LiveSetupError, match=f"isn't the contract the desk is set up for any more.*{field}"):
        adapter(view_rest(fake)).load_markets()


def test_a_changed_product_with_a_position_open_blocks_entries_but_keeps_the_market(fake):
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
    fake.product_edits = {"product_venue": "INTX"}
    alerts: list[str] = []
    ex = adapter(view_rest(fake), alerts)
    assert PRODUCT in ex.load_markets()
    assert "isn't the contract" in ex.entry_block() and alerts and "still managed" in alerts[0]


def test_trading_disabled_reads_as_closed_not_as_a_missing_market(fake):
    fake.trading_disabled = True
    ex = adapter(view_rest(fake))
    ex.load_markets()
    assert ex.session_open() is False and "closed" in ex.entry_block()


# ---------------------------------------------------------------- the balance

@pytest.mark.parametrize("style", ["object", "string"])
def test_the_balance_is_the_accounts_value_and_adds_up(fake, style):
    fake.amount_style = style
    fake.positions[PRODUCT] = Pos("LONG", 2, 82_000.0, 82_000.0)
    fake.margin_transfer(400.0)
    fake.realized = -12.5
    ex = adapter(view_rest(fake))
    bal = ex.fetch_balance()
    assert bal["total"]["USD"] == pytest.approx(fake.equity()) == pytest.approx(1_000 + 20 - 12.5)
    assert ex.balance_block is None and ex.usable == pytest.approx(fake.equity())


def test_a_settlement_and_a_sweep_in_transit_dont_move_the_value(fake):
    fake.positions[PRODUCT] = Pos("LONG", 1, 82_000.0, 82_000.0)
    fake.margin_transfer(500.0)
    ex = adapter(view_rest(fake))
    before = ex.fetch_balance()["total"]["USD"]
    fake.realized += 30.0
    fake.cbi -= 30.0                                    # (a P&L that happened elsewhere, just to move numbers)
    fake.settle()
    assert ex.fetch_balance()["total"]["USD"] == pytest.approx(before)
    fake.sweep_start(200.0)
    assert ex.fetch_balance()["total"]["USD"] == pytest.approx(before) and ex.balance_block is None
    fake.sweep_arrive()
    assert ex.fetch_balance()["total"]["USD"] == pytest.approx(before) and ex.balance_block is None


def test_money_on_an_ach_hold_is_in_the_value_but_not_what_entries_are_sized_on(fake):
    fake.deposit(300.0, hold=True)
    ex = adapter(view_rest(fake))
    assert ex.fetch_balance()["total"]["USD"] == pytest.approx(1_300.0)
    assert ex.balance_block is None and ex.usable == pytest.approx(1_000.0)


def test_a_balance_that_doesnt_add_up_blocks_entries_and_is_still_read(fake):
    class Odd(FakeCoinbase):
        def _balance(self):
            st, out = super()._balance()
            out["balance_summary"]["available_margin"] = self._amt(1_500.0)    # more than the account holds
            return st, out
    with Odd() as odd:
        ex = adapter(view_rest(odd))
        assert ex.fetch_balance()["total"]["USD"] == pytest.approx(1_000.0)
        assert "doesn't add up" in ex.entry_block()


# ---------------------------------------------------------------- positions, orders, fills

def test_positions_and_orders_on_other_products_are_someone_elses_and_block_entries(fake):
    fake.positions[PRODUCT] = Pos("SHORT", 3, 84_000.0, 84_000.0)
    fake.positions["ETP-20DEC30-CDE"] = Pos("LONG", 1, 2_500.0, 2_500.0)
    fake._order({"product_id": "BTC-USD", "side": "BUY", "client_order_id": "spot-1"}, "stop",
                {"limit_limit_gtc": {"base_size": "0.001", "limit_price": "50000"}}, "OPEN")
    ex = adapter(view_rest(fake))
    pos = ex.fetch_positions([PRODUCT])
    assert [(p["side"], p["contracts"], p["contractSize"]) for p in pos] == [("short", 3.0, 0.01)]
    assert ex.fetch_open_orders(PRODUCT) == []
    assert ex.foreign["positions"] == [{"product_id": "ETP-20DEC30-CDE", "side": "long", "contracts": 1}]
    assert ex.foreign["orders"][0]["product_id"] == "BTC-USD"
    assert "not the desk's" in ex.entry_block()
    assert {k for k, _ in ex.watchdog()} == {"unprotected", "foreign"}


def test_open_orders_read_open_then_pending_queued_and_cancel_queued(fake):
    for status in ("OPEN", "QUEUED", "PENDING", "CANCEL_QUEUED", "CANCELLED", "FILLED"):
        fake._order({"product_id": PRODUCT, "side": "SELL", "client_order_id": f"c-{status}"}, "stop",
                    {"stop_limit_stop_limit_gtc": {"base_size": "1", "stop_price": "80000", "limit_price": "76000",
                                                   "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}, status)
    ex = adapter(view_rest(fake))
    got = {o["coinbase_status"]: o["status"] for o in ex.fetch_open_orders(PRODUCT)}
    assert got == {"OPEN": "open", "QUEUED": "open", "PENDING": "open", "CANCEL_QUEUED": "canceling"}
    reads = [r["query"].get("order_status") for r in fake.requests if r["path"].endswith("/batch")]
    assert reads == [["OPEN"], ["PENDING", "QUEUED", "CANCEL_QUEUED"]]


def test_fills_are_this_products_by_entry_id_and_adjusted_rows_are_alerted_once(fake):
    rest = arm_rest(fake)
    alerts: list[str] = []
    ex = adapter(rest, alerts)
    ex.load_markets()
    res = ex.create_order(PRODUCT, "market", "buy", 2, 83_000, {"clientOrderId": "e-1"})
    assert res["status"] == "closed" and res["filled"] == 2.0 and res["average"] == pytest.approx(fake.ask)
    fake.fills.append({**fake.fills[-1], "entry_id": "adj-1", "trade_type": "CORRECTION"})
    t1 = ex.fetch_my_trades(PRODUCT, fake.clock_ms() - 60_000)
    t2 = ex.fetch_my_trades(PRODUCT, fake.clock_ms() - 60_000)
    assert [t["id"] for t in t1] == [fake.fills[0]["entry_id"]] == [t["id"] for t in t2]
    assert t1[0]["amount"] == 2.0 and t1[0]["fee"] == {"cost": pytest.approx(0.83), "currency": "USD"}
    assert len([a for a in alerts if "CORRECTION" in a]) == 1


# ---------------------------------------------------------------- deposits and withdrawals

def test_the_ledger_is_the_usd_wallets_transactions_with_stable_ids_and_statuses(fake):
    ex = adapter(view_rest(fake))
    d = fake.deposit(500.0, status="pending", credited=False)
    fake.withdraw(100.0, signed=False)
    rows = ex.fetch_ledger(None, fake.clock_ms())
    assert [(r["id"], r["status"], r["type"]) for r in rows] == [
        (f"coinbase-{d['id']}", "candidate", "fiat_deposit"), (rows[1]["id"], "final", "fiat_withdrawal")]
    assert ex.parse_flow(rows[1]["info"]) == (-100.0, "bank withdrawal")
    fake.set_status(d, "completed", credit=500.0)
    again = ex.fetch_ledger(None, fake.clock_ms())
    assert again[0]["id"] == rows[0]["id"] and again[0]["status"] == "final"
    assert ex.wallets() == {"usd_account": fake.usd_account, "usdc": 0.0}


def test_the_ledger_reads_back_two_weeks_and_no_further(fake):
    ex = adapter(view_rest(fake))
    fake.deposit(10.0)
    fake.sleep(20 * 86400)
    fake.deposit(20.0)
    assert [float(r["info"]["amount"]["amount"]) for r in ex.fetch_ledger(None, fake.clock_ms())] == [20.0]


# ---------------------------------------------------------------- orders: the guards

def test_without_an_armed_client_no_order_goes_out(fake):
    ex = adapter(view_rest(fake))
    ex.load_markets()
    with pytest.raises(OrdersLocked):
        ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "x"})
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
    with pytest.raises(OrdersLocked):
        ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "y", "stopLossPrice": 80_000})
    assert fake.non_get() == []


def test_a_stop_that_would_be_more_than_the_position_or_with_no_position_is_refused_unsent(fake):
    rest = arm_rest(fake)
    ex = adapter(rest)
    ex.load_markets()
    with pytest.raises(cb.OrderRefused, match="no position"):
        ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s0", "stopLossPrice": 80_000})
    ex.create_order(PRODUCT, "market", "buy", 2, 83_000, {"clientOrderId": "e1"})
    with pytest.raises(cb.OrderRefused, match="doesn't close"):
        ex.create_order(PRODUCT, "market", "buy", 1, 85_000, {"clientOrderId": "s1", "stopLossPrice": 85_000})
    ex.create_order(PRODUCT, "market", "sell", 2, 80_000, {"clientOrderId": "s2", "stopLossPrice": 80_000})
    with pytest.raises(cb.OrderRefused, match="would open a position"):
        ex.create_order(PRODUCT, "market", "sell", 1, 79_000, {"clientOrderId": "s3", "stopLossPrice": 79_000})
    with pytest.raises(cb.OrderRefused, match="already rests"):
        ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e2"})
    with pytest.raises(cb.OrderRefused, match="it would open the other way"):
        ex.create_order(PRODUCT, "market", "sell", 3, 83_000, {"clientOrderId": "c1", "reduceOnly": True})
    with pytest.raises(cb.OrderRefused, match="clientOrderId"):
        ex.create_order(PRODUCT, "market", "sell", 1, 83_000, {"reduceOnly": True})
    assert len(fake.stops()) == 1 and len([r for r in fake.non_get()]) == 2


def test_a_stop_through_the_market_is_said_as_such(fake):
    rest = arm_rest(fake)
    ex = adapter(rest)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e1"})
    with pytest.raises(cb.StopThroughMarket):
        ex.create_order(PRODUCT, "market", "sell", 1, 83_500, {"clientOrderId": "s1", "stopLossPrice": 83_500})


def test_a_price_band_refusing_the_5pct_limit_is_retried_at_2_then_1pct(fake):
    rest = arm_rest(fake)
    alerts: list[str] = []
    ex = adapter(rest, alerts)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e1"})
    fake.band = 0.06            # a limit more than 6% from the last trade (83,000) is refused: 5% past 80,000 is 8.4%
    o = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s1", "stopLossPrice": 80_000})
    assert o["price"] == 78_400.0 and any("trying 2%" in a for a in alerts)


def test_a_reused_client_id_whose_order_is_gone_is_said_stale(fake):
    rest = arm_rest(fake)
    ex = adapter(rest)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e1"})
    s = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s1", "stopLossPrice": 80_000})
    ex.cancel_order(s["id"])
    with pytest.raises(cb.StaleClientOrderId):
        ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s1", "stopLossPrice": 80_000})


def test_cancel_outcomes_map_to_what_the_broker_does_next(fake):
    rest = arm_rest(fake)
    ex = adapter(rest)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e1"})
    s = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s1", "stopLossPrice": 80_000})
    assert ex.cancel_order(s["id"]) == {"id": s["id"], "status": "canceled"}
    assert ex.cancel_order(s["id"])["status"] == "canceled"            # DUPLICATE_CANCEL_REQUEST, read back
    s2 = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s2", "stopLossPrice": 80_000})
    fake.trade(79_990)                                                  # it fires and fills
    with pytest.raises(cb.StopAlreadyFilled):
        ex.cancel_order(s2["id"])
    ex.create_order(PRODUCT, "market", "buy", 1, 80_000, {"clientOrderId": "e2"})
    s3 = ex.create_order(PRODUCT, "market", "sell", 1, 78_000, {"clientOrderId": "s3", "stopLossPrice": 78_000})
    fake.close_market()
    fake.no_cancel_phase = True
    with pytest.raises(cb.SessionClosed):
        ex.cancel_order(s3["id"])
    fake.no_cancel_phase = False
    assert ex.cancel_order(s3["id"])["status"] == "canceling"           # queued for the reopen
    with pytest.raises(cb.CancelFailed):
        ex.cancel_order("00000000-0000-0000-0000-00000000dead")


def test_a_market_order_while_closed_is_said_closed_and_a_stop_queues(fake):
    rest = arm_rest(fake)
    ex = adapter(rest)
    ex.load_markets()
    ex.create_order(PRODUCT, "market", "buy", 1, 83_000, {"clientOrderId": "e1"})
    fake.close_market()
    with pytest.raises(cb.SessionClosed):
        ex.create_order(PRODUCT, "market", "sell", 1, 83_000, {"clientOrderId": "c1", "reduceOnly": True})
    o = ex.create_order(PRODUCT, "market", "sell", 1, 80_000, {"clientOrderId": "s1", "stopLossPrice": 80_000})
    assert o["coinbase_status"] == "QUEUED" and o["status"] == "open"


def test_prices_older_than_a_minute_arent_acted_on(fake):
    ex = adapter(view_rest(fake))
    assert ex.last_trade()[0] == fake.last and ex.best_bid_ask()[:2] == (fake.bid, fake.ask)
    fake.close_market()
    fake.sleep(120)
    assert ex.last_trade() is None and ex.best_bid_ask() is None


def test_a_position_size_coinbase_reports_in_fractions_is_refused(fake):
    class Btc(FakeCoinbase):
        def _position_row(self, pid, p):
            return {**super()._position_row(pid, p), "number_of_contracts": "0.01"}
    with Btc() as f:
        f.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
        with pytest.raises(CoinbaseDataError, match="whole number of contracts"):
            adapter(view_rest(f)).fetch_positions([PRODUCT])


def test_a_moment_is_priced_from_its_minute_candle_or_the_last_one_before_a_closure(fake):
    ex = adapter(view_rest(fake))
    fake.trade(83_100.0, dt=60)
    t_mid = fake.clock_ms()
    fake.trade(83_400.0, dt=30)
    assert ex.price_at(t_mid + 10_000) == 83_100.0                 # the open of the minute it falls in
    fake.trade(83_600.0, dt=3600)
    fake.close_market()
    fake.sleep(3 * 3600)
    assert ex.price_at(fake.clock_ms()) == 83_600.0               # closed: no candles, the last close before
    assert ex.price_at(fake.clock_ms() + 30 * 86400_000) is None
