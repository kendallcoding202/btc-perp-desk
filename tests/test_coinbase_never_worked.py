"""Round 6, SHADOW onboarding: nothing a first run leaves (or a refusal while BTC/status.json is gone) can keep a later,
corrected secret from saving, through the workflow's load, run, check, stage and commit (tests/test_coinbase_kills_
fail_closed.py's Branch and Job). Each fails on the code before it (6115c44):

- BTC/status.json deleted (the guide: the next run writes it back), and the next run refused over a secret before it
  opened the state: it wrote a status with runs 0 and a new start, the save check took it, and every run with the
  secrets right was refused from then on ("would replace the saved SHADOW account"), the dashboard showing the old
  reason. Now such a refusal writes nothing, and the check takes only the status written back from the state;
- a first run that stopped after the key passed (Coinbase answering the key, not the candles or the balance) saved a
  state sealed with DESK_STATE_KEY, so changing it, as the guide allows before SHADOW has worked, left every later
  run refused. Now, until SHADOW has had a run that works, any run that stops saves its status alone;
- the password pasted into both secrets was said to be the key file, the secrets swapped.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from desk import statebox
from fake_coinbase import V3, FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import KEY, Shadow

ROOT = Path(__file__).resolve().parents[1]
STATE = "BTC/state-shadow.enc.json"
GH = {"GITHUB_ACTIONS": "true"}


@pytest.fixture
def first(tmp_path, monkeypatch):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("never6-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        yield sh, fake, br, Job(sh, fake, br, None)


def _run(job, over=None, **kw):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over=over, **kw)
    return code, r, saved


def _titles(sh):
    return [p[0] for p in sh.pushes]


# ---------------------------------------------------------------- status.json deleted, then a secret refused

BAD = {"short": lambda sh: {"DESK_STATE_KEY": "x" * 31},
       "other_password": lambda sh: {"DESK_STATE_KEY": "o" * 40},
       "line_break": lambda sh: {"DESK_STATE_KEY": KEY + "\n"},
       "swapped": lambda sh: {"DESK_STATE_KEY": sh.env["COINBASE_CDP_KEY"], "COINBASE_CDP_KEY": KEY}}


@pytest.mark.parametrize("bad", sorted(BAD))
def test_status_deleted_then_a_secret_refusal_then_fixed_saves_again(first, bad):
    sh, fake, br, job = first
    assert _run(job, GH)[2]
    _next(fake)
    assert _run(job, GH)[2]
    was = job.status()
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())
    _next(fake)
    files = dict(br.files())
    code, r, saved = _run(job, {**GH, **BAD[bad](sh)})
    assert code != 0 and not saved and br.files() == files, (bad, saved, br.errors[-1:])   # nothing written
    seen = []
    for _ in range(3):
        _next(fake)
        code, r, saved = _run(job, GH)
        seen.append((code, saved, _titles(sh), br.errors[-1:] if not saved else None))
    assert all(s for _, s, _, _ in seen), seen          # before: refused for good ("would replace the saved account")
    assert seen[0][2] == ["BTC SHADOW: NOT SAVED"] and seen[1][2] == ["BTC SHADOW: working again"], seen
    st = job.status()
    assert st["ok"] is True and st["started_ts"] == was["started_ts"] and st["runs"] == was["runs"] + 2


def test_a_refusal_with_the_status_gone_says_so_on_the_phone(first, monkeypatch):
    sh, fake, br, job = first
    assert _run(job, GH)[2]
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())
    _next(fake)
    monkeypatch.setattr(cr.Run, "in_window", lambda self: True)
    code, r, saved = _run(job, {**GH, "DESK_STATE_KEY": "x" * 31})
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and "BTC/status.json is missing on the coinbase-state branch too" in got[0], _titles(sh)
    assert "the first run that opens the saved state writes it back" in got[0]


def test_the_check_takes_only_the_status_written_back_beside_a_state_without_one(tmp_path):
    """The save check, alone: a status beside the branch's unchanged state, the branch having none, is the write-back
    only (problem state, its note, the state's runs); a runs-0 refusal's is refused."""
    state = statebox.seal_json({"x": 1}, KEY, statebox.state_aad("shadow"), iterations=1000).encode()

    class B:
        def __init__(self, files):
            self.files = files

        def tree(self):
            return {p: "100644" for p in self.files}

        def show(self, path):
            return self.files.get(path)
    branch = B({"README.md": b"x", STATE: state})
    d = tmp_path / "BTC"
    d.mkdir()
    (d / "state-shadow.enc.json").write_bytes(state)
    base = {"v": 1, "mode": "SHADOW", "coin": "BTC", "product": "BIP-20DEC30-CDE", "updated": 1, "started_ts": 5,
            "ok": False, "opened": False}
    for st, ok in (({**base, "runs": 0, "failed_runs": 0, "problem": "key", "note": "DESK_STATE_KEY is too short"},
                    False),
                   ({**base, "runs": 3, "failed_runs": 0, "problem": "key", "note": "DESK_STATE_KEY is too short"},
                    False),
                   ({**base, "runs": 3, "failed_runs": 0, "problem": "state", "note": cr.STATUS_BACK}, True)):
        (d / "status.json").write_text(json.dumps(st))
        if ok:
            assert any("written back" in s for s in cs.check(tmp_path, branch=branch))
        else:
            with pytest.raises(cs.StateCheckError, match="isn't written back from that state"):
                cs.check(tmp_path, branch=branch, loaded=None)


# ---------------------------------------------------------------- a first run that stops after the key passed

@pytest.mark.parametrize("read", ["balance", "candles"])
def test_first_run_failing_after_the_key_passed_then_desk_state_key_changed(first, read):
    sh, fake, br, job = first
    path = {"balance": f"{V3}/cfm/balance_summary", "candles": f"{V3}/products/BIP-20DEC30-CDE/candles"}[read]
    fake.fail_next("GET", path, 503, times=50)
    code, r, saved = _run(job, GH)
    fake.fail.clear()
    st = job.status()
    assert code == 1 and saved and STATE not in br.files(), (r.problem, sorted(br.files()))   # before: a state
    assert st["ok"] is False and st["runs"] == 0 and st["problem"] == "read" and \
        st["note"] == "Coinbase didn't answer part of the run (the next run tries again)", st
    got = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]
    assert got and got[0].startswith("SHADOW stopped part way this run:") and "DESK_STATE_KEY can still change" in \
        got[0], got
    assert "stopped (read) part way" in r.public_line() and "only its status is saved" in r.public_line()
    new = "a-new-password-for-shadow-" + "n" * 20
    _next(fake)
    code, r, saved = _run(job, {**GH, "DESK_STATE_KEY": new})
    assert code == 0 and saved and job.status()["ok"] is True, (r.problem, r.lines[-3:], br.errors[-1:])
    statebox.open_json(br.files()[STATE].decode(), new, statebox.state_aad("shadow"))


def test_a_first_run_that_crashes_after_the_key_saves_only_its_status(first, monkeypatch):
    sh, fake, br, job = first

    def boom(*a, **kw):
        raise ValueError("a bug after the key passed")
    monkeypatch.setattr(cr, "read_extras", boom)
    code, r, saved = _run(job, GH)
    assert code == 1 and saved and STATE not in br.files() and job.status()["runs"] == 0
    monkeypatch.undo()
    _next(fake)
    code, r, saved = _run(job, {**GH, "DESK_STATE_KEY": "another-password-entirely-" + "q" * 20})
    assert code == 0 and saved and job.status()["ok"] is True


def test_a_shadow_that_has_worked_still_saves_a_failed_run(first):
    """The control: once SHADOW has a working run, a run that stops part way is a failed run of its state."""
    sh, fake, br, job = first
    assert _run(job, GH)[2]
    _next(fake)
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 503, times=50)
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and st["runs"] == 2 and st["failed_runs"] == 1


# ---------------------------------------------------------------- the password in both secrets

def test_the_password_in_both_secrets_is_said_plainly(first):
    sh, fake, br, job = first
    code, r, saved = _run(job, {**GH, "COINBASE_CDP_KEY": KEY})
    st = job.status()
    assert code == 2 and saved and fake.requests == [] and STATE not in br.files()
    assert st["note"] == "COINBASE_CDP_KEY and DESK_STATE_KEY hold the same text (put the key file in " \
                         "COINBASE_CDP_KEY)", st["note"]
    assert "swapped" not in sh.pushes[0][1] and "hold the same text" in sh.pushes[0][1]
    _next(fake)
    code, r, saved = _run(job, GH)
    assert code == 0 and saved


# ---------------------------------------------------------------- what the guide and README say

def test_the_guide_and_readme_say_what_the_code_does():
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert '"I can publish and subscribe, everyone can publish"' in guide and "log in to your ntfy.sh account" in guide
    assert "paper's pushes never use `NTFY_TOKEN`" in guide and "Paper's pushes never use `NTFY_TOKEN`" in readme
    assert "a run that's refused or stops part way saves its status, never a state" in guide
    assert "the next run that opens the saved state writes it back" in guide
    assert '"the balance found" if the first runs were refused' not in guide
    assert "**COINBASE_CDP_KEY and DESK_STATE_KEY hold the same text:**" in guide
    assert "when that run saves its board" in guide and "when that run saves its board" in readme
