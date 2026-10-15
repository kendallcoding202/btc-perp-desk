"""The owner's first SHADOW runs, as they'll really go: the secrets added, one of them wrong, then fixed. Through the
workflow's load, run, check, stage and commit (tests/test_coinbase_kills_fail_closed.py's Branch and Job), on a
coinbase-state branch that doesn't exist yet. Each case that fails on the code before it (0e1f31b) says so.

Before: a first run refused over a secret (DESK_STATE_KEY too short, or not set; a blank COINBASE_CDP_KEY) saved its
public status alone (runs 0, started at that run), and the first run after the fix opened a state that started later,
which the save check refused at every run after ("would replace the saved SHADOW account (started T0, 0 runs) with
one started T1"): SHADOW never saved again.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import pytest

from desk import coinbase_state as cs
from fake_coinbase import FakeCoinbase, make_ec_key, make_ed_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import Shadow

PERMISSIONS = "/api/v3/brokerage/key_permissions"


@pytest.fixture
def first(tmp_path, monkeypatch):
    """A first SHADOW job: no coinbase-state branch yet, the View key in place."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("onboard-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        yield sh, fake, br, Job(sh, fake, br, None)


def _setup(fake, how):
    """What the owner got wrong on the first run: the run's environment over the right one (and the fake's side)."""
    if how == "state_key_short":
        return {"DESK_STATE_KEY": "my-20-char-password!"}
    if how == "state_key_unset":
        return {"DESK_STATE_KEY": None}
    if how == "cdp_key_blank":
        return {"COINBASE_CDP_KEY": "  "}
    if how == "cdp_key_wrong_json":
        return {"COINBASE_CDP_KEY": '{"name": "organizations/x/apiKeys/y", "privateKey": "-----BEGIN'}
    if how == "cdp_key_half_file":
        return {"COINBASE_CDP_KEY": '"privateKey": "-----BEGIN EC PRIVATE KEY-----"'}
    if how == "ed25519":
        return {"COINBASE_CDP_KEY": fake.add_key(make_ed_key("onboard-ed-not-real")).file_json()}
    if how == "trade_key":
        return {"COINBASE_CDP_KEY": fake.add_key(make_ec_key("onboard-trade-not-real", trade=True)).file_json()}
    if how == "deleted_key":
        return {"COINBASE_CDP_KEY": make_ec_key("onboard-unknown-not-real").file_json()}     # Coinbase: 401
    if how == "coinbase_down":
        fake.fail_next("GET", PERMISSIONS, 503, times=20)     # Coinbase down
        return {}
    raise AssertionError(how)


HOW = ["state_key_short", "state_key_unset", "cdp_key_blank", "cdp_key_wrong_json", "cdp_key_half_file", "ed25519",
       "trade_key", "deleted_key", "coinbase_down"]


@pytest.mark.parametrize("how", HOW)
def test_every_first_run_refusal_recovers_once_fixed_and_saves_from_then_on(first, how):
    sh, fake, br, job = first
    over = _setup(fake, how)
    for _ in range(2):                              # refused twice before the owner notices
        code, r, changed, saved = job.run(over=over)
        assert code != 0, (how, r.problem)
        assert saved is not False, (how, br.errors)
        _next(fake)
    st = job.status()
    if st is not None:                              # the dashboard says what's wrong, with no amount
        assert st["ok"] is False and st["problem"] in ("key", "auth", "permissions", "read")
    fake.fail.clear()
    code, r, changed, saved = job.run()             # fixed
    assert code == 0 and changed and saved, (how, r.problem, r.lines[-3:], br.errors)     # before: refused
    st = job.status()
    assert st["ok"] is True and st["problem"] is None and st["note"] is None, st    # the old reason is gone
    start, runs = st["started_ts"], st["runs"]
    _next(fake)
    code, r, changed, saved = job.run()             # and it carries on
    assert code == 0 and saved, br.errors
    st = job.status()
    assert st["started_ts"] == start and st["runs"] == runs + 1


def test_a_short_state_key_then_another_secret_wrong_then_both_fixed(first):
    sh, fake, br, job = first
    assert job.run(over={"DESK_STATE_KEY": "short"})[3]
    _next(fake)
    code, r, changed, saved = job.run(over={"COINBASE_CDP_KEY": "{not a key}"})     # a state now: its first save
    assert code != 0 and saved, br.errors
    st0 = job.status()
    assert st0["runs"] == 1 and st0["failed_runs"] == 1
    _next(fake)
    code, r, changed, saved = job.run()
    assert code == 0 and saved, br.errors
    st = job.status()
    assert st["started_ts"] == st0["started_ts"] and st["runs"] == 2 and st["ok"] is True


def test_a_saved_status_with_runs_and_no_state_file_still_refuses_a_younger_state(tmp_path, monkeypatch):
    """The rule's edge: a status saved with runs but no state file beside it (you deleted the state, say) isn't
    'nothing started yet': a state that started later is still refused."""
    from test_coinbase_kills_fail_closed import REF
    from test_coinbase_state import _git, _state_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    saved = _state_dir(tmp_path / "saved", runs=3, started=1_000)
    for name in ("state-shadow.enc.json", "kills.set.json"):
        (saved / "BTC" / name).unlink()
    _git("--work-tree", str(saved), "add", "-A", ".", cwd=repo)
    _git("commit", "-qm", "saved", cwd=repo)
    _git("update-ref", REF, "HEAD", cwd=repo)
    monkeypatch.chdir(repo)
    with pytest.raises(cs.StateCheckError):
        cs.check(_state_dir(tmp_path / "new", runs=1, started=5_000), REF)
    # and the status-only save of a run refused before it ever had a state (runs 0) is no account: a first save
    (saved / "BTC" / "status.json").write_text((saved / "BTC" / "status.json").read_text()
                                               .replace('"runs": 3', '"runs": 0'))
    _git("--work-tree", str(saved), "add", "-A", ".", cwd=repo)
    _git("commit", "-qm", "status only", cwd=repo)
    _git("update-ref", REF, "HEAD", cwd=repo)
    said = cs.check(_state_dir(tmp_path / "new2", runs=1, started=5_000), REF)
    assert any("first save" in ln for ln in said), said
