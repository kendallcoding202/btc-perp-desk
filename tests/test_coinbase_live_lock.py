"""LIVE is locked in code: desk/coinbase_live.py's LIVE_REVIEWED is False until a review of its stop handling comes back
clean (docs/coinbase-live-status.md). arm() checks it first, before any other check, read or request, and LiveRun
before it loads anything: every LIVE attempt refuses with one plain reason, sends nothing (not one request) and leaves
the coinbase-state branch byte for byte as it was, whatever you set. SHADOW runs exactly as it does with the lock open.
These tests see the lock as the code has it (the live_locked marker: tests/conftest.py opens it for the other LIVE
tests); each fails on the code before it (4c0d11c), which had no such lock.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from desk import __main__ as cli
from desk import coinbase_live
from desk import coinbase_run as cr
from desk import statebox
from desk.brokers.coinbase_auth import load_cdp_key
from desk.brokers.coinbase_rest import OrdersLocked
from fake_coinbase import ARM_ENV, PRODUCT, FakeCoinbase, arm_rest, good_proof, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import IT, KEY, Shadow, _write_proof

pytestmark = pytest.mark.live_locked
ROOT = Path(__file__).resolve().parents[1]
GH = {"GITHUB_ACTIONS": "true"}
LOCKED = "LIVE isn't finished: its last review found open safety problems, see docs/coinbase-live-status.md"


def test_the_lock_is_shut_in_the_code():
    tree = ast.parse((ROOT / "desk" / "coinbase_live.py").read_text())
    got = [n.value.value for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
           and getattr(n.targets[0], "id", None) == "LIVE_REVIEWED" and isinstance(n.value, ast.Constant)]
    assert got == [False]                                   # one plain assignment, at the top level: a code change
    assert coinbase_live.LIVE_REVIEWED is False and coinbase_live.locked() and coinbase_live.LOCKED == LOCKED
    for name in ("os.environ", "getenv", "environ.get"):    # nothing read from outside can set it
        assert name not in (ROOT / "desk" / "coinbase_live.py").read_text().split("def arm(")[0]


@pytest.fixture
def fake():
    with FakeCoinbase(cbi=1_000.0) as f:
        f.now_s += 8 * 60
        yield f


def _arming_inputs(fake):
    """Every set of arming inputs the LIVE tests arm with (tests/test_coinbase_live.py, test_coinbase_protect_only.py,
    fake_coinbase.arm_rest): a first start with every check passing, a first start forced past the paper gate, an
    armed LIVE's next run, and an armed LIVE's protect-only run."""
    proof = good_proof(fake.clock_ms())
    gate = [("backtest: passes", True, "ok")]
    return {"first_start": dict(env=ARM_ENV, max_notional=1_000.0, gate=gate, shadow_proof=proof),
            "force_unproven": dict(env=ARM_ENV, max_notional=1_000.0, gate=[("paper traded >= 14 days", False, "3")],
                                   force_unproven=True, shadow_proof=proof),
            "armed_next_run": dict(env=ARM_ENV, max_notional=1_000.0, gate=[], live_state={"armed_at": 1}),
            "protect_only": dict(env={**ARM_ENV, "DESK_LIVE_CONFIRM": ""}, max_notional=None, gate=[],
                                 live_state={"armed_at": 1, "side": "long", "stop": 80_000.0})}


@pytest.mark.parametrize("which", ["first_start", "force_unproven", "armed_next_run", "protect_only"])
def test_arm_refuses_first_under_every_input_that_would_otherwise_arm(fake, monkeypatch, which):
    fk = fake.add_key(make_ec_key("lock7-trade-not-real", trade=True))
    kw = dict(base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep)

    def gate_read():
        raise AssertionError("the lock is checked before the go-live gate is read")
    inputs = _arming_inputs(fake)[which]
    out: list[str] = []
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), out=out.append, **kw,
                            **{**inputs, "gate": gate_read if which == "first_start" else inputs["gate"]})
    assert got is None and fake.requests == [], fake.requests           # not one request of any kind
    assert out[-1].strip() == f"Not going live on Coinbase: {LOCKED}" and len(out) == 2, out
    monkeypatch.setattr(coinbase_live, "LIVE_REVIEWED", True)           # the same inputs, the lock open: they arm
    got = coinbase_live.arm(load_cdp_key(fk.file_json()), out=lambda *a: None, **kw, **inputs)
    assert got is not None and got[1].scope == ("protect" if which == "protect_only" else "trade")


def test_no_token_can_be_made_or_used_while_locked(fake, monkeypatch):
    monkeypatch.setattr(coinbase_live, "LIVE_REVIEWED", True)
    rest = arm_rest(fake)                                   # an order client, made while the lock was open
    monkeypatch.setattr(coinbase_live, "LIVE_REVIEWED", False)
    n = len(fake.requests)
    with pytest.raises(OrdersLocked):
        rest.post("/api/v3/brokerage/orders", {"product_id": PRODUCT})
    assert len(fake.requests) == n and fake.non_get() == []
    with pytest.raises(TypeError, match="LIVE isn't finished"):
        coinbase_live.Armed(coinbase_live._SEAL, PRODUCT, 1_000.0, (), fake.clock())


# ---------------------------------------------------------------- a LIVE run, through the workflow's load and save

@pytest.fixture
def world(tmp_path, monkeypatch):
    """SHADOW has run here (its record good enough to arm, on the branch), and a trade key exists."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("lock7-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        tk = fake.add_key(make_ec_key("lock7-trade-not-real", trade=True))
        _write_proof(sh, fake)
        job = Job(sh, fake, br, tk)
        code, r = cr.run_shadow(sh.dir, {**sh.env, "GITHUB_OUTPUT": str(job.out)}, base_url=fake.base_url,
                                clock=fake.clock, sleep=fake.sleep, push=lambda *a: sh.pushes.append(a),
                                mask_emit=lambda s: None, iterations=IT)
        assert code == 0 and br.save(sh.dir), (r.lines[-3:], br.errors)       # the branch holds SHADOW's record
        yield sh, fake, br, job


LIVE_ATTEMPTS = {
    "every_check_set": {},
    "kill_variable_flat": {"DESK_COINBASE_KILL": "flat"},
    "no_state_key": {"DESK_STATE_KEY": None},
    "no_confirm": {"DESK_LIVE_CONFIRM": None},
}


@pytest.mark.parametrize("attempt", sorted(LIVE_ATTEMPTS))
def test_every_live_attempt_refuses_on_the_lock_and_leaves_the_branch_byte_identical(world, attempt):
    sh, fake, br, job = world
    before, n = br.files(), len(fake.requests)
    sh.pushes.clear()
    code, r, changed, saved = job.run(mode="live", over={**GH, **LIVE_ATTEMPTS[attempt]})
    assert code == 2 and not changed and saved is None, (code, changed, r.lines[-2:])
    assert br.files() == before and len(fake.requests) == n, fake.requests[n:]       # nothing read, sent or saved
    assert r.problem == "refused" and r.public_line() == (
        f"LIVE BTC: not armed: {LOCKED} (no order sent; nothing saved: the branch and SHADOW's state are as they were)")
    assert any(LOCKED in ln for ln in r.arm_lines)
    pushes = [(t, b) for t, b, *_ in sh.pushes]
    assert pushes and pushes[0][0] == "BTC LIVE: not armed" and LOCKED in pushes[0][1], pushes
    assert "SHADOW's last run is over 2 hours old" not in pushes[0][1]
    _next(fake)
    code, r, changed, saved = job.run(over=GH)                # SHADOW carries on as before
    assert code == 0 and saved and job.status()["mode"] == "SHADOW" and job.status()["ok"] is True


def test_an_armed_live_state_on_the_branch_gets_past_nothing(world, monkeypatch):
    """A LIVE state that has armed (one made with the lock open, or written by hand with DESK_STATE_KEY) skips the
    first-start checks; the lock is before all of them."""
    sh, fake, br, job = world
    with monkeypatch.context() as m:
        m.setattr(coinbase_live, "LIVE_REVIEWED", True)
        _next(fake)
        code, r, changed, saved = job.run(mode="live", over=GH)
        assert code == 0 and saved and r.meta.get("armed_at"), (r.lines[-3:], br.errors)
    before, n = br.files(), len(fake.requests)
    _next(fake)
    code, r, changed, saved = job.run(mode="live", over=GH)
    assert code == 2 and not changed and br.files() == before and len(fake.requests) == n
    assert r.public_line().startswith(f"LIVE BTC: not armed: {LOCKED}")


def test_the_cli_refuses_on_the_lock_whatever_the_flags(world, monkeypatch, capsys):
    sh, fake, br, job = world
    for k, v in {**sh.env, **ARM_ENV, "COINBASE_CDP_KEY": job.tk.file_json(),
                 "DESK_COINBASE_TEST_URL": fake.base_url}.items():
        monkeypatch.setenv(k, v)
    for k in ("NTFY_TOPIC", "NTFY_TOKEN", "GITHUB_ACTIONS", "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)
    before, n = br.files(), len(fake.requests)
    for flags in ([], ["--once"], ["--once", "--local"], ["--once", "--local", "--force-unproven"]):
        assert cli.main(["live", "--coin", "BTC", "--max-notional", "1000", "--state-dir", str(sh.dir), *flags]) == 2
        out = capsys.readouterr().out
        assert LOCKED in out and "::add-mask::" not in out, (flags, out)
    assert len(fake.requests) == n and br.files() == before


# ---------------------------------------------------------------- SHADOW: the same with the lock as without

def _shadow_runs(tmp_path, monkeypatch, fk, reviewed: bool) -> dict:
    monkeypatch.setattr(coinbase_live, "LIVE_REVIEWED", reviewed)
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, fk)
        out = {"codes": [], "pushes": [], "requests": [], "status": [], "board": [], "state": []}
        for k in range(3):
            if k == 1:
                sh.breakout()                                   # an entry SHADOW would send, and its stop
            elif k == 2:
                sh.close(sh.bars[-1].close * 1.01)
            sh.pushes.clear()
            code, r = sh.run()
            st = sh.status()
            out["codes"].append(code)
            out["pushes"].append(list(sh.pushes))
            out["status"].append(st)
            out["board"].append(sh.board())
            out["state"].append({k: v for k, v in sh.state()["files"].items()})
        out["requests"] = [(q["method"], q["path"], json.dumps(q["query"], sort_keys=True)) for q in fake.requests]
        return out


def test_shadow_runs_the_same_with_the_lock_shut_as_open(tmp_path, monkeypatch):
    fk = make_ec_key("lock7-same-view-not-real")
    shut = _shadow_runs(tmp_path / "shut", monkeypatch, fk, False)
    open_ = _shadow_runs(tmp_path / "open", monkeypatch, fk, True)
    assert shut["codes"] == [0, 0, 0] and any(t.startswith("BTC SHADOW: would") for p in shut["pushes"] for t, *_ in p)
    for k in shut:
        assert shut[k] == open_[k], k
    assert statebox.is_envelope(json.loads((tmp_path / "shut" / "cb" / "BTC" / "state-shadow.enc.json").read_text()))
    assert cr.run_shadow is not None and KEY


# ---------------------------------------------------------------- what the docs say

def test_the_status_page_lists_what_is_open_and_the_readme_and_guide_link_it():
    page = (ROOT / "docs" / "coinbase-live-status.md").read_text()
    assert "`LIVE_REVIEWED = False`" in page and "**SHADOW is unaffected.**" in page and LOCKED.split(", see")[0] in page
    items = [ln for ln in page.splitlines() if ln[:3] in {f"{k}. " for k in range(1, 10)}]
    assert len(items) == 6, items
    for word in ("gapped", "cancels them one by one", "grown in the app", "`flat_kill` while Coinbase is closed",
                 "cancel is already queued", "lagging an entry"):
        assert any(word in it for it in items), word
    readme = (ROOT / "README.md").read_text()
    live = readme.split("## Going live on Coinbase (locked)")[1].split("\n## ")[0]
    assert "[`docs/coinbase-live-status.md`](docs/coinbase-live-status.md)" in live and "`LIVE_REVIEWED = False`" in live
    going = readme.split("## Going live (only after paper earns it)")[1].split("\n## ")[0]
    assert "docs/coinbase-live-status.md" in going
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    part2 = guide.split("## Part 2:")[1]
    assert "[`coinbase-live-status.md`](coinbase-live-status.md)" in part2 and "**LIVE is locked in code" in part2
