"""Kill switches on the coinbase-state branch, round 3: a kill the desk sets outlives your fix of a kills.json typo, one
vocabulary for DESK_COINBASE_KILL and kills.json, a closed list of words that say off, a kill-switch-like file anywhere
on the branch, your own files deleted between a run's load and its save, and a .gitignore, .gitattributes or .github/
kept on the branch. Each test fails on the code before it (0e1f31b):

- a kill the desk set while BTC/kills.json couldn't be read was carried only by the typo's fail-closed reading, so
  fixing the typo turned entries back on (the desk's reason lost);
- DESK_COINBASE_KILL "flat entry", "flat_kill=true" and "close position now" read entry only; kills.json's {"flat":
  true}, {"close": true}, {"flatten": true} and {"flat_kil": true} read flat off, and so did any value starting with f
  or n ("now", "flat", "force" too);
- a kills.json at the branch's root, or under btc/, was kept and never read, with no alert;
- deleting your own file on the branch while a run was under way made that run's save refuse;
- a .gitignore kept on the branch could drop the desk's files from the save, and a .gitattributes change them.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from fake_coinbase import FakeCoinbase
from test_coinbase_kills_fail_closed import REF, _kills, _next, _run_reads, _saved_branch, world  # noqa: F401
from test_coinbase_shadow import Shadow
from test_coinbase_state import _bash, _git, _remote_files, _state_dir, _step_script, actions  # noqa: F401

TYPO = b'{"flat_kill": false, "entry_kill": false, "jev_kill": false,}'


# ---------------------------------------------------------------- a kill the desk set outlives your typo fix

def test_a_kill_the_desk_set_while_kills_json_is_unreadable_outlives_the_typo_fix(tmp_path, monkeypatch):
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: (b / "kills.json").write_bytes(TYPO))
    (d / "BTC" / "kills.set.json").write_text('{"entry_kill": true}')
    cs.check(d, REF)
    cs.stage(d, tmp_path / "w1", REF)
    out = tmp_path / "w1" / "BTC"
    assert (out / "kills.json").read_bytes() == TYPO                    # yours, kept as you wrote it
    rec = json.loads((out / "kills.desk.json").read_text())
    assert rec == {"entry_kill": cs.DESK_PENDING}                       # the desk's own record of it
    # you fix the typo, to what you meant (entry off): the desk's kill is still on
    fixed = TYPO.replace(b",}", b"}")
    kills, why, desk = cs.kills_effective(fixed, True, (out / "kills.desk.json").read_bytes())
    assert why is None and kills["entry_kill"] is True and desk == {"entry_kill": cs.DESK_PENDING}


def test_end_to_end_a_desk_kill_then_your_typo_fix_then_you_clear_it_on_purpose(world):
    sh, fake, br, job = world
    base = b'{"flat_kill": false, "entry_kill": false, "jev_kill": false, "reset_halt_at": "2026-11-02"'
    br.edit(_kills(base + b",}"))
    _desk_kill(job, fake)
    assert br.files()["BTC/kills.json"] == base + b",}"
    assert json.loads(br.files()["BTC/kills.desk.json"]) == {"entry_kill": cs.DESK_PENDING}
    br.edit(_kills(base + b"}"))                     # you fix the trailing comma the alert told you about
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is True, br.files()["BTC/kills.json"]    # before: entries back on
    reason = sh.board()["shadow"]["entry_kill_reason"]
    assert reason and "isn't explained" in reason, reason                  # with the desk's reason
    assert any("the desk set entry_kill" in a for a in r.alerts), r.alerts
    k = json.loads(br.files()["BTC/kills.json"])
    assert k["entry_kill"] is True and k["reset_halt_at"] == "2026-11-02"  # now written into your file
    assert json.loads(br.files()["BTC/kills.desk.json"]) == {"entry_kill": cs.DESK_WRITTEN}
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is True
    br.edit(_kills(json.dumps({**k, "entry_kill": False}).encode()))       # you looked, and cleared it on purpose
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is False
    assert sh.board()["shadow"]["entry_kill_reason"] is None
    assert "BTC/kills.desk.json" not in br.files()


def _desk_kill(job, fake):
    """A kill the desk sets itself: money appears with no transaction for it, and the 3-hour wait runs out."""
    fake.deposit(500.0, dt=600)
    fake.now_s += 300
    assert job.run()[3]
    fake.cbi += 40.0
    for _ in range(3):
        fake.now_s += 15 * 60
        assert job.run()[3], job.br.errors
    fake.now_s += 3 * 3600
    code, r, changed, saved = job.run()
    assert saved and r.kills_set.get("entry_kill") is True, (r.lines[-6:], job.br.errors)
    return r


def test_a_kill_the_desk_set_in_a_readable_file_clears_when_you_set_it_false_as_before(world):
    sh, fake, br, job = world
    br.edit(_kills(b'{"entry_kill": false}'))
    _desk_kill(job, fake)
    assert json.loads(br.files()["BTC/kills.json"])["entry_kill"] is True
    assert json.loads(br.files()["BTC/kills.desk.json"]) == {"entry_kill": cs.DESK_WRITTEN}
    br.edit(_kills(b'{"entry_kill": false}'))
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is False and "BTC/kills.desk.json" not in br.files()
    assert sh.board()["shadow"]["entry_kill_reason"] is None


def test_a_desk_record_you_mangle_fails_closed_and_is_written_valid_again(tmp_path, monkeypatch):
    def put(b):
        (b / "kills.json").write_text('{"entry_kill": false}')
        (b / "kills.desk.json").write_text("not json")
    repo, d = _saved_branch(tmp_path, monkeypatch, put)
    kills, why, desk = cs.kills_effective(b'{"entry_kill": false}', True, b"not json")
    assert kills["entry_kill"] is True and desk == {"entry_kill": cs.DESK_PENDING}
    cs.check(d, REF)                                # never a reason to refuse a save
    cs.stage(d, tmp_path / "w", REF)
    out = tmp_path / "w" / "BTC"
    assert json.loads((out / "kills.json").read_text())["entry_kill"] is True
    assert json.loads((out / "kills.desk.json").read_text()) == {"entry_kill": cs.DESK_WRITTEN}


# ---------------------------------------------------------------- one vocabulary, a closed list of offs

@pytest.mark.parametrize("var,flat,entry,odd", [
    ("flat entry", True, True, False), ("flat_kill=true", True, False, False), ("close position now", True, False,
                                                                                   False),
    ("close_all", True, False, False), ("close_position", True, False, False), ("FLATTEN", True, False, False),
    ("no_entries", False, True, False), ("Entry Kill", False, True, False), ("entry, flat", True, True, False),
    ("off", False, True, True), ("stop", False, True, True), ("", False, False, False)])
def test_desk_coinbase_kill_reads_every_word_for_flat_and_entry_anywhere_in_it(tmp_path, var, flat, entry, odd):
    with FakeCoinbase(cbi=0.0) as fake:
        r = cr.Run(Shadow(fake, tmp_path).dir, {"DESK_COINBASE_KILL": var})
        k = r.kills_now()
        assert (k.flat_kill, k.entry_kill) == (flat, entry), var
        r.say_kills_unreadable()
        assert any("DESK_COINBASE_KILL" in a for a in r.alerts) is odd


FLAT_ON = {"key_flat": b'{"flat": true}', "key_close": b'{"close": true}', "key_flatten": b'{"flatten": true}',
           "key_misspelt": b'{"flat_kil": true}', "key_close_position": b'{"close_position": true}',
           "key_close_all": b'{"close_all": "yes"}', "now": b'{"flat_kill": "now"}', "NOW!": b'{"flat_kill": "NOW!"}',
           "force": b'{"flat_kill": "force"}', "flat": b'{"flat_kill": "flat"}', "nah": b'{"flat_kill": "nah"}',
           "text_close": b'{"close": true,}', "text_flat": b"flat: true\n"}
FLAT_OFF = {"false": b'{"flat_kill": false}', "f": b'{"flat_kill": "f"}', "no": b'{"flat_kill": "no"}',
            "n": b'{"flat_kill": "n"}', "off": b'{"flat_kill": "off"}', "null": b'{"flat_kill": null}',
            "none": b'{"flat_kill": "none"}', "nil": b'{"flat_kill": "nil"}', "zero": b'{"flat_kill": 0}',
            "empty": b'{"flat_kill": ""}', "fasle": b'{"flat_kill": fasle}', "flase": b'{"flat_kill": "flase"}',
            "fals": b'{"flat_kill": "fals"}', "nul": b'{"flat_kill": "nul"}', "nulll": b'{"flat_kill": "nulll"}',
            "noo": b'{"flat_kill": "noo"}', "of": b'{"flat_kill": "of"}', "offf": b'{"flat_kill": "offf"}',
            "False": b'{"flat_kill": False}', "None": b'{"flat_kill": None}'}


@pytest.mark.parametrize("name", sorted(FLAT_ON))
def test_a_file_that_asks_for_a_close_in_any_of_its_words_reads_flat_on(name):
    got = cs.kills_read(FLAT_ON[name])
    assert got.why and got.kills["flat_kill"] is True and got.kills["entry_kill"] is True, (name, got)
    assert "flat_kill" in got.named


@pytest.mark.parametrize("name", sorted(FLAT_OFF))
def test_only_the_closed_list_of_offs_reads_flat_off(name):
    got = cs.kills_read(FLAT_OFF[name])
    if got.why is None:                             # {"flat_kill": false} is a file the desk reads as written
        assert got.kills["flat_kill"] is False
        return
    assert got.kills["flat_kill"] is False and got.kills["entry_kill"] is True, (name, got)


@pytest.mark.parametrize("raw", [FLAT_ON[k] for k in ("now", "key_close", "key_misspelt", "text_close")] +
                         [FLAT_OFF[k] for k in ("fasle", "noo")])
def test_the_run_check_and_stage_read_the_new_words_alike(tmp_path, monkeypatch, raw):
    run_k, run_why = _run_reads(tmp_path, raw)
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: (b / "kills.json").write_bytes(raw))
    cs.check(d, REF)
    cs.stage(d, tmp_path / "work", REF)
    staged = (tmp_path / "work" / "BTC" / "kills.json").read_bytes()
    stage_k, stage_why = cs.read_kills(staged)
    assert {k: stage_k[k] for k in cs.KILL_KEYS} == run_k and bool(run_why) == bool(stage_why)


# ---------------------------------------------------------------- a kill-switch-like file anywhere on the branch

@pytest.mark.parametrize("path", ["kills.json", "btc/kills.json", "BTC/sub/kills.json", "KILLS.JSON",
                                  "notes/kill-switch.txt"])
def test_a_file_named_like_a_kill_switch_anywhere_turns_entry_kill_on_and_says_so(world, path):
    sh, fake, br, job = world

    def add(d):
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_text('{"flat_kill": true, "entry_kill": true}')
    br.edit(add)
    for _ in range(2):
        _next(fake)
        code, r, changed, saved = job.run()
        assert code == 0 and saved, br.errors
    k = r.kills_now()
    assert k.entry_kill is True and k.flat_kill is False                 # named, not acted on from there
    assert any(f"{path} on the coinbase-state branch looks like a kill switch" in a and
               "it says flat_kill, which isn't acted on from there" in a for a in r.alerts), r.alerts
    assert job.status()["note"] == cr.KILLS_STRAY
    assert br.files()[path] == b'{"flat_kill": true, "entry_kill": true}'  # kept as you wrote it


# ---------------------------------------------------------------- your own file deleted between load and save

def test_deleting_your_own_file_while_a_run_is_under_way_never_refuses_its_save(world):
    sh, fake, br, job = world
    br.edit(lambda d: (d / "BTC" / "notes.md").write_text("mine"))
    br.edit(lambda d: (d / "TODO.txt").write_text("root"))
    _next(fake)
    assert job.run()[3]
    _next(fake)

    def delete(d):
        (d / "BTC" / "notes.md").unlink()
        (d / "TODO.txt").unlink()
    code, r, changed, saved = job.run(between=delete)
    assert code == 0 and saved, br.errors                                # before: refused
    assert "BTC/notes.md" not in br.files() and "TODO.txt" not in br.files()   # your deletion kept
    _next(fake)
    assert job.run()[3]


def test_a_file_the_run_wrote_is_still_refused_when_the_load_is_known(tmp_path, monkeypatch):
    repo, d = _saved_branch(tmp_path, monkeypatch, lambda b: (b / "notes.md").write_text("mine"))
    (d / "BTC" / "notes.md").write_text("mine")                         # as the load extracted it
    loaded = cs.GitBranch(REF)
    assert cs.check(d, REF, loaded=loaded)
    (d / "BTC" / "plain.json").write_text('{"balance": 500}')            # something the run wrote
    with pytest.raises(cs.StateCheckError, match="isn't a file the SHADOW job saves"):
        cs.check(d, REF, loaded=loaded)
    (d / "BTC" / "plain.json").unlink()
    (d / "BTC" / "notes.md").write_text("the run changed it")            # a file of yours the run overwrote
    with pytest.raises(cs.StateCheckError, match="changed since the load"):
        cs.check(d, REF, loaded=loaded)


# ---------------------------------------------------------------- .gitignore, .gitattributes, .github/ kept on the branch

def _first_save_then(actions, tmp_path, yours: dict):
    """The workflow's own load and save: a first save, then you commit `yours` ({path: text}) on the branch (as GitHub's
    web editor would: the bytes as written, no attributes applied), then one more run."""
    origin, ws, rt, env = actions
    load, save = _step_script("Load the SHADOW state"), _step_script("Save the SHADOW state")
    assert _bash(load, ws, env).returncode == 0
    _state_dir(rt / "cb", runs=1)
    res = _bash(save, ws, env)
    assert res.returncode == 0, res.stdout + res.stderr
    you = tmp_path / "you"
    _git("clone", "-q", "--branch", "coinbase-state", f"file://{origin}", str(you), cwd=tmp_path)
    for path, text in yours.items():
        (you / path).parent.mkdir(parents=True, exist_ok=True)
        (you / path).write_text(text)
        sha = _git("hash-object", "-w", "--no-filters", path, cwd=you).stdout.strip()
        _git("update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", cwd=you)
    _git("commit", "-qm", "yours", cwd=you)
    _git("push", "-q", "origin", "coinbase-state", cwd=you)
    old = _git("rev-parse", "coinbase-state", cwd=origin).stdout.strip()
    for p in sorted((rt / "cb").rglob("*"), reverse=True):
        p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()
    assert _bash(load, ws, env).returncode == 0
    _state_dir(rt / "cb", runs=2)
    res = _bash(save, ws, env)
    return res, old, origin


def test_a_gitignore_on_the_branch_never_drops_the_desks_files(actions, tmp_path):
    res, old, origin = _first_save_then(actions, tmp_path, {".gitignore": "BTC/\n*.json\n"})
    assert res.returncode == 0, res.stdout + res.stderr
    files = _remote_files(origin)
    assert json.loads(files["BTC/status.json"])["runs"] == 2               # before: dropped, every later save refused
    assert "BTC/state-shadow.enc.json" in files and files[".gitignore"] == "BTC/\n*.json\n"


def test_a_gitattributes_on_the_branch_is_dropped_so_it_cant_change_the_desks_files(actions, tmp_path):
    res, old, origin = _first_save_then(actions, tmp_path, {".gitattributes": "*.json working-tree-encoding=UTF-16\n",
                                                            "BTC/.gitattributes": "* text eol=crlf ident\n"})
    assert res.returncode == 0, res.stdout + res.stderr
    files = _remote_files(origin)
    assert ".gitattributes" not in files and "BTC/.gitattributes" not in files
    assert json.loads(files["BTC/status.json"])["runs"] == 2 and files["BTC/kills.json"].endswith("}\n")


def test_a_github_folder_on_the_branch_is_kept_exactly_as_it_is(actions, tmp_path):
    res, old, origin = _first_save_then(actions, tmp_path, {".github/workflows/x.yml": "on: push\r\njobs: {}\r\n",
                                                            ".gitattributes": "* text eol=lf\n"})
    assert res.returncode == 0, res.stdout + res.stderr
    new = _git("rev-parse", "coinbase-state", cwd=origin).stdout.strip()
    assert new != old and _git("rev-parse", f"{new}:.github", cwd=origin).stdout == \
        _git("rev-parse", f"{old}:.github", cwd=origin).stdout
    assert "add -A -f ." in _step_script("Save the SHADOW state")


# ---------------------------------------------------------------- the guide: NOT SAVED and ALERT are their own pushes

def test_the_guide_names_not_saved_and_alert_as_their_own_pushes():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    step22 = doc[doc.index("\n22. "):doc.index("\n23. ")]
    not_working = step22[:step22.index("**BTC SHADOW: ALERT**")]
    assert "**BTC SHADOW: not working**" in not_working
    assert "NOT SAVED" not in not_working and "kills.json" not in not_working     # before: listed as its reasons
    assert "**BTC SHADOW: NOT SAVED**" in step22


# ---------------------------------------------------------------- round 4: kills.json deleted or renamed
# Each fails on the code before it (da2c7f8): a desk kill already written into kills.json was cleared by deleting the
# file (no file read as "you set it false"), or by renaming it and then deleting the renamed one; and a kills.json you
# deleted while a run was under way came back from the copy the run loaded.

def _rm(path):
    return lambda d: (d / path).unlink()


def test_deleting_kills_json_never_clears_a_kill_the_desk_set(world):
    sh, fake, br, job = world
    br.edit(_kills(b'{"entry_kill": false}'))
    _desk_kill(job, fake)
    assert json.loads(br.files()["BTC/kills.desk.json"]) == {"entry_kill": cs.DESK_WRITTEN}
    br.edit(_rm("BTC/kills.json"))
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is True                    # before: cleared, entries back on
    assert any("isn't on the coinbase-state branch" in a for a in r.alerts), r.alerts
    assert json.loads(br.files()["BTC/kills.json"])["entry_kill"] is True    # written into a new one
    assert json.loads(br.files()["BTC/kills.desk.json"]) == {"entry_kill": cs.DESK_WRITTEN}
    assert sh.board()["shadow"]["entry_kill_reason"]
    br.edit(_kills(b'{"entry_kill": false}'))                           # cleared on purpose, as before
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is False and "BTC/kills.desk.json" not in br.files()


def test_renaming_kills_json_then_deleting_the_renamed_one_never_clears_a_desk_kill(world):
    sh, fake, br, job = world
    br.edit(_kills(b'{"entry_kill": false}'))
    _desk_kill(job, fake)
    br.edit(lambda d: (d / "BTC" / "kills.json").rename(d / "BTC" / "Kills.json"))
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is True
    assert "BTC/kills.desk.json" in br.files()                          # before: dropped at this save
    br.edit(_rm("BTC/Kills.json"))
    _next(fake)
    code, r, changed, saved = job.run()
    assert saved and r.kills_now().entry_kill is True                    # before: entries back on
    assert json.loads(br.files()["BTC/kills.json"])["entry_kill"] is True


def test_a_kills_json_you_delete_while_a_run_is_under_way_turns_your_switches_off(world):
    sh, fake, br, job = world
    br.edit(_kills(b'{"flat_kill": true}'))
    _next(fake)
    assert job.run()[3]
    _next(fake)
    code, r, changed, saved = job.run(between=_rm("BTC/kills.json"))
    assert saved, br.errors
    k = cs.read_kills(br.files().get("BTC/kills.json"))[0]
    assert k["flat_kill"] is False                                       # before: written back from the load
    # the save writes a fresh kills.json, every switch false (docs/coinbase-setup.md step 24 says so)
    assert json.loads(br.files()["BTC/kills.json"]) == {"flat_kill": False, "entry_kill": False, "jev_kill": False}
    guide = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    assert "turns your switches off: that run acted on the copy it loaded, and its save writes a fresh" in guide
    _next(fake)
    code, r, changed, saved = job.run()
    assert r.kills_now().flat_kill is False
