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
    BTC/kills.json                   the kill switches: yours to edit here (flat_kill, entry_kill, jev_kill), and
                                     reset_halt_at: any new text (a date, say) clears a drawdown halt once

check refuses (exit 1, an ::error:: naming the file) a file in the state dir that is neither one of the desk's nor on
the branch (the run wrote something it never saves), an encrypted file that isn't an envelope, a status or
kills.set.json that isn't what the desk writes (status keys and values are whitelisted, so a bug can't put an amount in
it), and a younger state: the same start time as the branch's, and more runs, or as many runs with the very same state
file. A run refused over its secrets (problem "key") saves its status alone: the state file as loaded, or none yet on a
first save. stage copies the desk's files into a fresh tree, keeps every file of yours on the branch as it is (any file
that isn't the desk's: notes, say), keeps the branch's kills.json (your edits) with any kill this run set added
(kills.set.json), and moves the branch's last state to .prev.

kills.json is yours, so a typo in it never stops a save, and never turns a switch off: kills_read is how the runs,
check and stage all read it. One it can't read (not JSON, a key it doesn't know, a switch that isn't true or false, a
key written twice, a symlink) fails closed: entry_kill on, and every switch it names on unless its value says off
(false, null, 0, "false", "no", "off"; a word starting with f or n). The save keeps such a file exactly as you wrote it,
so every run reads it the same way and says so until you fix it. LIVE counts on this branch only once it has armed: its
status says "armed": true. A LIVE run that never armed saves nothing (desk/coinbase_run.py), check refuses a LIVE status
that isn't armed, and a LIVE status without it (a LIVE attempt that never armed, before this rule) is no LIVE start:
SHADOW saves over it and stage drops its files.

check runs against the branch as git has it (GitBranch) or, inside a run, as the run loaded it (Loaded: the load step
extracts the branch's BTC/ into the state dir), so a run can tell before the save step that its save would be refused.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from .statebox import is_envelope

COIN = "BTC"
MODES = ("shadow", "live")
ENCRYPTED = {f"{kind}-{m}{suffix}" for m in MODES for kind, suffix in
             (("state", ".enc.json"), ("state", ".prev.enc.json"), ("board", ".enc.json"))}
ALLOWED = ENCRYPTED | {"status.json", "kills.json", "kills.set.json"}
KILL_KEYS = ("flat_kill", "entry_kill", "jev_kill")
RESET_KEYS = ("reset_halt_at",)         # one-shot: a new value clears the drawdown halt once (desk/coinbase_run.py)
STATUS_KEYS = {"v": int, "mode": str, "coin": str, "product": str, "updated": int, "started_ts": int, "runs": int,
               "failed_runs": int, "ok": bool, "problem": (str, type(None)), "key_view_only": (bool, type(None)),
               "decision": (str, type(None)), "would_send_last_24h": (int, type(None)), "alerts_open": int,
               "checks_passed": (int, type(None)), "checks_total": (int, type(None)), "opened": bool,
               "note": (str, type(None)), "armed": (bool, type(None))}
PROBLEMS = {None, "auth", "permissions", "clock", "read", "state", "refused", "key"}
LINK = "120000"                         # git's mode for a symlink (its blob is the link's target)
KILLS = f"{COIN}/kills.json"
BRANCH_README = """# coinbase-state

Written by the paper desk workflow's `shadow` job (desk/coinbase_run.py). This repository is public, so the real
Coinbase account's state and board are encrypted with the `DESK_STATE_KEY` secret (desk/statebox.py); only
`BTC/status.json` (no amounts) is plain.

To stop new entries or close the position from here: edit `BTC/kills.json` (`"entry_kill": true` or
`"flat_kill": true`). The next run reads it. Each save replaces this branch's one commit, and keeps any other file
you add here as it is (the desk never reads it).
"""


class StateCheckError(RuntimeError):
    pass


def _git_show(ref: str, path: str) -> bytes | None:
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# ---------------------------------------------------------------- the branch: as git has it, or as the run loaded it

class GitBranch:
    """The coinbase-state ref in this repository: its files (path -> git mode) and their bytes (a symlink's: its
    target, as git stores it). A ref that doesn't exist has no files."""

    def __init__(self, ref: str):
        self.ref = ref
        self._tree: dict[str, str] | None = None

    def tree(self) -> dict[str, str]:
        if self._tree is None:
            self._tree = {}
            try:
                out = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", self.ref], capture_output=True,
                                     timeout=60, check=False)
            except (OSError, subprocess.SubprocessError):
                out = None
            if out is not None and out.returncode == 0:
                for rec in out.stdout.split(b"\0"):
                    meta, tab, path = rec.partition(b"\t")
                    if tab:
                        self._tree[os.fsdecode(path)] = meta.split(b" ")[0].decode()
        return self._tree

    def show(self, path: str) -> bytes | None:
        mode = self.tree().get(path)
        if mode is None or mode == "160000":        # none, or a submodule (no blob)
            return None
        return _git_show(self.ref, path)


def _entries(d: Path, prefix: str) -> dict[str, tuple[str, bytes]]:
    """Every file under `d`, symlinks not followed: {path: (git-like mode, bytes)} (a symlink's bytes: its target)."""
    out: dict[str, tuple[str, bytes]] = {}
    if d.is_symlink() or not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        rel = f"{prefix}/{p.name}"
        if p.is_symlink():
            out[rel] = (LINK, os.fsencode(os.readlink(p)))
        elif p.is_dir():
            out.update(_entries(p, rel))
        elif p.is_file():
            out[rel] = ("100755" if os.access(p, os.X_OK) else "100644", p.read_bytes())
        else:
            out[rel] = ("special", b"")
    return out


class Loaded:
    """The branch as a run loaded it: the load step extracts the branch's BTC/ into the state dir, and the run copies
    it before it writes anything. Answers as GitBranch does, so the run checks its own save before the save step."""

    def __init__(self, state_dir: str | Path | None):
        self.files = {} if state_dir is None else _entries(Path(state_dir) / COIN, COIN)

    def tree(self) -> dict[str, str]:
        return {p: m for p, (m, _) in self.files.items()}

    def show(self, path: str) -> bytes | None:
        got = self.files.get(path)
        return None if got is None else got[1]


def _desks(path: str) -> bool:
    """One of the desk's own paths on the branch (kills.json, yours, apart)."""
    return path == "README.md" or (path.startswith(COIN + "/") and path[len(COIN) + 1:] in ALLOWED - {"kills.json"})


def yours(tree: dict[str, str]) -> list[str]:
    """The branch's files that aren't the desk's: kept as they are at every save, never read (kill-like ones apart:
    kill_like). BTC/kills.json is in it only when it isn't a plain file (a symlink, a folder)."""
    return sorted(p for p, m in tree.items()
                  if not _desks(p) and not (p == KILLS and m not in (LINK, "160000")))


def kill_like(path: str) -> bool:
    """A file of yours whose name says kill but isn't BTC/kills.json (BTC/Kills.json, say): it looks like a kill
    switch, so the runs read it as one they can't read (entry_kill on), and say so."""
    return path.startswith(COIN + "/") and "kill" in path[len(COIN) + 1:].lower() and path != KILLS and \
        not path.startswith(KILLS + "/")


def branch_kills(branch) -> tuple[bytes | None, bool]:
    """BTC/kills.json on `branch`: (its bytes, whether it is a plain file). A symlink: its target, not plain; a
    folder: b"", not plain; none: (None, True)."""
    tree = branch.tree()
    mode = tree.get(KILLS)
    if mode is None:
        if any(p.startswith(KILLS + "/") for p in tree):
            return b"", False
        return None, True
    return branch.show(KILLS) or b"", mode not in (LINK, "160000", "special")


# ---------------------------------------------------------------- the files' checks

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
    ok = isinstance(d, dict) and not set(d) - set(KILL_KEYS) - set(RESET_KEYS) and \
        all(isinstance(v, bool) for k, v in d.items() if k in KILL_KEYS) and \
        all(v is None or (isinstance(v, str) and len(v) <= 40) for k, v in d.items() if k in RESET_KEYS)
    if not ok:
        raise StateCheckError(f"{name} isn't a kill-switch file: only {', '.join(KILL_KEYS)}, each true or false, "
                              f"and {', '.join(RESET_KEYS)} (a short text)")
    return d


# ---------------------------------------------------------------- kills.json, read failing closed

OFF_WORDS = {"", "false", "f", "no", "n", "off", "null", "none", "nil"}
TRUE_WORDS = {"true", "t", "yes", "y", "on", "1"}
# a switch's name in text the desk can't parse: flat_kill, "Flat Kill", entry-kill..., then (maybe) : or =, then a value
_NAMED = re.compile(r"(?<![a-z])(flat|entry|jev)[\s_\-]*kills?[\"']?\s*([:=]?)\s*(\"[^\"\n]*\"?|'[^'\n]*'?|[\w.+\-]*)",
                    re.IGNORECASE)


class KillsRead(NamedTuple):
    kills: dict             # the switches (and reset_halt_at, from a file the desk can read)
    why: str | None         # None: read as written; else why the desk can't read it (then entry_kill is on)
    named: tuple            # the switches the file names


def says_off(v) -> bool:
    """A switch's value that says off: false, null, 0, or a word for off ("false", "no", "off", "", and a word
    starting with f or n: a typo of false, no or null). Anything else says on (failing closed)."""
    if v is None or v is False:
        return True
    if v is True:
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        t = v.strip().strip("\"'").strip().lower()
        if t in OFF_WORDS or t[:1] in ("f", "n"):
            return True
        try:
            return float(t) == 0
        except ValueError:
            return False
    return False                # a list or an object: not off


def _switch(key) -> str | None:
    m = re.fullmatch(r"(flat|entry|jev)kills?", re.sub(r"[^a-z]", "", str(key).lower()))
    return f"{m.group(1)}_kill" if m else None


def _text(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        enc = json.detect_encoding(raw)
    except Exception:           # noqa: BLE001 (any odd bytes: read them as UTF-8, replaced)
        enc = "utf-8"
    return raw.decode(enc, errors="replace")


def kills_read(raw: bytes | str | None, plain: bool = True) -> KillsRead:
    """BTC/kills.json as the SHADOW and LIVE runs, check and stage all read it. No file: every switch off. A file the
    desk can read (an object of the three switches, each true or false, and reset_halt_at): as written. Any other
    (`plain` False: a symlink or a folder) fails closed: entry_kill on, each switch it names on unless its value says
    off (says_off), wherever it's named (a key, or text that isn't JSON: "flat_kill": true, with a trailing comma, say);
    reset_halt_at not acted on."""
    kills: dict = {k: False for k in KILL_KEYS}
    if raw is None:
        return KillsRead(kills, None, ())
    top: list = []
    if not plain:
        why = "it isn't a plain file (a symlink or a folder)"
    else:
        calls: list = []

        def hook(pairs):
            calls.append(pairs)
            return dict(pairs)

        try:
            d = json.loads(raw, object_pairs_hook=hook)
        except (ValueError, RecursionError):
            why = "it isn't JSON"
        else:
            top = calls[-1] if isinstance(d, dict) and calls else []
            keys = [k for k, _ in top]
            if len(keys) != len(set(keys)):
                why = "a key is in it twice"
            else:
                try:
                    check_kills(d, "kills.json")
                    kills.update(d)
                    return KillsRead(kills, None, tuple(k for k in KILL_KEYS if k in d))
                except StateCheckError:
                    why = (f"it isn't a kill-switch file: only {', '.join(KILL_KEYS)}, each true or false, and "
                           f"{', '.join(RESET_KEYS)} (a short text)")
    named: dict = {}
    for k, v in top:
        sw = _switch(k)
        if sw:
            named[sw] = named.get(sw, False) or not says_off(v)
    for m in _NAMED.finditer(_text(raw)):
        sw, sep, val = f"{m.group(1).lower()}_kill", m.group(2), m.group(3)
        if sep:
            named[sw] = named.get(sw, False) or not says_off(val)
        elif val.strip("\"'").lower() in TRUE_WORDS:        # no : or = (prose, say): only a plain yes counts
            named[sw] = True
    for sw, on in named.items():
        kills[sw] = kills[sw] or on
    kills["entry_kill"] = True
    return KillsRead(kills, why, tuple(k for k in KILL_KEYS if k in named))


def read_kills(raw: bytes | str | None, plain: bool = True) -> tuple[dict, str | None]:
    """(the switches, None), or, for a file the desk can't read, (entry_kill on and each switch it names on unless
    it says off, why): kills_read."""
    got = kills_read(raw, plain)
    return got.kills, got.why


# ---------------------------------------------------------------- the state dir, against the branch

def _never_armed(status: dict | None) -> bool:
    """A LIVE status that isn't armed: the files of a LIVE attempt that never armed, which are no LIVE start."""
    return bool(status) and status.get("mode") == "LIVE" and status.get("armed") is not True


LIVE_FILES = tuple(f"{kind}-live{suffix}" for kind, suffix in
                   (("state", ".enc.json"), ("state", ".prev.enc.json"), ("board", ".enc.json")))


def _local(state_dir: Path, branch) -> dict[str, bytes]:
    """The desk's files in the state dir. Anything else in it must be the branch's (yours, as loaded: stage keeps the
    branch's own copy); a file that is neither is something the run wrote and the desk never saves: refused."""
    for p in state_dir.iterdir():
        if p.name != COIN:
            raise StateCheckError(f"{p.name} isn't something the SHADOW job saves (only {COIN}/)")
    tree = branch.tree()
    out = {}
    for rel, (mode, data) in _entries(state_dir / COIN, COIN).items():
        name = rel[len(COIN) + 1:]
        if name in ALLOWED and mode not in (LINK, "special"):
            out[name] = data
        elif rel not in tree:
            raise StateCheckError(f"{rel} isn't a file the SHADOW job saves, and it isn't on the branch: not saving")
    return out


def _kills_here(files: dict, branch) -> tuple[bytes | None, bool]:
    raw, plain = branch_kills(branch)
    if raw is None and plain:
        return files.get("kills.json"), True        # none on the branch yet: the one loaded (a first save)
    return raw, plain


def check(state_dir: str | Path, remote_ref: str | None = None, branch=None) -> list[str]:
    """Raises StateCheckError; returns what it checked, for the log."""
    b = branch if branch is not None else GitBranch(remote_ref)
    files = _local(Path(state_dir), b)
    said = []
    for name, data in files.items():
        if name in ENCRYPTED:
            if not is_envelope(_json(data, f"{COIN}/{name}")):
                raise StateCheckError(f"{COIN}/{name} isn't encrypted: never saving the real account in plain text")
        elif name == "kills.set.json":
            check_kills(_json(data, f"{COIN}/{name}"), f"{COIN}/{name}")
    # kills.json is yours: stage saves the branch's (or, with none there yet, this one), as kills_read reads it
    got = kills_read(*_kills_here(files, b))
    if got.why:
        on = ", ".join(k for k in KILL_KEYS if got.kills[k])
        said.append(f"{KILLS} can't be read ({got.why}): kept as you wrote it; every run reads it failing closed "
                    f"({on} on) until you fix it")
    mine = yours(b.tree())
    if mine:
        said.append(f"yours, kept as they are: {', '.join(mine[:10])}" + (" and more" if len(mine) > 10 else "")
                    + ("; read as a kill switch the desk can't read (entry_kill on): "
                       + ", ".join(p for p in mine if kill_like(p)) if any(kill_like(p) for p in mine) else ""))
    if "status.json" not in files:
        raise StateCheckError(f"{COIN}/status.json is missing: nothing to save")
    new = check_status(_json(files["status.json"], f"{COIN}/status.json"))
    mode = new["mode"].lower()
    state = f"state-{mode}.enc.json"
    status_only = new.get("problem") == "key" and new.get("ok") is False
    if new["mode"] == "LIVE" and new.get("armed") is not True:
        raise StateCheckError(f"{COIN}/status.json is a LIVE status that isn't armed: a LIVE run that never armed "
                              f"saves nothing (it isn't a LIVE start)")
    if state not in files:
        if status_only and new["runs"] == 0 and b.show(f"{COIN}/{state}") is None:
            said.append(f"a status-only save: {new['mode']} was refused over its secrets before it had a state")
            return said
        raise StateCheckError(f"{COIN}/{state} is missing next to a {new['mode']} status")
    old_raw = b.show(f"{COIN}/status.json")
    if old_raw is None:
        if b.show(f"{COIN}/{state}") is not None:
            raise StateCheckError(f"the branch has {COIN}/{state} but no status.json: not replacing it blind")
        said.append("no saved state on the branch: a first save")
        return said
    old = check_status(_json(old_raw, f"saved {COIN}/status.json"), f"saved {COIN}/status.json")
    if _never_armed(old):
        # the files of a LIVE attempt that never armed (saved before LIVE saved only once armed): no LIVE start
        if new["mode"] == "LIVE":
            said.append("the first LIVE save: the branch's LIVE files were a LIVE attempt's that never armed, "
                        "replaced")
            return said
        # SHADOW's own counts went with its status: this run must have loaded the branch (its LIVE file, as is)
        left = b.show(f"{COIN}/state-live.enc.json")
        loaded = files.get("state-live.enc.json") == left if left is not None else \
            b.show(f"{COIN}/{state}") is None
        if not loaded:
            raise StateCheckError("the branch holds a LIVE attempt that never armed, and this SHADOW run didn't "
                                  "load it: not saving a SHADOW state that may not be the branch's")
        said.append("the branch's LIVE files were a LIVE attempt's that never armed (no LIVE start): SHADOW's "
                    "state saved, they're dropped")
        return said
    if old["mode"] != new["mode"]:
        if old["mode"] == "SHADOW" and new["mode"] == "LIVE" and b.show(f"{COIN}/{state}") is None:
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
    if new["runs"] == old["runs"] and files[state] != (b.show(f"{COIN}/{state}") or b""):
        raise StateCheckError(f"{COIN}/{state} changed but its run count didn't: not saving")
    if status_only and new["runs"] == old["runs"]:
        said.append(f"a status-only save: {new['mode']} was refused over its secrets; its state is as loaded")
        return said
    said.append(f"{new['mode']} state: {old['runs']} -> {new['runs']} runs, same start")
    return said


def _safe(path: str) -> bool:
    parts = path.split("/")
    return all(p not in ("", ".", "..", ".git") for p in parts) and "\\" not in path


def _put(path: Path, mode: str, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == LINK:
        os.symlink(os.fsdecode(data), path)
    else:
        path.write_bytes(data)
        if mode == "100755":
            path.chmod(0o755)


def stage(state_dir: str | Path, work: str | Path, remote_ref: str | None = None, branch=None) -> list[str]:
    b = branch if branch is not None else GitBranch(remote_ref)
    files = _local(Path(state_dir), b)
    status = check_status(_json(files["status.json"], "status.json"))
    mode = status["mode"].lower()
    for top in ("README.md", COIN):
        p = Path(work) / top
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.exists():
            shutil.rmtree(p)
    out = Path(work) / COIN
    out.mkdir(parents=True)
    (Path(work) / "README.md").write_text(BRANCH_README)
    # yours: every file on the branch that isn't the desk's, as it is
    tree = b.tree()
    for path in yours(tree):
        data = b.show(path)
        if data is not None and _safe(path) and tree[path] != "160000":
            _put(Path(work) / path, tree[path], data)
    raw_status = b.show(f"{COIN}/status.json")
    try:
        leftover = _never_armed(json.loads(raw_status)) if raw_status is not None else False
    except ValueError:
        leftover = False
    for n in sorted(ENCRYPTED | {"status.json"}):
        if n in files and n != f"state-{mode}.prev.enc.json":     # the other mode's files stay as loaded
            if leftover and mode == "shadow" and n in LIVE_FILES:
                continue        # a LIVE attempt that never armed: no LIVE start, its files go
            (out / n).write_bytes(files[n])
    old_state = b.show(f"{COIN}/state-{mode}.enc.json")
    old_prev = b.show(f"{COIN}/state-{mode}.prev.enc.json")
    if leftover and mode == "live":
        old_state = old_prev = None                                 # nothing of it becomes LIVE's .prev
    if old_state is not None and old_state != files.get(f"state-{mode}.enc.json"):
        (out / f"state-{mode}.prev.enc.json").write_bytes(old_state)
    elif old_prev is not None:
        (out / f"state-{mode}.prev.enc.json").write_bytes(old_prev)
    raw, plain = _kills_here(files, b)
    got = kills_read(raw, plain)
    added = {}
    if "kills.set.json" in files:
        added = {k: v for k, v in check_kills(_json(files["kills.set.json"], "kills.set.json"),
                                              "kills.set.json").items() if k in KILL_KEYS and v}
    if got.why is None or any(not got.kills[k] for k in added):
        # read as written (or a kill this run set that the file can't carry): written valid, every switch on kept on
        kills = {**got.kills, **{k: True for k in added}}
        p = out / "kills.json"
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.exists():
            shutil.rmtree(p)
        p.write_text(json.dumps(kills, indent=1) + "\n")
    elif not ((out / "kills.json").is_symlink() or (out / "kills.json").exists()):
        (out / "kills.json").write_bytes(raw or b"")    # one the desk can't read: kept exactly as you wrote it
    # (a symlink or a folder in its place was kept above, as it is: every run reads it as one it can't read)
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_symlink() or p.is_file())


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
