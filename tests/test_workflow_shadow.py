"""The workflow's SHADOW job (.github/workflows/paper.yml): after paper, unable to fail or touch it, skipped without
its secrets, holding the Coinbase key in one step only, pushing only coinbase-state, and never a LIVE step."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "paper.yml"
TEXT = WF.read_text()


def _yaml():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(TEXT)


def _job_text(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z_]+:\n|\Z)", TEXT, re.S | re.M)
    assert m, name
    return m.group(1)


def test_nothing_under_workflows_can_go_live_or_point_at_a_fake():
    for path in (ROOT / ".github" / "workflows").rglob("*"):
        if path.is_file():
            t = path.read_text()
            for bad in ("desk live", "--once", "DESK_LIVE_CONFIRM", "DESK_LIVE_COINBASE", "COINBASE_CDP_TRADE_KEY",
                        "DESK_COINBASE_TEST_URL", "--force-unproven", "--local"):
                assert bad not in t, f"{path.name} mentions {bad}"


def test_the_triggers_are_still_the_schedule_and_the_manual_run_only():
    on = _yaml().get(True) or _yaml().get("on")
    assert set(on) == {"schedule", "workflow_dispatch"}          # never pull_request: secrets never reach a fork


def test_the_shadow_job_runs_after_paper_and_cant_fail_the_run():
    job = _yaml()["jobs"]["shadow"]
    assert job["needs"] == "step" and job["if"] == "${{ !cancelled() }}"
    assert job["continue-on-error"] is True and job["timeout-minutes"] <= 8
    assert job["permissions"] == {"contents": "write"} and "environment" not in job
    assert job["env"] == {"SHADOW_ON": "${{ secrets.COINBASE_CDP_KEY != '' && secrets.DESK_STATE_KEY != '' }}"}
    for step in job["steps"]:
        assert "SHADOW_ON" in str(step.get("if", "")), step.get("name") or step.get("uses")
    skip = job["steps"][0]
    assert skip["if"] == "env.SHADOW_ON != 'true'" and skip["run"].startswith("echo ")


def test_the_key_and_the_password_reach_one_step_only():
    job = _yaml()["jobs"]["shadow"]
    holders = [s.get("name") for s in job["steps"] if "secrets." in str(s)]
    assert holders == ["SHADOW run (reads the real account, sends nothing)"]
    run = next(s for s in job["steps"] if s.get("id") == "shadow")
    assert set(run["env"]) == {"COINBASE_CDP_KEY", "DESK_STATE_KEY", "NTFY_TOPIC", "DESK_COINBASE_KILL"}
    assert run["run"].startswith("python -m desk shadow --coin BTC") and run["timeout-minutes"] <= 6
    assert TEXT.count("secrets.COINBASE_CDP_KEY") == 2 and TEXT.count("secrets.DESK_STATE_KEY") == 2   # SHADOW_ON too


def test_only_the_shadow_job_installs_packages_pinned_and_hashed():
    jobs = _yaml()["jobs"]
    assert "pip" not in str(jobs["step"])
    pip = [s for s in jobs["shadow"]["steps"] if "pip install" in str(s.get("run", ""))]
    assert len(pip) == 1 and "--require-hashes" in pip[0]["run"] and "--only-binary :all:" in pip[0]["run"]
    assert "requirements-coinbase.txt" in pip[0]["run"]


def test_the_shadow_job_pushes_coinbase_state_only_and_never_names_paper_ledger():
    job = _job_text("shadow")
    assert "paper-ledger" not in job
    pushes = [ln.strip() for ln in job.splitlines() if "git push" in ln]
    assert pushes and all("refs/heads/coinbase-state" in p and "--force-with-lease=coinbase-state:" in p
                          for p in pushes)
    assert "python3 -m desk.coinbase_state check" in job and "python3 -m desk.coinbase_state stage" in job


def test_the_paper_job_is_as_it_was_but_for_the_shadow_copy_and_the_board_handover():
    jobs = _yaml()["jobs"]
    step = jobs["step"]
    assert "env" not in step and "needs" not in step
    desk = next(s for s in step["steps"] if s.get("name") == "Desk step")
    assert set(desk["env"]) == {"JEV_API_KEY", "NTFY_TOPIC"} and desk["run"] == "python -m desk step"
    names = [s.get("name") or s.get("uses") for s in step["steps"]]
    assert names[-1] == "Hand BTC's paper board to the shadow job"
    hand = step["steps"][-1]
    assert hand["continue-on-error"] is True and hand["uses"] == "actions/upload-artifact@v4"
    old = subprocess.run(["git", "show", "5edfb33:.github/workflows/paper.yml"], cwd=ROOT, capture_output=True,
                         text=True)
    if old.returncode == 0:         # the steps before the Build are byte for byte origin/main's
        cut = "      - name: Build the boards"
        assert TEXT.split(cut)[0] == old.stdout.split(cut)[0]


def _build_script() -> str:
    lines = TEXT.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.strip() == "- name: Build the boards")
    j = next(k for k in range(i, len(lines)) if lines[k].strip() == "run: |")
    out = []
    for ln in lines[j + 1:]:
        if ln.strip() and not ln.startswith(" " * 10):
            break
        out.append(ln[10:])
    return "\n".join(out) + "\n"


def _git(*a, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=cwd, check=True,
                   capture_output=True)


@pytest.mark.parametrize("branch", [False, True])
def test_the_build_steps_shadow_copy_never_fails_and_copies_only_with_a_branch(tmp_path, branch):
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    ws = tmp_path / "ws"
    _git("init", "-q", str(ws), cwd=tmp_path)
    _git("remote", "add", "origin", f"file://{origin}", cwd=ws)
    (ws / "dashboard").mkdir()
    (ws / "dashboard" / "index.html").write_text("<html></html>")
    (ws / "ledger" / "BTC").mkdir(parents=True)
    (ws / "ledger" / "BTC" / "board.json").write_text("{}")
    if branch:
        s = tmp_path / "s"
        (s / "BTC").mkdir(parents=True)
        (s / "BTC" / "status.json").write_text('{"v": 1}')
        (s / "BTC" / "board-shadow.enc.json").write_text('{"v": 1}')
        _git("init", "-q", str(s), cwd=tmp_path)
        _git("add", "-A", cwd=s)
        _git("commit", "-qm", "s", cwd=s)
        _git("push", "-q", f"file://{origin}", "HEAD:refs/heads/coinbase-state", cwd=s)
    res = subprocess.run(["bash", "-e", "-c", _build_script()], cwd=ws, capture_output=True, text=True,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert res.returncode == 0, res.stderr
    boards = sorted(p.name for p in (ws / "_site" / "boards").iterdir())
    if branch:
        assert boards == ["BTC-SHADOW.enc.json", "BTC-SHADOW.status.json", "BTC.json"]
    else:
        assert boards == ["BTC.json"]
