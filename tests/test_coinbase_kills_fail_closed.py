"""BTC/kills.json and the other files you edit on the coinbase-state branch, read failing closed by the runs, check and
stage alike (desk/coinbase_state.py, desk/coinbase_run.py), and never a reason for a save to be refused. Each test fails
on the code before it (da84d3e and 4bf30a7):

- a flat_kill with a typo ("true", 1, a trailing comma, single quotes, Python's True) was read as off, and the save
  then rewrote your file to "flat_kill": false; now it closes the position, and the file is kept as you wrote it;
- a typo that landed between a run's load and its save was rewritten silently; now it stays, and the next run says so;
- a dangling symlink for kills.json was read as every switch off by the run and on by the save;
- any extra file you add under BTC/ (notes.md, Kills.json) made every save refuse; now it's kept, and a file that looks
  like a kill switch but isn't BTC/kills.json is entry_kill on, said so;
- DESK_COINBASE_KILL=flat_kill (or close, or flatten) was entry only;
- a LIVE that never armed, refused over DESK_STATE_KEY, pushed nothing while SHADOW's record was fresh;
- a save the save step will refuse is pushed by the run that made it (it went unsaid before).

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from fake_coinbase import ARM_ENV, FakeCoinbase, make_ec_key
from test_coinbase_shadow import IT, Shadow, _write_proof
from test_coinbase_state import _git, _state_dir

REF = "refs/remotes/origin/coinbase-state"


# ---------------------------------------------------------------- the branch, and the job's load and save around runs

class Branch:
    """The coinbase-state branch in a local repository, and the workflow's load (coinbase_state.load: the whole
    branch, the commit loaded kept) and save (check and stage against it, one commit) around each run."""

    def __init__(self, tmp_path, monkeypatch):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git("init", "-q", cwd=self.repo)
        monkeypatch.chdir(self.repo)
        self.tmp, self.n = tmp_path, 0
        self.said: list[list[str]] = []
        self.errors: list[str] = []
        self.loaded: str | None = None

    def files(self) -> dict:
        out = _git("ls-tree", "-r", "--name-only", REF, cwd=self.repo, check=False).stdout.split()
        return {p: subprocess.run(["git", "show", f"{REF}:{p}"], cwd=self.repo, capture_output=True,
                                  check=True).stdout for p in out}

    def modes(self) -> dict:
        out = _git("ls-tree", "-r", REF, cwd=self.repo, check=False).stdout.splitlines()
        return {ln.split("\t", 1)[1]: ln.split()[0] for ln in out}

    def load(self, state_dir: Path) -> None:
        if state_dir.exists():
            shutil.rmtree(state_dir)
        state_dir.mkdir(parents=True)
        self.loaded = None
        got = _git("rev-parse", "--verify", "-q", REF, cwd=self.repo, check=False)
        if got.returncode == 0:
            cs.load(state_dir, REF)             # as the load step does
            self.loaded = got.stdout.strip()

    def commit(self, work: Path) -> None:
        idx = self.tmp / f"idx{self.n}"
        if idx.exists():
            idx.unlink()
        env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
        subprocess.run(["git", "--work-tree", str(work), "add", "-A", "-f", "."], cwd=self.repo, env=env, check=True)
        tree = subprocess.run(["git", "write-tree"], cwd=self.repo, env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
        c = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "commit-tree", tree, "-m", "s"],
                           cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        _git("update-ref", REF, c, cwd=self.repo)

    def save(self, state_dir: Path) -> bool:
        self.n += 1
        loaded = cs.GitBranch(self.loaded) if self.loaded else None
        try:
            said = cs.check(state_dir, REF, loaded=loaded)
        except cs.StateCheckError as exc:
            self.errors.append(str(exc))
            return False
        work = self.tmp / f"work{self.n}"
        cs.stage(state_dir, work, REF, loaded=loaded)
        self.commit(work)
        self.said.append(said)
        return True

    def edit(self, change) -> None:
        """You, on GitHub: a commit on the branch that changes its files (`change` gets a checkout of it)."""
        self.n += 1
        d = self.tmp / f"you{self.n}"
        keep = self.loaded
        self.load(d)
        self.loaded = keep              # your edit isn't a run's load
        change(d)
        self.commit(d)


class Job:
    """One shadow-job run: the load, the run step (SHADOW or the README's LIVE step), the save if changed=true."""

    def __init__(self, sh, fake, br: Branch, tk):
        self.sh, self.fake, self.br, self.tk = sh, fake, br, tk
        self.out = br.tmp / "gh_output"

    def run(self, mode="shadow", over=None, gate=(("backtest: passes", True, "ok"),), between=None):
        sh, fake = self.sh, self.fake
        self.br.load(sh.dir)
        if self.out.exists():
            self.out.unlink()
        env = {**sh.env, "GITHUB_OUTPUT": str(self.out)}
        if mode == "live":
            env = {**env, **ARM_ENV, "COINBASE_CDP_KEY": self.tk.file_json()}
        env = {k: v for k, v in {**env, **(over or {})}.items() if v is not None}
        kw = dict(base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep, push=lambda *a: sh.pushes.append(a),
                  mask_emit=lambda s: None, iterations=IT)
        if mode == "live":
            code, r = cr.run_live_once(sh.dir, env, max_notional=1_000.0, gate=list(gate), **kw)
        else:
            code, r = cr.run_shadow(sh.dir, env, **kw)
        if between is not None:
            self.br.edit(between)           # you commit on the branch while the run is under way
        changed = self.out.exists() and self.out.read_text().strip().splitlines()[-1] == "changed=true"
        saved = self.br.save(sh.dir) if changed else None
        return code, r, changed, saved

    def status(self):
        f = self.br.files().get("BTC/status.json")
        return json.loads(f) if f else None


@pytest.fixture
def world(tmp_path, monkeypatch):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        tk = fake.add_key(make_ec_key("test-trade-not-real", trade=True))
        _write_proof(sh, fake)
        job = Job(sh, fake, br, tk)
        code, r = cr.run_shadow(sh.dir, {**sh.env, "GITHUB_OUTPUT": str(job.out)}, base_url=fake.base_url,
                                clock=fake.clock, sleep=fake.sleep, push=lambda *a: sh.pushes.append(a),
                                mask_emit=lambda s: None, iterations=IT)
        assert code == 0, r.lines[-3:]
        assert br.save(sh.dir), br.errors
        yield sh, fake, br, job


def _next(fake, minutes=15):
    fake.now_s += minutes * 60


def _kills(text: bytes):
    return lambda d: (d / "BTC" / "kills.json").write_bytes(text)


# ---------------------------------------------------------------- the reading: a switch asked for is never off

FLAT_ASKED = {
    "quoted_true": b'{"flat_kill": "true"}',
    "one": b'{"flat_kill": 1}',
    "trailing_comma": b'{"flat_kill": true,}',
    "single_quotes": b"{'flat_kill': true}",
    "python_true": b'{"flat_kill": True}',
    "a_typo_of_true": b'{"flat_kill": tru}',
    "yes": b'{"flat_kill": "yes"}',
    "capitals_and_a_space": b'{"Flat Kill": true}',
    "nested": b'{"kills": {"flat_kill": true}}',
    "twice": b'{"flat_kill": false, "flat_kill": true}',
    "no_colon": b'{"flat_kill" true}',
    "yaml": b"flat_kill: true\n",
}
FLAT_NOT_ASKED = {
    "trailing_comma_off": b'{"flat_kill": false, "entry_kill": true,}',
    "quoted_false": b'{"flat_kill": "false"}',
    "zero": b'{"flat_kill": 0}',
    "null": b'{"flat_kill": null}',
    "a_typo_of_false": b'{"flat_kill": fasle}',
    "no": b'{"flat_kill": "no"}',
    "not_named": b'{"entry_kill": true,}',
    "prose": b"set flat_kill to true later, entry_kill now",
}


@pytest.mark.parametrize("name", sorted(FLAT_ASKED))
def test_a_flat_kill_asked_for_is_read_on_whatever_the_typo(name):
    k, why = cs.read_kills(FLAT_ASKED[name])
    assert why and k["flat_kill"] is True and k["entry_kill"] is True, (name, k, why)
    assert "flat_kill" in cs.kills_read(FLAT_ASKED[name]).named


@pytest.mark.parametrize("name", sorted(FLAT_NOT_ASKED))
def test_a_flat_kill_that_says_off_or_isnt_there_closes_nothing_but_entries_stay_off(name):
    k, why = cs.read_kills(FLAT_NOT_ASKED[name])
    assert why and k["flat_kill"] is False and k["entry_kill"] is True, (name, k, why)


def test_a_file_the_desk_can_read_is_read_as_written():
    assert cs.read_kills(None) == ({"flat_kill": False, "entry_kill": False, "jev_kill": False}, None)
    assert cs.read_kills(b'{"flat_kill": true, "entry_kill": false}') == (
        {"flat_kill": True, "entry_kill": False, "jev_kill": False}, None)
    assert cs.read_kills(b'\xef\xbb\xbf{"entry_kill": false, "reset_halt_at": "2026-11-02"}') == (
        {"flat_kill": False, "entry_kill": False, "jev_kill": False, "reset_halt_at": "2026-11-02"}, None)
    k, why = cs.read_kills(b'{"entry_kill": false}', plain=False)              # a symlink: never as written
    assert why and k["entry_kill"] is True


# the inputs the readers must agree on (the verifier's list, and more)
CASES = {
    "empty": b"", "whitespace": b"  \n", "null": b"null", "true_scalar": b"true", "a_list": b"[]",
    "bom_valid_on": b'\xef\xbb\xbf{"entry_kill": true}', "utf16_valid_off": '{"entry_kill": false}'.encode("utf-16"),
    "int_one": b'{"entry_kill": 1}', "nan": b'{"entry_kill": NaN}', "upper_key": b'{"ENTRY_KILL": true}',
    "dup_last_false": b'{"entry_kill": true, "entry_kill": false}', "nul": b"\0", "not_utf8": b"\xff\xfe\xfa",
    "deep": b"[" * 200000 + b"]" * 200000, "bigint": b'{"entry_kill": ' + b"1" * 6000 + b"}",
    "reset_long": b'{"entry_kill": false, "reset_halt_at": "' + b"x" * 41 + b'"}', "valid_off": b'{"entry_kill": false}',
    **{f"flat_{k}": v for k, v in FLAT_ASKED.items()}, **{f"off_{k}": v for k, v in FLAT_NOT_ASKED.items()},
}


def _run_reads(tmp_path, raw=None, link=None):
    with FakeCoinbase(cbi=0.0) as fake:
        sh = Shadow(fake, tmp_path / "run")
        r = cr.Run(sh.dir, sh.env)
        r.dir.mkdir(parents=True)
        if link is not None:
            os.symlink(link, r.dir / "kills.json")
        elif raw is not None:
            (r.dir / "kills.json").write_bytes(raw)
        k = r.kills_now()
        return {"flat_kill": k.flat_kill, "entry_kill": k.entry_kill, "jev_kill": k.jev_kill}, r.kills_file()[1]


def _saved_branch(tmp_path, monkeypatch, put):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    saved = _state_dir(tmp_path / "saved", runs=3)
    (saved / "BTC" / "kills.set.json").unlink()
    put(saved / "BTC")
    _git("--work-tree", str(saved), "add", "-A", ".", cwd=repo)
    _git("commit", "-qm", "saved", cwd=repo)
    _git("update-ref", REF, "HEAD", cwd=repo)
    monkeypatch.chdir(repo)
    d = _state_dir(tmp_path / "new", runs=4)
    a = subprocess.run(["git", "archive", REF, "BTC/kills.json"], cwd=repo, capture_output=True, check=False)
    if a.returncode == 0:
        subprocess.run(["tar", "-x", "-C", str(d)], input=a.stdout, check=True)     # loaded as the load step does
    return repo, d


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_run_check_and_stage_agree_and_an_unreadable_file_is_kept_as_written(tmp_path, monkeypatch, name):
    raw = CASES[name]
    run_k, run_why = _run_reads(tmp_path, raw)
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: (b / "kills.json").write_bytes(raw))
    said = cs.check(d, REF)
    cs.stage(d, tmp_path / "work", REF)
    staged = (tmp_path / "work" / "BTC" / "kills.json").read_bytes()
    stage_k, stage_why = cs.read_kills(staged)
    assert {k: stage_k[k] for k in cs.KILL_KEYS} == run_k, (name, run_k, stage_k)
    assert bool(run_why) == any("kills.json can't be read" in s for s in said) == bool(stage_why)
    if run_why:
        assert staged == raw                    # yours, never rewritten: every run reads it the same way
    assert run_k["entry_kill"] or not run_why    # whatever it can't read, entries are off


def test_a_dangling_symlink_for_kills_json_is_entry_kill_on_for_the_run_and_the_save(tmp_path, monkeypatch):
    run_k, run_why = _run_reads(tmp_path, link="nowhere-at-all")
    assert run_k["entry_kill"] is True and run_why          # before: every switch off ("no file")
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: os.symlink("nowhere-at-all", b / "kills.json"))
    said = cs.check(d, REF)                                 # before: refused, "isn't a file the SHADOW job saves"
    assert any("not a plain file" in s or "isn't a plain file" in s for s in said)
    cs.stage(d, tmp_path / "work", REF)
    out = tmp_path / "work" / "BTC" / "kills.json"
    assert out.is_symlink() and os.readlink(out) == "nowhere-at-all"       # kept as it is


def test_a_kill_the_run_set_is_kept_with_an_unreadable_file_and_never_lost(tmp_path, monkeypatch):
    typo = b'{"entry_kill": true,}'
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: (b / "kills.json").write_bytes(typo))
    (d / "BTC" / "kills.set.json").write_text('{"entry_kill": true}')      # already on in a file it can't read
    cs.check(d, REF)
    cs.stage(d, tmp_path / "w1", REF)
    assert (tmp_path / "w1" / "BTC" / "kills.json").read_bytes() == typo
    (d / "BTC" / "kills.set.json").write_text('{"flat_kill": true}')       # one the file doesn't say: written valid
    cs.stage(d, tmp_path / "w2", REF)
    assert json.loads((tmp_path / "w2" / "BTC" / "kills.json").read_text()) == {
        "flat_kill": True, "entry_kill": True, "jev_kill": False}


# ---------------------------------------------------------------- the whole loop: load, run, check, stage, commit

def _open_long(sh, fake, job):
    _next(fake)
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved, (r.arm_lines, job.br.errors)
    sh.breakout()
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved and fake.position() is not None, (r.lines[-10:], job.br.errors)


@pytest.mark.parametrize("raw", [FLAT_ASKED[k] for k in ("trailing_comma", "quoted_true", "one", "single_quotes",
                                                         "python_true")])
def test_live_closes_the_position_for_a_flat_kill_with_a_typo_and_keeps_your_file(world, raw):
    sh, fake, br, job = world
    _open_long(sh, fake, job)
    br.edit(_kills(raw))
    _next(fake)
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved, (r.lines[-5:], br.errors)
    assert fake.position() is None and fake.resting() == []          # before: still open, "flat_kill": false saved
    assert br.files()["BTC/kills.json"] == raw                         # kept as you wrote it
    assert any("flat_kill on too" in a for a in r.alerts)
    assert job.status()["note"] == cr.KILLS_UNREADABLE_FLAT
    # and every run after reads it the same way: no new entry while your file says flat
    sh.breakout()
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved and fake.position() is None
    assert any("kills.json on the coinbase-state branch can't be read" in a for a in r.alerts)


def test_shadow_a_flat_kill_that_says_off_closes_nothing_and_says_so(world):
    sh, fake, br, job = world
    br.edit(_kills(b'{"flat_kill": fasle, "entry_kill": true}'))
    _next(fake)
    code, r, changed, saved = job.run()
    assert code == 0 and saved
    assert any("flat_kill is in it but says off" in a and "DESK_COINBASE_KILL to flat" in a for a in r.alerts)
    assert job.status()["note"] == cr.KILLS_UNREADABLE


def test_a_typo_that_lands_between_the_load_and_the_save_is_kept_and_said_by_the_next_run(world):
    sh, fake, br, job = world
    _next(fake)
    code, r, changed, saved = job.run(between=_kills(b'{"flat_kill": true,}'))
    assert code == 0 and saved, br.errors
    assert br.files()["BTC/kills.json"] == b'{"flat_kill": true,}'     # before: rewritten as flat_kill false
    assert any("kills.json can't be read" in s for s in br.said[-1])
    n0 = len(sh.pushes)
    _next(fake)
    code, r, changed, saved = job.run()
    assert code == 0 and saved
    assert job.status()["note"] == cr.KILLS_UNREADABLE_FLAT                   # before: None, no alert, no push
    assert any(t == "BTC SHADOW: ALERT" and "kills.json" in b for t, b, *_ in sh.pushes[n0:])
    assert r.kills_now().flat_kill is True and r.kills_now().entry_kill is True


STRAYS = {"notes.md": b"my notes", "kills.json.orig": b'{"flat_kill": false}', "Kills.json": b'{"flat_kill": true}',
          "old/state.txt": b"x"}


@pytest.mark.parametrize("name", sorted(STRAYS))
def test_a_file_you_add_under_btc_never_stops_a_save_and_is_kept(world, name):
    sh, fake, br, job = world

    def add(d):
        (d / "BTC" / name).parent.mkdir(parents=True, exist_ok=True)
        (d / "BTC" / name).write_bytes(STRAYS[name])
        (d / "TODO.txt").write_text("root file\n")

    br.edit(add)
    runs0 = job.status()["runs"]
    for _ in range(2):
        _next(fake)
        code, r, changed, saved = job.run()
        assert code == 0 and saved, br.errors                      # before: every save refused
    assert job.status()["runs"] == runs0 + 2
    files = br.files()
    assert files[f"BTC/{name}"] == STRAYS[name] and files["TODO.txt"] == b"root file\n"
    kill_like = "kill" in name.lower()
    assert r.kills_now().entry_kill is kill_like
    assert (job.status()["note"] == cr.KILLS_STRAY) is kill_like
    assert any(f"BTC/{name} on the coinbase-state branch looks like a kill switch" in a for a in r.alerts) is kill_like
    if name == "Kills.json":
        assert any("it says flat_kill, which isn't acted on from there" in a for a in r.alerts)
        assert r.kills_now().flat_kill is False


def test_live_with_a_file_you_added_trades_and_saves_every_run(world):
    sh, fake, br, job = world
    _next(fake)
    assert job.run("live")[3]
    br.edit(lambda d: (d / "BTC" / "notes.md").write_text("my notes"))
    sh.breakout()
    code, r, changed, saved = job.run("live")
    assert code == 0 and saved and fake.position() is not None, br.errors
    for _ in range(2):
        _next(fake)
        code, r, changed, saved = job.run("live")
        assert code == 0 and saved, br.errors                      # before: unsaved, re-adopting each run
    st = job.status()
    assert st["runs"] == 4 and st["armed"] is True and not any("adopt" in a.lower() for a in r.alerts)


def test_a_file_the_run_wrote_that_isnt_the_desks_or_the_branchs_is_still_refused(tmp_path, monkeypatch):
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: None)
    (d / "BTC" / "plain.json").write_text('{"balance": 500}')
    with pytest.raises(cs.StateCheckError, match="isn't on the branch"):
        cs.check(d, REF)


# ---------------------------------------------------------------- a save the save step will refuse is pushed

def test_a_shadow_save_that_will_be_refused_is_pushed_by_the_run(world):
    sh, fake, br, job = world
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())         # you delete status.json on the branch
    fake.now_s = (fake.now_s // 21600) * 21600 + 21600 + 4 * 60     # 18:04 UTC: SHADOW's 6-hour push slot
    code, r, changed, saved = job.run(over={"GITHUB_ACTIONS": "true"})
    assert code == 0 and changed and saved is False and "no status.json" in br.errors[-1]
    assert any(t == "BTC SHADOW: NOT SAVED" and "no status.json" in b for t, b, *_ in sh.pushes)
    assert "its save is refused" in r.public_line()       # before: "state saved", and nothing said anywhere else
    _next(fake)
    n0 = len(sh.pushes)
    job.run(over={"GITHUB_ACTIONS": "true"})
    assert not any("NOT SAVED" in t for t, *_ in sh.pushes[n0:])     # SHADOW: once every 6 hours


def test_a_live_save_that_will_be_refused_is_pushed_urgently_at_every_run(world):
    sh, fake, br, job = world
    _next(fake)
    assert job.run("live", over={"GITHUB_ACTIONS": "true"})[3]
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())
    for _ in range(2):
        _next(fake)
        n0 = len(sh.pushes)
        code, r, changed, saved = job.run("live", over={"GITHUB_ACTIONS": "true"})
        assert code == 0 and changed and saved is False
        assert [p for t, b, p, *_ in sh.pushes[n0:] if t == "BTC LIVE: NOT SAVED"] == ["urgent"]


def test_a_save_that_will_go_through_pushes_nothing_more(world):
    sh, fake, br, job = world
    br.edit(lambda d: (d / "BTC" / "notes.md").write_text("mine"))
    _next(fake)
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run(over={"GITHUB_ACTIONS": "true"})
    assert code == 0 and saved and r.save_refused_why is None
    assert not any("NOT SAVED" in t for t, *_ in sh.pushes[n0:]) and "state saved" in r.public_line()


# ---------------------------------------------------------------- the repository variable

@pytest.mark.parametrize("var,flat,entry,odd", [
    ("flat", True, False, False), ("FLAT", True, False, False), ("flat_kill", True, False, False),
    ("Flat-Kill", True, False, False), ("close", True, False, False), ("flatten", True, False, False),
    ("entry", False, True, False), (" entry_kill ", False, True, False), ("off", False, True, True),
    ("false", False, True, True), ("0", False, True, True), ("", False, False, False)])
def test_desk_coinbase_kill_reads_flat_kill_close_and_flatten_as_flat_and_alerts_anything_else(tmp_path, var, flat,
                                                                                               entry, odd):
    with FakeCoinbase(cbi=0.0) as fake:
        sh = Shadow(fake, tmp_path)
        r = cr.Run(sh.dir, {**sh.env, "DESK_COINBASE_KILL": var})
        k = r.kills_now()
        assert (k.flat_kill, k.entry_kill) == (flat, entry)     # before: flat_kill, close, flatten were entry only
        r.say_kills_unreadable()
        assert any("DESK_COINBASE_KILL" in a and "isn't entry or flat" in a for a in r.alerts) is odd


# ---------------------------------------------------------------- a LIVE that never armed, refused over its secrets

@pytest.mark.parametrize("over", [{"DESK_STATE_KEY": None}, {"DESK_STATE_KEY": "short"}])
def test_a_never_armed_live_refused_over_desk_state_key_pushes_not_armed_while_shadow_is_fresh(world, over):
    sh, fake, br, job = world
    head = br.files()
    _next(fake, 30)                                         # 14:38 UTC: outside the 6-hour slot, SHADOW 30 min old
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run("live", over)
    assert code == 2 and not changed and br.files() == head
    assert [t for t, *_ in sh.pushes[n0:]] == ["BTC LIVE: not armed"]      # before: nothing
    assert "DESK_STATE_KEY" in sh.pushes[-1][1]
    fake.now_s += 3 * 3600                                  # 17:38: SHADOW's public status is over 2 hours old
    n0 = len(sh.pushes)
    job.run("live", over)
    assert sh.pushes[n0:] == []
    fake.now_s += 30 * 60                                   # 18:08: the 6-hour slot
    job.run("live", over)
    assert [t for t, *_ in sh.pushes[n0:]] == ["BTC LIVE: not armed"]
    assert "put the SHADOW run step back" in sh.pushes[-1][1]


def test_an_armed_live_refused_over_desk_state_key_still_saves_its_status_alone(world):
    sh, fake, br, job = world
    _next(fake)
    assert job.run("live")[3]
    _next(fake, 30)
    n0 = len(sh.pushes)
    code, r, changed, saved = job.run("live", {"DESK_STATE_KEY": None})
    assert code == 2 and changed and saved
    st = job.status()
    assert st["armed"] is True and st["problem"] == "key" and st["note"] == "DESK_STATE_KEY is not set"
    assert sh.pushes[n0:] == []                             # outside the 6-hour slot: as before
