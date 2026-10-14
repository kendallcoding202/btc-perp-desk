"""Two fixes, each with tests that fail on the code before them (desk/coinbase_run.py, desk/coinbase_state.py):

1. A first LIVE attempt that refused (a mistyped DESK_LIVE_CONFIRM, say) saved state-live.enc.json, board-live and a
   LIVE status.json; the save check took them as "the first LIVE save"; every SHADOW run after that refused ("LIVE runs
   on this account"), so SHADOW's record went stale and LIVE could never arm either. Now LIVE saves only once it has
   armed, and a LIVE state that never armed is no LIVE start, for SHADOW, for LIVE and for the save check.
2. A BTC/kills.json the owner mistyped was read as every switch off (fail-open), and then the save check refused the
   save over it, so every run after went unsaved. Now the runs, check and stage all read it one way
   (coinbase_state.read_kills): entry_kill on, said on the board, in the status and on the phone, and saved.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import coinbase_state as cs
from desk import statebox
from fake_coinbase import ARM_ENV, FakeCoinbase, make_ec_key
from test_coinbase_shadow import IT, KEY, Shadow, _write_proof
from test_coinbase_state import _bash, _git, _remote_files, _state_dir, _step_script, actions  # noqa: F401

NO_BRANCH = "refs/remotes/origin/none"


def _live(sh, fake, env, gate=(("backtest: passes", True, "ok"),), cap=1_000.0):
    return cr.run_live_once(sh.dir, env, max_notional=cap, gate=list(gate), base_url=fake.base_url,
                            clock=fake.clock, sleep=fake.sleep, push=lambda *a: sh.pushes.append(a),
                            mask_emit=lambda s: None, iterations=IT)


def _files(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted((d / "BTC").iterdir())} if (d / "BTC").is_dir() else {}


def _titles(sh):
    return [p[0] for p in sh.pushes]


def _shadow_ok(sh):
    code, r = sh.run()
    assert code == 0 and r.problem is None, (code, r.problem, r.lines[-3:])
    return r


# ---------------------------------------------------------------- 1. a refused first LIVE attempt saves nothing

REFUSALS = {
    "confirm_typo": ({"DESK_LIVE_CONFIRM": "I understand this trades real money"}, True),
    "product_typo": ({"DESK_LIVE_COINBASE": "BIP-20DEC30"}, True),
    "view_only_key": ({}, False),                  # refused at arm()'s network reads: the key can't trade
    "not_a_key_file": ({"COINBASE_CDP_KEY": "not a key"}, True),     # refused before arm(): the key won't load
}


@pytest.mark.parametrize("how", sorted(REFUSALS))
def test_a_refused_first_live_attempt_saves_nothing_and_shadow_then_live_carry_on(tmp_path, how):
    over, trade = REFUSALS[how]
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-view-not-real"))
        _write_proof(sh, fake)
        _shadow_ok(sh)
        before = _files(sh.dir)
        tk = fake.add_key(make_ec_key("test-trade-not-real", trade=True))
        out = tmp_path / "gh_output"
        live_env = {**sh.env, **ARM_ENV, "COINBASE_CDP_KEY": tk.file_json() if trade else sh.env["COINBASE_CDP_KEY"],
                    "GITHUB_OUTPUT": str(out)}
        code, r = _live(sh, fake, {**live_env, **over})
        assert code == 2 and r.problem in ("refused", "key")
        # nothing on the branch changes: no LIVE state, board, status or kill, and the save step is skipped
        assert _files(sh.dir) == before
        assert out.read_text() == "changed=false\n"
        assert fake.non_get() == []
        assert r.public_line().startswith("LIVE BTC: not armed: ") and "nothing saved" in r.public_line()
        assert "BTC LIVE: not armed" in _titles(sh)            # SHADOW's record is fresh: pushed at once
        # SHADOW carries on, as before the attempt, for the next 4 hours
        for _ in range(2):
            sh.close(fake.last * 1.001)
            _shadow_ok(sh)
        assert sh.status()["mode"] == "SHADOW"
        # and LIVE, with everything right, is a first start: the gate and SHADOW's record checked, and it arms
        out.unlink()
        code, r = _live(sh, fake, {**live_env, "COINBASE_CDP_KEY": tk.file_json()})
        assert code == 0, r.arm_lines
        assert any("SHADOW ran 14+ days" in ln and "PASS" in ln for ln in r.arm_lines)
        assert any("backtest: passes" in ln and "PASS" in ln for ln in r.arm_lines)
        assert out.read_text() == "changed=true\n"
        st = sh.status()
        assert st["mode"] == "LIVE" and st["armed"] is True and st["runs"] == 1 and st["failed_runs"] == 0
        assert cr.armed_at(sh.state("live"))
        # once LIVE has armed it counts: SHADOW refuses beside it
        code, r = sh.run()
        assert code == 2 and r.problem == "refused" and "LIVE runs on this account" in r.lines[-1]


def test_a_never_armed_attempt_pushes_while_shadows_record_is_fresh_then_every_six_hours(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-trade-not-real", trade=True))
        _write_proof(sh, fake)
        env = {**sh.env, **ARM_ENV, "DESK_LIVE_CONFIRM": "no"}
        _live(sh, fake, env)
        _live(sh, fake, env)
        assert _titles(sh).count("BTC LIVE: not armed") == 2      # within 2 hours of SHADOW's last run: each one
        fake.now_s += 3 * 3600                                      # 17:08 UTC: SHADOW's record is stale now
        _live(sh, fake, env)
        assert _titles(sh).count("BTC LIVE: not armed") == 2
        fake.now_s += 3600 - 8 * 60 + 60                            # 18:01 UTC: the 6-hour mark
        _live(sh, fake, env)
        assert _titles(sh).count("BTC LIVE: not armed") == 3
        assert "put the SHADOW run step back" in sh.pushes[-1][1]
        assert not (sh.dir / "BTC" / "state-live.enc.json").exists() and fake.non_get() == []


def _leftover(sh, fake):
    """What a refused first LIVE attempt left before this fix: a LIVE state with no armed_at, its board, and a LIVE
    status (no "armed") over SHADOW's."""
    t0 = fake.clock_ms() - 3600_000
    bundle = {"v": 1, "files": {"desk.json": json.dumps({"meta": {"started_ts": t0, "runs": 1, "failed_runs": 1,
                                                                  "mode": "live"}}),
                                "account.json": json.dumps({**cr.new_account(), "last_run_ts": t0})}}
    d = sh.dir / "BTC"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state-live.enc.json").write_text(statebox.seal_json(bundle, KEY, statebox.state_aad("live"), iterations=IT))
    (d / "board-live.enc.json").write_text(statebox.seal_json({"updated": t0}, KEY, statebox.board_aad("live"),
                                                              compress=False, iterations=IT))
    (d / "status.json").write_text(json.dumps({"v": 1, "mode": "LIVE", "coin": "BTC", "product": cr.PRODUCT,
                                               "updated": t0, "started_ts": t0, "runs": 1, "failed_runs": 1,
                                               "ok": False, "problem": "refused"}))
    return t0


def test_a_live_state_that_never_armed_is_no_live_start_for_shadow_or_live(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-view-not-real"))
        _write_proof(sh, fake)
        t0 = _leftover(sh, fake)
        _shadow_ok(sh)                                           # before: refused, "LIVE runs on this account"
        assert any("never armed" in e["text"] for e in sh.board()["events"])
        assert sh.status()["mode"] == "SHADOW"
        tk = fake.add_key(make_ec_key("test-trade-not-real", trade=True))
        env = {**sh.env, **ARM_ENV, "COINBASE_CDP_KEY": tk.file_json()}
        code, r = _live(sh, fake, env, gate=[("backtest: passes", False, "no")])
        assert code == 2 and any("backtest: passes" in ln and "FAIL" in ln for ln in r.arm_lines)
        code, r = _live(sh, fake, env)
        assert code == 0, r.arm_lines
        meta = sh.file("live", "desk.json")["meta"]
        assert meta["started_ts"] > t0 and meta["runs"] == 1 and meta["failed_runs"] == 0 and meta["armed_at"]


def test_shadow_still_refuses_beside_an_armed_live_state_or_one_it_cant_open(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path)
        d = sh.dir / "BTC"
        d.mkdir(parents=True)
        armed = {"v": 1, "files": {"desk.json": json.dumps({"meta": {"armed_at": 1, "started_ts": 1}})}}
        for text in (statebox.seal_json(armed, KEY, statebox.state_aad("live"), iterations=IT),
                     statebox.seal_json(armed, "another-key-" + "z" * 30, statebox.state_aad("live"), iterations=IT)):
            (d / "state-live.enc.json").write_text(text)
            code, r = sh.run()
            assert code == 2 and r.problem == "refused" and fake.requests == []
            assert not (d / "state-shadow.enc.json").exists()


# ---------------------------------------------------------------- 1. the save check: LIVE counts once armed

@pytest.fixture
def leftover_repo(tmp_path):
    """A coinbase-state ref as the old code left it: SHADOW's state (3 runs) with a never-armed LIVE attempt's files
    and its LIVE status over SHADOW's."""
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", cwd=r)
    saved = _state_dir(tmp_path / "saved", runs=3)
    (saved / "BTC" / "kills.set.json").unlink()
    live = _state_dir(tmp_path / "live", runs=1, started=5_000, mode="LIVE")
    for n in ("state-live.enc.json", "board-live.enc.json", "status.json"):
        (saved / "BTC" / n).write_bytes((live / "BTC" / n).read_bytes())
    st = json.loads((saved / "BTC" / "status.json").read_text())
    assert "armed" not in st and st["mode"] == "LIVE"
    _git("--work-tree", str(saved), "add", "-A", ".", cwd=r)
    _git("commit", "-qm", "saved", cwd=r)
    _git("update-ref", "refs/remotes/origin/coinbase-state", "HEAD", cwd=r)
    return r, saved


def test_check_refuses_a_live_status_that_never_armed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    monkeypatch.chdir(repo)
    for kw in ({}, {"armed": False}, {"armed": None}):
        d = _state_dir(tmp_path / f"live{len(kw)}{kw.get('armed')}", runs=1, mode="LIVE", status_kw=kw)
        with pytest.raises(cs.StateCheckError, match="never armed"):
            cs.check(d, NO_BRANCH)
    d = _state_dir(tmp_path / "armed", runs=1, mode="LIVE", status_kw={"armed": True})
    assert cs.check(d, NO_BRANCH) == ["no saved state on the branch: a first save"]


def test_a_shadow_save_over_a_never_armed_live_attempt_drops_its_files(leftover_repo, tmp_path, monkeypatch):
    r, saved = leftover_repo
    monkeypatch.chdir(r)
    ref = "refs/remotes/origin/coinbase-state"
    # the SHADOW run loaded the branch (the LIVE leftover as is) and saves its own state over it
    d = _state_dir(tmp_path / "new", runs=4)
    for n in ("state-live.enc.json", "board-live.enc.json"):
        (d / "BTC" / n).write_bytes((saved / "BTC" / n).read_bytes())
    said = cs.check(d, ref)
    assert "never armed" in said[-1] and "dropped" in said[-1]
    work = tmp_path / "work"
    cs.stage(d, work, ref)
    names = sorted(p.name for p in (work / "BTC").iterdir())
    assert names == ["board-shadow.enc.json", "kills.json", "state-shadow.enc.json", "state-shadow.prev.enc.json",
                     "status.json"]
    assert json.loads((work / "BTC" / "status.json").read_text())["mode"] == "SHADOW"
    # a SHADOW state that didn't come from the branch (no leftover in it: a fresh start) isn't saved over it
    fresh = _state_dir(tmp_path / "fresh", runs=1, started=9_000)
    with pytest.raises(cs.StateCheckError, match="didn't load it"):
        cs.check(fresh, ref)


def test_an_armed_live_save_replaces_a_never_armed_attempts_files(leftover_repo, tmp_path, monkeypatch):
    r, saved = leftover_repo
    monkeypatch.chdir(r)
    ref = "refs/remotes/origin/coinbase-state"
    d = _state_dir(tmp_path / "live", runs=1, started=8_000, mode="LIVE", status_kw={"armed": True})
    (d / "BTC" / "state-shadow.enc.json").write_bytes((saved / "BTC" / "state-shadow.enc.json").read_bytes())
    (d / "BTC" / "board-shadow.enc.json").write_bytes((saved / "BTC" / "board-shadow.enc.json").read_bytes())
    assert "the first LIVE save" in cs.check(d, ref)[-1]
    work = tmp_path / "work"
    cs.stage(d, work, ref)
    out = work / "BTC"
    assert not (out / "state-live.prev.enc.json").exists()          # the leftover isn't LIVE's previous state
    assert (out / "state-live.enc.json").read_bytes() == (d / "BTC" / "state-live.enc.json").read_bytes()
    assert (out / "state-shadow.enc.json").read_bytes() == (saved / "BTC" / "state-shadow.enc.json").read_bytes()


# ---------------------------------------------------------------- 2. an unreadable kills.json fails closed, and saves

BAD_KILLS = {
    "trailing_comma": b'{"entry_kill": true,}',
    "off_with_a_typo": b'{"entry_kill": false,}',
    "not_a_bool": b'{"entry_kill": "no"}',
    "unknown_key": b'{"entry_kill": false, "note": "x"}',
    "a_list": b"[]",
    "not_utf8": b"\xff\xfe",
}


@pytest.mark.parametrize("name", sorted(BAD_KILLS))
def test_the_runs_check_and_stage_read_a_bad_kills_json_the_same_way(tmp_path, monkeypatch, name):
    raw = BAD_KILLS[name]
    kills, why = cs.read_kills(raw)
    assert kills["entry_kill"] is True and kills["flat_kill"] is False and why
    with FakeCoinbase(cbi=0.0) as fake:
        sh = Shadow(fake, tmp_path)
        r = cr.Run(sh.dir, sh.env)
        r.dir.mkdir(parents=True)
        (r.dir / "kills.json").write_bytes(raw)
        assert r.kills_file() == (kills, why) and r.kills_now().entry_kill is True
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    monkeypatch.chdir(repo)
    d = _state_dir(tmp_path / "new", runs=1, extra={"kills.json": raw.decode("latin-1")})
    (d / "BTC" / "kills.json").write_bytes(raw)
    assert any("kills.json isn't readable" in ln for ln in cs.check(d, NO_BRANCH))      # before: refused, unsaved
    cs.stage(d, tmp_path / "work", NO_BRANCH)
    assert json.loads((tmp_path / "work" / "BTC" / "kills.json").read_text()) == kills


def test_a_bad_kills_json_keeps_a_switch_written_exactly_true_and_acts_on_nothing_else():
    kills, why = cs.read_kills(b'{"flat_kill": true, "entry_kill": "yes", "reset_halt_at": "2026-11-02", "x": 1}')
    assert why and kills == {"flat_kill": True, "entry_kill": True, "jev_kill": False}
    assert cs.read_kills(None) == ({"flat_kill": False, "entry_kill": False, "jev_kill": False}, None)
    assert cs.read_kills(b'{"entry_kill": false, "reset_halt_at": "2026-11-02"}') == (
        {"flat_kill": False, "entry_kill": False, "jev_kill": False, "reset_halt_at": "2026-11-02"}, None)


def _to_a_breakout(sh, fake):
    sh.run()
    fake.deposit(2_000.0, dt=600)
    fake.now_s += 300
    sh.run()
    sh.breakout()


@pytest.mark.parametrize("raw", [b'{"entry_kill": true,}', b'{"entry_kill": false,}'])
def test_shadow_with_a_kills_json_it_cant_read_takes_no_entry_says_so_and_saves(tmp_path, monkeypatch, raw):
    with FakeCoinbase(cbi=0.0) as fake:
        fake.now_s += 8 * 60
        ctrl = Shadow(fake, tmp_path / "control")
        _to_a_breakout(ctrl, fake)
        (ctrl.dir / "BTC" / "kills.json").write_text('{"entry_kill": false}')
        assert ctrl.run()[0] == 0
        assert [w["kind"] for w in ctrl.r.would_send_now][:1] == ["entry"]     # the same close enters, read right
    with FakeCoinbase(cbi=0.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path / "bad")
        _to_a_breakout(sh, fake)
        (sh.dir / "BTC" / "kills.json").write_bytes(raw)
        code, r = sh.run()
        assert code == 0 and r.problem is None
        assert [w for w in r.would_send_now if w["kind"] == "entry"] == []     # before: it entered (all off)
        b = sh.board()
        assert b["kills"]["entry_kill"] is True
        assert "BTC/kills.json couldn't be read (it isn't JSON)" in b["shadow"]["entry_kill_reason"]
        assert any("kills.json on the coinbase-state branch can't be read" in a for a in b["shadow"]["alerts"])
        assert any(t == "BTC SHADOW: ALERT" and "kills.json" in body for t, body, *_ in sh.pushes)
        st = sh.status()
        assert st["ok"] is True and st["note"] == cr.KILLS_UNREADABLE
        assert "entry_kill on" in r.public_line()
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-q", cwd=repo)
        monkeypatch.chdir(repo)
        cs.check(sh.dir, NO_BRANCH)                                          # before: "kills.json isn't JSON"
        cs.stage(sh.dir, tmp_path / "work", NO_BRANCH)
        saved = json.loads((tmp_path / "work" / "BTC" / "kills.json").read_text())
        assert saved == {"flat_kill": False, "entry_kill": True, "jev_kill": False}
        # the next run reads the file the save wrote: entry_kill on, the reason kept, nothing unreadable now
        (sh.dir / "BTC" / "kills.json").write_text(json.dumps(saved))
        assert sh.run()[0] == 0 and sh.status()["note"] is None
        assert "couldn't be read" in sh.board()["shadow"]["entry_kill_reason"]


def test_live_with_a_kills_json_it_cant_read_sends_no_entry(tmp_path):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("test-trade-key-not-real", trade=True))
        _write_proof(sh, fake)
        assert sh.run("live")[0] == 0
        (sh.dir / "BTC" / "kills.json").write_text('{"entry_kill": true,}')     # entries off, with a typo
        sh.breakout()
        assert sh.run("live")[0] == 0
        assert fake.position() is None and fake.non_get() == []                   # before: it bought one contract
        assert sh.status()["note"] == cr.KILLS_UNREADABLE and sh.status()["armed"] is True
        assert any(t == "BTC LIVE: ALERT" and "kills.json" in body for t, body, *_ in sh.pushes)


def test_the_workflow_saves_through_a_kills_json_you_mistyped(actions, tmp_path):  # noqa: F811
    origin, ws, rt, env = actions
    load, save = _step_script("Load the SHADOW state"), _step_script("Save the SHADOW state")
    assert _bash(load, ws, env).returncode == 0
    _state_dir(rt / "cb", runs=1)
    assert _bash(save, ws, env).returncode == 0
    you = tmp_path / "you"
    _git("clone", "-q", "--branch", "coinbase-state", f"file://{origin}", str(you), cwd=tmp_path)
    (you / "BTC" / "kills.json").write_text('{"flat_kill": false, "entry_kill": true,}')
    _git("commit", "-qam", "entry_kill on (a typo)", cwd=you)
    _git("push", "-q", "origin", "coinbase-state", cwd=you)
    for p in (rt / "cb").glob("BTC/*"):
        p.unlink()
    assert _bash(load, ws, env).returncode == 0
    assert (rt / "cb" / "BTC" / "kills.json").read_text().endswith(",}")          # loaded as you wrote it
    _state_dir(rt / "cb", runs=2)
    res = _bash(save, ws, env)
    assert res.returncode == 0, res.stdout + res.stderr                          # before: ::error:: isn't JSON
    assert "kills.json isn't readable" in res.stdout
    files = _remote_files(origin)
    assert json.loads(files["BTC/kills.json"]) == {"flat_kill": False, "entry_kill": True, "jev_kill": False}
    assert json.loads(files["BTC/status.json"])["runs"] == 2



# ---------------------------------------------------------------- the setup guide says what the code does

def test_the_setup_guide_matches_the_code_and_coinbases_key_page():
    from desk.brokers import coinbase_rest as rest
    from desk.brokers.coinbase_rest import CoinbaseREST

    doc = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    part2 = doc[doc.index("## Part 2"):]
    first, every = part2.index("At the first LIVE start only:"), part2.index("At every LIVE run:")
    assert first < part2.index("The go-live gate passes") < every
    assert first < part2.index("SHADOW's record on this account") < every
    assert part2.index("`DESK_LIVE_CONFIRM` is set to exactly") > every
    assert "A LIVE run that refuses before LIVE has ever armed saves nothing" in part2
    # the trailing gap: one POST, POST_RETRIES + 1 tries of the client's timeout, the backoff between, PROTECT_S
    import inspect
    timeout = inspect.signature(CoinbaseREST.__init__).parameters["timeout"].default
    gap = (rest.POST_RETRIES + 1) * timeout + sum(rest.BACKOFF_S * 2 ** k for k in range(rest.POST_RETRIES))
    assert 45 < gap < 60 and "close to a minute" in part2 and "about 30 s" not in doc
    assert f"waits up to {timeout:g} s" in part2 and f"tried {rest.POST_RETRIES + 1} times" in part2
    assert f"{rest.PROTECT_S:g} s past the run's deadline" in part2
    # Coinbase's labels (2026-10-14), quoted
    for label in ("Secret API Keys", "Create API key", "API key nickname", "Advanced Settings",
                  "Coinbase App & Advanced Trade", "**Accounts**", "View is enabled by default",
                  "Allow requests from any IP address", "ECDSA", "Create & Download", "Download API key"):
        assert label in doc, label
    assert "Coinbase renames things" in doc
