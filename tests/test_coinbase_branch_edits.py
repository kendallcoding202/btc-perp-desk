"""Round 7, your edits on the coinbase-state branch that the guide didn't cover, through the workflow's load, run,
check, stage and commit (tests/test_coinbase_kills_fail_closed.py's Branch and Job, with the load step's marker beside
the state dir, as the workflow writes it). Each fails on the code before it (4c0d11c):

- README.md and BTC/status.json deleted in one commit: the status was never written back (the run looked for README.md
  to know it was on the branch), and every save was refused until a revert. Now the load step's marker says so, and the
  next run writes the status back;
- a BTC/status.json made invalid (not JSON), or edited so it doesn't match the state (its start, its runs): every save
  was refused. Now the next run writes it back from the state, as for a deleted one;
- the state and the status deleted together, the state before the last still there: a new state opened silently, over
  SHADOW's record (each save replaces the branch's one commit, so the deleted files left its history too). Now every
  run is refused, nothing written, until you put them back (or delete the branch), and the save check refuses it too;
- every file deleted: SHADOW started over silently ("connected (read only)"). Now it says its record starts from that
  run (round 8: "started over" only while the branch shows SHADOW worked there).

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
import shutil

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from desk import statebox
from fake_coinbase import FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import KEY, Shadow

STATE = "BTC/state-shadow.enc.json"
PREV = "BTC/state-shadow.prev.enc.json"
STATUS = "BTC/status.json"
GH = {"GITHUB_ACTIONS": "true"}


@pytest.fixture
def worked(tmp_path, monkeypatch):
    """SHADOW, two runs saved (so the branch has the state before the last too)."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("edits7-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        job = Job(sh, fake, br, None)
        for _ in range(2):
            sh.pushes.clear()
            code, r, changed, saved = job.run(over=GH)
            assert code == 0 and saved, (r.lines[-3:], br.errors)
            _next(fake)
        assert PREV in br.files()
        yield sh, fake, br, job


def _run(job, over=None):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over={**GH, **(over or {})})
    return code, r, saved


def _titles(sh):
    return [p[0] for p in sh.pushes]


def _rm(*paths):
    def ch(d):
        for p in paths:
            (d / p).unlink()
    return ch


def _put(files: dict):
    def ch(d):
        for p, data in files.items():
            (d / p).parent.mkdir(parents=True, exist_ok=True)
            (d / p).write_bytes(data)
    return ch


def _status_write_back_then_as_usual(sh, fake, br, job, was):
    code, r, saved = _run(job)
    assert saved and _titles(sh) == ["BTC SHADOW: NOT SAVED"], (code, saved, _titles(sh), br.errors[-1:])
    assert br.files()[STATE] == was[STATE]                   # the state as it was: only the status written back
    st = job.status()
    assert st["note"] == cs.STATUS_BACK and st["started_ts"] == json.loads(was[STATUS])["started_ts"]
    _next(fake)
    code, r, saved = _run(job)
    assert code == 0 and saved and _titles(sh) == ["BTC SHADOW: working again"], (_titles(sh), br.errors[-1:])
    st, old = job.status(), json.loads(was[STATUS])
    assert st["ok"] is True and st["started_ts"] == old["started_ts"] and st["runs"] == old["runs"] + 1


# ---------------------------------------------------------------- the status: deleted with README.md, garbled, edited

def test_readme_and_status_deleted_together_the_status_is_written_back(worked):
    sh, fake, br, job = worked
    was = br.files()
    br.edit(_rm("README.md", STATUS))
    _status_write_back_then_as_usual(sh, fake, br, job, was)   # before: refused at every run until a revert


@pytest.mark.parametrize("edit", ["not_json", "unknown_key", "another_start", "more_runs"])
def test_a_status_edited_into_one_the_save_check_refuses_is_written_back(worked, edit):
    sh, fake, br, job = worked
    was = br.files()

    def ch(d):
        p = d / STATUS
        st = json.loads(p.read_text())
        if edit == "not_json":
            p.write_text("{oops\n")
            return
        if edit == "unknown_key":
            st["mine"] = "a note of yours"
        elif edit == "another_start":
            st["started_ts"] = st["started_ts"] - 86400_000
        else:
            st["runs"] = st["runs"] + 5
        p.write_text(json.dumps(st, indent=1) + "\n")
    br.edit(ch)
    _status_write_back_then_as_usual(sh, fake, br, job, was)   # before: every save refused


def test_a_status_edited_into_one_the_check_takes_is_left_as_it_is(worked):
    """The control: runs set to 0 doesn't stop a save, so nothing is written back."""
    sh, fake, br, job = worked
    br.edit(lambda d: (d / STATUS).write_text(json.dumps({**json.loads((d / STATUS).read_text()), "runs": 0})))
    code, r, saved = _run(job)
    assert code == 0 and saved and "BTC SHADOW: NOT SAVED" not in _titles(sh) and job.status()["runs"] == 3


# ---------------------------------------------------------------- the state and the status deleted together

def test_state_and_status_deleted_beside_the_state_before_is_refused_until_put_back(worked):
    sh, fake, br, job = worked
    was = br.files()
    br.edit(_rm(STATE, STATUS))
    deleted = br.files()
    n0 = len(fake.requests)
    code, r, saved = _run(job)
    assert code == 2 and not saved and br.files() == deleted, (code, saved, sorted(br.files()))   # before: a new state
    assert len(fake.requests) == n0                         # nothing read
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and "the state before them is (BTC/state-shadow.prev.enc.json)" in got[0], _titles(sh)   # at once
    _next(fake)
    code, r, saved = _run(job)
    assert code == 2 and not saved and br.files() == deleted and _titles(sh) == []          # then every 6 hours
    br.edit(_put({STATE: was[STATE], STATUS: was[STATUS]}))                                 # put back (a revert)
    _next(fake)
    code, r, saved = _run(job)
    st, old = job.status(), json.loads(was[STATUS])
    assert code == 0 and saved and st["started_ts"] == old["started_ts"] and st["runs"] == old["runs"] + 1


def test_the_save_check_refuses_a_first_save_beside_the_state_before_the_last(tmp_path):
    state = statebox.seal_json({"x": 1}, KEY, statebox.state_aad("shadow"), iterations=1000).encode()

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
    (d / "status.json").write_text(json.dumps({"v": 1, "mode": "SHADOW", "coin": "BTC", "product": "BIP-20DEC30-CDE",
                                               "updated": 9, "started_ts": 9, "runs": 1, "failed_runs": 0, "ok": True,
                                               "problem": None, "opened": True}))
    with pytest.raises(cs.StateCheckError, match="not starting a new state over the record"):
        cs.check(tmp_path, branch=B({"README.md": b"x", PREV: state}))
    assert "a first save" in cs.check(tmp_path, branch=B({"README.md": b"x"}))[-1]


def test_every_file_deleted_starts_over_and_says_so(worked):
    sh, fake, br, job = worked

    def rm_all(d):
        for p in sorted(d.rglob("*"), reverse=True):
            if ".git" in p.relative_to(d).parts:
                continue
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
    br.edit(rm_all)
    assert br.files() == {}
    code, r, saved = _run(job)
    assert code == 0 and saved and job.status()["runs"] == 1
    # round 8: with nothing left on the branch to show SHADOW worked there, it isn't called a start over (a branch you
    # made yourself looks the same), but the push says SHADOW's record starts from this run. With its board left, it's
    # "started over" (tests/test_coinbase_deleted_record.py)
    assert _titles(sh) == ["BTC SHADOW: connected (read only)"], _titles(sh)
    assert "SHADOW's record (the 14 days LIVE needs) starts from this run" in sh.pushes[0][1]
    _next(fake)
    code, r, saved = _run(job)
    assert code == 0 and saved and _titles(sh) == [] and job.status()["runs"] == 2


def test_a_first_run_ever_is_no_start_over(tmp_path, monkeypatch):
    """The control: no branch yet (the marker empty) is a plain first run."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("edits7-first-not-real"))
        job = Job(sh, fake, Branch(tmp_path, monkeypatch), None)
        code, r, changed, saved = job.run(over=GH)
        assert code == 0 and saved and [p[0] for p in sh.pushes] == ["BTC SHADOW: connected (read only)"]
        assert cr.Run.load_marker(r) == ""


def test_the_guide_says_what_each_edit_does():
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    for said in ("**`BTC/status.json` deleted** on the `coinbase-state` branch (with `README.md` or anything else)",
                 "**`BTC/status.json` edited** into one the save check would refuse",
                 "**`BTC/state-shadow.enc.json` and `BTC/status.json` both deleted**",
                 "**Every SHADOW file deleted**", "**BTC SHADOW: connected, started over (read only)**",
                 "A revert only works until a run saves"):
        assert said in guide, said
