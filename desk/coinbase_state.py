"""The coinbase-state branch: what the workflow's SHADOW job saves, and the checks before it does. Standard library only,
no secrets: it never decrypts anything (desk/statebox.py does, inside the run).

    python3 -m desk.coinbase_state load <state-dir> <remote-ref>
    python3 -m desk.coinbase_state check <state-dir> <remote-ref> [<loaded-commit>]
    python3 -m desk.coinbase_state stage <state-dir> <work-dir> <remote-ref> [<loaded-commit>]

The branch holds one orphan commit, replaced at every save (encrypted blobs every 15 minutes would otherwise pile up):

    README.md                        what this branch is (fixed text)
    BTC/state-shadow.enc.json        SHADOW's state, encrypted (statebox): the broker's, the desk's, the virtual book
    BTC/state-shadow.prev.enc.json   the one before it
    BTC/board-shadow.enc.json        SHADOW's board, encrypted: the dashboard opens it with DESK_STATE_KEY
    BTC/status.json                  public, no amounts and no ids: mode, runs, ok, the problem, the decision
    BTC/kills.json                   the kill switches: yours to edit here (flat_kill, entry_kill, jev_kill), and
                                     reset_halt_at: any new text (a date, say) clears a drawdown halt once
    BTC/kills.desk.json              the kills the desk set itself, each on until you set it to false in kills.json
                                     (the desk's: no amounts, only which switch, and whether kills.json shows it yet)

The load step (load) writes the whole branch into the state dir, byte for byte as git stores it (git archive would apply
a .gitattributes of yours), and passes the commit it loaded on to check and stage.
check refuses (exit 1, an ::error:: naming the file) a file in the state dir that is neither one of the desk's nor as
the load extracted it (the run wrote something it never saves; without the loaded commit: one that isn't on the
branch), an encrypted file that isn't an envelope, a status or kills.set.json that isn't what the desk writes
(status keys and values are whitelisted, so a bug can't put an amount in it), and a younger state: the same start
time as the branch's, and more runs, or as many runs with the very same state file. A run refused over its secrets
(problem "key"), or a SHADOW refused before it ever had a working run (any problem), saves its status alone: the state
file as loaded, or none yet on a first save; such a status with no state beside it (runs 0) is no account, so the
first run that has a state (whatever was refused, fixed) saves as a first save. A status you deleted beside the state
is written back by the next run, the state as it was (nothing else saved); with no status and a state changed, check
refuses (not replacing a state blind).
stage copies the desk's files into a fresh tree, keeps every file of yours on the branch as it is now (any file that
isn't the desk's: notes, say; one you deleted since the load stays deleted; a .gitattributes is dropped, since it would
change how git stores the desk's files; .github/ is kept exactly as it is), writes the branch's kills.json (your edits)
with every kill the desk set added (kills.set.json, kills.desk.json), and moves the branch's last state to .prev.

kills.json is yours, so a typo in it never stops a save, and never turns a switch off: kills_read is how the runs, check
and stage all read it. One it can't read (not JSON, a key it doesn't know, a switch that isn't true or false, a key
written twice, a symlink) fails closed: entry_kill on, and every switch it names on unless its value says off (false,
null, 0, "false", "no", "off", "none", "nil", "", or a near-typo of false, null, off or no, like "fasle"; any other
word, "now" or "flat" say, is on). Its switch words are DESK_COINBASE_KILL's: flat, close (close_all, close_position)
and flatten for flat_kill, entry (entry_kill, no_entries) for entry_kill, anywhere in a name. The save keeps such a file
exactly as you wrote it, so every run reads it the same way and says so until you fix it. A kill the desk sets while it
can't read the file is kept in kills.desk.json and written into kills.json at the first save that can: fixing the typo
never turns it off, nor does deleting or renaming kills.json (the next save writes a new one with it on); setting it to
false in a kills.json the desk can read, once it's there, does (kills_effective). A kills.json you delete while a run is
under way turns your switches off: that run's save writes a fresh one, every switch false (the desk's own kills on). A
file of yours named like a kill switch anywhere on the branch (kill_like) turns entry_kill on, said so. LIVE counts on
this branch only once it has armed: its status says "armed": true. A LIVE run that never armed saves nothing
(desk/coinbase_run.py), check refuses a LIVE status that isn't armed, and a LIVE status without it (a LIVE attempt that
never armed, before this rule) is no LIVE start: SHADOW saves over it and stage drops its files.

check runs against the branch as git has it (GitBranch) or, inside a run, as the run loaded it (Loaded: the load step
extracts the branch into the state dir), so a run can tell before the save step that its save would be refused.
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
ALLOWED = ENCRYPTED | {"status.json", "kills.json", "kills.set.json", "kills.desk.json"}
KILL_KEYS = ("flat_kill", "entry_kill", "jev_kill")
RESET_KEYS = ("reset_halt_at",)         # one-shot: a new value clears the drawdown halt once (desk/coinbase_run.py)
STATUS_KEYS = {"v": int, "mode": str, "coin": str, "product": str, "updated": int, "started_ts": int, "runs": int,
               "failed_runs": int, "ok": bool, "problem": (str, type(None)), "key_view_only": (bool, type(None)),
               "decision": (str, type(None)), "would_send_last_24h": (int, type(None)), "alerts_open": int,
               "checks_passed": (int, type(None)), "checks_total": (int, type(None)), "opened": bool,
               "note": (str, type(None)), "armed": (bool, type(None))}
PROBLEMS = {None, "auth", "permissions", "clock", "read", "state", "refused", "key", "error"}
# the note of the status a run writes back from the state it opened, BTC/status.json having gone from beside it, or
# been edited into one the save check refuses
STATUS_BACK = (f"{COIN}/status.json on the coinbase-state branch was missing or edited: this run wrote it back, the next "
               f"run saves")
LINK = "120000"                         # git's mode for a symlink (its blob is the link's target)
KILLS = f"{COIN}/kills.json"
DESK_KILLS = f"{COIN}/kills.desk.json"
# kills.desk.json: each kill the desk set, until you clear it on purpose in a kills.json the desk can read
DESK_WRITTEN = "on in kills.json: set it to false there to clear it"
DESK_PENDING = "on, not in kills.json yet: the next save the desk can read kills.json at writes it there"
DESK_STATES = (DESK_WRITTEN, DESK_PENDING)
BRANCH_README = """# coinbase-state

Written by the paper desk workflow's `shadow` job (desk/coinbase_run.py). This repository is public, so the real
Coinbase account's state and board are encrypted with the `DESK_STATE_KEY` secret (desk/statebox.py); only
`BTC/status.json` (no amounts) is plain.

To stop new entries or close the position from here: edit `BTC/kills.json` (`"entry_kill": true` or
`"flat_kill": true`). The next run reads it. A kill the desk sets itself is written into `BTC/kills.json` and recorded
in `BTC/kills.desk.json` (the desk's, don't edit it): it stays on until you set it to `false` in `BTC/kills.json`. Each
save replaces this branch's one commit, and keeps any other file you add here as it is (the desk never reads it; one
whose name has "kill" in it turns entry_kill on, said so; a `.gitattributes` is dropped, `.github/` kept as it is).
"""


class StateCheckError(RuntimeError):
    pass


def _git_show(ref: str, path: str) -> bytes | None:
    """The blob's bytes exactly as stored: cat-file applies no filter, no line-ending or encoding attribute."""
    try:
        out = subprocess.run(["git", "cat-file", "blob", f"{ref}:{path}"], capture_output=True, timeout=60)
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


def _all_entries(d: Path) -> dict[str, tuple[str, bytes]]:
    """Every file in the state dir, by its path on the branch (_entries, from the top)."""
    return {rel[1:]: got for rel, got in _entries(d, "").items()}


class Loaded:
    """The branch as a run loaded it: the load step extracts the whole branch into the state dir, and the run copies
    it before it writes anything. Answers as GitBranch does, so the run checks its own save before the save step."""

    def __init__(self, state_dir: str | Path | None):
        self.files = {} if state_dir is None else _all_entries(Path(state_dir))

    def tree(self) -> dict[str, str]:
        return {p: m for p, (m, _) in self.files.items()}

    def show(self, path: str) -> bytes | None:
        got = self.files.get(path)
        return None if got is None else got[1]


def _desks(path: str) -> bool:
    """One of the desk's own paths on the branch (kills.json, yours, apart)."""
    return path == "README.md" or (path.startswith(COIN + "/") and path[len(COIN) + 1:] in ALLOWED - {"kills.json"})


desks_own = _desks


def yours(tree: dict[str, str]) -> list[str]:
    """The branch's files that aren't the desk's: kept as they are at every save, never read (kill-like ones apart:
    kill_like). BTC/kills.json is in it only when it isn't a plain file (a symlink, a folder)."""
    return sorted(p for p, m in tree.items()
                  if not _desks(p) and not (p == KILLS and m not in (LINK, "160000")))


def kill_like(path: str) -> bool:
    """A file of yours, anywhere on the branch, whose name says kill but isn't BTC/kills.json (kills.json at the
    root, btc/kills.json, BTC/Kills.json, notes/kill-switch.txt): it looks like a kill switch, so the runs read it as
    one they can't read (entry_kill on), and say so. A flat_kill in it is named, never acted on from there."""
    return "kill" in path.lower() and path != KILLS and not path.startswith(KILLS + "/") and not _desks(path)


def branch_file(branch, path: str) -> tuple[bytes | None, bool]:
    """A file on `branch`: (its bytes, whether it is a plain file). A symlink: its target, not plain; a folder: b"",
    not plain; none: (None, True)."""
    tree = branch.tree()
    mode = tree.get(path)
    if mode is None:
        if any(p.startswith(path + "/") for p in tree):
            return b"", False
        return None, True
    return branch.show(path) or b"", mode not in (LINK, "160000", "special")


def branch_kills(branch) -> tuple[bytes | None, bool]:
    """BTC/kills.json on `branch` (branch_file)."""
    return branch_file(branch, KILLS)


def attributes(path: str) -> bool:
    """A .gitattributes of yours (outside .github/): dropped at each save, since it would change how git stores the
    desk's files (an encoding, a line-ending filter). .github/ is kept exactly as it is: the job's token can't change
    a workflow file, and never does."""
    return path.rsplit("/", 1)[-1] == ".gitattributes" and not path.startswith(".github/")


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

# a switch's value that says off: these words, and their near-typos (a letter doubled or dropped, two swapped: "fasle",
# "nul", "offf"); anything else says on (failing closed), "now", "flat" and "force" included
OFF_WORDS = ("false", "f", "no", "n", "off", "null", "none", "nil", "0", "")
TYPO_OFF = ("false", "null", "off", "no")
TRUE_WORDS = {"true", "t", "yes", "y", "on", "1"}
# the switches' words, the same for DESK_COINBASE_KILL and kills.json: any of these anywhere in a name (or in the
# variable's text), letters only, lowercased. "flat" covers flat_kill and flatten, "close" close_all and close_position,
# "entr" entry, entry_kill, entries and no_entries
SWITCH_WORDS = (("flat_kill", ("flat", "close")), ("entry_kill", ("entr",)), ("jev_kill", ("jev",)))
# a switch named in text the desk can't parse: a word starting with one of SWITCH_WORDS (flat_kill, "Flat Kill",
# close_position, flat_kil...), then (maybe) : or =, then a value
_NAMED = re.compile(r"(?<![a-z])((?:flat|close|entr|jev)[a-z_\-]*(?:\s+kills?(?![a-z]))?)[\"']?\s*([:=]?)\s*"
                    r"(\"[^\"\n]*\"?|'[^'\n]*'?|[\w.+\-!]*)", re.IGNORECASE)


class KillsRead(NamedTuple):
    kills: dict             # the switches (and reset_halt_at, from a file the desk can read)
    why: str | None         # None: read as written; else why the desk can't read it (then entry_kill is on)
    named: tuple            # the switches the file names


def _near(t: str, w: str) -> bool:
    """`t` is `w` with a letter doubled, dropped or added, or two neighbours swapped (never another letter in place of
    one: "full" isn't "null")."""
    squeeze = lambda x: re.sub(r"(.)\1+", r"\1", x)       # noqa: E731
    if squeeze(t) == squeeze(w):
        return True
    if len(w) < 4:              # "no", "off": only doubled letters ("noo", "offf"), never "now" or "on"
        return False
    if abs(len(t) - len(w)) == 1:
        a, b = (t, w) if len(t) > len(w) else (w, t)
        return any(a[:k] + a[k + 1:] == b for k in range(len(a)))
    return len(t) == len(w) and any(t[:k] + t[k + 1] + t[k] + t[k + 2:] == w for k in range(len(t) - 1))


def says_off(v) -> bool:
    """A switch's value that says off: false, null, 0, or one of OFF_WORDS ("false", "no", "off", "", ...) or a
    near-typo of false, null, off or no. Anything else says on (failing closed): "now", "NOW!", "force",
    "flat", "nah" all do."""
    if v is None or v is False:
        return True
    if v is True:
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        t = v.strip().strip("\"'").strip().lower()
        if t in OFF_WORDS or any(_near(t, w) for w in TYPO_OFF):
            return True
        try:
            return float(t) == 0
        except ValueError:
            return False
    return False                # a list or an object: not off


def named_switches(text) -> list[str]:
    """The switches `text` names (a key of kills.json, or DESK_COINBASE_KILL's value): SWITCH_WORDS anywhere in it."""
    t = re.sub(r"[^a-z]", "", str(text).lower())
    return [sw for sw, words in SWITCH_WORDS if any(w in t for w in words)]


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
        for sw in named_switches(k):
            named[sw] = named.get(sw, False) or not says_off(v)
    for m in _NAMED.finditer(_text(raw)):
        sep, val = m.group(2), m.group(3)
        for sw in named_switches(m.group(1)):
            if sep:
                named[sw] = named.get(sw, False) or not says_off(val)
            elif val.strip("\"'").lower() in TRUE_WORDS:    # no : or = (prose, say): only a plain yes counts
                named[sw] = True
    for sw, on in named.items():
        kills[sw] = kills[sw] or on
    kills["entry_kill"] = True
    return KillsRead(kills, why, tuple(k for k in KILL_KEYS if k in named))


def desk_kills_read(raw: bytes | None, plain: bool = True) -> dict:
    """BTC/kills.desk.json: {switch: DESK_WRITTEN or DESK_PENDING}. None: {}. One that isn't what the desk writes
    (you edited it, a symlink) fails closed: entry_kill pending, and each switch it names pending too."""
    if raw is None:
        return {}
    try:
        d = json.loads(raw) if plain else None
    except (ValueError, RecursionError):
        d = None
    if isinstance(d, dict) and all(k in KILL_KEYS and v in DESK_STATES for k, v in d.items()):
        return dict(d)
    out = {"entry_kill": DESK_PENDING}
    for sw in named_switches(_text(raw) if plain else ""):
        out[sw] = DESK_PENDING
    return out


def kills_effective(raw: bytes | str | None, plain: bool = True, desk_raw: bytes | None = None,
                    desk_plain: bool = True) -> tuple[dict, str | None, dict]:
    """The switches as every run acts on them, the same for the runs and the save (stage): BTC/kills.json read
    failing closed (kills_read), with every kill the desk set and you haven't cleared (BTC/kills.desk.json) on. You
    clear one on purpose by setting it to false in a kills.json the desk can read, once the desk has written it there
    (DESK_WRITTEN); one it couldn't write yet (DESK_PENDING: kills.json couldn't be read when it was set) stays on, so
    fixing a typo never turns it off. With no kills.json at all (you deleted it, or renamed it) nothing says false: the
    desk's kills stay on, pending, and the next save writes them into a new kills.json. Returns (switches, why
    kills.json can't be read or None, the record after)."""
    got = kills_read(raw, plain)
    kills, record = dict(got.kills), {}
    for sw, st in desk_kills_read(desk_raw, desk_plain).items():
        if raw is None:
            record[sw] = DESK_PENDING       # no file says false: still on, written into the next kills.json
        elif got.why is None and not got.kills.get(sw) and st == DESK_WRITTEN:
            continue                        # you cleared it, in a kills.json the desk can read
        else:
            record[sw] = DESK_WRITTEN if got.why is None and got.kills.get(sw) else st
        kills[sw] = True
    return kills, got.why, record


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


def _local(state_dir: Path, branch, loaded=None) -> dict[str, bytes]:
    """The desk's files in the state dir (BTC/'s, by name). Anything else in it came from the load step, which
    extracts the whole branch: with the load known (`loaded`, the branch as it was loaded) it must be exactly as
    loaded; without, on the branch now. Either way stage takes the branch's own copy (or none, if you deleted it since
    the load), so your edits never stop a save. A file that is neither is something the run wrote and the desk never
    saves: refused."""
    tree = branch.tree()
    out = {}
    for rel, (mode, data) in _all_entries(state_dir).items():
        name = rel[len(COIN) + 1:] if rel.startswith(COIN + "/") else None
        if name in ALLOWED and mode not in (LINK, "special"):
            out[name] = data
        elif loaded is not None:
            was = loaded.tree().get(rel)
            if was is None:
                raise StateCheckError(f"{rel} isn't a file the SHADOW job saves, and the load step didn't extract it: "
                                      f"not saving")
            if (was == LINK) != (mode == LINK) or (loaded.show(rel) or b"") != data:
                raise StateCheckError(f"{rel} changed since the load step extracted it (the run never writes your "
                                      f"files): not saving")
        elif rel not in tree:
            raise StateCheckError(f"{rel} isn't a file the SHADOW job saves, and it isn't on the branch: not saving")
    return out


def _kills_here(files: dict, branch, loaded=None) -> tuple[bytes | None, bool]:
    """BTC/kills.json as this save takes it: the branch's now (your edits since the load win). None there: with the
    load known, one it had is one you deleted since (gone: your switches with it, the desk's own kept by
    kills_effective); otherwise the state dir's (a first save)."""
    raw, plain = branch_kills(branch)
    if raw is None and plain:
        if loaded is not None and branch_kills(loaded)[0] is not None:
            return None, True                       # you deleted it after the load
        return files.get("kills.json"), True        # none on the branch yet: the one loaded (a first save)
    return raw, plain


def kills_staged(files: dict, branch, loaded=None) -> tuple[dict, str | None, dict]:
    """The switches as this save reads them (kills_effective, on the branch's kills.json and kills.desk.json), and the
    record with this run's own kills (kills.set.json) in it."""
    desk_raw, desk_plain = branch_file(branch, DESK_KILLS)
    kills, why, record = kills_effective(*_kills_here(files, branch, loaded), desk_raw, desk_plain)
    if "kills.set.json" in files:
        for k, v in check_kills(_json(files["kills.set.json"], "kills.set.json"), "kills.set.json").items():
            if k in KILL_KEYS and v and k not in record:
                record[k] = DESK_PENDING          # written into kills.json below, if the save can
    return kills, why, record


def check(state_dir: str | Path, remote_ref: str | None = None, branch=None, loaded=None) -> list[str]:
    """Raises StateCheckError; returns what it checked, for the log. `loaded`: the branch as the load step extracted
    it (GitBranch on the commit loaded), when known."""
    b = branch if branch is not None else GitBranch(remote_ref)
    files = _local(Path(state_dir), b, loaded)
    said = []
    for name, data in files.items():
        if name in ENCRYPTED:
            if not is_envelope(_json(data, f"{COIN}/{name}")):
                raise StateCheckError(f"{COIN}/{name} isn't encrypted: never saving the real account in plain text")
        elif name == "kills.set.json":
            check_kills(_json(data, f"{COIN}/{name}"), f"{COIN}/{name}")
    # kills.json is yours: stage saves the branch's (or, with none there yet, this one), as kills_effective reads it
    kills, why, record = kills_staged(files, b, loaded)
    if why:
        on = ", ".join(k for k in KILL_KEYS if kills[k])
        said.append(f"{KILLS} can't be read ({why}): kept as you wrote it; every run reads it failing closed "
                    f"({on} on) until you fix it")
    if record:
        said.append(f"the desk's own kills, on until you set them to false in {KILLS}: "
                    + ", ".join(f"{k} ({'in kills.json' if v == DESK_WRITTEN else 'not in kills.json yet'})"
                                for k, v in sorted(record.items())))
    mine = yours(b.tree())
    if mine:
        said.append(f"yours, kept as they are: {', '.join(mine[:10])}" + (" and more" if len(mine) > 10 else "")
                    + ("; read as a kill switch the desk can't read (entry_kill on): "
                       + ", ".join(p for p in mine if kill_like(p)) if any(kill_like(p) for p in mine) else "")
                    + ("; dropped at this save (they'd change how git stores the desk's files): "
                       + ", ".join(p for p in mine if attributes(p)) if any(attributes(p) for p in mine) else ""))
    if "status.json" not in files:
        raise StateCheckError(f"{COIN}/status.json is missing: nothing to save")
    new = check_status(_json(files["status.json"], f"{COIN}/status.json"))
    mode = new["mode"].lower()
    state = f"state-{mode}.enc.json"
    # a run refused before it could save a state (over its secrets, or before SHADOW ever worked) saves its status
    # alone: never a state, only the status beside the state as it was (or none)
    status_only = new.get("problem") is not None and new.get("ok") is False
    if new["mode"] == "LIVE" and new.get("armed") is not True:
        raise StateCheckError(f"{COIN}/status.json is a LIVE status that isn't armed: a LIVE run that never armed "
                              f"saves nothing (it isn't a LIVE start)")
    if state not in files:
        if status_only and new["runs"] == 0 and b.show(f"{COIN}/{state}") is None:
            said.append(f"a status-only save: {new['mode']} was refused before it ever had a state")
            return said
        if b.show(f"{COIN}/{state}") is not None:
            raise StateCheckError(f"{COIN}/{state} is on the branch but this run has none: not saving a status beside "
                                  f"a state it didn't load")
        raise StateCheckError(f"{COIN}/{state} is missing next to a {new['mode']} status")
    old_raw = b.show(f"{COIN}/status.json")
    old, unreadable = None, False
    if old_raw is not None:
        try:
            if b.tree().get(f"{COIN}/status.json") == LINK:
                raise StateCheckError("a symlink")
            old = check_status(_json(old_raw, f"saved {COIN}/status.json"), f"saved {COIN}/status.json")
        except StateCheckError:
            unreadable = True           # edited: read as no status (it never decides whose the state is)
    written_back = status_only and new.get("problem") == "state" and new.get("note") == STATUS_BACK and new["runs"] >= 1
    on_branch = b.show(f"{COIN}/{state}")
    if on_branch is not None and written_back and files[state] == on_branch and \
            (old is None or old["mode"] == new["mode"]):
        # the status was deleted, edited into something the desk didn't write, or edited so it doesn't match the
        # state: the run that found it so wrote it back from the state it opened, and saved that state as it is
        # (nothing replaced: the state on the branch stays exactly as it was)
        said.append(f"{COIN}/status.json written back beside the branch's {COIN}/{state}, which is unchanged")
        return said
    if old is None:
        if on_branch is not None:
            if old_raw is None and status_only and files[state] == on_branch:
                # a status written by a run that couldn't open the state (refused over a secret) has none of the
                # state's counts, and would leave every later run's save refused as another account
                raise StateCheckError(f"{COIN}/status.json isn't on the branch beside {COIN}/{state}, and this one "
                                      f"isn't written back from that state (the run couldn't open it): not saving it. "
                                      f"The next run that opens the state writes it back")
            raise StateCheckError(f"the branch has {COIN}/{state} but " + (
                "a status.json the desk didn't write (edited?)" if unreadable else "no status.json (deleted?)") +
                f": not replacing the state blind. The next run that opens the state writes the status back; or put "
                f"{COIN}/status.json back as it was (revert the commit that changed it), or delete the coinbase-state "
                f"branch to start over")
        prev = f"{COIN}/state-{mode}.prev.enc.json"
        if b.show(prev) is not None:
            raise StateCheckError(f"the branch has {prev} but neither {COIN}/{state} nor a status.json the desk can "
                                  f"read (deleted?): not starting a new state over the record. Put them back as they "
                                  f"were (revert the commit that deleted them), or delete the coinbase-state branch to "
                                  f"start over")
        said.append("no saved state on the branch: a first save" + (
            " (its status.json wasn't one the desk wrote)" if unreadable else ""))
        return said
    if old["mode"] == new["mode"] and old["runs"] == 0 and b.show(f"{COIN}/{state}") is None:
        # the status-only save of a run refused before it ever had a state: no account yet, so the first run that has
        # one (its secrets fixed) is the first save, whatever start it gives
        said.append(f"a first save: the branch's {old['mode']} status is a run refused before it ever had a state")
        return said
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
        gone = b.show(f"{COIN}/{state}") is None
        raise StateCheckError(f"{COIN}/{state} would replace the saved {old['mode']} account (started "
                              f"{old['started_ts']}, {old['runs']} runs) with one started {new['started_ts']}: "
                              f"not saving. " + (f"{COIN}/{state} isn't on the branch (deleted?): put it back as it "
                                                 f"was (revert the commit that deleted it), or delete the "
                                                 f"coinbase-state branch to start over" if gone else
                                                 "To start over, delete the coinbase-state branch"))
    if new["runs"] < old["runs"]:
        raise StateCheckError(f"{COIN}/{state} has {new['runs']} runs, the saved one {old['runs']}: a younger state, "
                              f"not saving")
    if new["runs"] == old["runs"] and files[state] != (b.show(f"{COIN}/{state}") or b""):
        raise StateCheckError(f"{COIN}/{state} changed but its run count didn't: not saving")
    if status_only and new["runs"] == old["runs"]:
        said.append(f"a status-only save: {new['mode']} was refused before it could save a state; its state is as "
                    f"loaded")
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


def stage(state_dir: str | Path, work: str | Path, remote_ref: str | None = None, branch=None,
          loaded=None) -> list[str]:
    b = branch if branch is not None else GitBranch(remote_ref)
    files = _local(Path(state_dir), b, loaded)
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
    # yours: every file on the branch that isn't the desk's, as it is (a .gitattributes apart: attributes)
    tree = b.tree()
    for path in yours(tree):
        data = b.show(path)
        if data is not None and _safe(path) and tree[path] != "160000" and not attributes(path):
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
    raw, plain = _kills_here(files, b, loaded)
    got = kills_read(raw, plain)
    _, _, record = kills_staged(files, b, loaded)
    if got.why is None or any(not got.kills[k] for k in record):
        # read as written (or a desk kill the file can't carry): written valid, with every kill the desk set on, and
        # from then on the desk's record says it's in kills.json (DESK_WRITTEN: you clear it there)
        kills = {**got.kills, **{k: True for k in record}}
        record = {k: DESK_WRITTEN for k in record}
        p = out / "kills.json"
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.exists():
            shutil.rmtree(p)
        p.write_text(json.dumps(kills, indent=1) + "\n")
    elif not ((out / "kills.json").is_symlink() or (out / "kills.json").exists()):
        (out / "kills.json").write_bytes(raw or b"")    # one the desk can't read: kept exactly as you wrote it
    # (a symlink or a folder in its place was kept above, as it is: every run reads it as one it can't read)
    if record:
        (out / "kills.desk.json").write_text(json.dumps(dict(sorted(record.items())), indent=1) + "\n")
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_symlink() or p.is_file())


def load(state_dir: str | Path, remote_ref: str) -> list[str]:
    """The load step: every file on the branch into the state dir, byte for byte as git stores it (a symlink as a
    symlink, the exec bit kept), with no attribute of the branch's applied (git archive would apply a .gitattributes:
    line endings, an encoding, export-ignore leaving kills.json out). A submodule is left out."""
    b = GitBranch(remote_ref)
    tree = b.tree()
    out = []
    for path in sorted(tree):
        if tree[path] == "160000" or not _safe(path):
            continue
        data = b.show(path)
        if data is None:        # never a branch loaded in part: kills.json missing would read as every switch off
            raise StateCheckError(f"{path} couldn't be read from the branch: not loading it in part")
        _put(Path(state_dir) / path, tree[path], data)
        out.append(path)
    return out


def main(argv: list[str]) -> int:
    def loaded(args: list[str]):
        return GitBranch(args[0]) if args and args[0].strip() else None    # the commit the load step extracted

    try:
        if len(argv) in (3, 4) and argv[0] == "check":
            for line in check(argv[1], argv[2], loaded=loaded(argv[3:])):
                print(f"coinbase-state check: {line}")
            return 0
        if len(argv) == 3 and argv[0] == "load":
            print(f"coinbase-state load: {len(load(argv[1], argv[2]))} files")
            return 0
        if len(argv) in (4, 5) and argv[0] == "stage":
            print("coinbase-state stage: " + ", ".join(stage(argv[1], argv[2], argv[3], loaded=loaded(argv[4:]))))
            return 0
    except StateCheckError as exc:
        print(f"::error::coinbase-state: {exc}")
        return 1
    print("usage: python3 -m desk.coinbase_state load <state-dir> <ref> | check <state-dir> <ref> [<loaded>] | stage "
          "<state-dir> <work-dir> <ref> [<loaded>]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
