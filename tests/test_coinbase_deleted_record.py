"""Round 8, your edits on the coinbase-state branch and what the phone hears, through the workflow's load, run,
check, stage and commit (tests/test_coinbase_kills_fail_closed.py's Branch and Job; the branch's commits are dated by
the fake's clock, as GitHub dates yours). Each fails on the code before it (0610893):

- the state and the status deleted (the state before the last kept), then a DESK_STATE_KEY slip: the slip's run saved a
  status that said no run had worked, and the next run, the secret fixed, started a new state over SHADOW's record and
  pushed it as a plain "connected". Now neither run writes anything, and the save check refuses any save beside the
  state before the last;
- BTC/status.json's "mode" edited to LIVE: never written back, every save refused with a reason that wasn't so. Now the
  next run writes it back, as for any other edit;
- the state, the status and the board deleted, or the state and the status deleted while a run was under way, or the
  status deleted and then a secret slip: no push for up to 6 hours (the refusals write nothing, so they had no memo).
  Now the commit the load step extracted is the memo: a refusal is news when the branch changed just before the run;
- a brand-new SHADOW on a coinbase-state branch you made yourself (only your kills.json, or a note) said "connected,
  started over". Now it's "connected (read only)": a start over only when the branch shows SHADOW worked there.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from desk import statebox
from fake_coinbase import FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import KEY, Shadow

ROOT = Path(__file__).resolve().parents[1]
STATE = "BTC/state-shadow.enc.json"
PREV = "BTC/state-shadow.prev.enc.json"
STATUS = "BTC/status.json"
BOARD = "BTC/board-shadow.enc.json"
GH = {"GITHUB_ACTIONS": "true"}
NODE = shutil.which("node")


def _world(fake, tmp_path, monkeypatch, label, runs):
    fake.now_s += 8 * 60
    sh = Shadow(fake, tmp_path, make_ec_key(label))
    br = Branch(tmp_path, monkeypatch)
    br.clock = fake.clock                   # your commits, and the saves, dated as the runs see time
    job = Job(sh, fake, br, None)
    for _ in range(runs):
        code, r, saved = _run(job)
        assert code == 0 and saved, (r.lines[-3:], br.errors)
        _next(fake)
    return sh, fake, br, job


@pytest.fixture
def worked(tmp_path, monkeypatch):
    """SHADOW, three runs saved (the branch has the state before the last too)."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        got = _world(fake, tmp_path, monkeypatch, "record8-view-not-real", 3)
        assert PREV in got[2].files()
        yield got


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """No run yet."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        yield _world(fake, tmp_path, monkeypatch, "record8-first-not-real", 0)


def _run(job, over=None, **kw):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over={**GH, **(over or {})}, **kw)
    return code, r, saved


def _titles(sh):
    return [p[0] for p in sh.pushes]


def _rm(*paths):
    def ch(d):
        for p in paths:
            (d / p).unlink()
    return ch


def _set_status(**kw):
    def ch(d):
        p = d / STATUS
        st = json.loads(p.read_text())
        st.update(kw)
        p.write_text(json.dumps(st, indent=1) + "\n")
    return ch


# ---------------------------------------------------------------- the record deleted, and a secret slip with it

@pytest.mark.parametrize("slip", ["short", "changed"])
def test_state_and_status_deleted_then_a_secret_slip_writes_nothing_and_never_starts_over(worked, slip):
    sh, fake, br, job = worked
    was = br.files()
    br.edit(_rm(STATE, STATUS))
    deleted = br.files()
    n0 = len(fake.requests)
    bad = "x" * 31 if slip == "short" else "another-state-key-" + "q" * 30
    code, r, saved = _run(job, {"DESK_STATE_KEY": bad})
    assert br.files() == deleted and not saved, sorted(set(br.files()) ^ set(deleted))   # before: a never-worked status
    assert len(fake.requests) == n0
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and "the state before them is" in got[0], _titles(sh)            # at once, and both are named
    assert ("And a secret is wrong too: DESK_STATE_KEY" if slip == "short" else
            "And DESK_STATE_KEY doesn't open the state before the last either") in got[0], got[0]
    _next(fake)
    code, r, saved = _run(job)                                  # the secret fixed, the record still deleted
    assert code == 2 and not saved and br.files() == deleted, (code, saved, _titles(sh))   # before: a new state
    assert "BTC SHADOW: connected (read only)" not in _titles(sh)
    br.edit(lambda d: [(d / p).write_bytes(was[p]) for p in (STATE, STATUS)])               # put back (a revert)
    _next(fake)
    code, r, saved = _run(job)
    st, old = job.status(), json.loads(was[STATUS])
    assert code == 0 and saved and st["started_ts"] == old["started_ts"] and st["runs"] == old["runs"] + 1


def test_a_status_that_says_no_run_worked_beside_the_state_before_is_no_status(worked):
    """What the code before saved at a slip beside a deleted record: a status with no runs. It doesn't let a new state
    start over the record."""
    sh, fake, br, job = worked
    old = json.loads(br.files()[STATUS])

    def ch(d):
        (d / STATE).unlink()
        (d / STATUS).write_text(json.dumps({**old, "runs": 0, "failed_runs": 0, "ok": False, "problem": "key",
                                            "note": "DESK_STATE_KEY is too short (it needs thirty-two characters or "
                                                    "more)", "opened": False, "started_ts": old["started_ts"] + 9},
                                           indent=1) + "\n")
    br.edit(ch)
    before = br.files()
    code, r, saved = _run(job)
    assert code == 2 and not saved and br.files() == before, (code, saved, _titles(sh), br.errors[-1:])
    assert "BTC SHADOW: connected (read only)" not in _titles(sh)


def test_the_save_check_refuses_any_save_beside_the_state_before_the_last(tmp_path):
    """The check itself: a status alone that says no run has worked, or a first save after one, beside the state
    before the last with no state is refused, as a new state there is."""
    state = statebox.seal_json({"x": 1}, KEY, statebox.state_aad("shadow"), iterations=1000).encode()

    class B:
        def __init__(self, files):
            self.files = files

        def tree(self):
            return {p: "100644" for p in self.files}

        def show(self, path):
            return self.files.get(path)

    def status(**kw):
        return {"v": 1, "mode": "SHADOW", "coin": "BTC", "product": "BIP-20DEC30-CDE", "updated": 9, "started_ts": 9,
                "runs": 0, "failed_runs": 0, "ok": False, "problem": "key", "opened": False,
                "note": "DESK_STATE_KEY is too short (it needs thirty-two characters or more)", **kw}
    d = tmp_path / "a" / "BTC"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps(status()))
    with pytest.raises(cs.StateCheckError, match="not starting a new state over the record"):
        cs.check(d.parent, branch=B({"README.md": b"x", PREV: state}))            # before: "a status-only save"
    assert "a status-only save" in cs.check(d.parent, branch=B({"README.md": b"x"}))[-1]
    e = tmp_path / "b" / "BTC"
    e.mkdir(parents=True)
    (e / "state-shadow.enc.json").write_bytes(state)
    (e / "status.json").write_text(json.dumps(status(runs=1, ok=True, problem=None, note=None, opened=True)))
    with pytest.raises(cs.StateCheckError, match="not starting a new state over the record"):
        cs.check(e.parent, branch=B({"README.md": b"x", PREV: state, STATUS: json.dumps(status()).encode()}))
    said = cs.check(e.parent, branch=B({"README.md": b"x", STATUS: json.dumps(status()).encode()}))
    assert "a first save" in said[-1]


# ---------------------------------------------------------------- the status's mode edited

@pytest.mark.parametrize("armed", [False, True])
def test_status_mode_edited_to_live_is_written_back(worked, armed):
    sh, fake, br, job = worked
    old = json.loads(br.files()[STATUS])
    br.edit(_set_status(mode="LIVE", **({"armed": True} if armed else {})))
    code, r, saved = _run(job)
    assert saved and _titles(sh) == ["BTC SHADOW: NOT SAVED"], (code, saved, _titles(sh), br.errors[-1:])
    assert "its mode, its start or its runs differ" in sh.pushes[0][1]
    st = job.status()
    assert st["mode"] == "SHADOW" and st["note"] == cs.STATUS_BACK and st["runs"] == old["runs"]
    _next(fake)
    code, r, saved = _run(job)
    assert code == 0 and saved and _titles(sh) == ["BTC SHADOW: working again"], (_titles(sh), br.errors[-1:])
    st = job.status()
    assert st["ok"] is True and st["mode"] == "SHADOW" and st["runs"] == old["runs"] + 1
    assert st["started_ts"] == old["started_ts"]


def test_another_modes_status_beside_that_modes_files_is_left_alone(tmp_path):
    """The control: a LIVE status with LIVE's files on the branch is LIVE's, never written over by SHADOW."""
    state = statebox.seal_json({"x": 1}, KEY, statebox.state_aad("shadow"), iterations=1000).encode()
    live = {"v": 1, "mode": "LIVE", "coin": "BTC", "product": "BIP-20DEC30-CDE", "updated": 9, "started_ts": 9,
            "runs": 3, "failed_runs": 0, "ok": True, "problem": None, "opened": True, "armed": True}
    back = {**live, "mode": "SHADOW", "ok": False, "problem": "state", "note": cs.STATUS_BACK}
    back.pop("armed")

    class B:
        def __init__(self, files):
            self.files = files

        def tree(self):
            return {p: "100644" for p in self.files}

        def show(self, path):
            return self.files.get(path)
    d = tmp_path / "BTC"
    d.mkdir()
    (d / "state-shadow.enc.json").write_bytes(state)
    (d / "status.json").write_text(json.dumps(back))
    on = {"README.md": b"x", STATE: state, STATUS: json.dumps(live).encode()}
    assert "written back" in cs.check(tmp_path, branch=B(on))[-1]
    with pytest.raises(cs.StateCheckError, match="the branch's state is LIVE"):
        cs.check(tmp_path, branch=B({**on, "BTC/state-live.enc.json": state}))


# ---------------------------------------------------------------- pushed at once, though nothing is written

def test_state_status_and_board_deleted_push_at_once_then_not_every_run(worked):
    sh, fake, br, job = worked
    br.edit(_rm(STATE, STATUS, BOARD))
    _next(fake, 5)                          # the next run comes 5 minutes after your commit
    code, r, saved = _run(job)
    assert code == 2 and not saved
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and "the state before them is" in got[0], _titles(sh)                 # before: nothing for hours
    assert r.public_line() == ("SHADOW BTC: refused (state) before reading the account: " + cr.RECORD_GONE +
                               " (nothing read, sent or saved)")
    _next(fake, 30)                         # a later run, outside the 6-hour windows' first minutes
    code, r, saved = _run(job)
    assert code == 2 and not saved and not r.in_window() and _titles(sh) == [], _titles(sh)


def test_state_and_status_deleted_while_a_run_is_under_way_push_at_the_next_run(worked):
    sh, fake, br, job = worked
    code, r, saved = _run(job, between=_rm(STATE, STATUS))  # you commit while the run is under way
    assert code == 0 and not saved and "not starting a new state over the record" in br.errors[-1], br.errors[-1:]
    _next(fake)
    code, r, saved = _run(job)
    assert code == 2 and not saved
    assert [t for t in _titles(sh) if t == "BTC SHADOW: not working"], _titles(sh)   # before: none for hours


@pytest.mark.parametrize("slip", ["short", "changed"])
def test_status_deleted_then_a_secret_slip_pushes_at_once(worked, slip):
    sh, fake, br, job = worked
    br.edit(_rm(STATUS))
    deleted = br.files()
    bad = "x" * 31 if slip == "short" else "another-state-key-" + "q" * 30
    code, r, saved = _run(job, {"DESK_STATE_KEY": bad})
    assert not saved and br.files() == deleted
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and "BTC/status.json is missing on the coinbase-state branch too" in got[0], _titles(sh)


def test_the_commit_time_is_the_memo_only_when_the_branch_just_changed(worked):
    """The control: a commit of yours from hours ago, the state and status deleted then, is no news now."""
    sh, fake, br, job = worked
    br.edit(_rm(STATE, STATUS, BOARD))
    _next(fake, 3 * 60)
    code, r, saved = _run(job)
    assert code == 2 and not saved and not r.in_window() and _titles(sh) == [], _titles(sh)
    assert r.loaded_commit_ms() is not None and r.now - r.loaded_commit_ms() > cr.FRESH_STATUS_MS


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_page_with_no_status_and_no_board_doesnt_say_the_secrets_arent_set():
    out = subprocess.run([NODE, "-e", "const m = require(process.argv[1]); console.log(m.statusHtml(null, Date.now(), "
                                      "false))", str(ROOT / "dashboard" / "shadow.js")],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert "SHADOW starts once" not in out and "BTC/status.json (and the SHADOW board) were deleted" in out
    assert "BTC SHADOW: not working" in out


# ---------------------------------------------------------------- a branch you made first: no start over

@pytest.mark.parametrize("yours", ["kills", "note"])
def test_a_branch_you_made_first_is_a_plain_first_connection(fresh, yours):
    sh, fake, br, job = fresh
    if yours == "kills":
        br.edit(lambda d: ((d / "BTC").mkdir(parents=True, exist_ok=True),
                           (d / "BTC" / "kills.json").write_text('{"entry_kill": true}\n')))
    else:
        br.edit(lambda d: (d / "notes.txt").write_text("my notes\n"))
    code, r, saved = _run(job)
    assert code == 0 and saved
    assert _titles(sh) == ["BTC SHADOW: connected (read only)"], _titles(sh)       # before: "started over"
    assert "started over" not in sh.pushes[0][1] and "SHADOW's record (the 14 days LIVE needs) starts from this run" \
        in sh.pushes[0][1]


def test_a_never_worked_status_deleted_during_onboarding_is_no_start_over(fresh):
    sh, fake, br, job = fresh
    trade = fake.add_key(make_ec_key("record8-trade-not-real", trade=True))
    code, r, saved = _run(job, {"COINBASE_CDP_KEY": trade.file_json()})
    assert code == 2 and saved and STATUS in br.files() and STATE not in br.files()
    br.edit(_rm(STATUS))
    _next(fake)
    code, r, saved = _run(job)
    assert code == 0 and saved and _titles(sh) == ["BTC SHADOW: connected (read only)"], _titles(sh)


def test_every_file_deleted_but_the_board_of_a_run_that_worked_is_a_start_over(worked):
    sh, fake, br, job = worked
    br.edit(_rm(STATE, STATUS, PREV))
    code, r, saved = _run(job)
    assert code == 0 and saved and job.status()["runs"] == 1
    assert _titles(sh) == ["BTC SHADOW: connected, started over (read only)"], _titles(sh)
    assert "though SHADOW had worked there" in sh.pushes[0][1] and "starts again" in sh.pushes[0][1]
