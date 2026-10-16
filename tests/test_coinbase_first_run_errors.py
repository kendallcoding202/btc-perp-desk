"""Round 7, SHADOW onboarding: what a run says when it stops, and when a reason is news, through the workflow's load,
run, check, stage and commit (tests/test_coinbase_kills_fail_closed.py's Branch and Job). Each fails on the code before
it (4c0d11c):

- a first run that crashed after the key passed (an answer the code didn't expect) said in public that Coinbase didn't
  answer; one that crashed before the key passed, that a secret wasn't set right; one whose answer couldn't be read
  (Coinbase answered, in a shape the desk can't read), that Coinbase didn't answer. None kept its log, and the push
  said only "<error> while it ran". Now the public note says what happened, the push names where (the step and the
  desk's own function beside the error's type, no amount), and the run's board (encrypted: the SHADOW page) keeps its
  log, never a state;
- after SHADOW had worked, a new reason (a 401, then a key that can trade) waited up to 6 hours behind the first one's
  push; now a new reason is pushed at once, then every 6 hours while it lasts;
- the key file pasted into both secrets was said, in public, to be the two secrets swapped.

Everything runs against the local fake Coinbase (tests/fake_coinbase.py) on 127.0.0.1; every key is a fresh fake."""

from __future__ import annotations

import pytest

from desk import coinbase_run as cr
from desk import statebox
from desk.brokers import coinbase as cbx
from desk.brokers.coinbase_rest import CoinbaseDataError, CoinbaseREST
from fake_coinbase import V3, FakeCoinbase, make_ec_key
from test_coinbase_kills_fail_closed import Branch, Job, _next
from test_coinbase_shadow import KEY, Shadow

STATE = "BTC/state-shadow.enc.json"
BOARD = "BTC/board-shadow.enc.json"
GH = {"GITHUB_ACTIONS": "true"}
ERROR = ("SHADOW hit an error of its own, not Coinbase's (the reason is on your phone and the SHADOW page; the next run "
         "tries again)")
UNREADABLE = "Coinbase's answer couldn't be read (the reason is on your phone and the SHADOW page; the next run tries again)"
NO_ANSWER = "Coinbase didn't answer part of the run (the next run tries again)"


@pytest.fixture
def first(tmp_path, monkeypatch):
    with FakeCoinbase(cbi=1_000.0) as fake:
        fake.now_s += 8 * 60
        sh = Shadow(fake, tmp_path, make_ec_key("errors7-view-not-real"))
        br = Branch(tmp_path, monkeypatch)
        yield sh, fake, br, Job(sh, fake, br, None)


def _run(job, over=None, **kw):
    job.sh.pushes.clear()
    code, r, changed, saved = job.run(over=over, **kw)
    return code, r, saved


def _not_working(sh):
    return [b for t, b, *_ in sh.pushes if t == "BTC SHADOW: not working"]


def _board(br, password=KEY):
    return statebox.open_json(br.files()[BOARD].decode(), password, statebox.board_aad("shadow"))


# ---------------------------------------------------------------- a first run that stops: said as it happened

def test_a_first_run_that_crashes_after_the_key_says_so_and_keeps_its_log(first):
    """Coinbase answers the sweeps with a list the code doesn't expect: the desk's own code raises (AttributeError,
    in read_extras). Before: "Coinbase didn't answer part of the run", no log anywhere."""
    sh, fake, br, job = first
    fake.fail_next("GET", f"{V3}/cfm/sweeps", 200, times=1, body=b'{"sweeps": [5]}')
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and STATE not in br.files(), (r.problem, sorted(br.files()))
    assert st["ok"] is False and st["runs"] == 0 and st["problem"] == "error" and st["note"] == ERROR, st
    got = _not_working(sh)
    assert got and "an error in the desk's own code (AttributeError) while reading the fee tier, sweeps and fills " \
                   "(in read_extras)" in got[0], got
    assert "DESK_STATE_KEY can still change" in got[0] and "$" not in got[0]
    board = _board(br)                                       # the encrypted log, for the SHADOW page
    log = "\n".join(board["shadow"]["log"])
    assert board["updated"] is None and "'int' object has no attribute 'get'" in log and "AttributeError" in log
    assert board["status"]["ok"] is False and board["status"]["problem"] == "error"
    assert "stopped (error) part way: SHADOW hit an error of its own" in r.public_line()
    _next(fake)                                              # it was the answer: the next run works, a first save
    code, r, saved = _run(job, GH)
    assert code == 0 and saved and job.status()["ok"] is True and STATE in br.files()


@pytest.mark.parametrize("where", ["build_broker", "key_permissions"])
def test_a_first_run_that_crashes_is_never_said_as_coinbase_or_a_secret(first, monkeypatch, where):
    """A crash after the key passed (build_broker) and before it (key_permissions): before, "Coinbase didn't answer"
    and "a secret isn't set right"."""
    sh, fake, br, job = first

    def boom(*a, **kw):
        raise KeyError("a field the code expected")
    monkeypatch.setattr(cbx.CoinbaseExchange if where == "key_permissions" else cr.Run, where, boom)
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and STATE not in br.files()
    assert st["problem"] == "error" and st["note"] == ERROR, st
    step = {"build_broker": "reading the price, the positions, the orders and the deposits",
            "key_permissions": "reading the key's permissions"}[where]
    got = _not_working(sh)
    assert got and f"an error in the desk's own code (KeyError) while {step}" in got[0], got
    assert "a field the code expected" in "\n".join(_board(br)["shadow"]["log"])
    line = r.public_line()
    assert "SHADOW hit an error of its own" in line and "secret" not in line and "didn't answer" not in line, line


def test_a_first_run_whose_balance_answer_cant_be_read_says_so(first):
    """Coinbase answers the balance, in a shape the desk can't read (CoinbaseDataError): before, "Coinbase didn't
    answer". A balance that doesn't answer at all still says that (the control)."""
    sh, fake, br, job = first
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 200, times=50, body=b'{"balance_summary": 5}')
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and STATE not in br.files() and st["problem"] == "read" and st["note"] == UNREADABLE, st
    got = _not_working(sh)
    assert got and "the futures balance couldn't be read: Coinbase answered, but in a shape the desk can't read " \
                   "(CoinbaseDataError)" in got[0], got
    assert "balance_summary" in "\n".join(_board(br)["shadow"]["log"])
    fake.fail.clear()
    _next(fake)
    fake.fail_next("GET", f"{V3}/cfm/balance_summary", 503, times=50)
    code, r, saved = _run(job, GH)
    assert code == 1 and saved and job.status()["note"] == NO_ANSWER


def test_an_odd_clock_answer_before_the_key_passed_is_said_as_unreadable(first, monkeypatch):
    """Coinbase's time answered in a shape the desk can't read, before the key passed (CoinbaseREST.server_skew's own
    CoinbaseDataError). Before: "Coinbase didn't answer", and the clock check "not checked: Coinbase didn't answer"."""
    sh, fake, br, job = first

    def odd(self):
        raise CoinbaseDataError("Coinbase's time answer has no epochMillis or epochSeconds")
    monkeypatch.setattr(CoinbaseREST, "server_skew", odd)
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and st["note"] == UNREADABLE and "secret" not in r.public_line(), (st, r.public_line())
    assert BOARD in br.files() and STATE not in br.files()
    rows = {c["key"]: c for c in _board(br)["shadow"]["checks"]}
    assert rows["answered"]["ok"] is True and "didn't answer" not in rows["clock"]["note"], rows
    assert "epochMillis" in "\n".join(_board(br)["shadow"]["log"])


def test_once_shadow_has_worked_an_error_is_said_as_one_publicly_too(first, monkeypatch):
    """After SHADOW has worked, a crash is a failed run of its state, as before, and the public note says what it was
    (before: no note, the problem "read")."""
    sh, fake, br, job = first
    assert _run(job, GH)[2]

    def boom(*a, **kw):
        raise KeyError("a field the code expected")
    monkeypatch.setattr(cr, "read_extras", boom)
    _next(fake)
    code, r, saved = _run(job, GH)
    st = job.status()
    assert code == 1 and saved and st["runs"] == 2 and st["failed_runs"] == 1
    assert st["problem"] == "error" and st["note"] == ERROR, st
    got = _not_working(sh)
    assert got and "an error in the desk's own code (KeyError) while reading the fee tier, sweeps and fills" in got[0]


# ---------------------------------------------------------------- after SHADOW has worked: a new reason is news

def test_after_shadow_has_worked_a_new_reason_is_pushed_at_once(first):
    """A 401 (pushed), then a key that can trade (another reason: pushed at once; before, nothing for 6 hours), then
    the same again (not pushed), then fixed (working again)."""
    sh, fake, br, job = first
    assert _run(job, GH)[2]
    gone = make_ec_key("errors7-deleted-not-real")             # never added: 401
    trade = fake.add_key(make_ec_key("errors7-trade-not-real", trade=True))
    seen = []
    for over in ({"COINBASE_CDP_KEY": gone.file_json()}, {"COINBASE_CDP_KEY": trade.file_json()},
                 {"COINBASE_CDP_KEY": trade.file_json()}, {}):
        _next(fake)
        code, r, saved = _run(job, {**GH, **over})
        seen.append(([t for t, *_ in sh.pushes], _not_working(sh), job.status()["note"]))
    assert seen[0][0] == ["BTC SHADOW: not working"] and "401" in seen[0][1][0], seen[0]
    assert seen[1][0] == ["BTC SHADOW: not working"] and "can trade" in seen[1][1][0], seen[1]   # before: nothing
    assert seen[2][0] == [], seen[2]                                                            # the same: once
    assert seen[3][0] == ["BTC SHADOW: working again"], seen[3]


# ---------------------------------------------------------------- the key file in both secrets

def test_the_key_file_in_both_secrets_is_said_plainly_and_the_swap_still_is(first):
    sh, fake, br, job = first
    key_file = sh.env["COINBASE_CDP_KEY"]
    code, r, saved = _run(job, {**GH, "DESK_STATE_KEY": key_file})
    st = job.status()
    assert code == 2 and saved and fake.requests == [] and STATE not in br.files()
    assert st["note"] == "DESK_STATE_KEY looks like the key file (the key file is in both secrets)", st["note"]
    assert "swapped" not in sh.pushes[0][1] and "the key file is in both secrets" in sh.pushes[0][1]
    _next(fake)
    code, r, saved = _run(job, {**GH, "DESK_STATE_KEY": key_file, "COINBASE_CDP_KEY": KEY})     # swapped
    assert job.status()["note"] == "DESK_STATE_KEY looks like the key file (the two secrets are swapped)"
    _next(fake)
    code, r, saved = _run(job, GH)
    assert code == 0 and saved


def test_the_guide_says_each_reason_as_the_status_does():
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "docs" / "coinbase-setup.md").read_text()
    for said in ("**Coinbase didn't answer** (at the clock check, or part of the run)",
                 "**Coinbase's answer couldn't be read:**", "**Coinbase refused a read:**",
                 "**SHADOW hit an error of its own, not Coinbase's:**",
                 "**DESK_STATE_KEY looks like the key file (the key file is in both secrets):**",
                 "and at once whenever the reason changes", "the balance hasn't been read yet"):
        assert said in guide, said
