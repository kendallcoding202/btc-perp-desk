"""SHADOW's saved state: the encryption (desk/statebox.py), the save checks (desk/coinbase_state.py), and the
workflow's own load and save scripts, run as they are in .github/workflows/paper.yml against a local bare repository
that moves between tries (no network: file:// only)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from desk import coinbase_state as cs
from desk import statebox

ROOT = Path(__file__).resolve().parents[1]
PW = "test-password-" + "p" * 30
IT = 1000


# ---------------------------------------------------------------- the envelopes

def test_seal_and_open_round_trip_with_a_fresh_salt_and_iv_each_time():
    a = json.loads(statebox.seal_json({"x": 1}, PW, statebox.state_aad(), iterations=IT))
    b = json.loads(statebox.seal_json({"x": 1}, PW, statebox.state_aad(), iterations=IT))
    assert statebox.is_envelope(a) and a["salt"] != b["salt"] and a["iv"] != b["iv"] and a["ct"] != b["ct"]
    assert statebox.open_json(json.dumps(a), PW, statebox.state_aad()) == {"x": 1}
    assert a["kdf"] == "pbkdf2-sha256" and a["aad"] == "btc-perp-desk:state-shadow:BTC" and a["zlib"] is True


def test_the_default_is_600000_rounds_of_pbkdf2_sha256():
    env = statebox.seal(b"{}", PW, statebox.board_aad(), compress=False)
    assert env["iter"] == statebox.ITERATIONS == 600_000 and env["zlib"] is False


def test_a_wrong_key_a_changed_byte_or_another_files_aad_never_opens():
    text = statebox.seal_json({"x": 1}, PW, statebox.state_aad(), iterations=IT)
    with pytest.raises(statebox.WrongKey):
        statebox.open_json(text, "another-password-" + "q" * 30, statebox.state_aad())
    env = json.loads(text)
    ct = bytearray(base64.b64decode(env["ct"]))
    ct[3] ^= 1
    env["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(statebox.WrongKey):
        statebox.open_json(json.dumps(env), PW, statebox.state_aad())
    with pytest.raises(statebox.BoxError, match="not opened in its place"):
        statebox.open_json(text, PW, statebox.board_aad())
    forged = json.loads(text)
    forged["aad"] = statebox.board_aad()                     # relabelled: the tag still binds the real aad
    with pytest.raises(statebox.WrongKey):
        statebox.open_json(json.dumps(forged), PW, statebox.board_aad())


@pytest.mark.parametrize("pw", [None, "", "short", "x" * 31])
def test_a_missing_or_short_password_is_refused(pw):
    with pytest.raises(statebox.BoxError):
        statebox.seal(b"{}", pw, statebox.state_aad())


def test_the_password_never_appears_in_the_envelope_or_an_error():
    text = statebox.seal_json({"secret-balance": 123.45}, PW, statebox.state_aad(), iterations=IT)
    assert PW not in text and "123.45" not in text and "secret-balance" not in text
    try:
        statebox.open_json(text, "wrong-" + "w" * 30, statebox.state_aad())
    except statebox.BoxError as exc:
        assert "wrong-" not in str(exc) and PW not in str(exc)


# ---------------------------------------------------------------- the save checks

def _git(*args, cwd, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          capture_output=True, text=True, check=check)


def _status(runs=1, started=1_000, mode="SHADOW", **kw):
    return {"v": 1, "mode": mode, "coin": "BTC", "product": "BIP-20DEC30-CDE", "updated": 2_000, "started_ts": started,
            "runs": runs, "failed_runs": 0, "ok": True, "problem": None, "key_view_only": True, "decision": "WAIT",
            "would_send_last_24h": 0, "alerts_open": 0, "checks_passed": 10, "checks_total": 10, "opened": False, **kw}


def _state_dir(path: Path, runs=1, started=1_000, mode="SHADOW", state=None, kills_set=None, extra=None) -> Path:
    d = path / "BTC"
    d.mkdir(parents=True, exist_ok=True)
    m = mode.lower()
    (d / f"state-{m}.enc.json").write_text(state if state is not None else
                                            statebox.seal_json({"runs": runs}, PW, statebox.state_aad(m), iterations=IT))
    (d / f"board-{m}.enc.json").write_text(statebox.seal_json({"b": runs}, PW, statebox.board_aad(m), compress=False,
                                                              iterations=IT))
    (d / "status.json").write_text(json.dumps(_status(runs, started, mode)))
    (d / "kills.set.json").write_text(json.dumps(kills_set or {}))
    for name, text in (extra or {}).items():
        (d / name).write_text(text)
    return path


@pytest.fixture
def repo(tmp_path):
    """A git repository with a coinbase-state ref holding a saved SHADOW state (runs 3, started 1000)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", cwd=r)
    saved = _state_dir(tmp_path / "saved", runs=3)
    (saved / "BTC" / "kills.set.json").unlink()
    (saved / "BTC" / "kills.json").write_text(json.dumps({"entry_kill": True}))
    _git("--work-tree", str(saved), "add", "-A", ".", cwd=r)
    _git("commit", "-qm", "saved", cwd=r)
    _git("update-ref", "refs/remotes/origin/coinbase-state", "HEAD", cwd=r)
    return r, saved


def test_check_takes_an_older_state_with_more_runs(repo, tmp_path, monkeypatch):
    r, _ = repo
    monkeypatch.chdir(r)
    said = cs.check(_state_dir(tmp_path / "new", runs=4), "refs/remotes/origin/coinbase-state")
    assert said == ["SHADOW state: 3 -> 4 runs, same start"]


@pytest.mark.parametrize("case", ["fewer_runs", "other_start", "same_runs_other_bytes", "plaintext", "unknown_file",
                                  "amount_in_status", "kills_not_bools", "live_over_shadow_live"])
def test_check_refuses_a_younger_state_plain_text_and_anything_else(repo, tmp_path, monkeypatch, case):
    r, saved = repo
    monkeypatch.chdir(r)
    kw = {"fewer_runs": dict(runs=2), "other_start": dict(runs=5, started=999), "same_runs_other_bytes": dict(runs=3),
          "plaintext": dict(runs=4, state=json.dumps({"balance": 500})), "unknown_file": dict(runs=4, extra={
              "notes.txt": "x"}), "amount_in_status": dict(runs=4), "kills_not_bools": dict(runs=4, extra={
              "kills.json": json.dumps({"entry_kill": "yes"})}), "live_over_shadow_live": dict(runs=4)}[case]
    d = _state_dir(tmp_path / "new", **kw)
    if case == "amount_in_status":
        st = json.loads((d / "BTC" / "status.json").read_text())
        st["balance"] = 500.0
        (d / "BTC" / "status.json").write_text(json.dumps(st))
    if case == "live_over_shadow_live":
        st = json.loads((d / "BTC" / "status.json").read_text())
        st["note"] = "real balance $500"
        (d / "BTC" / "status.json").write_text(json.dumps(st))
    with pytest.raises(cs.StateCheckError):
        cs.check(d, "refs/remotes/origin/coinbase-state")


def test_check_allows_a_first_save_and_the_first_live_save_over_shadow(repo, tmp_path, monkeypatch):
    r, _ = repo
    monkeypatch.chdir(r)
    assert cs.check(_state_dir(tmp_path / "first", runs=1), "refs/remotes/origin/nothing-here") == \
        ["no saved state on the branch: a first save"]
    live = _state_dir(tmp_path / "live", runs=1, started=5_000, mode="LIVE")
    assert "the first LIVE save" in cs.check(live, "refs/remotes/origin/coinbase-state")[0]


def test_stage_keeps_your_kills_adds_the_runs_and_rotates_the_previous_state(repo, tmp_path, monkeypatch):
    r, saved = repo
    monkeypatch.chdir(r)
    d = _state_dir(tmp_path / "new", runs=4, kills_set={"flat_kill": True})
    work = tmp_path / "work"
    cs.stage(d, work, "refs/remotes/origin/coinbase-state")
    out = work / "BTC"
    assert sorted(p.name for p in out.iterdir()) == ["board-shadow.enc.json", "kills.json", "state-shadow.enc.json",
                                                     "state-shadow.prev.enc.json", "status.json"]
    assert json.loads((out / "kills.json").read_text()) == {"flat_kill": True, "entry_kill": True, "jev_kill": False}
    assert (out / "state-shadow.prev.enc.json").read_bytes() == (saved / "BTC" / "state-shadow.enc.json").read_bytes()
    assert (work / "README.md").read_text().startswith("# coinbase-state")


# ---------------------------------------------------------------- the workflow's own scripts, against a local remote

def _step_script(name: str) -> str:
    """The `run: |` block of the shadow job's step called `name`, as in the workflow file."""
    lines = (ROOT / ".github/workflows/paper.yml").read_text().splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.strip() == f"- name: {name}")
    j = next(k for k in range(i, len(lines)) if lines[k].strip() == "run: |")
    out = []
    for ln in lines[j + 1:]:
        if ln.strip() and not ln.startswith(" " * 10):
            break
        out.append(ln[10:])
    return "\n".join(out) + "\n"


def _bash(script: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture
def actions(tmp_path):
    """A bare 'GitHub' remote with main, and the job's checkout of it."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", "-q", str(origin), str(seed), cwd=tmp_path)
    (seed / "x.txt").write_text("main\n")
    _git("add", "x.txt", cwd=seed)
    _git("commit", "-qm", "main", cwd=seed)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=seed)
    ws = tmp_path / "ws"
    _git("clone", "-q", "--depth", "1", "--branch", "main", f"file://{origin}", str(ws), cwd=tmp_path)
    rt = tmp_path / "rt"
    rt.mkdir()
    env = {**os.environ, "RUNNER_TEMP": str(rt), "PYTHONPATH": str(ROOT), "GIT_TERMINAL_PROMPT": "0"}
    return origin, ws, rt, env


def _remote_files(origin: Path, ref="coinbase-state") -> dict:
    out = _git("ls-tree", "-r", "--name-only", ref, cwd=origin, check=False).stdout.split()
    return {p: _git("show", f"{ref}:{p}", cwd=origin).stdout for p in out}


def test_the_workflow_saves_a_first_state_and_then_survives_your_kills_edit_between_load_and_save(actions, tmp_path):
    origin, ws, rt, env = actions
    load, save = _step_script("Load the SHADOW state"), _step_script("Save the SHADOW state")
    # run 1: no branch yet
    assert _bash(load, ws, env).returncode == 0 and (rt / "cb.lease").read_text() == ""
    _state_dir(rt / "cb", runs=1)
    res = _bash(save, ws, env)
    assert res.returncode == 0, res.stdout + res.stderr
    files = _remote_files(origin)
    assert set(files) == {"README.md", "BTC/board-shadow.enc.json", "BTC/kills.json", "BTC/state-shadow.enc.json",
                          "BTC/status.json"}
    assert _git("rev-list", "--count", "coinbase-state", cwd=origin).stdout.strip() == "1"     # one orphan commit
    # run 2: loaded, and then you edit kills.json on the branch before the save
    for p in (rt / "cb").glob("BTC/*"):
        p.unlink()
    assert _bash(load, ws, env).returncode == 0
    _state_dir(rt / "cb", runs=2)
    you = tmp_path / "you"
    _git("clone", "-q", "--branch", "coinbase-state", f"file://{origin}", str(you), cwd=tmp_path)
    (you / "BTC" / "kills.json").write_text(json.dumps({"entry_kill": True}))
    _git("commit", "-qam", "entry_kill on", cwd=you)
    _git("push", "-q", "origin", "coinbase-state", cwd=you)
    res = _bash(save, ws, env)
    assert res.returncode == 0, res.stdout + res.stderr
    files = _remote_files(origin)
    assert json.loads(files["BTC/kills.json"])["entry_kill"] is True                # your edit kept
    assert json.loads(files["BTC/status.json"])["runs"] == 2
    assert "BTC/state-shadow.prev.enc.json" in files
    assert _git("rev-list", "--count", "coinbase-state", cwd=origin).stdout.strip() == "1"
    # run 3: a younger state never replaces the saved one
    before = _git("rev-parse", "coinbase-state", cwd=origin).stdout
    for p in (rt / "cb").glob("BTC/*"):
        p.unlink()
    assert _bash(load, ws, env).returncode == 0
    _state_dir(rt / "cb", runs=1)
    res = _bash(save, ws, env)
    assert res.returncode != 0 and "::error::coinbase-state" in res.stdout
    assert _git("rev-parse", "coinbase-state", cwd=origin).stdout == before


def test_the_load_stops_when_the_branch_exists_but_cant_be_fetched(actions):
    origin, ws, rt, env = actions
    load = _step_script("Load the SHADOW state").replace("sleep $((attempt * 10))", "true")
    _git("remote", "set-url", "origin", "file:///nonexistent/origin.git", cwd=ws)
    res = _bash(load, ws, env)
    assert res.returncode == 1 and "could not fetch the coinbase-state branch" in res.stdout
