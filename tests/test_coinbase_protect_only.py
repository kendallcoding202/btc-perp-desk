"""An armed LIVE whose per-run arming check fails keeps protecting the position it holds (PROTECT ONLY), and the order
client itself enforces it. Each test fails on the code before it (0e1f31b), where any failed per-run check refused the
whole run before the safety pass: flat_kill was ignored, a stop-limit the price gapped past was never closed at
market, a stop cancelled by hand was never put back, and nothing urgent was pushed.

What any order needs, checked every run (failing it refuses as before; the read-only watchdog still looks and pushes
urgently): the key authenticates, is View + Trade and can't move money, on SHADOW's Default portfolio, the clock, the
state opens, and it's for BIP-20DEC30-CDE. What only gates NEW risk: --max-notional, USDC in the portfolio,
DESK_LIVE_CONFIRM, DESK_LIVE_COINBASE, the balance adding up (and, at the first start only, the go-live gate,
SHADOW's record, a flat account). Once LIVE has armed, failing only those gives a PROTECT-ONLY run.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import pytest

from desk import coinbase_live
from desk import coinbase_run as cr
from desk.brokers import coinbase_orders as co
from desk.brokers.coinbase_auth import load_cdp_key
from desk.brokers.coinbase_rest import OrdersLocked
from fake_coinbase import ARM_ENV, PRODUCT, V3, FakeCoinbase, Pos, good_proof, make_ec_key
from test_coinbase_kills_fail_closed import _kills, _next, world  # noqa: F401
from test_coinbase_shadow import IT


def _armed_long(sh, fake, job):
    _next(fake)
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved, (r.arm_lines, job.br.errors)
    sh.breakout()
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved and fake.position() is not None and len(fake.stops()) == 1, (r.lines[-8:],
                                                                                           job.br.errors)
    return float(fake.stops()[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"])


def _live(job, *, cap=1_000.0, over=None):
    """The README's LIVE step through the job's load and save, with its own --max-notional and environment."""
    sh, fake = job.sh, job.fake
    job.br.load(sh.dir)
    if job.out.exists():
        job.out.unlink()
    env = {**sh.env, "GITHUB_OUTPUT": str(job.out), **ARM_ENV, "COINBASE_CDP_KEY": job.tk.file_json(), **(over or {})}
    env = {k: v for k, v in env.items() if v is not None}
    code, r = cr.run_live_once(sh.dir, env, max_notional=cap, gate=[("backtest: passes", True, "ok")],
                               base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep,
                               push=lambda *a: sh.pushes.append(a), mask_emit=lambda s: None, iterations=IT)
    changed = job.out.exists() and job.out.read_text().strip().splitlines()[-1] == "changed=true"
    saved = job.br.save(sh.dir) if changed else None
    return code, r, changed, saved


def _balance_off(fake):
    """Coinbase's balance answer stops adding up (spot + futures isn't the total)."""
    orig = fake._balance

    def off():
        status, body = orig()
        s = body["balance_summary"]
        s["total_usd_balance"] = {"value": f"{float(s['total_usd_balance']['value']) + 50:.2f}", "currency": "USD"}
        return status, body
    fake._balance = off


# each check that gates only new risk, and how a run fails it
RISK = {
    "confirm_deleted": ({"over": {"DESK_LIVE_CONFIRM": None}}, "DESK_LIVE_CONFIRM"),
    "product_variable": ({"over": {"DESK_LIVE_COINBASE": "BIP"}}, "DESK_LIVE_COINBASE"),
    "no_max_notional": ({"cap": None}, "--max-notional"),
    "cap_below_one_contract": ({"cap": "below"}, "--max-notional"),
    "usdc_in_portfolio": ({"usdc": 50.0}, "USDC"),
    "balance_off": ({"balance_off": True}, "balance"),
}


def _fail(job, how):
    kw, _ = RISK[how]
    fake = job.fake
    if kw.get("usdc"):
        fake.usdc = kw["usdc"]
    if kw.get("balance_off"):
        _balance_off(fake)
    cap = kw.get("cap", 1_000.0)
    if cap == "below":
        cap = 0.01 * fake.last * 0.98               # BTC rose past what the cap buys
    return dict(cap=cap, over=dict(kw.get("over") or {}))


def _protect_pushes(sh, n0):
    return [(t, b, p) for t, b, p, *_ in sh.pushes[n0:] if t == "BTC LIVE: PROTECT ONLY"]


# ---------------------------------------------------------------- flat_kill is honoured

@pytest.mark.parametrize("how", sorted(RISK))
def test_flat_kill_closes_the_position_when_only_a_new_risk_check_fails(world, how):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    args = _fail(job, how)
    args["over"]["DESK_COINBASE_KILL"] = "flat"
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, **args)
    assert fake.position() is None and fake.resting() == [], (how, r.problem, r.arm_lines[-4:])   # before: open
    assert code == 2 and changed and saved, br.errors
    assert fake.max_closing <= 0
    got = _protect_pushes(sh, n0)
    assert len(got) == 1 and got[0][2] == "urgent", sh.pushes[n0:]
    assert RISK[how][1] in got[0][1] and "flat_kill" in got[0][1] and "closed" in got[0][1]
    st = job.status()
    assert st["mode"] == "LIVE" and st["armed"] is True and st["ok"] is False and st["problem"] == "refused"
    assert st["note"].startswith("protect only")
    posts = [q["path"].rsplit("/", 1)[-1] for q in fake.non_get()]
    assert "close_position" in posts


# ---------------------------------------------------------------- a stop the price gapped past is closed at market

@pytest.mark.parametrize("how", ["confirm_deleted", "usdc_in_portfolio", "cap_below_one_contract"])
def test_a_gapped_stop_limit_is_closed_at_market_when_only_a_new_risk_check_fails(world, how):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.trade(fake.last * 0.85)                    # through the trigger and the limit at once: triggered, unfilled
    args = _fail(job, how)
    o = fake.stops()[0]
    assert o["trigger_status"] == "STOP_TRIGGERED" and o["status"] == "OPEN"
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, **args)
    assert fake.position() is None and fake.resting() == [], (r.problem, r.lines[-6:])        # before: still open
    assert code == 2 and saved
    got = _protect_pushes(sh, n0)
    assert len(got) == 1 and got[0][2] == "urgent" and "closed" in got[0][1], sh.pushes[n0:]


def test_a_gapped_stop_limit_is_closed_by_the_safety_pass_of_a_full_run_too(world):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.trade(fake.last * 0.85)
    _next(fake)
    code, r, changed, saved = job.run("live")
    assert fake.position() is None and fake.resting() == [] and saved, r.lines[-6:]
    assert any("past its limit" in s or "triggered" in s for s in r.safety_said), r.safety_said


# ---------------------------------------------------------------- a stop cancelled by hand is put back, never wider

@pytest.mark.parametrize("how", ["confirm_deleted", "usdc_in_portfolio", "balance_off"])
def test_a_missing_stop_is_put_back_at_the_saved_level_when_only_a_new_risk_check_fails(world, how):
    sh, fake, br, job = world
    level = _armed_long(sh, fake, job)
    for o in fake.stops():
        o["status"] = "CANCELLED"                   # you cancelled the stop in the Coinbase app
    args = _fail(job, how)
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, **args)
    stops = fake.stops()
    assert len(stops) == 1, r.lines[-6:]                                                     # before: none
    got = float(stops[0]["order_configuration"]["stop_limit_stop_limit_gtc"]["stop_price"])
    assert got >= level - co.PRICE_STEP                                                     # never wider
    assert fake.position() is not None and code == 2 and saved
    assert fake.max_closing <= 0
    push = _protect_pushes(sh, n0)
    assert len(push) == 1 and "placed" in push[0][1], sh.pushes[n0:]


# ---------------------------------------------------------------- no entry, no add, no stop moved; pushed every run

@pytest.mark.parametrize("how", sorted(RISK))
def test_protect_only_sends_no_entry_and_nothing_when_the_position_is_protected(world, how):
    sh, fake, br, job = world
    _next(fake)
    assert job.run("live")[3]
    args = _fail(job, how)
    sh.breakout()                                   # a setup a full run would enter on
    n_post = len(fake.non_get())
    n0 = len(sh.pushes)
    code, r, changed, saved = _live(job, **args)
    assert fake.position() is None and len(fake.non_get()) == n_post, fake.non_get()[n_post:]
    assert code == 2 and saved
    push = _protect_pushes(sh, n0)
    assert len(push) == 1 and push[0][2] == "urgent" and RISK[how][1] in push[0][1], sh.pushes[n0:]
    assert "flat_kill" in push[0][1]                # it says how to close a position
    assert r.public_line().startswith("LIVE BTC: protect only")


def test_protect_only_with_a_protected_position_sends_nothing_and_pushes_at_every_run(world):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    rest0 = [o["order_id"] for o in fake.resting()]
    n_post = len(fake.non_get())
    for _ in range(3):
        _next(fake)
        n0 = len(sh.pushes)
        code, r, changed, saved = _live(job, over={"DESK_LIVE_CONFIRM": None})
        assert code == 2 and saved and len(_protect_pushes(sh, n0)) == 1
    assert len(fake.non_get()) == n_post and [o["order_id"] for o in fake.resting()] == rest0
    assert fake.position() is not None
    # the confirmation back: a full run again, carrying on from the same state
    _next(fake)
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved and job.status()["ok"] is True and job.status()["note"] is None


def test_a_live_that_never_armed_and_fails_a_new_risk_check_still_sends_nothing(world):
    sh, fake, br, job = world
    head = br.files()
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)      # someone's position, no stop
    _next(fake)
    code, r, changed, saved = _live(job, over={"DESK_LIVE_CONFIRM": None, "DESK_COINBASE_KILL": "flat"})
    assert code == 2 and not changed and fake.non_get() == [] and br.files() == head
    assert not any(t == "BTC LIVE: PROTECT ONLY" for t, *_ in sh.pushes)


# ---------------------------------------------------------------- failing what any order needs: refused, watched

@pytest.mark.parametrize("bad", ["transfer", "view_only", "other_portfolio"])
def test_failing_what_any_order_needs_refuses_and_the_watchdog_pushes_a_gapped_stop_and_flat_kill(world, bad):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fk = make_ec_key(f"r3-{bad}-not-real", trade=bad != "view_only", transfer=bad == "transfer")
    if bad == "other_portfolio":
        fk.portfolio_uuid = "99999999-2222-3333-4444-555555555555"
    fake.add_key(fk)
    fake.trade(fake.last * 0.85)                    # the stop triggered, unfilled
    n_post = len(fake.non_get())
    for _ in range(2):                              # every run
        _next(fake)
        n0 = len(sh.pushes)
        code, r, changed, saved = _live(job, over={"COINBASE_CDP_KEY": fk.file_json(), "DESK_COINBASE_KILL": "flat"})
        assert code == 2 and saved and len(fake.non_get()) == n_post, fake.non_get()[n_post:]
        urgent = [(t, b) for t, b, p, *_ in sh.pushes[n0:] if p == "urgent"]
        assert any(t == "BTC LIVE: STOP DIDN'T FILL" for t, _ in urgent), sh.pushes[n0:]
        assert any(t == "BTC LIVE: FLAT_KILL NOT DONE" for t, _ in urgent), sh.pushes[n0:]
    assert fake.position() is not None


def test_a_state_that_wont_open_refuses_and_the_watchdog_still_pushes(world):
    sh, fake, br, job = world
    _armed_long(sh, fake, job)
    fake.trade(fake.last * 0.85)
    _next(fake)
    n0 = len(sh.pushes)
    n_post = len(fake.non_get())
    code, r, changed, saved = _live(job, over={"DESK_STATE_KEY": "w" * 40})
    assert code == 2 and len(fake.non_get()) == n_post
    assert any(t == "BTC LIVE: STOP DIDN'T FILL" and p == "urgent" for t, b, p, *_ in sh.pushes[n0:]), sh.pushes[n0:]


# ---------------------------------------------------------------- the order client enforces it, not only the caller

def _protect_client(fake, live_state=None):
    fk = fake.add_key(make_ec_key("r3-trade-not-real", trade=True))
    out: list[str] = []
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), env={**ARM_ENV, "DESK_LIVE_CONFIRM": ""},
                            max_notional=1_000.0, gate=[], shadow_proof=good_proof(fake.clock_ms()),
                            live_state={"armed_at": 1, **(live_state or {})}, base_url=fake.base_url,
                            clock=fake.clock, sleep=fake.sleep, out=out.append)
    assert got is not None, out
    client, token = got
    assert token.scope == "protect" and any("protect only" in ln.lower() for ln in out), out
    client.min_gap_s = 0
    return client, token


def test_arm_gives_a_protect_only_token_only_once_live_has_armed(fake_cb):
    fake = fake_cb
    fk = fake.add_key(make_ec_key("r3-trade2-not-real", trade=True))
    kw = dict(env={**ARM_ENV, "DESK_LIVE_CONFIRM": ""}, max_notional=1_000.0, gate=[], base_url=fake.base_url,
              clock=fake.clock, sleep=fake.sleep, out=lambda *a: None)
    assert coinbase_live.arm(load_cdp_key(fk.file_json()), shadow_proof=good_proof(fake.clock_ms()), **kw) is None
    assert fake.requests == []                      # a first start: refused before any request, as before
    client, token = coinbase_live.arm(load_cdp_key(fk.file_json()), live_state={"armed_at": 1}, **kw)
    assert token.scope == "protect" and token.failed == ("DESK_LIVE_CONFIRM isn't the exact phrase",)
    full = coinbase_live.arm(load_cdp_key(fk.file_json()), live_state={"armed_at": 1}, **{**kw, "env": ARM_ENV})
    assert full[1].scope == "trade" and full[1].failed == ()


@pytest.fixture
def fake_cb():
    with FakeCoinbase(cbi=1_000.0) as f:
        yield f


def test_the_protect_client_refuses_an_entry_and_anything_that_could_grow_a_position(fake_cb):
    fake = fake_cb
    client, _ = _protect_client(fake)
    cid = co.client_order_id("entry", "BTC", 1)
    with pytest.raises(OrdersLocked, match="protect only"):
        client.post(f"{V3}/orders", co.market(cid, "BUY", 1))                       # flat: an entry
    with pytest.raises(OrdersLocked, match="protect only"):
        client.post(f"{V3}/orders", co.stop_limit(cid, "SELL", 1, 80_000.0))        # flat: a stop opens one
    with pytest.raises(OrdersLocked, match="protect only"):
        client.post(f"{V3}/orders/close_position", co.close_position(cid, 1))       # flat: nothing to close
    fake.positions[PRODUCT] = Pos("LONG", 2, 83_000.0, 83_000.0)
    for body in (co.market(cid, "BUY", 1), co.market(cid, "SELL", 1),             # an add, or a market sell
                 co.stop_limit(cid, "BUY", 1, 90_000.0),                            # the wrong side
                 co.stop_limit(cid, "SELL", 3, 80_000.0)):                          # more than is held
        with pytest.raises(OrdersLocked, match="protect only"):
            client.post(f"{V3}/orders", body)
    with pytest.raises(OrdersLocked, match="protect only"):
        client.post(f"{V3}/orders/close_position", co.close_position(cid, 3))
    assert fake.non_get() == []                     # every one refused before anything was sent
    client.post(f"{V3}/orders", co.stop_limit(cid, "SELL", 2, 80_000.0))             # a stop that only shrinks it
    with pytest.raises(OrdersLocked, match="protect only"):                        # a second one would over-close
        client.post(f"{V3}/orders", co.stop_limit(co.client_order_id("x", "BTC", 2), "SELL", 1, 80_000.0))
    oid = fake.stops()[0]["order_id"]
    client.post(f"{V3}/orders/batch_cancel", co.batch_cancel([oid]))                # a cancel: always
    client.post(f"{V3}/orders/close_position", co.close_position(co.client_order_id("c", "BTC", 3), 2))
    assert fake.position() is None
    assert [q["path"].rsplit("/", 1)[-1] for q in fake.non_get()] == ["orders", "batch_cancel", "close_position"]


def test_the_protect_client_never_places_a_stop_wider_than_the_saved_one(fake_cb):
    fake = fake_cb
    fake.positions[PRODUCT] = Pos("LONG", 1, 83_000.0, 83_000.0)
    client, token = _protect_client(fake, {"side": "long", "stop": 80_000.0})
    cid = co.client_order_id("stop", "BTC", 1)
    with pytest.raises(OrdersLocked, match="wider"):
        client.post(f"{V3}/orders", co.stop_limit(cid, "SELL", 1, 79_000.0))
    assert fake.non_get() == []
    client.post(f"{V3}/orders", co.stop_limit(cid, "SELL", 1, 80_000.0))
    assert len(fake.stops()) == 1
