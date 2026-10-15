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


# ---------------------------------------------------------------- round 4: the wrong portfolio, pushes, the password
# Each fails on the code before it (da2c7f8): a key on another portfolio ran as OK, opened the account on that
# portfolio's money, and the fix the desk's own alert asked for read as a +$700 move nothing explained (entry_kill set,
# LIVE's 7 clean days restarted); a refusal with no state pushed only in a 15-minute slot every 6 hours (a password
# changed at 15:00 said nothing until 18:00); nothing said when SHADOW worked again.

OTHER_PF = "99999999-2222-3333-4444-555555555555"


def _other_portfolio(fake, label, kind="CONSUMER", cbi=300.0):
    """A key on another portfolio, and that portfolio's own money (its USD account, no transactions)."""
    k = fake.add_key(make_ec_key(label, portfolio_uuid=OTHER_PF, portfolio_type=kind))
    default = (fake.usd_account, fake.cbi, list(fake.transactions))
    fake.usd_account, fake.cbi, fake.transactions = "bbbbbbbb-0000-4000-8000-000000000001", cbi, []

    def back():
        fake.usd_account, fake.cbi, fake.transactions = default
    return k, back


def _titles(sh, n0=0):
    return [p[0] for p in sh.pushes[n0:]]


@pytest.mark.parametrize("other_cbi", [0.0, 300.0, 5000.0])
def test_a_key_on_another_portfolio_is_refused_and_the_default_key_then_opens_cleanly(first, other_cbi):
    sh, fake, br, job = first
    k, back = _other_portfolio(fake, f"onb4-pf-{int(other_cbi)}-not-real", cbi=other_cbi)
    for _ in range(2):
        code, r, changed, saved = job.run(over={"COINBASE_CDP_KEY": k.file_json()})
        assert code == 2 and r.problem == "permissions" and saved is not False, br.errors   # before: OK, opened
        assert not any("/cfm/balance_summary" in q["path"] for q in fake.requests[-12:])
        _next(fake)
    st = job.status()
    assert st["ok"] is False and st["note"] == "the key isn't on the Default portfolio"
    assert any("not Default" in b for _, b, *_ in sh.pushes)
    back()
    n0 = len(sh.pushes)
    for _ in range(16):                             # past the 3-hour wait for a move nothing explains
        code, r, changed, saved = job.run()
        assert code == 0 and saved, (r.lines[-4:], br.errors)
        _next(fake)
    titles = _titles(sh, n0)
    assert not any("ALERT" in t for t in titles), [p[:2] for p in sh.pushes[n0:]]      # before: "+700 ... ALERT"
    assert "BTC/kills.desk.json" not in br.files()
    meta = sh.file("shadow", "desk.json")["meta"]
    assert meta["opened"]["amount"] == 1_000.0                          # the Default portfolio's money
    days = sh.file("shadow", "account.json")["days"]
    assert not any(d.get("unexplained") for d in days.values())
    assert job.status()["ok"] is True and job.status()["note"] is None


def test_a_key_on_another_logins_default_portfolio_is_another_account_not_money_moving(first):
    sh, fake, br, job = first
    for _ in range(2):
        assert job.run()[0] == 0
        _next(fake)
    assert sh.file("shadow", "desk.json")["meta"]["opened"]["amount"] == 1_000.0
    k, _ = _other_portfolio(fake, "onb4-login2-not-real", kind="DEFAULT", cbi=300.0)
    n0 = len(sh.pushes)
    switched = fake.clock_ms()
    for _ in range(16):
        code, r, changed, saved = job.run(over={"COINBASE_CDP_KEY": k.file_json()})
        assert code == 0 and saved, (r.lines[-4:], br.errors)
        _next(fake)
    bodies = [b for _, b, *_ in sh.pushes[n0:]]
    assert any("another account, not money moving" in b for b in bodies), bodies
    assert not any("isn't explained" in b or "nothing the desk can read explains" in b for b in bodies), bodies
    assert "BTC/kills.desk.json" not in br.files() and not cs.read_kills(br.files()["BTC/kills.json"])[0]["entry_kill"]
    meta = sh.file("shadow", "desk.json")["meta"]
    assert meta["opened"]["amount"] == 300.0 and meta["periods"][-1]["start_equity"] == 1_000.0
    acct = sh.file("shadow", "account.json")
    assert acct["portfolio_uuid"] == OTHER_PF and abs(acct["portfolio_since_ts"] - switched) < 60_000
    from desk import coinbase_run as cr
    assert cr.shadow_proof(acct, meta)["started_ts"] == acct["portfolio_since_ts"]     # its 14 days start again


def test_a_first_refusal_with_no_state_is_pushed_at_once_then_every_6_hours(first):
    sh, fake, br, job = first
    fake.now_s = (fake.now_s // 21600) * 21600 + 3 * 3600 + 4 * 60      # 15:04 UTC: outside the 6-hourly window
    over = {"DESK_STATE_KEY": "x" * 31}
    n0 = len(sh.pushes)
    assert job.run(over=over)[3] is not False
    assert _titles(sh, n0) == ["BTC SHADOW: not working"]               # before: nothing until 18:04
    assert ".." not in sh.pushes[-1][1]
    for _ in range(3):
        _next(fake)
        n0 = len(sh.pushes)
        job.run(over=over)
        assert _titles(sh, n0) == []                                    # the same one again: not news
    fake.now_s = (fake.now_s // 21600) * 21600 + 21600 + 10 * 60        # 18:10: the window
    n0 = len(sh.pushes)
    job.run(over=over)
    assert _titles(sh, n0) == ["BTC SHADOW: not working"]
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run()
    assert code == 0 and saved and "BTC SHADOW: connected (read only)" in _titles(sh, n0)


def test_a_changed_state_key_is_pushed_at_once_and_working_again_when_the_old_one_is_back(first):
    sh, fake, br, job = first
    fake.now_s = (fake.now_s // 21600) * 21600 + 3 * 3600 + 4 * 60
    assert job.run()[0] == 0
    new = {"DESK_STATE_KEY": "a-new-password-of-more-than-32-chars-xyz"}
    _next(fake)
    n0 = len(sh.pushes)
    job.run(over=new)
    assert _titles(sh, n0) == ["BTC SHADOW: not working"]
    assert "doesn't open the saved" in sh.pushes[-1][1]
    _next(fake)
    n0 = len(sh.pushes)
    job.run(over=new)
    assert _titles(sh, n0) == []
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run()
    assert code == 0 and saved and _titles(sh, n0) == ["BTC SHADOW: working again"], _titles(sh, n0)
    assert "DESK_STATE_KEY doesn't open the saved state" in sh.pushes[-1][1]


def test_a_run_that_stops_part_way_then_works_says_working_again_and_never_doubles_a_period(first):
    sh, fake, br, job = first
    assert job.run()[0] == 0
    _next(fake)
    fake.fail_next("GET", PERMISSIONS, 503, times=20)
    n0 = len(sh.pushes)
    assert job.run()[0] != 0
    assert _titles(sh, n0) == ["BTC SHADOW: not working"] and ".." not in sh.pushes[-1][1]
    fake.fail.clear()
    _next(fake)
    n0 = len(sh.pushes)
    assert job.run()[0] == 0
    assert _titles(sh, n0) == ["BTC SHADOW: working again"]


def test_a_shadow_save_that_will_be_refused_is_pushed_at_its_first_run_outside_the_window(first):
    sh, fake, br, job = first
    fake.now_s = (fake.now_s // 21600) * 21600 + 3 * 3600 + 4 * 60
    assert job.run()[0] == 0
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run(over={"GITHUB_ACTIONS": "true"})
    assert saved is False and _titles(sh, n0) == ["BTC SHADOW: NOT SAVED"]   # before: nothing until 18:04
    _next(fake)
    n0 = len(sh.pushes)
    job.run(over={"GITHUB_ACTIONS": "true"})
    assert _titles(sh, n0) == []
