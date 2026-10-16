"""Round 8, what SHADOW says, through the workflow's load, run, check and save
(tests/test_coinbase_kills_fail_closed.py's Branch and Job). Each fails on the code before it (0610893):

- the SHADOW page showed an earlier run's board as "the last run" and "EVERY READ, THIS RUN" under a status that said a
  later run stopped (its checks contradicting the status: a View-only key under "the key can trade");
- a key-permissions answer with a flag missing or not true or false was read as an answer ("this key can't View"), and
  SHADOW told you to make a new key, which would never help;
- an order book empty on one side (or candles missing, or no funding rate yet) was "an answer in a shape the desk can't
  read", which the guide says may need a fix in the code;
- after SHADOW had worked, a refusal at the key check said "SHADOW stopped part way", though nothing was read;
- "working again" nested the old note in brackets, and an "ok": false of yours in BTC/status.json came back as "the runs
  before had stopped: <your note>";
- the public line of a never-worked run that saved its board said "only its status is saved";
- the README and the guide's Part 2 said as fact LIVE behaviour that docs/coinbase-live-status.md lists as broken.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from desk import coinbase_run as cr
from desk import statebox
from fake_coinbase import V3, FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import KEY, Shadow

ROOT = Path(__file__).resolve().parents[1]
STATE = "BTC/state-shadow.enc.json"
STATUS = "BTC/status.json"
BOARD = "BTC/board-shadow.enc.json"
GH = {"GITHUB_ACTIONS": "true"}
NODE = shutil.which("node")
UNREADABLE = ("Coinbase's answer couldn't be read (the reason is on your phone and the SHADOW page; the next run tries "
              "again)")
SHORT = "Coinbase's answer was incomplete, or its market was empty (the next run tries again)"


@pytest.fixture
def first(tmp_path, monkeypatch):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("words8-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        br.clock = fake.clock
        yield sh, fake, br, Job(sh, fake, br, None)


@pytest.fixture
def worked(first):
    sh, fake, br, job = first
    for _ in range(2):
        code, r, saved = _run(job)
        assert code == 0 and saved, (r.lines[-3:], br.errors)
        _next(fake)
    return first


def _run(job, over=None, **kw):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over={**GH, **(over or {})}, **kw)
    return code, r, saved


def _titles(sh):
    return [p[0] for p in sh.pushes]


def _bodies(sh, title="BTC SHADOW: not working"):
    return [b for t, b, *_ in sh.pushes if t == title]


def _board(br):
    return statebox.open_json(br.files()[BOARD].decode(), KEY, statebox.board_aad("shadow"))


# ---------------------------------------------------------------- the page: a board older than the status says so

PAGE = r"""
const m = require(process.argv[1]);
const bd = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const strip = (h) => h.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
const same = Object.assign({}, bd.status, {updated: bd.board.status.checked});
console.log(JSON.stringify({older: strip(m.boardHtml(bd.board, bd.now, bd.status)),
                            same: strip(m.boardHtml(bd.board, bd.now, same))}));
"""


@pytest.mark.skipif(NODE is None, reason="node isn't installed")
def test_the_page_says_a_board_is_an_earlier_runs_when_a_later_run_saved_only_its_status(first):
    """The verifier's case: run 1 stops after the key passed (its board kept), run 2 is refused over a trade key
    before the key passed (its status alone)."""
    sh, fake, br, job = first
    fake.fail_next("GET", f"{V3}/cfm/sweeps", 200, times=1, body=b'{"sweeps": [5]}')
    _run(job)
    b1 = _board(br)
    _next(fake)
    trade = fake.add_key(make_ec_key("words8-trade-not-real", trade=True))
    _run(job, {"COINBASE_CDP_KEY": trade.file_json()})
    b2, st = _board(br), job.status()
    assert b2 == b1 and st["updated"] > b2["status"]["checked"] and st["problem"] == "permissions"
    out = json.loads(subprocess.run([NODE, "-e", PAGE, str(ROOT / "dashboard" / "shadow.js")], input=json.dumps(
        {"board": b2, "status": st, "now": st["updated"] + 60_000}), capture_output=True, text=True, timeout=60,
        check=True).stdout)
    older = out["older"]
    assert "what an earlier run," in older and "The last run, " in older and "saved only its status" in older, older
    assert "EVERY READ, THE RUN OF" in older and "EVERY READ, THIS RUN" not in older     # before: "THIS RUN"
    assert "what the last run," in out["same"] and "EVERY READ, THIS RUN" in out["same"]   # the control


# ---------------------------------------------------------------- an answer it can't read, an answer that's short

@pytest.mark.parametrize("body", [b'{"can_view": "maybe", "portfolio_uuid": 7}',
                                  b'{"can_view": true, "portfolio_type": "DEFAULT"}'])
def test_a_permissions_answer_it_cant_read_is_never_said_as_the_keys_permissions(first, body):
    sh, fake, br, job = first
    fake.fail_next("GET", f"{V3}/key_permissions", 200, times=1, body=body)
    code, r, saved = _run(job)
    st = job.status()
    assert code == 1 and saved and st["problem"] == "read" and st["note"] == UNREADABLE, st   # before: "can't View"
    got = _bodies(sh)
    assert got and "Coinbase answered, but in a shape the desk can't read (CoinbaseDataError)" in got[0], got
    assert "View" not in got[0] and "Create a key" not in got[0]


@pytest.mark.parametrize("worked_first", [False, True])
def test_an_empty_book_is_said_as_an_answer_that_was_short(first, worked_first):
    sh, fake, br, job = first
    if worked_first:
        code, r, saved = _run(job)
        assert code == 0
        _next(fake)
    fake.book_levels = 0
    code, r, saved = _run(job)
    st = job.status()
    assert code == 1 and saved and st["problem"] == "read" and st["note"] == SHORT, st    # before: "couldn't be read"
    got = _bodies(sh)
    assert got and "Coinbase's order book was empty on one side (FeedError), while reading the candles and the book" \
        in got[0], got
    assert "shape" not in got[0]


def test_candles_missing_are_said_as_an_answer_that_was_short():
    exc = cr.failure(__import__("desk.feeds.coinbase", fromlist=["FeedError"]).FeedError(
        "14 2h candles missing from Coinbase's answer outside any closure (x to y): a short answer; the next run tries "
        "again"), "shadow")
    assert exc == ("read", SHORT, "Coinbase's candles came back short (candles missing) (FeedError)")
    shape = cr.failure(__import__("desk.feeds.coinbase", fromlist=["FeedError"]).FeedError(
        "Coinbase answered products/x with a list, not an object"), "shadow")
    assert shape[1] == UNREADABLE                                   # the control: a shape it can't read stays that


# ---------------------------------------------------------------- the pushes' words

def test_after_shadow_worked_a_refusal_at_the_key_check_says_it_didnt_run(worked):
    sh, fake, br, job = worked
    fake.fail_next("GET", f"{V3}/key_permissions", 401, times=5)
    code, r, saved = _run(job)
    fake.fail.clear()
    got = _bodies(sh)
    assert code == 1 and saved and got and got[0].startswith("SHADOW didn't run: "), got   # before: "stopped part way"


def test_working_again_says_the_reason_once_without_its_brackets(worked):
    sh, fake, br, job = worked
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 200, times=20, body=b'{"balance_summary": 5}')
    code, r, saved = _run(job)
    fake.fail.clear()
    assert code == 1 and saved and job.status()["note"] == UNREADABLE
    _next(fake)
    code, r, saved = _run(job)
    got = _bodies(sh, "BTC SHADOW: working again")
    assert code == 0 and got, _titles(sh)
    assert got[0].startswith("SHADOW ran and saved this run. The runs before had stopped: Coinbase's answer couldn't "
                             "be read."), got[0]                    # before: the note nested in brackets
    assert "((" not in got[0] and "))" not in got[0]


def test_an_ok_false_of_yours_in_the_status_is_never_said_as_runs_stopping(worked):
    sh, fake, br, job = worked

    def ch(d):
        p = d / STATUS
        st = json.loads(p.read_text())
        st.update(ok=False, problem="auth", note="owner says no")
        p.write_text(json.dumps(st, indent=1) + "\n")
    br.edit(ch)
    code, r, saved = _run(job)
    assert code == 0 and saved
    assert not any("owner says no" in b for t, b, *_ in sh.pushes), sh.pushes          # before: "working again"
    assert "BTC SHADOW: working again" not in _titles(sh)


def test_a_secret_slip_after_shadow_worked_then_fixed_says_working_again_with_its_reason(worked):
    sh, fake, br, job = worked
    code, r, saved = _run(job, {"DESK_STATE_KEY": "x" * 31})
    assert saved and job.status()["problem"] == "key"
    _next(fake)
    code, r, saved = _run(job)
    got = _bodies(sh, "BTC SHADOW: working again")
    assert code == 0 and got and "The runs before had stopped: DESK_STATE_KEY is too short." in got[0], got


def test_a_never_worked_run_that_saved_its_board_says_so_in_public(first):
    sh, fake, br, job = first
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 503, times=50)
    code, r, saved = _run(job)
    fake.fail.clear()
    assert code == 1 and saved and BOARD in br.files() and STATE not in br.files()
    line = r.public_line()
    assert "its status and its board are saved, no state" in line and "only its status" not in line, line


def test_a_refusal_with_no_state_on_the_branch_never_says_the_state_is_unchanged(first):
    sh, fake, br, job = first
    trade = fake.add_key(make_ec_key("words8-trade2-not-real", trade=True))
    code, r, saved = _run(job, {"COINBASE_CDP_KEY": trade.file_json()})
    line = r.public_line()
    assert saved and "the state is unchanged" not in line and "only its status is saved, no state" in line, line


# ---------------------------------------------------------------- the docs

def test_the_guide_says_how_often_not_working_comes_again():
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    for said in ("**once SHADOW has saved a run that works**, for a run that stops part way (its state is still "
                 "saved): 6 hours after the last push",
                 "in the first run after 12 am, 6 am, 12 pm and 6 pm MDT (11 pm, 5 am, 11 am and 5 pm MST)",
                 "the first run after the branch last changed",
                 "**Coinbase's answer was incomplete, or its market was empty:**",
                 "its `mode` isn't SHADOW (set to LIVE, say, which arms nothing)",
                 "The save check refuses any save beside the state before the last"):
        assert said in guide, said
    assert "The 6 hours start at" not in guide          # before: said of every refusal, though not how pushes repeat


def test_the_readme_and_part_2_say_live_is_meant_to_and_where_it_doesnt_yet():
    readme = (ROOT / "README.md").read_text()
    guide = (ROOT / "docs" / "coinbase-setup.md").read_text()
    status = (ROOT / "docs" / "coinbase-live-status.md").read_text()
    assert "is meant to be pushed urgently" in readme and "items 1, 2, 3 and 6 in" in readme
    assert "Anything that leaves a position without a stop, or a stop without a position, is pushed urgently" \
        not in readme
    assert "is meant never to be cancelled to keep one that can't (not yet when one of two desk stops has gapped" \
        in guide
    assert "a stop that can still fill is never cancelled to keep one that can't." not in guide
    assert "the push reassures you when it shouldn't" in status
