"""The coinbase-state branch: what the workflow's SHADOW job saves, and the checks before it does. Standard library only,
no secrets: it never decrypts anything (desk/statebox.py does, inside the run).

    python3 -m desk.coinbase_state check <state-dir> <remote-ref>
    python3 -m desk.coinbase_state stage <state-dir> <work-dir> <remote-ref>

The branch holds one orphan commit, replaced at every save (encrypted blobs every 15 minutes would otherwise pile up):

    README.md                        what this branch is (fixed text)
    BTC/state-shadow.enc.json        SHADOW's state, encrypted (statebox): the broker's, the desk's, the virtual book
    BTC/state-shadow.prev.enc.json   the one before it
    BTC/board-shadow.enc.json        SHADOW's board, encrypted: the dashboard opens it with DESK_STATE_KEY
    BTC/status.json                  public, no amounts and no ids: mode, runs, ok, the problem, the decision
    BTC/kills.json                   the kill switches: yours to edit here (flat_kill, entry_kill, jev_kill)

check refuses (exit 1, an ::error:: naming the file) a file not on that list, an encrypted file that isn't an envelope,
a status or kills file that isn't what the desk writes (status keys and values are whitelisted, so a bug can't put an
amount in it), and a younger state: the same start time as the branch's, and more runs, or as many runs with the very
same state file. stage copies the allowed files into a fresh tree, keeps the branch's kills.json (your edits) with any
kill this run set added (kills.set.json), and moves the branch's last state to .prev.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .statebox import is_envelope

COIN = "BTC"
MODES = ("shadow", "live")
ENCRYPTED = {f"{kind}-{m}{suffix}" for m in MODES for kind, suffix in
             (("state", ".enc.json"), ("state", ".prev.enc.json"), ("board", ".enc.json"))}
ALLOWED = ENCRYPTED | {"status.json", "kills.json", "kills.set.json"}
KILL_KEYS = ("flat_kill", "entry_kill", "jev_kill")
STATUS_KEYS = {"v": int, "mode": str, "coin": str, "product": str, "updated": int, "started_ts": int, "runs": int,
               "failed_runs": int, "ok": bool, "problem": (str, type(None)), "key_view_only": (bool, type(None)),
               "decision": (str, type(None)), "would_send_last_24h": (int, type(None)), "alerts_open": int,
               "checks_passed": (int, type(None)), "checks_total": (int, type(None)), "opened": bool,
               "note": (str, type(None))}
PROBLEMS = {None, "auth", "permissions", "clock", "read", "state", "refused", "key"}
BRANCH_README = """# coinbase-state

Written by the paper desk workflow's `shadow` job (desk/coinbase_run.py). This repository is public, so the real
Coinbase account's state and board are encrypted with the `DESK_STATE_KEY` secret (desk/statebox.py); only
`BTC/status.json` (no amounts) is plain.

To stop new entries or close the position from here: edit `BTC/kills.json` (`"entry_kill": true` or
`"flat_kill": true`). The next run reads it. Each save replaces this branch's one commit.
"""


class StateCheckError(RuntimeError):
    pass


def _git_show(ref: str, path: str) -> bytes | None:
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _json(data: bytes, name: str):
    try:
        return json.loads(data)
    except ValueError:
        raise StateCheckError(f"{name} isn't JSON") from None


def check_status(d, name: str = "status.json") -> dict:
    if not isinstance(d, dict):
        raise StateCheckError(f"{name} isn't an object")
    for k, v in d.items():
        want = STATUS_KEYS.get(k)
        if want is None:
            raise StateCheckError(f"{name} has a key the desk doesn't write there: {k!r}")
        if isinstance(v, bool) and want is int:
            raise StateCheckError(f"{name}: {k} isn't a number")
        if not isinstance(v, want):
            raise StateCheckError(f"{name}: {k} has the wrong type")
    for k in ("v", "mode", "coin", "started_ts", "runs"):
        if k not in d:
            raise StateCheckError(f"{name} has no {k}")
    if d["v"] != 1 or d["mode"] not in ("SHADOW", "LIVE") or d["coin"] != COIN:
        raise StateCheckError(f"{name} isn't a v1 SHADOW or LIVE status for {COIN}")
    if d.get("problem") not in PROBLEMS:
        raise StateCheckError(f"{name}: problem {d.get('problem')!r} isn't one the desk writes")
    for k in ("note", "decision"):
        if isinstance(d.get(k), str) and (len(d[k]) > 200 or "$" in d[k] or any(ch.isdigit() for ch in d[k])):
            raise StateCheckError(f"{name}: {k} holds a number; the public status never says an amount")
    return d


def check_kills(d, name: str) -> dict:
    if not isinstance(d, dict) or set(d) - set(KILL_KEYS) or not all(isinstance(v, bool) for v in d.values()):
        raise StateCheckError(f"{name} isn't a kill-switch file: only {', '.join(KILL_KEYS)}, each true or false")
    return d


def _local(state_dir: Path) -> dict[str, bytes]:
    d = state_dir / COIN
    for p in state_dir.iterdir():
        if p.name != COIN:
            raise StateCheckError(f"{p.name} isn't something the SHADOW job saves (only {COIN}/)")
    out = {}
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        if p.name not in ALLOWED or not p.is_file():
            raise StateCheckError(f"{COIN}/{p.name} isn't a file the SHADOW job saves; not saving")
        out[p.name] = p.read_bytes()
    return out


def check(state_dir: str | Path, remote_ref: str) -> list[str]:
    """Raises StateCheckError; returns what it checked, for the log."""
    files = _local(Path(state_dir))
    said = []
    for name, data in files.items():
        if name in ENCRYPTED:
            if not is_envelope(_json(data, f"{COIN}/{name}")):
                raise StateCheckError(f"{COIN}/{name} isn't encrypted: never saving the real account in plain text")
        elif name in ("kills.json", "kills.set.json"):
            check_kills(_json(data, f"{COIN}/{name}"), f"{COIN}/{name}")
    if "status.json" not in files:
        raise StateCheckError(f"{COIN}/status.json is missing: nothing to save")
    new = check_status(_json(files["status.json"], f"{COIN}/status.json"))
    mode = new["mode"].lower()
    state = f"state-{mode}.enc.json"
    if state not in files:
        raise StateCheckError(f"{COIN}/{state} is missing next to a {new['mode']} status")
    old_raw = _git_show(remote_ref, f"{COIN}/status.json")
    if old_raw is None:
        if _git_show(remote_ref, f"{COIN}/{state}") is not None:
            raise StateCheckError(f"the branch has {COIN}/{state} but no status.json: not replacing it blind")
        said.append("no saved state on the branch: a first save")
        return said
    old = check_status(_json(old_raw, f"saved {COIN}/status.json"), f"saved {COIN}/status.json")
    if old["mode"] != new["mode"]:
        if old["mode"] == "SHADOW" and new["mode"] == "LIVE" and _git_show(remote_ref, f"{COIN}/{state}") is None:
            said.append("the first LIVE save: SHADOW's files stay on the branch beside it")
            return said
        raise StateCheckError(f"the branch's state is {old['mode']}, this run's {new['mode']}: not saving over it")
    if new["started_ts"] != old["started_ts"]:
        raise StateCheckError(f"{COIN}/{state} would replace the saved {old['mode']} account (started "
                              f"{old['started_ts']}, {old['runs']} runs) with one started {new['started_ts']}: "
                              f"not saving (a reset needs a deliberate manual edit of the branch)")
    if new["runs"] < old["runs"]:
        raise StateCheckError(f"{COIN}/{state} has {new['runs']} runs, the saved one {old['runs']}: a younger state, "
                              f"not saving")
    if new["runs"] == old["runs"] and files[state] != (_git_show(remote_ref, f"{COIN}/{state}") or b""):
        raise StateCheckError(f"{COIN}/{state} changed but its run count didn't: not saving")
    said.append(f"{new['mode']} state: {old['runs']} -> {new['runs']} runs, same start")
    return said


def stage(state_dir: str | Path, work: str | Path, remote_ref: str) -> list[str]:
    files = _local(Path(state_dir))
    status = check_status(_json(files["status.json"], "status.json"))
    mode = status["mode"].lower()
    out = Path(work) / COIN
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (Path(work) / "README.md").write_text(BRANCH_README)
    for n in sorted(ENCRYPTED | {"status.json"}):
        if n in files and n != f"state-{mode}.prev.enc.json":     # the other mode's files stay as loaded
            (out / n).write_bytes(files[n])
    old_state = _git_show(remote_ref, f"{COIN}/state-{mode}.enc.json")
    old_prev = _git_show(remote_ref, f"{COIN}/state-{mode}.prev.enc.json")
    if old_state is not None and old_state != files.get(f"state-{mode}.enc.json"):
        (out / f"state-{mode}.prev.enc.json").write_bytes(old_state)
    elif old_prev is not None:
        (out / f"state-{mode}.prev.enc.json").write_bytes(old_prev)
    kills = {k: False for k in KILL_KEYS}
    raw = _git_show(remote_ref, f"{COIN}/kills.json")
    if raw is not None:
        try:
            kills.update(check_kills(json.loads(raw), "the branch's kills.json"))
        except (ValueError, StateCheckError):
            kills["entry_kill"] = True      # unreadable: the safe side, and the run says so on the board
    elif "kills.json" in files:
        kills.update(check_kills(_json(files["kills.json"], "kills.json"), "kills.json"))
    if "kills.set.json" in files:
        for k, v in check_kills(_json(files["kills.set.json"], "kills.set.json"), "kills.set.json").items():
            kills[k] = kills[k] or v
    (out / "kills.json").write_text(json.dumps(kills, indent=1) + "\n")
    return sorted(p.name for p in out.iterdir())


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "check":
            for line in check(argv[1], argv[2]):
                print(f"coinbase-state check: {line}")
            return 0
        if len(argv) == 4 and argv[0] == "stage":
            print("coinbase-state stage: " + ", ".join(stage(argv[1], argv[2], argv[3])))
            return 0
    except StateCheckError as exc:
        print(f"::error::coinbase-state: {exc}")
        return 1
    print("usage: python3 -m desk.coinbase_state check <state-dir> <ref> | stage <state-dir> <work-dir> <ref>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
