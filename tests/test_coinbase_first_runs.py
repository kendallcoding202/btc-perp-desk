"""Round 5, SHADOW onboarding: the owner's first runs, with the secrets mixed up, then put right, through the workflow's
load, run, check, stage and commit (tests/test_coinbase_kills_fail_closed.py's Branch and Job). Each fails on the
code before it (9259605):

- the two secrets swapped (the key file in DESK_STATE_KEY, the password in COINBASE_CDP_KEY): the run saved a whole
  state sealed with the key file as its password, and once both were put right every run was refused ("DESK_STATE_KEY
  doesn't open the saved state") until the branch was deleted. Any first-run refusal over the key did the same once
  DESK_STATE_KEY changed. Now, until SHADOW has had a working run, a refusal saves its status alone;
- a DESK_STATE_KEY with a line break ran, and its board never opened on the SHADOW page (its password field drops
  line breaks). Now it's refused, status only, and said;
- BTC/status.json deleted while a run was under way: NOT SAVED waited for the 6-hourly window. Now it's pushed at the
  first run that can't save, and the SHADOW page says the status is missing;
- a refusal that writes its status pushed up to three times in a row in each 6-hour window; now once a window;
- a push ntfy.sh refused with NTFY_TOKEN was lost without trace; "working again" said "every read OK" of any run that
  finished.

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
from fake_coinbase import V3, FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import IT, KEY, Shadow

ROOT = Path(__file__).resolve().parents[1]
STATE = "BTC/state-shadow.enc.json"


@pytest.fixture
def first(tmp_path, monkeypatch):
    """A first SHADOW job: no coinbase-state branch yet, the View key in place."""
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("first5-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        yield sh, fake, br, Job(sh, fake, br, None)


def _run(job, over=None, **kw):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over=over, **kw)
    return code, r, saved


def _titles(sh):
    return [p[0] for p in sh.pushes]


# ---------------------------------------------------------------- never a state a corrected secret can't open

def test_secrets_swapped_then_put_right_starts_shadow_without_deleting_the_branch(first):
    sh, fake, br, job = first
    key_file, password = sh.env["COINBASE_CDP_KEY"], sh.env["DESK_STATE_KEY"]
    code, r, saved = _run(job, {"COINBASE_CDP_KEY": password, "DESK_STATE_KEY": key_file})
    assert code == 2 and saved and fake.requests == [], (r.problem, r.lines[-2:])
    assert STATE not in br.files()                  # before: a whole state, sealed with the key file
    st = job.status()
    assert st["problem"] == "key" and st["runs"] == 0 and st["note"] == \
        "DESK_STATE_KEY looks like the key file (the two secrets are swapped)"
    assert _titles(sh) == ["BTC SHADOW: not working"] and "swapped" in sh.pushes[0][1]
    _next(fake)
    code, r, saved = _run(job)                      # each secret where it belongs
    assert code == 0 and saved and job.status()["ok"] is True, (r.problem, r.lines[-3:], br.errors)
    assert "BTC SHADOW: connected (read only)" in _titles(sh)


@pytest.mark.parametrize("how", ["password_as_key", "trade_key", "unauthorized", "coinbase_down"])
def test_a_refusal_before_shadow_ever_worked_saves_no_state_and_the_password_can_still_change(first, how):
    """Refused over the key itself at the first runs: status only (the dashboard says why), no state; the owner then
    fixes the key and changes DESK_STATE_KEY too, and SHADOW starts on the new one."""
    sh, fake, br, job = first
    over = {"password_as_key": {"COINBASE_CDP_KEY": "p" * 40},
            "trade_key": {"COINBASE_CDP_KEY": fake.add_key(make_ec_key("first5-trade-not-real",
                                                                       trade=True)).file_json()},
            "unauthorized": {"COINBASE_CDP_KEY": make_ec_key("first5-unknown-not-real").file_json()},
            "coinbase_down": {}}[how]
    if how == "coinbase_down":
        fake.fail_next("GET", f"{V3}/key_permissions", 503, times=30)
    for _ in range(2):
        code, r, saved = _run(job, over)
        assert code != 0 and saved, (how, r.problem, br.errors)
        assert STATE not in br.files() and "BTC/board-shadow.enc.json" not in br.files(), (how, sorted(br.files()))
        st = job.status()
        assert st["ok"] is False and st["runs"] == 0 and st["problem"] == {
            "password_as_key": "key", "trade_key": "permissions", "unauthorized": "auth", "coinbase_down": "read"}[how]
        _next(fake)
    if how == "trade_key":
        assert job.status()["key_view_only"] is False
    fake.fail.clear()
    new = "a-new-password-for-shadow-" + "n" * 20   # changed while the first runs were refused: fine now
    code, r, saved = _run(job, {"DESK_STATE_KEY": new})
    assert code == 0 and saved and job.status()["ok"] is True, (how, r.problem, r.lines[-3:], br.errors)
    statebox.open_json(br.files()[STATE].decode(), new, statebox.state_aad("shadow"))


def test_a_shadow_that_has_worked_still_saves_its_state_when_refused_over_the_key(first):
    """The control: once SHADOW has a working run, a refusal over the key is a failed run of its state, as before."""
    sh, fake, br, job = first
    assert _run(job)[2]
    _next(fake)
    trade = fake.add_key(make_ec_key("first5-trade2-not-real", trade=True)).file_json()
    code, r, saved = _run(job, {"COINBASE_CDP_KEY": trade})
    st = job.status()
    assert code == 2 and saved and st["runs"] == 2 and st["failed_runs"] == 1 and st["problem"] == "permissions"


@pytest.mark.parametrize("tail", ["\n", "\r\n", "\r"])
def test_a_state_key_with_a_line_break_is_refused_status_only_and_said(first, tail):
    sh, fake, br, job = first
    code, r, saved = _run(job, {"DESK_STATE_KEY": KEY + tail})
    assert code == 2 and saved and fake.requests == [] and STATE not in br.files(), (r.problem, r.lines[-2:])
    st = job.status()
    assert st["problem"] == "key" and st["note"] == "DESK_STATE_KEY has a line break in it (set it again without one)"
    assert "line break" in sh.pushes[0][1]
    _next(fake)
    code, r, saved = _run(job)                      # set again without it: the page's password field opens the board
    assert code == 0 and saved
    statebox.open_(json.loads(br.files()["BTC/board-shadow.enc.json"]), KEY, statebox.board_aad("shadow"))


def test_the_guide_says_a_refused_first_run_saves_no_state_and_a_missing_secret_skips_the_job():
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    assert "Until SHADOW has had a run that works, a refused run saves only its status, never a state" in guide
    assert "Keep the same `DESK_STATE_KEY` while you do" not in guide
    assert "**DESK_STATE_KEY looks like the key file:**" in guide and "**DESK_STATE_KEY has a line break" in guide
    assert "the `shadow` job is skipped and stays green. It sends no push and writes no status" in guide
    assert "**DESK_STATE_KEY is too short**, or **is not set**" not in guide


# ---------------------------------------------------------------- a desk file deleted on the branch is news

def _gh(over=None):
    return {"GITHUB_ACTIONS": "true", **(over or {})}


@pytest.mark.parametrize("when", ["mid_run", "between_runs"])
@pytest.mark.parametrize("actions", [True, False])
def test_status_json_deleted_is_written_back_and_pushed_not_saved_once(first, when, actions):
    """The first run that finds BTC/status.json gone (one deleted while a run was under way: two runs after the last
    save) writes it back from the state it opened, saves nothing else, and pushes NOT SAVED; the run after saves as
    usual and says it's working again. No owner edit is needed."""
    sh, fake, br, job = first
    gh = _gh() if actions else None
    assert _run(job, gh)[2]
    _next(fake)
    rm = lambda d: (d / "BTC" / "status.json").unlink()        # noqa: E731
    if when == "mid_run":
        code, r, saved = _run(job, gh, between=rm)
        assert saved is False and "no status.json" in br.errors[-1]
    else:
        assert _run(job, gh)[2]
        br.edit(rm)
    state = br.files()[STATE]
    seen = []
    for _ in range(4):
        _next(fake)
        code, r, saved = _run(job, gh)
        seen.append((saved, _titles(sh)))
    assert seen[0][0] and seen[0][1] == ["BTC SHADOW: NOT SAVED"], seen      # before (mid_run): nothing for 6 hours
    assert "BTC/status.json" in br.said[-4][0] and "written back" in br.said[-4][0], br.said[-4]
    assert all(s for s, _ in seen) and seen[1][1] == ["BTC SHADOW: working again"], seen
    assert [t for _, ts in seen for t in ts].count("BTC SHADOW: NOT SAVED") == 1, seen
    assert br.files()[STATE] != state and job.status()["ok"] is True and job.status()["runs"] == (
        4 if when == "mid_run" else 5)


def test_the_status_written_back_carries_the_states_own_start_and_runs(first):
    sh, fake, br, job = first
    assert _run(job)[2]
    _next(fake)
    assert _run(job)[2]
    was = job.status()
    br.edit(lambda d: (d / "BTC" / "status.json").unlink())
    _next(fake)
    n = len(fake.requests)
    code, r, saved = _run(job)
    st = job.status()
    assert saved and code == 1 and len(fake.requests) == n          # nothing read, nothing sent: the status alone
    assert (st["started_ts"], st["runs"], st["failed_runs"], st["opened"]) == \
        (was["started_ts"], was["runs"], was["failed_runs"], was["opened"])
    assert st["ok"] is False and st["problem"] == "state" and st["note"] == cr.STATUS_BACK


def test_the_state_deleted_between_runs_is_pushed_not_saved_at_once_with_how_to_recover(first):
    sh, fake, br, job = first
    assert _run(job, _gh())[2]
    _next(fake)
    assert _run(job, _gh())[2]
    br.edit(lambda d: (d / STATE).unlink())
    seen = []
    for _ in range(3):
        _next(fake)
        code, r, saved = _run(job, _gh())
        seen.append((saved, [t for t in _titles(sh) if "NOT SAVED" in t]))
    assert seen[0] == (False, ["BTC SHADOW: NOT SAVED"]) and seen[1:] == [(False, []), (False, [])], seen
    assert "BTC SHADOW: connected (read only)" not in _titles(sh)      # before: at every run, none of them saved
    why = r.save_refused_why
    assert "isn't on the branch (deleted?): put it back as it was" in why and "a reset needs" not in why, why


def test_a_state_deleted_while_a_run_is_under_way_is_put_back_by_that_runs_save(first):
    sh, fake, br, job = first
    assert _run(job, _gh())[2]
    _next(fake)
    code, r, saved = _run(job, _gh(), between=lambda d: (d / STATE).unlink())
    assert saved and STATE in br.files() and job.status()["runs"] == 2


NODE = shutil.which("node")
PAGE = r"""
const m = require(process.argv[2]);
const now = Date.now();
console.log(JSON.stringify({
  gone: m.statusHtml(null, now, true), none: m.statusHtml(null, now, false),
  zero: m.statusHtml({v: 1, mode: "SHADOW", coin: "BTC", updated: now, started_ts: now, runs: 0, failed_runs: 0,
                      ok: false, problem: "key", note: "COINBASE_CDP_KEY isn't a Coinbase key file", opened: false}, now),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_shadow_page_says_the_status_is_missing_when_only_the_board_is_there(tmp_path):
    js = tmp_path / "t.js"
    js.write_text(PAGE)
    out = json.loads(subprocess.run([NODE, str(js), str(ROOT / "dashboard" / "shadow.js")], capture_output=True,
                                    text=True, timeout=60, check=True).stdout)
    assert "NOT SAVED: the SHADOW status is missing from the coinbase-state branch" in out["gone"]
    assert "delete the coinbase-state branch to start over" in out["gone"] and "secrets are set" not in out["gone"]
    assert "No SHADOW status yet" in out["none"]
    assert "no run has worked yet" in out["zero"] and "run 0" not in out["zero"]


# ---------------------------------------------------------------- once a window, not once a run

def test_a_refusal_that_writes_its_status_is_pushed_once_a_window(first):
    sh, fake, br, job = first
    times, t0 = [], fake.now_s
    for i in range(4 * 13):                         # 13 hours, a run every 15 minutes, landing early in each window
        fake.now_s = t0 + i * 900 + 60
        sh.pushes.clear()
        job.run(over={"DESK_STATE_KEY": "s" * 20})
        times += [fake.now_s for p in sh.pushes]
    slots = [int(s * 1000) // cr.PUSH_EVERY_MS for s in times[1:]]
    gaps = [(b - a) / 3600 for a, b in zip(times, times[1:])]
    assert times and times[0] - t0 < 1800, times                       # at once
    assert len(slots) == len(set(slots)) and len(slots) >= 2, times   # then once a window (before: up to 3 a window)
    assert max(gaps) <= 6.5, gaps


# ---------------------------------------------------------------- the pushes themselves

def test_a_push_ntfy_refuses_with_ntfy_token_goes_again_without_it_and_is_recorded(first, monkeypatch):
    sh, fake, br, job = first
    sent = []

    def ntfy(title, body, priority="default", tags="robot", env=None, token=True):
        sent.append((title, body, token))
        return "HTTP 401" if token else None
    monkeypatch.setattr(cr, "ntfy_send", ntfy)
    br.load(sh.dir)
    env = {**sh.env, "NTFY_TOPIC": "first5-topic-not-real", "NTFY_TOKEN": "tk_first5-not-real",
           "GITHUB_OUTPUT": str(job.out)}
    code, r = cr.run_shadow(sh.dir, env, base_url=fake.base_url, clock=fake.clock, sleep=fake.sleep,
                            mask_emit=lambda s: None, iterations=IT)
    assert code == 0 and sent
    title, body, token = sent[0]
    assert token is True and sent[1][0] == title and sent[1][2] is False
    assert "NTFY_TOKEN was refused by ntfy.sh (HTTP 401)" in sent[1][1] and "$" not in sent[1][1]
    assert r.push_failures and "HTTP 401" in r.push_failures[0]
    board = statebox.open_json((sh.dir / "BTC" / "board-shadow.enc.json").read_text(), KEY, statebox.board_aad())
    assert board["shadow"]["push_failed"] and any("didn't go out" in a for a in board["shadow"]["alerts"])
    assert any("didn't go out" in ln for ln in board["shadow"]["log"])


def test_ntfy_send_says_why_a_push_didnt_go_out(monkeypatch):
    import urllib.error

    def refuse(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(cr.urllib.request, "urlopen", refuse)
    env = {"NTFY_TOPIC": "first5-topic-not-real", "NTFY_TOKEN": "tk_first5-not-real"}
    assert cr.ntfy_send("t", "b", env=env) == "HTTP 401" and cr.ntfy_push("t", "b", env=env) is False
    assert cr.ntfy_send("t", "b", env={}) == cr.NO_TOPIC


def test_working_again_says_ran_and_saved_and_names_a_read_that_still_failed(first):
    sh, fake, br, job = first
    assert _run(job)[2]
    _next(fake)
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 503, times=8)        # a run that stops part way
    code, r, saved = _run(job)
    assert code == 1 and saved
    _next(fake)
    fake.fail.clear()
    fake.fail_next("GET", f"{V3}/transaction_summary", 503, times=8)        # fixed, but the fee tier isn't read
    code, r, saved = _run(job)
    again = [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: working again"]
    assert code == 0 and again, _titles(sh)
    assert "ran and saved this run" in again[0] and "every read OK" not in again[0]
    assert "Not every read was OK: Fee tier read" in again[0], again[0]
