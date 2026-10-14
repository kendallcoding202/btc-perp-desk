"""SHADOW on Coinbase (desk/coinbase_run.py): the REAL account read with a View-only key, LIVE's own decisions and
orders played on a virtual book, nothing sent. Every test runs against the local fake Coinbase (tests/fake_coinbase.py)
on 127.0.0.1; tests/conftest.py fails any other connection. Every key is generated fresh and named "...-not-real"."""

from __future__ import annotations

import copy
import json

import pytest

from desk import coinbase_run as cr
from desk import statebox
from desk.brokers import coinbase_orders as co
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, good_proof, make_ec_key, next_bar, seed_bars, trend_bars

KEY = "test-state-key-" + "k" * 30          # a DESK_STATE_KEY for the tests (32+ characters, not a real secret)
IT = 1000                                    # key rounds for the tests' envelopes (the desk's own default: 600,000)


class Shadow:
    """One SHADOW account on the fake: its state directory, its key, and the runs."""

    def __init__(self, fake, tmp_path, fk=None, start_bars=True):
        self.fake = fake
        self.fk = fake.add_key(fk or make_ec_key())
        self.dir = tmp_path / "cb"
        self.env = {"COINBASE_CDP_KEY": self.fk.file_json(), "DESK_STATE_KEY": KEY}
        self.pushes: list[tuple] = []
        self.bars = trend_bars(fake.now_s, last=fake.last)
        if start_bars:
            seed_bars(fake, self.bars)

    def run(self, mode="shadow", **kw):
        kw.setdefault("iterations", IT)
        args = dict(base_url=self.fake.base_url, clock=self.fake.clock, sleep=self.fake.sleep,
                    push=lambda *a: self.pushes.append(a), mask_emit=lambda line: None, **kw)
        if mode == "live":
            code, r = cr.run_live_once(self.dir, {**self.env, **ARM_ENV}, max_notional=1_000.0,
                                       gate=[("backtest: passes", True, "ok")], **args)
        else:
            code, r = cr.run_shadow(self.dir, self.env, **args)
        self.code, self.r = code, r
        return code, r

    def board(self, mode="shadow"):
        return statebox.open_json((self.dir / "BTC" / f"board-{mode}.enc.json").read_text(), KEY,
                                  statebox.board_aad(mode))

    def state(self, mode="shadow"):
        return cr.open_bundle(self.dir / "BTC" / f"state-{mode}.enc.json", KEY, mode)

    def file(self, mode, name):
        return json.loads(self.state(mode)["files"][name])

    def status(self):
        return json.loads((self.dir / "BTC" / "status.json").read_text())

    def close(self, close: float, *, at_min: float = 8.0, low=None, high=None):
        """The next 2h candle closes at `close`; the run comes `at_min` minutes after."""
        self.bars = next_bar(self.bars, round(close), low=low, high=high)
        seed_bars(self.fake, self.bars)
        self.fake.now_s = self.bars[-1].ts / 1000 + 7200 + at_min * 60 - 60
        self.fake.trade(round(close))

    def breakout(self, **kw):
        hi = max(b.high for b in self.bars[-60:])
        self.close(hi * 1.015, **kw)
        return hi * 1.015


@pytest.fixture
def fake():
    with FakeCoinbase(cbi=0.0) as f:
        f.now_s += 8 * 60           # 14:08 UTC: a run 8 minutes after the 14:00 close
        yield f


def titles(sh):
    return [p[0] for p in sh.pushes]


def events(sh):
    return [e["text"] for e in sh.board()["events"]]


# ---------------------------------------------------------------- day one, $0, read only

def test_day_one_at_zero_reads_everything_decides_and_sends_nothing(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    code, r = sh.run()
    assert code == 0 and r.problem is None
    b = sh.board()
    checks = {c["key"]: c["ok"] for c in b["shadow"]["checks"]}
    assert set(checks) >= {"key", "view_only", "clock", "balance", "adds_up", "positions", "fills", "fees", "flows",
                           "decided"}
    assert all(checks.values())
    assert b["header"]["mode"] == "SHADOW"
    assert b["header"]["tagline"] == "SHADOW · YOUR REAL COINBASE ACCOUNT, READ ONLY · NO ORDERS SENT · TRADES SIMULATED"
    assert b["shadow"]["real_equity"] == 0.0 and b["ticker"]["bank"] == 0.0 and b["ticker"]["pnl_pct"] == 0.0
    me = b["shadow"]["min_equity_for_one"]
    assert me["need"] > 0 and "would need an account of at least" in me["note"] and "overnight margin" in me["note"]
    assert b["shadow"]["what_if"]["text"].startswith("WHAT-IF, not your account: with the planned $500")
    assert b["goal"]["test_cagr"] is None and b["goal"]["progress"] == 0.0     # no backtest projection here
    assert fake.non_get() == []
    sh.run()
    assert titles(sh).count("BTC SHADOW: connected (read only)") == 1
    st = sh.status()
    assert st["mode"] == "SHADOW" and st["ok"] and st["runs"] == 2 and st["key_view_only"] is True
    assert st["decision"] in ("WAIT", "STAND DOWN") and st["opened"] is False


@pytest.mark.parametrize("perm", ["trade", "transfer"])
def test_a_key_that_can_trade_or_move_money_is_refused_before_any_account_read(fake, tmp_path, perm):
    sh = Shadow(fake, tmp_path, make_ec_key(**{perm: True}))
    code, r = sh.run()
    assert code == 2 and r.problem == "permissions"
    paths = [q["path"] for q in fake.requests]
    assert f"{V3}/key_permissions" in paths and f"{V3}/cfm/balance_summary" not in paths
    assert fake.non_get() == []
    assert any("View-only key" in p[1] or "View only" in p[1] for p in sh.pushes)
    assert sh.status()["problem"] == "permissions" and sh.status()["ok"] is False


def test_a_key_on_another_portfolio_is_alerted(fake, tmp_path):
    fk = make_ec_key()
    fk.portfolio_type = "CONSUMER"
    sh = Shadow(fake, tmp_path, fk)
    assert sh.run()[0] == 0
    assert any("not DEFAULT" in a for a in sh.r.alerts)


# ---------------------------------------------------------------- the would-send script: entry, stop, trail, exit

def scripted(sh):
    fake = sh.fake
    sh.run()                                     # day one
    fake.deposit(1_000.0, dt=600)
    fake.now_s += 300
    sh.run()                                     # the deposit opens the account
    close = sh.breakout()
    sh.run()                                     # the breakout: entry and its stop
    fake.trade(close * 1.01, dt=1800)
    sh.close(close * 1.03)
    sh.run()                                     # a higher close: the trail moves (cancel first, then the new stop)
    stop = sh.board()["ticker"]["pos"]["stop"]
    fake.trade(stop - 50, dt=600)
    fake.trade(stop - 100, dt=300)
    fake.now_s += 600
    sh.run()                                     # the virtual stop fills from the 5-minute candles
    return close, stop


def test_entry_stop_trail_and_exit_are_recorded_as_would_send_and_nothing_is_sent(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    close, stop = scripted(sh)
    ws = sh.file("shadow", "book.json")["would_send"]
    assert [w["kind"] for w in ws] == ["entry", "stop", "cancel", "stop"]
    entry, first_stop, cancel, second = ws
    assert entry["body"]["order_configuration"] == {"market_market_ioc": {"base_size": "1"}}
    assert entry["path"] == f"{V3}/orders" and entry["body"]["side"] == "BUY"
    assert entry["fill"] == fake.history[-1][1] or entry["fill"] > 0
    sl = first_stop["body"]["order_configuration"]["stop_limit_stop_limit_gtc"]
    assert sl["stop_direction"] == "STOP_DIRECTION_STOP_DOWN" and int(sl["limit_price"]) % 5 == 0
    assert int(sl["limit_price"]) == co.floor5(int(sl["stop_price"]) * 0.95)
    assert cancel["path"] == f"{V3}/orders/batch_cancel"
    assert int(second["body"]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"]) > int(sl["stop_price"])
    assert fake.non_get() == []                                  # not one POST reached Coinbase
    b = sh.board()
    assert b["ticker"]["position"] == "FLAT" and b["ticker"]["record"] == "0W 1L"
    texts = events(sh)
    assert any(t.startswith("WOULD BUY 1 contract (0.01 BTC) at market: simulated fill") and t.endswith(
        "No order sent.") for t in texts)
    assert any(t.startswith("WOULD HAVE SOLD 1 contract") and "No order sent." in t for t in texts)
    assert "BTC SHADOW: would BUY (no order sent)" in titles(sh)
    assert "BTC SHADOW: would SELL (stop, simulated)" in titles(sh)
    # the money: the real account never moved; SHADOW's is it plus the simulated trade's P&L, fees included
    t = b["shadow"]
    assert t["real_equity"] == 1_000.0 and t["simulated_equity"] == pytest.approx(1_000.0 + t["simulated_pnl"])
    assert t["simulated_pnl"] < 0


def test_shadows_would_send_requests_are_lives_requests_for_the_same_script(tmp_path):
    with FakeCoinbase(cbi=0.0) as f1:
        f1.now_s += 8 * 60
        s1 = Shadow(f1, tmp_path / "s")
        scripted(s1)
        shadow_sent = [(w["path"], w["body"]) for w in s1.file("shadow", "book.json")["would_send"]]
    with FakeCoinbase(cbi=0.0) as f2:
        f2.now_s += 8 * 60
        s2 = Shadow(f2, tmp_path / "l", make_ec_key("test-trade-key-not-real", trade=True))
        _write_proof(s2, f2)
        # LIVE starts on an account that's already funded and flat: the same script from the deposit on
        f2.deposit(1_000.0, dt=600)
        f2.now_s += 300
        assert s2.run("live")[0] == 0
        close = s2.breakout()
        assert s2.run("live")[0] == 0
        f2.trade(close * 1.01, dt=1800)
        s2.close(close * 1.03)
        assert s2.run("live")[0] == 0
        stop = s2.board("live")["ticker"]["pos"]["stop"]
        f2.trade(stop - 50, dt=600)
        f2.trade(stop - 100, dt=300)
        f2.now_s += 600
        assert s2.run("live")[0] == 0
        live_sent = [(r["path"], r["body"]) for r in f2.non_get()]
    # LIVE's cancel names Coinbase's order id, SHADOW's its virtual one; a stop's client id is made from the moment of
    # its entry (each run's own clock). Everything else is the same request, and the entry's id (made from the bar it
    # was decided at) is the same too.
    strip = lambda xs: [(p, {k: v for k, v in b.items() if k not in ("order_ids", "client_order_id")})   # noqa: E731
                        for p, b in xs]
    assert strip(shadow_sent) == strip(live_sent)
    assert [p.rsplit("/", 1)[-1] for p, _ in live_sent] == ["orders", "orders", "batch_cancel", "orders"]
    assert shadow_sent[0][1]["client_order_id"] == live_sent[0][1]["client_order_id"]


def _write_proof(sh, fake):
    """A SHADOW record good enough to arm LIVE (the tests' own; SHADOW writes the real one)."""
    pr = good_proof(fake.clock_ms())
    acct = {**cr.new_account(), "days": pr["days"], "deposits_reconciled": 1, "portfolio_type": "DEFAULT",
            "portfolio_uuid": pr["portfolio_uuid"], "last_run_ts": pr["last_run_ts"]}
    bundle = {"v": 1, "files": {"account.json": json.dumps(acct),
                                "desk.json": json.dumps({"meta": {"started_ts": pr["started_ts"]}})}}
    (sh.dir / "BTC").mkdir(parents=True, exist_ok=True)
    (sh.dir / "BTC" / "state-shadow.enc.json").write_text(
        statebox.seal_json(bundle, KEY, statebox.state_aad("shadow"), iterations=IT))


# ---------------------------------------------------------------- the decision against paper's, on the same candles

def _paper_board(fake, tmp_path, monkeypatch, sh):
    """The BTC paper account's own step on the same Coinbase candles (the fake's public market endpoints)."""
    from desk import paper
    from desk.feeds import coinbase as cbf
    from desk.thresholds import params_for

    monkeypatch.setattr(cbf, "BASE", fake.base_url + "/api/v3/brokerage/market")
    feed = cbf.CoinbaseFeed(clock=fake.clock)
    return paper.step(params_for("BTC"), tmp_path / "ledger" / "BTC", feed=feed, now_ms=fake.clock_ms(),
                      log=lambda *a: None)


def test_the_decision_matches_papers_on_the_same_candles_close_after_close(fake, tmp_path, monkeypatch):
    sh = Shadow(fake, tmp_path)
    for k in range(4):
        pb = _paper_board(fake, tmp_path, monkeypatch, sh)
        assert sh.run(paper_board=pb)[0] == 0
        pc = sh.board()["shadow"]["paper_check"]
        assert pc["ok"] is True, pc
        assert pc["shadow"]["setup"] == pc["paper"]["setup"]
        sh.close(sh.bars[-1].close * (1.003 if k % 2 else 0.997))
    # a paper board that saw other candles: alerted, never a silent difference
    pb = _paper_board(fake, tmp_path, monkeypatch, sh)
    bad = copy.deepcopy(pb)
    for g in bad["analysis"]["gates"]:
        if g["name"] == "setup":
            g["verdict"] = "PASS" if g["verdict"] != "PASS" else "WAIT"
    sh.run(paper_board=bad)
    pc = sh.board()["shadow"]["paper_check"]
    assert pc["ok"] is False and "setup" in pc["differs"]
    assert any("decided the" in a and "differently" in a for a in sh.r.alerts)


def test_the_account_only_gates_are_named_not_alerted(fake, tmp_path, monkeypatch):
    sh = Shadow(fake, tmp_path)
    pb = _paper_board(fake, tmp_path, monkeypatch, sh)      # paper holds $500, the real account $0
    sh.run(paper_board=pb)
    pc = sh.board()["shadow"]["paper_check"]
    assert pc["ok"] is True and not sh.r.alerts


# ---------------------------------------------------------------- money in and out

def test_the_first_deposit_opens_the_account_and_later_ones_are_flows_not_profit(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    tx = fake.deposit(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    meta = sh.file("shadow", "desk.json")["meta"]
    assert meta["start_equity"] == 500.0 and meta["opened"]["what"] == "bank deposit"
    assert meta["opened"]["flow"] == f"coinbase-{tx['id']}"
    assert sh.file("shadow", "desk.json").get("flows") is None             # the start, not a flow
    assert any(t.startswith("ACCOUNT OPENED with $500.00 (bank deposit") and "not profit" in t for t in events(sh))
    assert "BTC SHADOW: account opened" in titles(sh)
    assert sh.file("shadow", "account.json")["deposits_reconciled"] == 1
    fake.deposit(100.0, dt=600)
    fake.now_s += 300
    sh.run()
    flows = sh.file("shadow", "desk.json")["flows"]
    assert len(flows) == 1 and flows[0]["amount"] == 100.0 and flows[0]["kind"] == "exchange"
    assert flows[0]["units"] == pytest.approx(100.0)                       # at the NAV, $1 a unit: nothing traded
    b = sh.board()
    assert b["ticker"]["bank"] == 600.0 and b["ticker"]["pnl"] == pytest.approx(0.0)
    assert b["flows"]["net"] == 100.0
    assert "BTC SHADOW: DEPOSIT" in titles(sh)
    fake.withdraw(50.0, dt=600)
    fake.now_s += 300
    sh.run()
    b = sh.board()
    assert b["ticker"]["bank"] == 550.0 and b["ticker"]["pnl"] == pytest.approx(0.0) and b["flows"]["net"] == 50.0
    assert "BTC SHADOW: WITHDRAWAL" in titles(sh)
    # run again with nothing new: nothing counted twice
    n_events, n_push = len(events(sh)), len(sh.pushes)
    sh.run()
    assert len(sh.file("shadow", "desk.json")["flows"]) == 2 and len(events(sh)) == n_events
    assert len(sh.pushes) == n_push


def test_emptying_the_account_is_money_out_not_a_drawdown_and_the_next_deposit_reopens_it(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    fake.withdraw(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    d = sh.file("shadow", "desk.json")
    assert d["meta"]["start_equity"] == 0.0 and d["risk"]["halted"] is False and d["risk"]["peak_equity"] == 0.0
    assert any(t.startswith("ACCOUNT EMPTIED") and "not a loss" in t for t in events(sh))
    sh.close(sh.bars[-1].close)
    sh.run()                                         # a close on an empty account: no halt, no flatten
    d = sh.file("shadow", "desk.json")
    assert d["risk"]["halted"] is False and not any("DRAWDOWN HALT" in t for t in events(sh))
    fake.deposit(300.0, dt=600)
    fake.now_s += 300
    sh.run()
    d = sh.file("shadow", "desk.json")
    assert d["meta"]["start_equity"] == 300.0 and len(d["meta"]["periods"]) == 1
    assert d.get("flows") is None


def test_a_move_nothing_explains_waits_three_hours_across_runs_then_sets_entry_kill(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    fake.cbi += 40.0                                 # money appears with no transaction for it
    for _ in range(3):
        fake.now_s += 15 * 60
        sh.run()
        assert not sh.r.kills_set.get("entry_kill")
        lp = sh.file("shadow", "live_position.json")
        assert lp["unexplained_since"] is not None   # the wait's start is kept across one-shot runs
    fake.now_s += 3 * 3600
    sh.run()
    assert sh.r.kills_set.get("entry_kill") is True
    assert json.loads((sh.dir / "BTC" / "kills.set.json").read_text()) == {"entry_kill": True}
    assert any("isn't explained" in a for a in sh.r.alerts)
    reason = sh.board()["shadow"]["entry_kill_reason"]
    assert reason.startswith("LIVE would be in entry_kill now") and "BTC/kills.json" in reason
    assert sh.file("shadow", "account.json")["days"][cr._day(fake.clock_ms())]["unexplained"] >= 1


# ---------------------------------------------------------------- the virtual stop, against 5-minute candles

def _long(sh):
    fake = sh.fake
    sh.run()
    fake.deposit(1_000.0, dt=600)
    fake.now_s += 300
    sh.run()
    sh.breakout()
    sh.run()
    pos = sh.board()["ticker"]["pos"]
    assert pos is not None
    return pos


def test_a_gap_past_the_stop_limits_limit_leaves_it_unfilled_then_the_next_run_closes_at_market(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    _long(sh)
    stop = [x for x in sh.file("shadow", "book.json")["orders"].values() if x["kind"] == "stop"][0]
    fake.trade(stop["limit"] - 500, dt=600)       # far through the limit in one jump, and it stays there
    fake.now_s += 600
    sh.run()
    book = sh.file("shadow", "book.json")
    st = book["orders"][stop["order_id"]]
    assert st["trigger"] == "STOP_TRIGGERED" and st["filled"] == 0            # triggered, never filled
    assert any("past its limit" in a for a in sh.r.alerts)
    assert book["position"] is None and sh.board()["ticker"]["position"] == "FLAT"
    kinds = [w["kind"] for w in book["would_send"]]
    assert kinds == ["entry", "stop", "cancel", "close"]                       # the stop off first, then the close
    close = book["would_send"][-1]
    assert close["path"] == f"{V3}/orders/close_position" and close["body"]["size"] == "1"
    assert close["fill"] == fake.bid and fake.non_get() == []


def test_nothing_triggers_while_coinbase_is_closed_and_nothing_is_sent_then(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    pos = _long(sh)
    fake.close_market()
    fake.trade(pos["stop"] - 2_000, dt=600)       # no trade happens while closed: no candle, no trigger
    fake.now_s += 600
    sh.run()
    book = sh.file("shadow", "book.json")
    assert book["position"] is not None
    assert all(x["trigger"] == "STOP_PENDING" for x in book["orders"].values() if x["kind"] == "stop"
               and x["status"] == "OPEN")
    assert [w["kind"] for w in book["would_send"]] == ["entry", "stop"]


# ---------------------------------------------------------------- freshness and sessions

@pytest.mark.parametrize("minutes,taken", [(20, True), (35, False)])
def test_an_entry_is_taken_up_to_30_minutes_after_its_close(fake, tmp_path, minutes, taken):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(1_000.0, dt=600)
    fake.now_s += 300
    sh.run()
    sh.breakout(at_min=minutes)
    sh.run()
    sent = [w["kind"] for w in sh.file("shadow", "book.json")["would_send"]]
    assert (sent == ["entry", "stop"]) is taken
    if not taken:
        assert any("was not taken" in t for t in events(sh))


def test_a_closed_market_with_no_book_at_all_is_expected_not_a_failure(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.close_market()
    fake.book_404_when_closed = True
    fake.now_s += 900
    code, r = sh.run()
    assert code == 0 and r.problem is None
    assert sh.board()["venue"]["session"]["open"] is False
    fake.reopen()
    fake.now_s += 900
    assert sh.run()[0] == 0


def test_no_entry_goes_out_while_coinbase_is_closed(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(1_000.0, dt=600)
    fake.now_s += 300
    sh.run()
    sh.breakout()
    fake.close_market()
    sh.run()
    assert sh.file("shadow", "book.json")["would_send"] == []
    assert fake.non_get() == []


# ---------------------------------------------------------------- the encrypted state

def test_the_state_round_trips_and_a_wrong_or_missing_key_refuses_without_touching_it(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    path = sh.dir / "BTC" / "state-shadow.enc.json"
    first = path.read_bytes()
    sh.run()
    second = path.read_bytes()
    assert first != second and json.loads(first)["salt"] != json.loads(second)["salt"]    # fresh salt and iv
    for f in ("account.json", "desk.json", "venue.json", "book.json", "live_position.json"):
        assert f in sh.state()["files"]
    # a wrong key: refused, nothing written over the saved state, nothing read from Coinbase
    n = len(fake.requests)
    out = tmp_path / "gh_output"
    sh.env["GITHUB_OUTPUT"] = str(out)
    sh.env["DESK_STATE_KEY"] = "another-key-" + "z" * 30
    runs = sh.status()["runs"]
    code, r = sh.run()
    assert code == 2 and r.problem == "key" and path.read_bytes() == second
    # the state stays as it was; only the public status says what's wrong (the save step saves it alone)
    assert len(fake.requests) == n and out.read_text() == "changed=true\n"
    st = sh.status()
    assert st["ok"] is False and st["problem"] == "key" and st["runs"] == runs
    assert st["note"] == "DESK_STATE_KEY doesn't open the saved state"
    assert r.public_line() == ("SHADOW BTC: refused before reading the account: DESK_STATE_KEY doesn't open the saved "
                               "state (nothing read or sent; the state is unchanged)")
    # too short, or missing: refused before any network call, and the status names which
    for bad, note in (("short", "DESK_STATE_KEY is too short (it needs thirty-two characters or more)"),
                      ("", "DESK_STATE_KEY is not set")):
        sh.env["DESK_STATE_KEY"] = bad
        assert sh.run()[0] == 2 and len(fake.requests) == n and path.read_bytes() == second
        assert sh.status()["note"] == note and sh.status()["runs"] == runs
    sh.env["DESK_STATE_KEY"] = KEY
    sh.env["COINBASE_CDP_KEY"] = ""
    assert sh.run()[0] == 2 and len(fake.requests) == n


def test_the_board_and_the_state_cant_be_swapped(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    board_text = (sh.dir / "BTC" / "board-shadow.enc.json").read_text()
    with pytest.raises(statebox.BoxError):
        statebox.open_json(board_text, KEY, statebox.state_aad("shadow"))
    (sh.dir / "BTC" / "state-shadow.enc.json").write_text(board_text)
    code, r = sh.run()
    assert code == 2 and r.problem == "state"


def test_shadow_refuses_when_live_runs_on_the_account(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    (sh.dir / "BTC").mkdir(parents=True)
    (sh.dir / "BTC" / "state-live.enc.json").write_text("{}")
    code, r = sh.run()
    assert code == 2 and r.problem == "refused" and fake.requests == []


def test_a_rerun_with_nothing_new_changes_nothing_but_the_run_count(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    _long(sh)
    before = {k: sh.file("shadow", k) for k in ("book.json", "desk.json")}
    pushes = len(sh.pushes)
    sh.run()
    after = {k: sh.file("shadow", k) for k in ("book.json", "desk.json")}
    assert after["book.json"]["would_send"] == before["book.json"]["would_send"]
    assert after["book.json"]["fills"] == before["book.json"]["fills"]
    assert after["desk.json"]["events"] == before["desk.json"]["events"]
    assert after["desk.json"]["meta"]["runs"] == before["desk.json"]["meta"]["runs"] + 1
    assert len(sh.pushes) == pushes


# ---------------------------------------------------------------- the public log: one line, nothing private

@pytest.fixture
def realtime_fake():
    """A fake on this machine's clock: the CLI signs with the real one."""
    import time

    with FakeCoinbase(cbi=0.0, now_s=time.time()) as f:
        f.realtime = True
        yield f


def test_the_cli_prints_only_the_masks_and_one_line_with_no_amounts(realtime_fake, tmp_path, monkeypatch, capsys):
    from desk import __main__ as cli

    fake = realtime_fake
    sh = Shadow(fake, tmp_path)
    env = {**sh.env, "DESK_COINBASE_TEST_URL": fake.base_url, "GITHUB_ACTIONS": "true"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    fake.deposit(1_000.0, dt=0)
    assert cli.main(["shadow", "--coin", "BTC", "--state-dir", str(sh.dir)]) == 0
    out, err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln]
    public = [ln for ln in lines if not ln.startswith("::add-mask::")]
    assert err == "" and len(public) == 1 and public[0].startswith("SHADOW BTC: run ")
    assert "$" not in public[0] and "1,000" not in public[0] and "coinbase-" not in public[0]
    masks = [ln[len("::add-mask::"):] for ln in lines if ln.startswith("::add-mask::")]
    assert sh.fk.name in masks and all("\n" not in m for m in masks)
    assert any("PRIVATE KEY" not in m and len(m) > 40 for m in masks)       # the PEM's base64 lines
    # a failure inside the run whose text holds amounts: still one fixed line
    from desk.brokers import live

    def boom(self, *a, **k):
        raise RuntimeError("balance $1,234.56 on order 0de70000-0000-0000-0000-000000000001")

    monkeypatch.setattr(live.LiveBroker, "check_stop_price", boom)
    assert cli.main(["shadow", "--coin", "BTC", "--state-dir", str(sh.dir)]) == 1
    out, err = capsys.readouterr()
    public = [ln for ln in out.splitlines() if ln and not ln.startswith("::add-mask::")]
    assert err == "" and public == ["SHADOW BTC: stopped: read (details on your phone and in the encrypted board)"]
    log = "\n".join(sh.board()["shadow"]["log"])
    assert "RuntimeError" in log                                             # private, encrypted: for the owner


def test_a_bad_401_says_so_on_the_phone_and_the_board_never_in_the_log(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.keys.clear()                            # the key was deleted in the portal
    code, r = sh.run()
    assert code == 1 and r.problem == "auth"
    assert any(t == "BTC SHADOW: not working" and "401" in b for t, b, *_ in sh.pushes)
    assert sh.status()["problem"] == "auth" and sh.status()["ok"] is False
    assert sh.file("shadow", "account.json")["days"][cr._day(fake.clock_ms())]["auth_fail"] == 1
    b = sh.board()                               # the last good board stays, with this run's checks
    assert b["status"]["ok"] is False and b["shadow"]["checks"]


# ---------------------------------------------------------------- LIVE --once, on the fake (LOCKED everywhere else)

def test_live_once_places_whole_contracts_and_a_resting_stop_on_the_fake(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-trade-key-not-real", trade=True))
        _write_proof(sh, fake)
        assert sh.run("live")[0] == 0
        sh.breakout()
        assert sh.run("live")[0] == 0
        p = fake.position()
        assert (p.side, p.n) == ("LONG", 1) and len(fake.stops()) == 1
        assert [r["path"].rsplit("/", 1)[-1] for r in fake.non_get()] == ["orders", "orders"]
        st = sh.status()
        assert st["mode"] == "LIVE" and st["decision"] is None and st["key_view_only"] is None
        assert "BTC LIVE: TRADE" in titles(sh)


@pytest.mark.parametrize("missing", ["phrase", "product"])
def test_live_once_refuses_and_sends_nothing_without_the_arming_settings(tmp_path, missing):
    with FakeCoinbase(cbi=1_000.0) as fake:
        sh = Shadow(fake, tmp_path, make_ec_key("test-trade-key-not-real", trade=True))
        _write_proof(sh, fake)
        bad = {"phrase": {"DESK_LIVE_CONFIRM": "yes"}, "product": {"DESK_LIVE_COINBASE": "BIT-30OCT26-CDE"}}[missing]
        code, r = cr.run_live_once(sh.dir, {**sh.env, **ARM_ENV, **bad}, max_notional=1_000.0, gate=[],
                                   base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep,
                                   push=lambda *a: sh.pushes.append(a), mask_emit=lambda s: None, iterations=IT)
        assert code == 2 and r.problem == "refused" and fake.non_get() == []
        assert any(ln.lstrip().startswith("FAIL") for ln in r.arm_lines)


def test_a_refusing_live_run_still_pushes_a_stop_resting_with_no_position(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        sh = Shadow(fake, tmp_path, make_ec_key("test-trade-key-not-real", trade=True))
        _write_proof(sh, fake)
        fake.orders["x1"] = {"order_id": "x1", "product_id": PRODUCT, "status": "OPEN", "side": "SELL",
                             "client_order_id": "c1", "created_time": "2026-10-13T14:00:00.000000Z",
                             "order_configuration": {"stop_limit_stop_limit_gtc": {
                                 "base_size": "1", "stop_price": "80000", "limit_price": "76000",
                                 "stop_direction": "STOP_DIRECTION_STOP_DOWN"}}, "filled_size": "0",
                             "trigger_status": "STOP_PENDING"}
        env = {**sh.env, **ARM_ENV, "DESK_LIVE_CONFIRM": ""}
        code, r = cr.run_live_once(sh.dir, env, max_notional=1_000.0, gate=[], base_url=fake.base_url,
                                   clock=fake.clock, sleep=fake.sleep, push=lambda *a: sh.pushes.append(a),
                                   mask_emit=lambda s: None, iterations=IT)
        assert code == 2 and fake.non_get() == []
        assert "BTC LIVE: STOP WITH NO POSITION" in titles(sh)


def test_the_proof_shadow_writes_is_what_arm_reads(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    proof = cr.shadow_proof(sh.file("shadow", "account.json"), sh.file("shadow", "desk.json")["meta"])
    assert proof["portfolio_type"] == "DEFAULT" and proof["portfolio_uuid"] and proof["deposits_reconciled"] == 1
    from desk import coinbase_live

    checks = dict((label, ok) for label, ok, _ in coinbase_live.shadow_checks(proof, fake.clock_ms()))
    assert checks["SHADOW reconciled a bank deposit to the cent"] is True
    assert checks["SHADOW ran 14+ days on this account"] is False          # one day old: LIVE can't arm on it


def test_clearing_entry_kill_in_kills_json_clears_the_boards_reason(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.run()
    fake.deposit(500.0, dt=600)
    fake.now_s += 300
    sh.run()
    fake.cbi += 40.0
    fake.now_s += 900
    sh.run()                                         # the move is seen: its 3-hour wait starts
    fake.now_s += 3 * 3600 + 60
    sh.run()                                         # the wait's over: alerted, entry_kill set
    assert sh.r.kills_set.get("entry_kill") is True
    # the save step would OR kills.set.json into the branch's kills.json: as the next run loads it
    (sh.dir / "BTC" / "kills.json").write_text(json.dumps({"entry_kill": True}))
    fake.now_s += 900
    sh.run()
    b = sh.board()
    assert b["kills"]["entry_kill"] is True and b["shadow"]["entry_kill_reason"].startswith("LIVE would be in")
    (sh.dir / "BTC" / "kills.json").write_text(json.dumps({"entry_kill": False}))     # you looked, and cleared it
    fake.now_s += 900
    sh.run()
    b = sh.board()
    assert b["kills"]["entry_kill"] is False and b["shadow"]["entry_kill_reason"] is None


def test_desk_coinbase_kill_entry_stops_simulated_entries(fake, tmp_path):
    sh = Shadow(fake, tmp_path)
    sh.env["DESK_COINBASE_KILL"] = "entry"
    sh.run()
    fake.deposit(1_000.0, dt=600)
    fake.now_s += 300
    sh.run()
    sh.breakout()
    sh.run()
    assert sh.file("shadow", "book.json")["would_send"] == []
    assert sh.board()["kills"]["entry_kill"] is True
