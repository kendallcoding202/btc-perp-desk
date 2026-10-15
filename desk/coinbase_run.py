"""BTC on Coinbase (BIP-20DEC30-CDE), one check per call: the way GitHub Actions runs it, every 15 minutes.

    python -m desk shadow --coin BTC --state-dir DIR            SHADOW: the REAL account, read only, orders simulated
    python -m desk live --coin BTC --once --state-dir DIR ...   LIVE: real orders, LOCKED (coinbase_live.arm())

SHADOW reads the real Coinbase account with a View-only key (COINBASE_CDP_KEY), runs the desk's own decisions on
Coinbase's candles exactly as LIVE would (LiveBroker, desk/brokers/live.py, over ShadowCoinbase: the real balance and
the real deposits and withdrawals, a virtual book for the orders), and records the requests LIVE would have sent.
Nothing is sent: its client can't POST at all, and SHADOW refuses any key that can trade or move money.

One run (run_shadow):
  1. refuse, before any network call, without COINBASE_CDP_KEY or DESK_STATE_KEY (32+ characters), when LIVE runs on
     this account (state-live.enc.json that has armed, or that DESK_STATE_KEY doesn't open), or when DESK_STATE_KEY
     doesn't open the saved state (never a new state over it); a LIVE state that never armed is no LIVE start;
  2. from here every run saves (changed=true is written first): the state, the board and a public status (LIVE: only
     once it has armed; a LIVE run that stops before it ever armed saves nothing, so it changes nothing for SHADOW);
  3. the key, its masks on Actions, the clock against Coinbase's, and the key's permissions (View only, or refuse);
  4. the real account: balance, positions, open orders, fills, the fee tier, sweeps, the margin window, the USD wallet
     and its deposits and withdrawals; each read is one line of the checklist on the board;
  5. Coinbase's 2h candles, product, book and ticker (authenticated: the public answers are cached by a CDN), with
     paper's own venue rules (desk/paper.py: the missing-candle wait, the funding log, the session warnings, imported);
  6. the virtual book catches up (its stops against the 5-minute candles since the last run, funding), then one check
     of the live loop (desk/runner.py's): kills, the reconciliation, close retries, flat_kill, the missing-stop guard,
     then every 2h close since the last one through runner.process_closed_bars, an entry acted on only within
     ONESHOT_FRESH_MS of its close and only while Coinbase is open;
  7. the decision is compared with the paper account's at the same close (its board, if the workflow handed it over);
  8. state and board encrypted (desk/statebox.py), status.json, the kills this run set, pushes, ONE public log line.

The repository is public: the CLI captures stdout and stderr from its first line and keeps them only in the encrypted
board; what reaches the Actions log is the masks and one fixed line with no amount, id or Coinbase wording. Pushes go to
ntfy.sh, where anyone who knows the topic's name can read them: without NTFY_TOKEN (an ntfy access token, for a
reserved topic) their amounts are left out (redact_amounts); the encrypted board has them.

LIVE also runs a safety pass first, right after its arming and the key's permissions, before any market read: the
position and the open orders, read fresh; a stop put back on a position that has none, any desk order left resting
with no position cancelled, flat_kill honoured. A run that then can't read the candles or the product still leaves the
position protected; and a run that fails after it looks once more (the watchdog) and pushes urgently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import board as board_mod
from . import coinbase_state, statebox
from . import flows as flows_mod
from . import indicators as ind
from . import venue as venue_mod
from .engine import Desk, Kills
from .filter import evaluate_entry
from .models import Verdict
from .thresholds import COINBASE, params_for

COIN = "BTC"
PRODUCT = COINBASE.product
ONESHOT_FRESH_MS = 30 * 60_000      # runs land 7-9 minutes after a 2h close on Actions (53 ledger commits measured)
DEADLINE_S = 270.0                  # 4.5 minutes for the whole run (the job's own limit is 8, the step's 6)
UNEXPLAINED_WAIT_MS = 3 * 3600_000  # a move nothing explains waits this long for its ledger entry (12 one-shot runs)
PROVISIONAL_TRIES = 8
PUSH_EVERY_MS = 6 * 3600_000
HEALTH_DAYS = 30
LOG_KEEP = 400
FIRST_FILLS_MS = 30 * 86400_000
DAY_MS = 86400_000
V3 = "/api/v3/brokerage"
FILES = ("live_position.json", "desk.json", "venue.json", "book.json", "account.json")
# the gates a decision depends on the candles alone for: SHADOW's and paper's must agree on these at the same close
SIGNAL_GATES = ("history_bars", "atr_pct_min", "atr_pct_max", "setup", "trend_strength", "efficiency",
                "momentum_already_spent")
# the ones that differ by design: the account (its balance, its kills and brakes), the moment the book was read,
# each one's own log of Coinbase's funding rate
OWN_GATES = {"drawdown_halt": "the account", "daily_loss": "the account", "cooldown": "the account",
             "manual_kill": "the account", "flat": "the account", "desk_size_usd": "the account",
             "contracts": "the account", "margin": "the account", "ticket_usd": "the account",
             "data_age_bars": "when it ran", "spread_bps": "when it ran", "depth_usd": "when it ran",
             "session": "when it ran", "funding_known": "its own funding log", "abs_funding_apr": "its own funding log",
             "crowding": "its own funding log"}


class Stop(Exception):
    """The run stops here: `problem` for status.json (auth, permissions, clock, read, state, refused, key), `code`
    the exit code (1 a read failed, 2 refused). The text goes to the private log, the board and the phone."""

    def __init__(self, problem: str, text: str, code: int = 2, public: str | None = None):
        super().__init__(text)
        self.problem, self.text, self.code = problem, text, code
        self.public = public            # a fixed phrase for the public log and status.json (no amount, no digit)


def _utc(ms: int, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    return time.strftime(fmt, time.gmtime(ms / 1000))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _usd(x: float | None, dp: int = 2) -> str:
    return "—" if x is None else f"${x:,.{dp}f}"


# What a push may not say without NTFY_TOKEN: dollar amounts, and signed or currency-tagged two-decimal figures (a
# balance move, a P&L). Prices without a $ (a stop level, BTC's price) and contract counts stay.
_MONEY = re.compile(r"[-+]?\$\s?-?[\d,]+(?:\.\d+)?|(?<![\w.])[-+][\d,]*\d\.\d{2}\b|\b\d[\d,]*\.\d{2}\s?USDC?\b")
REDACTED_NOTE = ("(amounts left out: anyone who knows an ntfy.sh topic's name can read it. They're on the {mode} "
                 "board, encrypted; set NTFY_TOKEN for a reserved topic to get them here)")
# the public status's note when BTC/kills.json can't be read (no digit, no amount: coinbase_state.check_status)
KILLS_UNREADABLE = "BTC/kills.json on the coinbase-state branch can't be read: read as entry_kill on, no new entries"
KILLS_UNREADABLE_FLAT = KILLS_UNREADABLE + "; flat_kill on: any position is closed"
KILLS_STRAY = ("a file on the coinbase-state branch looks like a kill switch but isn't BTC/kills.json: read as "
               "entry_kill on, no new entries")
# DESK_COINBASE_KILL (a repository variable): its words for flat_kill and for entry_kill, letters only, lowercased;
# any other value is entry_kill, alerted (failing closed)
KILL_VAR_FLAT = {"flat", "flatkill", "flatten", "close", "closeall", "closeposition", "closeit"}
KILL_VAR_ENTRY = {"entry", "entrykill", "entries", "noentry", "noentries"}
URGENT_TITLES = {"no_stop": "NO STOP", "stray": "STOP WITH NO POSITION", "stray-foreign": "STOP WITH NO POSITION",
                 "foreign": "AN ORDER THAT ISN'T THE DESK'S", "unprotected": "NO STOP"}


def redact_amounts(text: str) -> str:
    """`text` with its money amounts taken out (see _MONEY)."""
    return _MONEY.sub("…", text)


def ntfy_push(title: str, body: str, priority: str = "default", tags: str = "robot", env=None) -> bool:
    """To the owner's phone (ntfy.sh, NTFY_TOPIC), if set; with NTFY_TOKEN (an ntfy access token), authenticated. A
    failed push never stops a run."""
    e = env if env is not None else os.environ
    topic = (e.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    token = (e.get("NTFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), method="POST", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def github_output(changed: bool, env=None) -> None:
    path = (env if env is not None else os.environ).get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")


# ---------------------------------------------------------------- the encrypted bundle

def open_bundle(path: Path, password: str, mode: str) -> dict | None:
    """The saved state's files ({name: text}), or None when there is none yet. Raises statebox.WrongKey/BoxError."""
    if not path.exists():
        return None
    d = statebox.open_json(path.read_text(), password, statebox.state_aad(mode))
    if not isinstance(d, dict) or d.get("v") != 1 or not isinstance(d.get("files"), dict):
        raise statebox.BoxError("the saved state isn't one the desk wrote")
    return d


def armed_at(bundle: dict | None):
    """When the LIVE state in `bundle` first armed (its desk meta's armed_at), or None: a LIVE state with none is the
    leftover of a LIVE attempt that never armed, which is no LIVE start. Raises ValueError for a desk.json that isn't
    JSON."""
    meta = (json.loads(((bundle or {}).get("files") or {}).get("desk.json") or "null") or {})
    meta = meta.get("meta") if isinstance(meta, dict) else None
    return (meta or {}).get("armed_at") if isinstance(meta, dict) else None


def unpack(bundle: dict | None, work: Path) -> None:
    (work / COIN).mkdir(parents=True, exist_ok=True)
    for name, text in ((bundle or {}).get("files") or {}).items():
        if name in FILES:
            (work / COIN / name).write_text(text)


def pack(work: Path) -> dict:
    files = {}
    for name in FILES:
        p = work / COIN / name
        if p.exists():
            files[name] = p.read_text()
    return {"v": 1, "files": files}


def _read(work: Path, name: str, default=None):
    p = work / COIN / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except ValueError:
        raise Stop("state", f"the saved state's {name} isn't readable") from None


def _write(work: Path, name: str, data) -> None:
    p = work / COIN / name
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, default=str, separators=(",", ":")))
    tmp.replace(p)


# ---------------------------------------------------------------- the run's account memory (account.json)

def new_account() -> dict:
    return {"v": 1, "days": {}, "pushed": {}, "adapter": {}, "last_real": None, "log": [], "decisions": {},
            "deposits_reconciled": 0, "portfolio_type": None, "portfolio_uuid": None, "first_ok_ts": None,
            "fills_cursor": None, "entry_kill_reason": None, "last_run_ts": None, "problem": None}


def health(account: dict, now_ms: int, **inc) -> dict:
    """Today's (UTC) counts: runs, ok, flows_unreadable, unexplained, unknown_entries, equity_mismatch, auth_fail,
    funded. Kept HEALTH_DAYS days: the arming checks read them (coinbase_live.shadow_checks)."""
    days = account.setdefault("days", {})
    d = days.setdefault(_day(now_ms), {"runs": 0, "ok": 0})
    for k, v in inc.items():
        if isinstance(v, bool):
            d[k] = bool(d.get(k)) or v
        else:
            d[k] = int(d.get(k) or 0) + int(v)
    for k in sorted(days)[:-HEALTH_DAYS]:
        days.pop(k)
    return d


def shadow_proof(account: dict | None, meta: dict | None) -> dict | None:
    """SHADOW's record for coinbase_live.arm(): what it ran, how clean, on which portfolio."""
    if not account or not meta:
        return None
    return {"started_ts": meta.get("started_ts"), "last_run_ts": account.get("last_run_ts"),
            "portfolio_type": account.get("portfolio_type"), "portfolio_uuid": account.get("portfolio_uuid"),
            "deposits_reconciled": account.get("deposits_reconciled", 0), "days": account.get("days") or {}}


def _kind(text: str) -> str:
    """An alert's kind for the once-per-6-hours push memo: its words without numbers or ids."""
    t = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}|[\d$,.:+\-%]+", "#", text.lower())
    return re.sub(r"\s+", " ", t)[:80]


# ---------------------------------------------------------------- the decision against paper's

def decision_summary(dec, bar_ts: int) -> dict:
    if dec is None:
        return {"bar_ts": bar_ts, "side": None, "direction": None, "gates": {}}
    return {"bar_ts": bar_ts, "side": dec.side.value if dec.side else None, "direction": dec.direction.value,
            "setup": dec.setup, "gates": {g.name: g.verdict.value for g in dec.gates}}


def compare_with_paper(paper_board: dict | None, decisions: dict) -> dict | None:
    """SHADOW's decision at the paper account's newest close against paper's own (its board): the gates that rest on
    the candles alone must agree; the account's, the moment's and the funding log's may differ, and say why."""
    if not paper_board:
        return None
    try:
        bar_ts = int(paper_board["chart"]["candles"][-1][0])
        gates = {g["name"]: g["verdict"] for g in paper_board["analysis"]["gates"]}
        direction = paper_board["analysis"].get("direction")
    except (KeyError, IndexError, TypeError, ValueError):
        return {"ok": None, "note": "the paper board handed over isn't readable"}
    mine = decisions.get(str(bar_ts)) or decisions.get(bar_ts)
    if mine is None:
        return {"ok": None, "bar_ts": bar_ts, "note": f"SHADOW didn't decide the {_utc(bar_ts + 7200_000, '%H:%M')} "
                                                       f"UTC close paper's board shows (not processed yet)"}
    differs = [g for g in SIGNAL_GATES if g in gates and g in mine["gates"] and gates[g] != mine["gates"][g]]
    if direction is not None and mine.get("direction") is not None and direction != mine["direction"]:
        differs.insert(0, "direction")
    own = sorted({OWN_GATES[g] for g in OWN_GATES if g in gates and g in mine["gates"] and gates[g] != mine["gates"][g]})
    close = _utc(bar_ts + 7200_000, "%H:%M UTC")
    if differs:
        note = (f"SHADOW and paper decided the {close} close differently on the candles alone ({', '.join(differs)}): "
                f"their candles from Coinbase differ")
    else:
        note = f"SHADOW's decision at the {close} close matches paper's on the candles" + (
            f"; it differs only by {', '.join(own)}" if own else "")
    return {"ok": not differs, "bar_ts": bar_ts, "differs": differs, "own": own, "note": note,
            "paper": {k: gates.get(k) for k in SIGNAL_GATES if k in gates},
            "shadow": {k: mine["gates"].get(k) for k in SIGNAL_GATES if k in mine["gates"]}}


# ---------------------------------------------------------------- one check of the live loop, once

class OneShot:
    """runner.run's loop body, one pass, on a venue Desk (paper's Coinbase rules), for SHADOW and LIVE alike."""

    def __init__(self, p, mode: str, broker, desk, feed, rules, vstate: dict, meta: dict, recorded: list,
                 kills: Kills, kills_set: dict, log, alert, now: int, fresh_ms: int = ONESHOT_FRESH_MS):
        self.p, self.mode, self.broker, self.desk, self.feed, self.rules = p, mode, broker, desk, feed, rules
        self.vstate, self.meta, self.recorded = vstate, meta, recorded
        self.kills, self.kills_set, self.log, self.alert, self.now, self.fresh_ms = kills, kills_set, log, alert, now, \
            fresh_ms
        self.decisions: dict = {}
        self.processed = 0
        self.entry_kill_why: str | None = None
        self.unexplained = False

    def set_kill(self, name: str, why: str) -> None:
        if not getattr(self.kills, name):
            setattr(self.kills, name, True)
        self.kills_set[name] = True
        if name == "entry_kill":
            self.entry_kill_why = why

    def spy(self) -> None:
        orig = self.desk.on_bar_close

        def on_bar_close(f, i, ctx=None, now_ms=None):
            out = orig(f, i, ctx=ctx, now_ms=now_ms)
            self.decisions[str(f.bars[i].ts)] = decision_summary(self.desk.last_decision, f.bars[i].ts)
            self.processed += 1
            return out

        self.desk.on_bar_close = on_bar_close

    def run(self, bars, f, ctx, mid: float, cut: int | None) -> None:
        from . import runner

        b, desk, now, p = self.broker, self.desk, self.now, self.p
        desk.kills = self.kills
        b.check_stop_price(mid, now)            # sync: the exchange (SHADOW: the virtual book) is the truth
        self.take_broker_flags()
        if b.close_wanted and b.position is not None:
            reason = b.close_wanted
            try:
                t = b.retry_close(mid, now)
                if t is not None:
                    desk.say(now, "fills", f"the {reason.replace('_', ' ')} exit went through on a retry @ "
                                           f"{t.exit_price:,.2f}, net {t.net_pnl:+,.2f}")
            except Exception as exc:
                self.log(f"close retry ({reason}) failed: {type(exc).__name__}: {exc}")
                self.set_kill("entry_kill", f"the {reason} close failed again ({type(exc).__name__})")
        if self.kills.flat_kill and b.position is not None:
            desk.flatten_now(mid, now, "flat_kill")
        if b.position is not None and not b.position.stop:
            a = f.atr[-1] or 0.0
            b.update_stop(b.position.entry_price - b.position.side.sign * p.strategy.stop_atr * a)
            if b.position is not None and b.stop_order_id:
                desk.say(now, "risk", f"position had no stop. placed one at {b.position.stop:,.0f}"
                                      + (" (simulated)." if self.mode == "shadow" else "."))
            elif b.position is not None:
                desk.say(now, "risk", f"position had no stop and none could be placed this run (wanted at "
                                      f"{b.position.stop:,.0f}).")
        self.spy()
        if not self.meta["last_bar_ts"]:
            # day one: show the newest close, trade from the next one (as paper and live do)
            self.meta["last_bar_ts"] = bars[-1].ts
            eq = b.equity(mid)
            desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                                ctx=ctx, now_ms=now, venue=self.rules)
            self.decisions[str(bars[-1].ts)] = decision_summary(desk.last_decision, bars[-1].ts)
            desk.say(now, "chief", f"{self.mode.upper()} opened on Coinbase ({PRODUCT}): the real account, "
                                   + ("read only, orders simulated. " if self.mode == "shadow" else "real orders. ")
                                   + "first decision on the next 2h close.")
        elif not getattr(b, "balance_confirmed", True):
            self.log(f"bar close waits: the balance isn't confirmed ({b.unconfirmed_why})")
        else:
            new = [k for k, bar in enumerate(bars) if bar.ts > self.meta["last_bar_ts"] and (cut is None or bar.ts < cut)]
            if new and bars[0].ts > self.meta["last_bar_ts"]:
                raise Stop("state", f"the last 2h close processed was {_utc(self.meta['last_bar_ts'] + 7200_000)}, "
                                    f"older than the {len(bars)}-candle history: not trading on a stale view. Check the "
                                    f"account by hand", code=2)
            if new:
                runner.process_closed_bars(desk, b, f, new, ctx=ctx, now=now, mid=mid, meta=self.meta, log=self.log,
                                           fresh_ms=self.fresh_ms)
        self.take_broker_flags()
        if desk.last_decision is None:
            # nothing decided this run (no new close): the newest close, judged again for the board, as paper does
            eq = b.equity(mid)
            desk.last_decision = evaluate_entry(p, f, len(bars) - 1, equity=eq, kills=desk.kill_gates(now, eq),
                                                ctx=ctx, now_ms=now, venue=self.rules)
            self.decisions.setdefault(str(bars[-1].ts), decision_summary(desk.last_decision, bars[-1].ts))

    def take_broker_flags(self) -> None:
        """What the broker asks entries to stop for (alerted already): a move nothing explains, a flow priced late
        with a position open, an order-path problem."""
        b = self.broker
        if getattr(b, "flow_gap", None):
            self.unexplained = True
            self.set_kill("entry_kill", b.flow_gap)
            b.flow_gap = None
        if getattr(b, "entry_kill_why", None):
            self.set_kill("entry_kill", b.entry_kill_why)
            b.entry_kill_why = None


# ---------------------------------------------------------------- flows: money in and out, the opening, the emptying

def make_on_flow(desk, broker, meta: dict, recorded: list, account: dict, say_bank, log, push, mode: str,
                 applied: list | None = None):
    """runner.run's on_flow, plus what an account that starts at $0 needs: while it isn't open (start_equity 0), the
    first money in opens it (the start, not a flow, not profit); a withdrawal that leaves at most a cent while flat
    closes the period (money out, not a loss: no drawdown halt), and the next deposit opens a new one. Every flow is
    handled once, by id (meta["handled"], kept past a closed period's archive). `applied` gets what this run took."""
    title = f"{COIN} {mode.upper()}"
    applied = applied if applied is not None else []

    def handled(fid: str | None, amount: float = 0.0, what: str = "") -> None:
        if fid:
            meta["handled"] = (list(meta.get("handled") or []) + [fid])[-2000:]
            applied.append({"id": fid, "amount": amount, "what": what})

    def open_account(amount: float, ts: int, what: str, fid: str | None) -> None:
        meta["start_equity"] = amount
        meta["opened"] = {"ts": ts, "amount": amount, "what": what, "date": _day(ts), "flow": fid}
        r = desk.risk
        r.peak_equity, r.day_start_equity, r.day = amount, amount, _day(ts)
        r.halted, r.halt_reason, r.day_locked = False, "", ""
        text = (f"ACCOUNT OPENED with {_usd(amount)} ({what}, {_day(ts)}) — money in, not profit. {mode.upper()} "
                f"measures performance from here.")
        say_bank(text)
        push(f"{title}: account opened", text, "high", "moneybag")

    def close_period(fl: dict) -> None:
        meta.setdefault("periods", []).append({"start_equity": meta["start_equity"], "opened": meta.get("opened"),
                                               "closed": {"ts": fl["ts"], "amount": fl["amount"]},
                                               "flows": list(recorded)})
        meta["periods"] = meta["periods"][-20:]
        meta["start_equity"], meta["opened"] = 0.0, None
        recorded.clear()
        r = desk.risk
        r.peak_equity = r.day_start_equity = 0.0
        r.halted, r.halt_reason = False, ""
        text = (f"ACCOUNT EMPTIED: a {fl['what']} of {_usd(-fl['amount'])} took the account to "
                f"{_usd(fl['equity_after'])} — money out, not a loss (no drawdown halt). The next deposit opens it again.")
        say_bank(text)
        push(f"{title}: WITHDRAWAL", text, "high", "moneybag")

    def on_flow(fl: dict) -> None:
        if fl["id"] in {r["id"] for r in recorded} or fl["id"] in (meta.get("handled") or []) or \
                fl.get("read_ts", fl["ts"]) < meta["started_ts"]:
            return
        before = fl.get("equity_before", fl["equity_after"] - fl["amount"])
        if meta["start_equity"] <= 0:
            if fl["amount"] > 0 and fl["equity_after"] > 0:
                open_account(fl["equity_after"], fl["ts"], fl["what"], fl["id"])
                account["opening_deposit"] = fl["id"]
            else:
                say_bank(f"a {fl['what']} of {_usd(abs(fl['amount']))} on an account that isn't open: not recorded")
            handled(fl["id"], fl["amount"], fl["what"])
            return
        if fl["amount"] < 0 and fl["equity_after"] <= 0.01 and broker.position is None:
            close_period(fl)
            handled(fl["id"], fl["amount"], fl["what"])
            return
        when = _day(fl["ts"])
        try:
            rec = flows_mod.record(flows_mod.Flow(fl["id"], when, fl["ts"], fl["amount"], "exchange", fl["what"]),
                                   fl["ts"], before, before, meta["start_equity"], recorded)
        except flows_mod.FlowsError as exc:
            say_bank(f"{exc}: not recorded, so the brakes count it as profit or loss. Check the account")
            push(f"{title}: ALERT", f"{exc}: not recorded. Check the account", "high", "warning")
            return
        flows_mod.rescale_brakes(desk.risk, before, fl["amount"])
        recorded.append(rec)
        handled(fl["id"], fl["amount"], fl["what"])
        if str(fl["id"]).startswith("unexplained-"):
            meta["unexplained"] = (list(meta.get("unexplained") or []) +
                                   [{"id": fl["id"], "ts": fl["ts"], "amount": fl["amount"],
                                     "text": "booked as money in or out, not profit or loss: nothing the desk can read "
                                             "explains it"}])[-50:]
        text = flows_mod.describe(rec)
        say_bank(text)
        push(f"{title}: {'DEPOSIT' if fl['amount'] > 0 else 'WITHDRAWAL'}", text, "default", "moneybag")
        log(f"flow recorded: {fl['id']}")

    return on_flow, open_account


# ---------------------------------------------------------------- the real account's reads, as a checklist

ED25519_REFUSED = ("COINBASE_CDP_KEY is an Ed25519 key: Coinbase's API key page says Ed25519 isn't supported by the "
                   "Coinbase App APIs (the deposits and withdrawals the desk reads). Create the key again with "
                   "Advanced Settings, signature algorithm ECDSA (docs/coinbase-setup.md)")


class Checklist:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, key: str, label: str, ok, note: str = "") -> bool:
        self.rows = [r for r in self.rows if r["key"] != key] + [{"key": key, "label": label, "ok": ok, "note": note}]
        return bool(ok)

    def ordered(self) -> list[dict]:
        order = list(CHECKS) + ["view_only"]
        return sorted(self.rows, key=lambda r: order.index(r["key"]) if r["key"] in order else len(order))

    def lines(self) -> list[str]:
        mark = {True: "OK  ", False: "FAIL", None: "—   "}
        return [f"{mark[r['ok']]} {r['label']}" + (f": {r['note']}" if r["note"] else "") for r in self.ordered()]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["ok"] is True)


CHECKS = {
    "answered": "Coinbase answered",
    "key": "Key accepted (signed request)",
    "view_only": "Key is View only (can't trade, can't move money)",
    "clock": "Clock within 30 s of Coinbase",
    "balance": "Futures balance read",
    "adds_up": "Balance adds up (spot + futures + open P&L = available margin + holds)",
    "positions": "Positions and open orders read (none the desk didn't place)",
    "fills": "Fills read",
    "fees": "Fee tier read (the per-contract futures fee isn't in it; fees stay the estimate)",
    "flows": "Deposits and withdrawals readable",
    "decided": "Desk decided on Coinbase's candles",
    "paper": "Decision matches paper's at the same close",
}


def _optional(fn, log, what: str):
    from .brokers.coinbase_rest import CoinbaseError

    try:
        return fn(), None
    except CoinbaseError as exc:
        log(f"{what}: {type(exc).__name__}: {exc}")
        return None, type(exc).__name__


def read_extras(real, account: dict, now: int, log) -> dict:
    """The reads only the board shows: the fee tier, sweeps, the intraday margin setting and window, real fills."""
    rest = real.rest
    out: dict = {}
    out["fees"], out["fees_err"] = _optional(real.fee_tier, log, "fee tier")
    sw, _ = _optional(lambda: rest.get(f"{V3}/cfm/sweeps"), log, "sweeps")
    out["sweeps"] = None if sw is None else [{"status": s.get("status"), "amount": (s.get("requested_amount") or {})
                                              .get("value"), "all": s.get("should_sweep_all")}
                                             for s in sw.get("sweeps") or []]
    ms, _ = _optional(lambda: rest.get(f"{V3}/cfm/intraday/margin_setting"), log, "margin setting")
    out["margin_setting"] = (ms or {}).get("setting")
    mw, _ = _optional(lambda: rest.get(f"{V3}/cfm/intraday/current_margin_window"), log, "margin window")
    out["margin_window"] = ((mw or {}).get("margin_window") or {}).get("margin_window_type")
    since = account.get("fills_cursor") or now - FIRST_FILLS_MS
    fills, err = _optional(lambda: real.fetch_my_trades(PRODUCT, since), log, "fills")
    out["fills_err"] = err
    out["real_fills"] = None if fills is None else [
        {"side": t["side"], "contracts": t["amount"], "price": t["price"], "fee": (t.get("fee") or {}).get("cost"),
         "ts": t["timestamp"]} for t in fills][-20:]
    if fills is not None:
        account["fills_cursor"] = now - 3600_000        # overlap an hour: a fill Coinbase lists late isn't missed
    return out


# ---------------------------------------------------------------- the run

class Run:
    """Everything one call does, for SHADOW (mode "shadow") or LIVE ("live")."""

    def __init__(self, state_dir, env, *, mode: str = "shadow", base_url: str | None = None, clock=time.time,
                 sleep=time.sleep, push=None, mask_emit=print, paper_board: dict | None = None,
                 deadline_s: float = DEADLINE_S, iterations: int | None = None, stray=None):
        from .brokers.coinbase_rest import DEFAULT_BASE

        self.stray = stray                  # the CLI's buffer of anything printed: kept in the encrypted state only
        self.mode = mode
        self.dir = Path(state_dir) / COIN
        self.env = env
        self.base_url = base_url or DEFAULT_BASE
        self.clock, self.sleep = clock, sleep
        self._push = push
        self.mask_emit = mask_emit
        self.paper_board = paper_board
        self.deadline_s = deadline_s
        self.iterations = iterations
        self.p = params_for(COIN)
        self.lines: list[str] = []
        self.alerts: list[str] = []
        self.checks = Checklist()
        self.problem: str | None = None
        self.code = 0
        self.changed = False
        self.public_note = ""
        self.public_why: str | None = None
        self.extras: dict = {}
        self.events_said: list[str] = []
        self.board_state: dict | None = None
        self.decision = None
        self.would_send_now: list[dict] = []
        self.paper_check = None
        self.what_if = None
        self.min_equity = None
        self.rest = None
        self.real = None
        self.shadow = None
        self.broker = None
        self.desk = None
        self.meta: dict = {}
        self.recorded: list = []
        self.account = new_account()
        self.kills_set: dict = {}
        self._urgent_texts: set[str] = set()
        self.unexplained: list[dict] = []
        self.kills_read: tuple[dict, str | None] | None = None
        self.kills_named: tuple = ()        # the switches BTC/kills.json names (an unreadable one's, read failing closed)
        self.kills_strays: list = []        # files of yours that look like kill switches: [(path, KillsRead)]
        self.kill_var_odd: str | None = None    # DESK_COINBASE_KILL set to a word that isn't entry or flat
        self.loaded = None                  # the branch as this run loaded it (coinbase_state.Loaded)
        self.save_refused_why: str | None = None
        self.live_leftover = False          # SHADOW: a never-armed LIVE attempt's files are here (dropped on save)
        self.replaces_leftover = False      # LIVE: the saved LIVE state is one, and this run doesn't carry on from it
        self.refused_before_state = False
        self.now = int(clock() * 1000)

    # ---- small things
    def log(self, text: str) -> None:
        self.lines.append(f"{_utc(int(self.clock() * 1000), '%H:%M:%S')} {text}")

    def push(self, title: str, body: str, priority: str = "default", tags: str = "robot") -> None:
        """To the phone. Without NTFY_TOKEN the topic is readable by anyone who knows its name: no amounts."""
        if not (self.env.get("NTFY_TOKEN") or "").strip():
            clean = redact_amounts(body)
            body = clean if clean == body else f"{clean}\n\n{REDACTED_NOTE.format(mode=self.mode.upper())}"
        if self._push is not None:
            self._push(title, body, priority, tags)
        else:
            ntfy_push(title, body, priority, tags, self.env)

    def on_urgent(self, kind: str, text: str) -> None:
        """The broker's urgent cases (a position with no stop, a stop with no position, an order on the product that
        isn't the desk's). LIVE: an urgent push, every time (real money). SHADOW: an alert (a simulated book)."""
        if self.mode == "live":
            self._urgent_texts.add(text)
            self.push(f"{COIN} LIVE: {URGENT_TITLES.get(kind, 'URGENT')}", text, "urgent", "rotating_light")

    def push_alert(self, text: str) -> None:
        memo = self.account.setdefault("pushed", {})
        key = "alert:" + _kind(text)
        if self.now - int(memo.get(key) or 0) >= PUSH_EVERY_MS:
            memo[key] = self.now
            self.push(f"{COIN} {self.mode.upper()}: ALERT", text, "high", "warning")
        memo.update({k: v for k, v in memo.items() if self.now - int(v or 0) < 7 * DAY_MS})

    def on_alert(self, text: str) -> None:
        self.alerts.append(text)
        self.log(f"ALERT: {text}")
        if self.desk is not None:
            self.desk.say(self.now, "risk", f"ALERT: {text}")
        if text not in self._urgent_texts:        # pushed already, urgently
            self.push_alert(text)

    @property
    def paths(self) -> dict:
        m = self.mode
        return {"state": self.dir / f"state-{m}.enc.json", "board": self.dir / f"board-{m}.enc.json",
                "status": self.dir / "status.json", "kills": self.dir / "kills.json",
                "kills_set": self.dir / "kills.set.json"}

    # ---- before anything: what refuses with no network call
    def preflight(self) -> tuple[str, str]:
        live = self.mode == "shadow" and (self.dir / "state-live.enc.json").exists()
        runs = "LIVE runs on this account; SHADOW would read its trades as moves nothing explains. Not running."
        unsure = ("a LIVE state is on this account and {}, so SHADOW can't tell whether LIVE runs here; it doesn't run "
                  "beside a LIVE that might (it would read its trades as moves nothing explains). Not running.")
        try:
            password = statebox.check_password(self.env.get("DESK_STATE_KEY"))
        except statebox.BoxError as exc:
            if live:
                raise Stop("refused", unsure.format("DESK_STATE_KEY isn't set right")) from None
            short = bool(self.env.get("DESK_STATE_KEY"))
            raise Stop("key", str(exc), public="DESK_STATE_KEY is too short (it needs thirty-two characters or more)"
                       if short else "DESK_STATE_KEY is not set") from None
        if live:
            # LIVE counts only once it has armed: a LIVE attempt that refused before it ever armed is no LIVE start
            try:
                armed = armed_at(open_bundle(self.dir / "state-live.enc.json", password, "live"))
            except ValueError:          # statebox.BoxError and WrongKey included: a LIVE state SHADOW can't read
                raise Stop("refused", unsure.format("DESK_STATE_KEY doesn't open it")) from None
            if armed:
                raise Stop("refused", runs)
            self.live_leftover = True
            self.log("the LIVE state here is a LIVE attempt's that never armed: no LIVE start, so SHADOW runs, and "
                     "this save drops its files from coinbase-state")
        raw = self.env.get("COINBASE_CDP_KEY") or ""
        if not raw.strip():
            raise Stop("key", "COINBASE_CDP_KEY is not set: SHADOW needs the View-only key's file (README, Coinbase "
                              "SHADOW)", public="COINBASE_CDP_KEY is not set")
        return password, raw

    def branch_loaded(self):
        """The coinbase-state branch's BTC/ as the load step extracted it here (taken before this run writes)."""
        if self.loaded is None:
            try:
                self.loaded = coinbase_state.Loaded(self.dir.parent)
            except OSError as exc:
                self.log(f"the state dir couldn't be read as loaded ({type(exc).__name__})")
                self.loaded = coinbase_state.Loaded(None)
        return self.loaded

    def kills_file(self) -> tuple[dict, str | None]:
        """BTC/kills.json (yours, on the coinbase-state branch), read once a run the way the save step reads it
        (coinbase_state.kills_read, on the same bytes: a symlink is its target, not plain): one the desk can't read is
        entry_kill on and each switch it names on unless it says off, never every switch off. A file of yours that
        looks like a kill switch but isn't BTC/kills.json (BTC/Kills.json, say) turns entry_kill on too."""
        if self.kills_read is None:
            b = self.branch_loaded()
            got = coinbase_state.kills_read(*coinbase_state.branch_kills(b))
            kills = dict(got.kills)
            self.kills_named = got.named
            tree = b.tree()
            self.kills_strays = [(p, coinbase_state.kills_read(b.show(p), tree[p] != coinbase_state.LINK))
                                 for p in coinbase_state.yours(tree) if coinbase_state.kill_like(p)]
            if self.kills_strays:
                kills["entry_kill"] = True
            self.kills_read = (kills, got.why)
        return self.kills_read

    def kills_now(self) -> Kills:
        d, _ = self.kills_file()
        k = Kills(**{n: bool(d.get(n)) for n in coinbase_state.KILL_KEYS})
        v = (self.env.get("DESK_COINBASE_KILL") or "").strip()
        w = re.sub(r"[^a-z]", "", v.lower())
        if w in KILL_VAR_FLAT:
            k.flat_kill = True
        elif w in KILL_VAR_ENTRY:
            k.entry_kill = True
        elif v:
            k.entry_kill = True             # failing closed: a word it doesn't know is entry_kill, and alerted
            self.kill_var_odd = v[:40]
        return k

    # ---- the key, the clock, the permissions
    def client_for(self, key):
        """SHADOW's client: read only (CoinbaseREST refuses every POST without an Armed token)."""
        from .brokers.coinbase_rest import CoinbaseREST

        return CoinbaseREST(key, base_url=self.base_url, clock=self.clock, sleep=self.sleep,
                            deadline_s=self.clock() + self.deadline_s, emit=self.mask_emit, env=self.env)

    def connect(self):
        from .brokers.coinbase import CoinbaseExchange
        from .brokers.coinbase_auth import CdpKeyError, load_cdp_key, mask_for_actions
        from .brokers.coinbase_rest import ClockSkewError, CoinbaseAuthError, CoinbaseError, CoinbasePermissionError

        try:
            key = load_cdp_key(self.env.get("COINBASE_CDP_KEY"))
        except CdpKeyError as exc:
            self.checks.add("key", CHECKS["key"], False, str(exc))
            raise Stop("key", str(exc), public="COINBASE_CDP_KEY isn't a Coinbase key file") from None
        mask_for_actions(key, emit=self.mask_emit, env=self.env)
        if key.alg != "ES256":
            # https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication: "Do NOT
            # select Ed25519 as it is not supported by Coinbase App APIs" (the v2 deposits and withdrawals read here)
            self.checks.add("key", CHECKS["key"], False, ED25519_REFUSED)
            raise Stop("key", ED25519_REFUSED, public="COINBASE_CDP_KEY isn't an ECDSA key")
        self.rest = self.client_for(key)
        try:
            skew = self.rest.server_skew()
            self.checks.add("answered", CHECKS["answered"], True)
            self.checks.add("clock", CHECKS["clock"], True, f"{-skew + 0.0:+.1f} s")
        except ClockSkewError as exc:
            self.checks.add("answered", CHECKS["answered"], True)
            self.checks.add("clock", CHECKS["clock"], False, str(exc))
            raise Stop("clock", str(exc)) from None
        except CoinbaseError as exc:
            self.checks.add("answered", CHECKS["answered"], False, type(exc).__name__)
            self.checks.add("clock", CHECKS["clock"], None, "not checked: Coinbase didn't answer")
            raise Stop("read", f"Coinbase didn't answer ({type(exc).__name__})", code=1) from None
        self.now = int(self.rest.now_s() * 1000)
        self.real = CoinbaseExchange(self.rest, memo=self.account.setdefault("adapter", {}), on_alert=self.on_alert)
        try:
            perms = self.real.key_permissions()
        except CoinbaseAuthError as exc:
            self.checks.add("key", CHECKS["key"], False, str(exc))
            health(self.account, self.now, auth_fail=1)
            raise Stop("auth", str(exc), code=1) from None
        except CoinbasePermissionError as exc:
            self.checks.add("key", CHECKS["key"], True)
            raise Stop("permissions", f"{exc}: the key needs the View permission", code=2) from None
        except CoinbaseError as exc:
            raise Stop("read", f"the key's permissions couldn't be read ({type(exc).__name__})", code=1) from None
        self.checks.add("key", CHECKS["key"], True, f"…{key.tail}")
        self.account["portfolio_type"] = perms.get("portfolio_type")
        self.account["portfolio_uuid"] = perms.get("portfolio_uuid")
        return key, perms

    def check_permissions(self, perms: dict) -> None:
        if not perms["can_view"]:
            self.checks.add("view_only", CHECKS["view_only"], False, "the key can't view")
            raise Stop("permissions", "this key can't View: SHADOW needs a key with View only")
        if perms["can_trade"] or perms["can_transfer"]:
            what = " and ".join(w for w, on in (("trade", perms["can_trade"]), ("move money", perms["can_transfer"]))
                                if on)
            self.checks.add("view_only", CHECKS["view_only"], False, f"it can {what}")
            raise Stop("permissions", f"SHADOW only runs with a View-only key: this one can {what}. Create a key with "
                                      f"View only and put it in COINBASE_CDP_KEY.")
        self.checks.add("view_only", CHECKS["view_only"], True,
                        "portfolio " + str(perms.get("portfolio_type") or "not said"))
        if perms.get("portfolio_type") != "DEFAULT":
            self.on_alert(f"the key's portfolio is {perms.get('portfolio_type') or 'not said'}, not DEFAULT: US futures "
                          f"live in the Default (Primary) portfolio. Create the key on that portfolio")

    def safety_pass(self, work: Path) -> None:
        """LIVE only (LiveRun): the position's protection, before any market read. SHADOW has nothing real to
        protect."""

    def reset_halt_if_asked(self) -> None:
        """BTC/kills.json's reset_halt_at (any new value, written by you on the coinbase-state branch): clear the
        drawdown halt once, as `python -m desk reset-halt` does for the other desks, and say so on the board."""
        raw, why = self.kills_file()
        if why:
            return              # nothing in a kills.json the desk can't read is acted on but entry_kill
        want = raw.get("reset_halt_at")
        if not want or want == self.meta.get("reset_halt_seen"):
            return
        self.meta["reset_halt_seen"] = want
        r = self.desk.risk
        was = r.halt_reason
        self.desk.reset_halt(self.broker.cash)
        r.day_locked = ""
        self.desk.say(self.now, "risk", f"halt reset from kills.json (reset_halt_at {want})"
                      + (f": it was halted ({was})" if was else ": it wasn't halted") +
                      ". Peak equity restarts from the balance now.")

    # ---- the market: paper's venue rules, on the authenticated reads
    def market(self, work: Path):
        from .brokers.coinbase_rest import CoinbaseError
        from .brokers.live import LiveSetupError
        from .feeds.coinbase import FeedError
        from .paper import FUNDING_WINDOW_MS, LOOKBACK_BARS, LOOKBACK_SPARE

        real = self.real
        real.feed.clock = self.rest.now_s
        saved_v = _read(work, "venue.json")
        last0 = (self.meta or {}).get("last_bar_ts") or 0
        try:
            real.load_markets()
        except LiveSetupError as exc:
            raise Stop("refused", str(exc)) from None
        except (CoinbaseError, FeedError) as exc:
            raise Stop("read", f"Coinbase's product couldn't be read ({type(exc).__name__})", code=1) from None
        vstate = venue_mod.remember(saved_v, real.details, self.now, COINBASE,
                                    keep_from_ms=min(last0, self.now) - FUNDING_WINDOW_MS if last0 else None)
        rules = venue_mod.Rules(COINBASE, vstate["windows"], vstate["margin"])
        feed = real.feed
        feed.log = venue_mod.FundingLog(vstate["funding_log"], COINBASE.funding_log_days)
        feed.sessions, feed.gaps = rules.sessions, list((vstate.get("holes") or {}).get("accepted") or [])
        try:
            raw = feed.candles(start_ms=self.now - (LOOKBACK_BARS + LOOKBACK_SPARE) * feed.bar_ms, closed_only=False)
            raw = raw[-(LOOKBACK_BARS + 1):]
            forming = raw[-1] if raw and raw[-1].ts + feed.bar_ms > self.now else None
            bars = raw[:-1] if forming else raw
            if forming is None:
                bars = bars[-LOOKBACK_BARS:]
            if not bars:
                raise FeedError("no candles")
            since = min(last0 or self.now, self.now) - FUNDING_WINDOW_MS
            funding = feed.funding(max(bars[0].ts, since))
            ctx = feed.context()
        except (CoinbaseError, FeedError) as exc:
            self.checks.add("decided", CHECKS["decided"], False, f"candles: {type(exc).__name__}")
            raise Stop("read", f"Coinbase's candles or book couldn't be read ({type(exc).__name__})", code=1) from None
        return vstate, rules, feed, bars, funding, ctx

    def mark(self, ctx) -> float:
        try:
            bba = self.real.best_bid_ask()
        except Exception:
            bba = None
        if bba:
            return (bba[0] + bba[1]) / 2
        return float(ctx.mid)

    # ---- the whole run
    def execute(self) -> int:
        paths = self.paths
        self.branch_loaded()                # the branch as loaded, before anything here is written
        try:
            password, _ = self.preflight()
        except Stop as s:
            return self.refused_early(s)
        try:
            bundle = open_bundle(paths["state"], password, self.mode)
        except statebox.WrongKey:
            return self.refused_early(Stop("key", f"DESK_STATE_KEY doesn't open the saved {self.mode.upper()} state; "
                                                  f"not starting a new one over it",
                                           public="DESK_STATE_KEY doesn't open the saved state"))
        except statebox.BoxError as exc:
            return self.refused_early(Stop("state", f"the saved {self.mode.upper()} state can't be opened: {exc}"))
        bundle = self.counted(bundle)
        work = Path(tempfile.mkdtemp(prefix=f"desk-{self.mode}-"))
        try:
            unpack(bundle, work)
            self.account = {**new_account(), **(_read(work, "account.json") or {})}
            saved_desk = _read(work, "desk.json") or {}
            self.meta = {"started_ts": self.now, "start_equity": 0.0, "last_bar_ts": 0, "runs": 0, "failed_runs": 0,
                         "mode": self.mode, "product": PRODUCT, "opened": None, **(saved_desk.get("meta") or {})}
            self.recorded = list(saved_desk.get("flows") or [])
            if self.saves_from_the_start():
                self.begin_saving()             # written ahead: from here this run is saved, whatever happens
            ok = False
            try:
                self.body(work, password, saved_desk)
                ok = True
            except Stop as s:
                self.problem, self.code = s.problem, s.code
                self.public_why = s.public
                self.log(f"stopped: {s.text}")
                self.broker_flags()
                self.after_failure()
                self.stopped(s.text)
            except Exception as exc:     # anything else, mid-run: LIVE would be in entry_kill now; so is SHADOW
                from .brokers.coinbase_rest import CoinbaseError, CoinbaseUncertain

                self.problem, self.code = "read", 1
                transient = isinstance(exc, CoinbaseError) and exc.transient and not isinstance(exc, CoinbaseUncertain)
                self.log(f"error: {type(exc).__name__}: {exc}")
                if not transient and self.broker is not None:
                    self.kills_set["entry_kill"] = True
                    self.account["entry_kill_reason"] = {"ts": self.now, "why": f"{type(exc).__name__}: {exc}"}
                self.broker_flags()
                self.after_failure()
                self.stopped(f"{type(exc).__name__} while it ran" + (
                    "; entry_kill is on" if self.kills_set.get("entry_kill") else ""))
            if self.changed:
                self.finish(work, password, ok)
            else:
                github_output(False, self.env)      # LIVE that never armed: nothing to save, so nothing changes
                self.public_note = self.problem or ""
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return self.code

    def counted(self, bundle: dict | None) -> dict | None:
        """The saved state this run carries on from (LiveRun: not one from a LIVE attempt that never armed)."""
        return bundle

    def saves_from_the_start(self) -> bool:
        """Whether this run saves whatever happens (SHADOW: always; LiveRun: once LIVE has armed)."""
        return True

    def begin_saving(self) -> None:
        self.changed = True
        github_output(True, self.env)

    def refused_early(self, s: Stop) -> int:
        """Refused before the state was opened: the saved state stays exactly as it was. A setup slip with the secrets
        (problem "key") still writes the public status (no amount, no digit), so the dashboard says what's wrong
        instead of nothing: the save step then saves the status alone, the state file as loaded."""
        self.refused_before_state = True
        self.problem, self.code = s.problem, s.code
        self.public_why = s.public
        self.log(f"refused: {s.text}")
        wrote = s.problem == "key" and self.status_only(s)
        github_output(wrote, self.env)
        self.changed = False
        self.public_note = s.text
        self.refused_push(s)
        return self.code

    def refused_push(self, s: Stop) -> None:
        """No state to remember a push in: once every 6 hours by the clock (LiveRun: see its own)."""
        if self.now % PUSH_EVERY_MS < 15 * 60_000:
            self.push(f"{COIN} {self.mode.upper()}: not working", f"{self.mode.upper()} didn't run: {s.text}. Nothing "
                                                                  f"was sent. Paper is unaffected.", "high", "warning")

    def saved_status(self) -> dict | None:
        """BTC/status.json as loaded (public, plain: readable without DESK_STATE_KEY), or None."""
        try:
            st = json.loads(self.branch_loaded().show(f"{COIN}/status.json") or b"null")
        except ValueError:
            return None
        return st if isinstance(st, dict) else None

    def status_only(self, s: Stop) -> bool:
        """The public status of a run refused over the secrets, on the last status's counts (same start, same runs:
        the save step's check takes it with the state file unchanged)."""
        p = self.paths["status"]
        old = {}
        if p.exists():
            try:
                old = json.loads(p.read_text())
            except ValueError:
                old = {}
            if not isinstance(old, dict) or old.get("mode") != self.mode.upper():
                return False        # another mode's status: not overwritten from here
        if self.mode == "live" and old.get("armed") is not True:
            return False            # a LIVE that never armed saves nothing, not even a status
        self.dir.mkdir(parents=True, exist_ok=True)
        st = {"v": 1, "mode": self.mode.upper(), "coin": COIN, "product": PRODUCT, "updated": self.now,
              "started_ts": int(old.get("started_ts") or self.now), "runs": int(old.get("runs") or 0),
              "failed_runs": int(old.get("failed_runs") or 0), "ok": False, "problem": "key",
              "key_view_only": None, "decision": None, "would_send_last_24h": None,
              "alerts_open": int(old.get("alerts_open") or 0), "checks_passed": None, "checks_total": None,
              "opened": bool(old.get("opened")), "note": s.public or "a secret isn't set right",
              **({"armed": True} if self.mode == "live" else {})}
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(st, indent=1) + "\n")
        tmp.replace(p)
        return True

    def sent_note(self) -> str:
        """What this run sent, said plainly: SHADOW never sends; LIVE says how many order requests went out."""
        sent = list(getattr(self.rest, "posts_paths", None) or [])
        if self.mode != "live":
            return "Nothing was sent."
        if not sent:
            return "No order request was sent this run."
        return (f"{len(sent)} order request{'s' if len(sent) != 1 else ''} went to Coinbase this run "
                f"({', '.join(sent)}).")

    def broker_flags(self) -> None:
        """What the broker asked entries to stop for before the run stopped (a position it couldn't protect, say)."""
        b = self.broker
        why = getattr(b, "entry_kill_why", None) or getattr(b, "flow_gap", None)
        if why:
            self.kills_set["entry_kill"] = True
            self.account["entry_kill_reason"] = {"ts": self.now, "why": why}

    def after_failure(self) -> None:
        """LIVE, a run that stopped part way: one more read-only look at the protection (the watchdog), each problem
        pushed urgently. SHADOW: nothing real to protect."""
        if self.mode != "live" or self.real is None:
            return
        try:
            with self.real.protecting():
                found = self.real.watchdog()
        except Exception as exc:
            self.log(f"watchdog: {type(exc).__name__}")
            found = [("unprotected", f"LIVE stopped part way and couldn't read {PRODUCT}'s position and orders "
                                     f"back: check in Coinbase that the position (if any) has its stop")] \
                if getattr(self.rest, "posts_paths", None) else []
        for kind, text in found:
            if kind in ("stray", "unprotected") and text not in self._urgent_texts:
                self._urgent_texts.add(text)
                self.push(f"{COIN} LIVE: {URGENT_TITLES[kind]}", f"{text}. LIVE stopped part way this run: "
                                                                 f"{self.sent_note()}", "urgent", "rotating_light")
            self.alerts.append(text)

    def stopped(self, why: str) -> None:
        memo = self.account.setdefault("pushed", {})
        first = self.account.get("problem") is None
        if first or self.now - int(memo.get("not_working") or 0) >= PUSH_EVERY_MS:
            memo["not_working"] = self.now
            self.push(f"{COIN} {self.mode.upper()}: not working",
                      f"{self.mode.upper()} stopped part way this run: {why}. {self.sent_note()} Paper is unaffected.",
                      "high", "warning")

    def body(self, work: Path, password: str, saved_desk: dict) -> None:

        key, perms = self.connect()
        self.check_permissions(perms)
        self.safety_pass(work)
        vstate, rules, feed, bars, funding, ctx = self.market(work)
        self.read_account()
        self.extras = read_extras(self.real, self.account, self.now, self.log)
        self.checks.add("fills", CHECKS["fills"], self.extras.get("fills_err") is None,
                        "" if self.extras.get("fills_err") is None else self.extras["fills_err"])
        fees = self.extras.get("fees")
        self.checks.add("fees", CHECKS["fees"], fees is not None, "" if fees is None else
                        f"{fees.get('tier') or 'tier not named'}, taker {fees.get('taker')}, maker {fees.get('maker')}")
        mid = self.mark(ctx)
        self.mid = mid
        self.build_broker(work, vstate, rules, feed, mid, saved_desk)
        from .paper import _venue_context, _wait_for_holes

        cut, _ = _wait_for_holes(self.desk, vstate, bars, feed.bar_ms, self.now, self.meta["last_bar_ts"], self.log)
        ctx, _ = _venue_context(self.desk, vstate, ctx, self.now, COINBASE)
        f = ind.compute(bars, funding, self.p)
        if f.atr and f.atr[-1]:
            self.meta["last_atr"] = float(f.atr[-1])      # the safety pass's level for a stop with no saved one
        kills = self.kills_now()
        self.kills_loaded = Kills(**kills.__dict__)
        self.say_kills_unreadable()
        self.reset_halt_if_asked()
        shot = OneShot(self.p, self.mode, self.broker, self.desk, feed, rules, vstate, self.meta, self.recorded, kills,
                       self.kills_set, self.log, self.on_alert, self.now)
        try:
            shot.run(bars, f, ctx, mid, cut)
        finally:
            self.say_fills(rules)
            if shot.entry_kill_why:
                self.account["entry_kill_reason"] = {"ts": self.now, "why": shot.entry_kill_why}
            self.shot = shot
            self.decisions_now = shot.decisions
            if self.shadow is not None:
                self.would_send_now = list(self.shadow.sent_this_run)
            _write(work, "venue.json", vstate)
            self.save_desk(work)
        self.checks.add("decided", CHECKS["decided"], True,
                        f"{len(bars)} 2h candles to the {_utc(bars[-1].ts + feed.bar_ms, '%H:%M UTC')} close, "
                        f"{shot.processed} close(s) decided this run")
        dec = self.desk.last_decision
        self.decision = dec
        self.account["decisions"] = dict(sorted({**self.account.get("decisions", {}), **shot.decisions}.items())[-24:])
        self.paper_check = compare_with_paper(self.paper_board, self.account["decisions"])
        if self.paper_check is not None:
            self.checks.add("paper", CHECKS["paper"], self.paper_check.get("ok"), self.paper_check.get("note", ""))
            if self.paper_check.get("ok") is False:
                self.on_alert(self.paper_check["note"])
        self.size_story(f, ctx, rules)
        self.board_state = self.board(f, ctx, vstate, feed, bars, rules)
        health(self.account, self.now, funded=bool((self.real_equity or 0) > 0 and self.real.balance_block is None
                                                   and self.broker.balance_confirmed))

    def say_kills_unreadable(self) -> None:
        """Every kill switch the desk can't read as written fails closed (entry_kill on), and is said on the board, in
        the status and on the phone (each kind at most every 6 hours): a BTC/kills.json it can't read (kept as you
        wrote it, so every run reads it the same way until you fix it), a file of yours that looks like a kill switch
        but isn't BTC/kills.json, and a DESK_COINBASE_KILL that isn't entry or flat."""
        d, why = self.kills_file()
        if why:
            parts = [(f"BTC/kills.json on the coinbase-state branch can't be read ({why}). Read failing closed: "
                      f"entry_kill on, no new entries")]
            if d.get("flat_kill"):
                parts.append("flat_kill on too: any position is closed, and stays closed")
            elif "flat_kill" in self.kills_named:
                parts.append("flat_kill is in it but says off, so nothing is closed (for a sure close, set the "
                             "variable DESK_COINBASE_KILL to flat)")
            if d.get("jev_kill"):
                parts.append("jev_kill on too")
            parts.append("nothing else in it is acted on (reset_halt_at included). It's kept as you wrote it, so every "
                         "run reads it this way, and says so, until you fix it")
            if not self.account.get("entry_kill_reason"):
                self.account["entry_kill_reason"] = {"ts": self.now, "why": f"BTC/kills.json couldn't be read ({why})"}
            self.on_alert("; ".join(parts))
        for path, got in self.kills_strays:
            flat = got.kills.get("flat_kill") and "flat_kill" in got.named
            self.on_alert(f"{path} on the coinbase-state branch looks like a kill switch, but the desk reads only "
                          f"BTC/kills.json (that name, all lowercase): entry_kill on, no new entries, until you rename "
                          f"it to that or delete it" + (
                              "; it says flat_kill, which isn't acted on from there: put it in BTC/kills.json, or set "
                              "the variable DESK_COINBASE_KILL to flat for a sure close" if flat else ""))
            if not self.account.get("entry_kill_reason"):
                self.account["entry_kill_reason"] = {"ts": self.now, "why": f"{path} looks like a kill switch"}
        if self.kill_var_odd is not None:
            self.on_alert(f"the variable DESK_COINBASE_KILL is {self.kill_var_odd!r}, which isn't entry or flat: read "
                          f"as entry (no new entries). Set it to flat to close the position, or delete it to turn it "
                          f"off")

    # ---- the real account's reads
    def read_account(self) -> None:
        from .brokers.coinbase_rest import CoinbaseError

        real = self.real
        try:
            real.fetch_balance()
            b = real.balance
            self.checks.add("balance", CHECKS["balance"], True,
                            f"total USD {_usd(b['total'])} (spot {_usd(b['cbi'])} + futures {_usd(b['cfm'])}), open "
                            f"P&L {_usd(b['upnl'])}")
        except CoinbaseError as exc:
            self.checks.add("balance", CHECKS["balance"], False, type(exc).__name__)
            raise Stop("read", f"the futures balance couldn't be read ({type(exc).__name__})", code=1) from None
        if real.balance_block:
            self.checks.add("adds_up", CHECKS["adds_up"], False, real.balance_block)
            health(self.account, self.now, equity_mismatch=1)
        else:
            self.checks.add("adds_up", CHECKS["adds_up"], True, f"off by {_usd(abs(b['check_diff']))}" +
                            (" (part of it can't be margin yet: an ACH hold?)" if real.usable is not None and
                             real.usable < b["equity"] - 1 else ""))
        try:
            wallets = real.wallets()
            self.usdc = wallets["usdc"]
            if wallets["usdc"]:
                self.on_alert(f"{wallets['usdc']:,.2f} USDC sits in the key's portfolio: Coinbase can pull it into "
                              f"futures margin. LIVE won't arm with USDC there: move it to another portfolio")
        except CoinbaseError as exc:
            self.usdc = None
            self.checks.add("flows", CHECKS["flows"], False, f"the USD wallet: {type(exc).__name__}")
            health(self.account, self.now, flows_unreadable=1)
        prev = self.account.get("last_real")
        self.real_before = (prev or {}).get("equity")
        self.account["last_real"] = {"ts": self.now, "equity": b["equity"]}

    @property
    def real_equity(self) -> float | None:
        b = self.real.balance if self.real is not None else None
        return None if b is None else b["equity"]

    # ---- the broker (LIVE's own), on the overlay or the armed client
    def exchange(self, rules, feed):
        from .brokers.coinbase_shadow import ShadowCoinbase

        book = None
        p = self.work / COIN / "book.json"
        if p.exists():
            book = json.loads(p.read_text())
        self.shadow = ShadowCoinbase(self.real, book, rules=rules, fee_rate=self.p.costs.taker_fee_bps / 1e4,
                                     funding_rate=feed.log.rate_at, log=self.log)
        self.shadow.refresh_real()
        return self.shadow

    def build_broker(self, work: Path, vstate, rules, feed, mid: float, saved_desk: dict) -> None:
        from .brokers.live import LiveBroker

        self.work = work
        ex = self.exchange(rules, feed)
        f = ex.foreign if hasattr(ex, "foreign") else {}
        self.checks.add("positions", CHECKS["positions"], not (f.get("positions") or f.get("orders")),
                        "none" if not (f.get("positions") or f.get("orders")) else
                        f"{len(f.get('positions') or [])} position(s), {len(f.get('orders') or [])} order(s) the desk "
                        f"didn't place: no entries while they're there")
        if self.shadow is not None:
            self.shadow.set_mark(mid, self.now)
            for kind, text in self.shadow.advance(self.now):
                self.events_said.append((kind, text))
        saved = _read(work, "live_position.json") or {}
        self.trades0 = len(saved.get("trades") or [])
        self.pos0 = (saved.get("position") or {}).get("entry_ts")
        clock_ms = lambda: int(self.rest.now_s() * 1000)        # noqa: E731
        self.broker = LiveBroker(self.p, exchange=ex, state_path=work / COIN / "live_position.json",
                                 max_notional_usd=self.max_notional(), log=self.log, sleep=self.sleep, clock=clock_ms,
                                 allow_empty=self.mode == "shadow", unexplained_wait_ms=UNEXPLAINED_WAIT_MS,
                                 provisional_tries=PROVISIONAL_TRIES, rules=rules,
                                 market_closed=lambda: rules.sessions.closure(clock_ms()) is not None,
                                 unexplained_as_flow=self.mode == "shadow")
        b = self.broker
        b.on_urgent = self.on_urgent
        if self.meta.get("runs", 0) == 0 and not self.meta.get("start_equity") and b.cash > 0:
            self.meta["start_equity"] = b.cash
            self.meta["opened"] = {"ts": self.now, "amount": b.cash, "what": f"the balance when {self.mode.upper()} "
                                   f"started", "date": _day(self.now), "flow": None}
        desk = Desk(self.p, b, jev=None, venue=rules)
        if saved_desk:
            desk.load_dict(saved_desk)
        self.desk = desk
        for _, ev in self.events_said:
            desk.say(self.now, "risk", f"while away: {ev}")
        for ev in getattr(self, "safety_said", []):
            desk.say(self.now, "risk", f"safety pass: {ev}")
        if self.live_leftover:
            desk.say(self.now, "chief", "a LIVE attempt that never armed had left its files on coinbase-state: they "
                                        "aren't a LIVE start, so SHADOW runs on, and this save drops them")
        self.applied: list[dict] = []
        on_flow, self.open_account = make_on_flow(desk, b, self.meta, self.recorded, self.account,
                                                  lambda text: desk.say(self.now, "bank", text), self.log, self.push,
                                                  self.mode, self.applied)
        b.on_alert = self.on_alert
        b.on_urgent = self.on_urgent
        b.on_flow = on_flow
        for _, text in b.alerts:
            self.on_alert(text)
        for fl in list(b.flows):
            on_flow(fl)
        if self.meta["start_equity"] <= 0 and b.balance_confirmed and b.cash > 0.01:
            self.open_account(b.cash, self.now, "the balance found (no deposit read for it)", None)
        self.reconcile_flows()
        readable = b.flows_readable and b.unconfirmed_why != "the ledger can't be read"
        self.checks.add("flows", CHECKS["flows"], readable,
                        (f"{len(self.recorded)} recorded as money in or out since the account opened" +
                         (" (the opening deposit is the start, not one of them)" if self.meta.get("opened") else ""))
                        if readable else "not this run: the balance waits for them")
        if not readable:
            health(self.account, self.now, flows_unreadable=1)
        if self.shadow is not None and self.shadow.unknown_entries:
            health(self.account, self.now, unknown_entries=len(self.shadow.unknown_entries))

    def say_fills(self, rules) -> None:
        """What the broker did this run, as the desk's events (paper says its fills the same way): SHADOW's in the
        conditional (no order was sent), LIVE's as done."""
        from .brokers import coinbase_orders as co

        b, desk, shadow = self.broker, self.desk, self.mode == "shadow"
        if b is None or desk is None:
            return
        for t in b.trades[self.trades0:]:
            verb = ("WOULD HAVE SOLD" if t.side.value == "long" else "WOULD HAVE COVERED") if shadow else \
                ("SOLD" if t.side.value == "long" else "COVERED")
            desk.say(self.now, "risk" if "stop" in t.reason else "fills",
                     f"{verb} {rules.describe(t.qty)} @ ${t.exit_price:,.2f} ({t.reason.replace('_', ' ')}"
                     f"{', simulated' if shadow else ''}) · trade {t.net_pnl:+,.2f}{' (est.)' if shadow else ''}"
                     + (". No order sent." if shadow else ""))
        pos = b.position
        if pos is not None and pos.entry_ts != self.pos0:
            stop, limit, _ = co.stop_limit_prices("SELL" if pos.side.value == "long" else "BUY", pos.stop or 0.0)
            verb = ("WOULD BUY" if pos.side.value == "long" else "WOULD SHORT") if shadow else \
                ("BOUGHT" if pos.side.value == "long" else "SHORTED")
            desk.say(self.now, "fills", f"{verb} {rules.describe(pos.qty)} at market: "
                                        + (f"simulated fill ${pos.entry_price:,.2f} (Coinbase's best "
                                           f"{'ask' if pos.side.value == 'long' else 'bid'}), fee est. "
                                           f"${pos.fees_paid:,.2f}" if shadow else
                                           f"filled ${pos.entry_price:,.2f}, fee ${pos.fees_paid:,.2f}")
                                        + (f" · stop ${stop:,.2f} (limit ${limit:,.2f})" if pos.stop else "")
                                        + (". No order sent." if shadow else ""))

    def reconcile_flows(self) -> None:
        """A bank deposit counts as reconciled to the cent when the real balance moved by exactly the money that came
        in and out this run, since the last run's balance (the arming checks ask for at least one)."""
        deposits = [a for a in self.applied if a["amount"] > 0 and a["what"] == "bank deposit"]
        if not deposits or self.real_before is None or self.real_equity is None:
            return
        if abs((self.real_equity - self.real_before) - sum(a["amount"] for a in self.applied)) <= 0.01:
            self.account["deposits_reconciled"] = int(self.account.get("deposits_reconciled") or 0) + len(deposits)

    def max_notional(self):
        return None

    def save_desk(self, work: Path) -> None:
        if self.desk is not None:
            _write(work, "desk.json", {**self.desk.to_dict(), "meta": self.meta,
                                       **({"flows": self.recorded} if self.recorded else {})})
        if self.shadow is not None:
            from .brokers.coinbase_shadow import gc_book

            gc_book(self.shadow.book, 30 * DAY_MS, self.now)
            _write(work, "book.json", self.shadow.book)

    # ---- what $0 can't buy, and what the planned start would
    def size_story(self, f, ctx, rules) -> None:
        dec, eq = self.decision, self.broker.cash
        i = len(f.bars) - 1
        close = f.bars[i].close
        a = f.atr[i]
        if a is None or self.broker.position is not None:
            return
        stop = dec.stop if dec is not None and dec.stop else close - self.p.strategy.stop_atr * a
        need = rules.min_equity_for_one(close, stop, 1, self.p.sizing.risk_per_trade, self.p.sizing.max_leverage)
        one, rate = rules.spec.contract_size * close, rules.overnight_margin(1)
        self.min_equity = {"need": need, "one_contract": one, "margin": one * rate, "rate": rate, "stop": stop,
                           "note": (f"The account holds {_usd(eq)}, so the desk sizes "
                                    + ("nothing" if eq < need else "at least one contract") +
                                    f". At this close one contract ({rules.spec.contract_size:g} BTC, {_usd(one, 0)}) "
                                    f"would need an account of at least {_usd(need)}: {self.p.sizing.risk_per_trade:.0%}"
                                    f" risk at the {self.p.strategy.stop_atr:g}-ATR stop, and {_usd(one * rate)} "
                                    f"overnight margin ({rate:.1%}).")}
        planned = self.p.sizing.starting_equity
        if eq < need:
            wi = evaluate_entry(self.p, f, i, equity=planned, kills=self.desk.kill_gates(self.now, planned), ctx=ctx,
                                now_ms=self.now, venue=rules)
            blocked = wi.blocked_by
            n = rules.contracts(wi.qty) if wi.qty else 0
            self.what_if = {"equity": planned, "side": wi.side.value if wi.side else None, "contracts": n,
                            "gate": blocked.name if blocked else None,
                            "text": (f"WHAT-IF, not your account: with the planned {_usd(planned, 0)} the desk would "
                                     + (f"buy {n} contract{'s' if n != 1 else ''}" if wi.side else
                                        f"stand aside: {blocked.name.replace('_', ' ') if blocked else 'no setup'}"
                                        + (f" ({blocked.note})" if blocked and blocked.note else "")))}

    # ---- the board
    def board(self, f, ctx, vstate, feed, bars, rules) -> dict:
        from .brokers.coinbase_shadow import stop_risk

        b, desk = self.broker, self.desk
        i = len(f.bars) - 1
        state = board_mod.build(desk, f, i, mode=self.mode, ctx=ctx, now_ms=self.now, goal_pace=None,
                                elapsed_days=(self.now - self.meta["started_ts"]) / DAY_MS,
                                flows=flows_mod.State(self.meta["start_equity"], self.recorded),
                                start_equity=self.meta["start_equity"], venue_state=vstate, crawler={
                                    "source": "coinbase · 2h", "funding_prints": len(f.funding_bar),
                                    "requests": self.rest.requests, "latency_ms": self.rest.last_latency_ms,
                                    "age_s": (self.now - (f.bars[-1].ts + f.bar_ms)) / 1000})
        state["ticker"]["price"] = self.mid
        real = self.real.balance or {}
        pos = b.position
        risk = None
        if pos is not None and pos.stop:
            risk = stop_risk(pos.entry_price, pos.stop, pos.side.sign, rules.contracts(pos.qty), b.cash)
        elif self.decision is not None and self.decision.side is not None and self.decision.qty:
            risk = stop_risk(f.bars[i].close, self.decision.stop, self.decision.side.sign,
                             rules.contracts(self.decision.qty), b.cash)
        if risk is not None:
            risk["text"] = (f"risk at the stop trigger {_usd(risk['at_stop'])} ({risk['at_stop_pct']:.1%}); if filled at "
                            f"the stop-limit's limit {_usd(risk['limit'], 0)}: {_usd(risk['at_limit'])} "
                            f"({risk['at_limit_pct']:.1%}); unlimited if the price gaps past the limit, until the next "
                            f"run closes at market (15 minutes or more). Estimates")
        state["account"] = {
            "real_equity": self.real_equity, "total_usd": real.get("total"), "spot_usd": real.get("cbi"),
            "futures_usd": real.get("cfm"), "open_pnl": real.get("upnl"), "unsettled_pnl": real.get("realized"),
            "in_transit": real.get("pending"), "buying_power": real.get("buying_power"),
            "available_margin": real.get("available_margin"), "holds": real.get("holds"),
            "liquidation_buffer_pct": real.get("liquidation_buffer_pct"),
            "margin_window": self.extras.get("margin_window") or ((real.get("overnight") or {}).get("type")),
            "intraday_setting": self.extras.get("margin_setting"), "usdc": getattr(self, "usdc", None),
            "fee_tier": self.extras.get("fees"), "sweeps": self.extras.get("sweeps"),
            "fills": self.extras.get("real_fills"),
            "positions": getattr(self.shadow, "real_positions", None) if self.shadow is not None else None,
            "orders": getattr(self.shadow, "real_orders", None) if self.shadow is not None else None,
            "portfolio_type": self.account.get("portfolio_type"), "balance_block": self.real.balance_block,
        }
        sim_pnl = None if self.shadow is None else self.shadow.book["cash"] + self.shadow.upnl()
        reason = self.account.get("entry_kill_reason")
        loaded = getattr(self, "kills_loaded", None)
        if not (self.kills_set.get("entry_kill") or (loaded is not None and loaded.entry_kill)):
            reason = None
        state[self.mode] = {
            "real_equity": self.real_equity, "simulated_equity": b.cash if self.shadow is not None else None,
            "simulated_pnl": sim_pnl, "checks": self.checks.ordered(), "would_send": (
                self.shadow.book["would_send"][-20:] if self.shadow is not None else []),
            "would_send_now": self.would_send_now, "min_equity_for_one": self.min_equity, "what_if": self.what_if,
            "entry_kill_reason": None if not reason else (
                f"LIVE would be in entry_kill now (set {_utc(reason['ts'], '%H:%M UTC')}: {reason['why']}). SHADOW "
                f"mirrors it: set entry_kill to false in BTC/kills.json on the coinbase-state branch once you've "
                f"looked." if self.mode == "shadow" else
                f"entry_kill is on (set {_utc(reason['ts'], '%H:%M UTC')}: {reason['why']}): set it to false in "
                f"BTC/kills.json on the coinbase-state branch once you've looked."),
            "opened": self.meta.get("opened"), "paper_check": self.paper_check, "risk": risk,
            "unexplained": list(self.meta.get("unexplained") or []),
            "book": None if self.shadow is None else self.shadow.summary(), "alerts": self.alerts[-20:],
            "notes": [
                "SHADOW sends no order: its key can't, and its client refuses every POST before anything is sent.",
                "Simulated fills are at Coinbase's best bid or ask when the run looked, with the fee estimate "
                "max($0.20 a contract, 5 bps): the real fill and fee will differ.",
                "Funding on the simulated position is an estimate from the hourly rates SHADOW logged.",
                "GitHub runs this about every 15 minutes, best effort: runs are late or skipped at busy times, and "
                "between runs only an order resting on Coinbase would protect a LIVE position.",
                "Entries go out at market at the next run after a close (up to 30 minutes later), not at the close.",
            ] if self.mode == "shadow" else [],
            "log": self.lines[-120:],
        }
        return state

    # ---- the end: state, board, status, kills, pushes
    def finish(self, work: Path, password: str, ok: bool) -> None:
        paths = self.paths
        now = self.now
        self.meta["runs"] = int(self.meta.get("runs") or 0) + 1
        if not ok:
            self.meta["failed_runs"] = int(self.meta.get("failed_runs") or 0) + 1
        health(self.account, now, runs=1, ok=1 if ok else 0)
        if self.kills_set.get("entry_kill") and not self.account.get("entry_kill_reason"):
            self.account["entry_kill_reason"] = {"ts": now, "why": "an error while acting"}
        loaded = getattr(self, "kills_loaded", None)
        if loaded is not None and not loaded.entry_kill and not self.kills_set.get("entry_kill"):
            self.account["entry_kill_reason"] = None        # you cleared it in kills.json, and nothing set it again
        if getattr(getattr(self, "shot", None), "unexplained", False):
            health(self.account, now, unexplained=1)
        first_ok = ok and not self.account.get("first_ok_ts")
        self.account["last_run_ts"] = now
        self.account["problem"] = self.problem
        if ok:
            self.account["first_ok_ts"] = self.account.get("first_ok_ts") or now
        printed = [ln for ln in (self.stray.getvalue().splitlines() if self.stray is not None else []) if ln.strip()]
        self.lines += [f"printed: {ln}" for ln in printed[-50:]]
        self.account["log"] = (list(self.account.get("log") or []) + [f"--- run {self.meta['runs']} {_utc(now)}"]
                               + self.lines)[-LOG_KEEP:]
        if self.desk is None:
            # stopped before the desk was built: the saved desk stays as it was, only the meta moves
            saved = _read(work, "desk.json") or {}
            if saved or self.meta.get("runs"):
                _write(work, "desk.json", {**saved, "meta": self.meta})
        else:
            self.save_desk(work)
        _write(work, "account.json", self.account)
        self.dir.mkdir(parents=True, exist_ok=True)
        it = self.iterations
        tmp = paths["state"].with_name(paths["state"].name + ".tmp")
        tmp.write_text(statebox.seal_json(pack(work), password, statebox.state_aad(self.mode), iterations=it))
        tmp.replace(paths["state"])
        board = self.board_state
        if board is None:
            # stopped before a board: the last one stays (its own "updated" says how old it is), with this run's checks
            try:
                if self.replaces_leftover:
                    raise FileNotFoundError("a never-armed LIVE attempt's board isn't this LIVE's")
                board = statebox.open_json(paths["board"].read_text(), password, statebox.board_aad(self.mode))
            except (OSError, statebox.BoxError):
                board = {"updated": None, "header": {"mode": self.mode.upper(), "coin": COIN,
                                                     "title": "COINBASE · BTC PERP",
                                                     "tagline": board_mod.tagline_for(self.mode)}}
            part = board.setdefault(self.mode, {})
            part.update({"checks": self.checks.ordered(), "alerts": self.alerts[-20:], "log": self.lines[-120:]})
        board.setdefault(self.mode, {})["problem"] = self.problem
        board["status"] = {"ok": ok, "problem": self.problem, "runs": self.meta["runs"], "checked": now,
                           "why": None if ok else (self.lines[-1] if self.lines else None)}
        tmp = paths["board"].with_name(paths["board"].name + ".tmp")
        tmp.write_text(statebox.seal_json(board, password, statebox.board_aad(self.mode), compress=False,
                                          iterations=it))
        tmp.replace(paths["board"])
        for name, d in (("status", self.status(ok)), ("kills_set", {k: True for k, v in self.kills_set.items() if v})):
            tmp = paths[name].with_name(paths[name].name + ".tmp")
            tmp.write_text(json.dumps(d, indent=1 if name == "status" else None) + "\n")
            tmp.replace(paths[name])        # never through a symlink the branch put there
        self.check_own_save()
        if first_ok:
            self.push(f"{COIN} {self.mode.upper()}: connected (read only)" if self.mode == "shadow" else
                      f"{COIN} LIVE: armed and connected",
                      "\n".join(self.checks.lines()) + ("\n\nNo orders were sent; none can be with this key. Paper is "
                                                        "unaffected." if self.mode == "shadow" else ""),
                      "default", "white_check_mark")
        self.push_trades()
        self.public_note = "" if ok else (self.problem or "")

    def check_own_save(self) -> None:
        """On GitHub Actions the state dir is the branch as the load step extracted it, so the save step's own check
        (coinbase_state.check) runs here first, against the branch as loaded: a save it would refuse is pushed now,
        since nothing after it would say so (the saved state and status just stop moving)."""
        if self.env.get("GITHUB_ACTIONS") != "true":
            return
        try:
            coinbase_state.check(self.dir.parent, branch=self.branch_loaded())
        except coinbase_state.StateCheckError as exc:
            self.save_refused(str(exc))
        except Exception as exc:        # noqa: BLE001 (advisory: the save step runs the check itself)
            self.log(f"the save check couldn't run here ({type(exc).__name__}); the save step runs it")

    def save_refused(self, why: str) -> None:
        """This run's save will be refused. LIVE: an urgent push at every run (what it remembers between runs, the
        stop's level and the risk brakes, stops being saved). SHADOW: once every 6 hours by the clock (the push memo
        is in the state that isn't saved)."""
        self.save_refused_why = why
        self.log(f"this run's save will be refused: {why}")
        if self.mode == "live":
            self.push(f"{COIN} LIVE: NOT SAVED",
                      f"The save step will refuse this run's state: {why}. The orders and the stop are on Coinbase, "
                      f"but what LIVE remembers between runs (the stop's level, the halt, the daily loss, the "
                      f"cooldown) isn't saved, so each run starts again from the last saved state until this is "
                      f"fixed on the coinbase-state branch. {self.sent_note()}", "urgent", "rotating_light")
        elif self.now % PUSH_EVERY_MS < 15 * 60_000:
            self.push(f"{COIN} SHADOW: NOT SAVED",
                      f"The save step will refuse this run's state: {why}. SHADOW ran, but its state, board and status "
                      f"aren't saved (the dashboard goes stale) until this is fixed on the coinbase-state branch. "
                      f"Nothing was sent. Paper is unaffected.", "high", "warning")

    def push_trades(self) -> None:
        if self.mode != "shadow":
            return
        bal = (f"\n\nReal balance {_usd(self.real_equity)} · SHADOW balance {_usd(self.broker.cash)} (est.)"
               if self.broker is not None and self.real_equity is not None else "")
        for rec in self.would_send_now:
            if rec["kind"] == "entry":
                side = rec["body"]["side"]
                self.push(f"{COIN} SHADOW: would {side} (no order sent)", rec["text"] + "\n" + "\n".join(
                    r["text"] for r in self.would_send_now if r["kind"] == "stop") + bal, "high",
                    "chart_with_upwards_trend")
            elif rec["kind"] == "close":
                self.push(f"{COIN} SHADOW: would SELL (no order sent)", rec["text"] + bal, "high",
                          "chart_with_downwards_trend")
        for kind, ev in self.events_said:
            if kind == "stop_filled":
                self.push(f"{COIN} SHADOW: would SELL (stop, simulated)", ev + bal, "high", "chart_with_downwards_trend")

    def status(self, ok: bool) -> dict:
        dec = self.decision
        decision = None
        if self.mode == "shadow":
            if self.broker is not None and self.broker.position is not None:
                decision = f"HOLD {self.broker.position.side.value.upper()} (simulated)"
            elif dec is not None and dec.side is not None and getattr(getattr(self, "shot", None), "processed", 0):
                decision = f"GO {dec.side.value.upper()}"
            elif dec is not None and dec.blocked_by is not None and dec.blocked_by.verdict == Verdict.DROP:
                decision = "STAND DOWN"
            elif dec is not None:
                decision = "WAIT"
        ws = None
        if self.shadow is not None:
            ws = sum(1 for r in self.shadow.book["would_send"] if self.now - r["ts"] < DAY_MS)
        return {"v": 1, "mode": self.mode.upper(), "coin": COIN, "product": PRODUCT, "updated": self.now,
                "started_ts": int(self.meta["started_ts"]), "runs": int(self.meta["runs"]),
                "failed_runs": int(self.meta.get("failed_runs") or 0), "ok": ok, "problem": self.problem,
                "key_view_only": None if self.mode != "shadow" else (
                    True if any(r["key"] == "view_only" and r["ok"] for r in self.checks.rows) else
                    False if any(r["key"] == "view_only" for r in self.checks.rows) else None),
                "decision": decision, "would_send_last_24h": ws, "alerts_open": len(self.alerts),
                "checks_passed": self.checks.passed, "checks_total": len(self.checks.rows),
                "opened": bool(self.meta.get("start_equity", 0) > 0),
                "note": self.public_why if not ok else self.kills_note(),
                **({"armed": bool(self.meta.get("armed_at"))} if self.mode == "live" else {})}

    def kills_note(self) -> str | None:
        """The public status's note on an OK run: a kill switch the desk can't read as written (fixed text, no digit:
        coinbase_state.check_status)."""
        d, why = self.kills_file()
        if why:
            return KILLS_UNREADABLE_FLAT if d.get("flat_kill") else KILLS_UNREADABLE
        return KILLS_STRAY if self.kills_strays else None

    def public_line(self) -> str:
        """The one line the public log gets: no amounts, no ids, no Coinbase wording."""
        m = self.mode.upper()
        if self.code == 0:
            n = len(self.would_send_now) if self.mode == "shadow" else getattr(self.rest, "posts_sent", 0)
            done = getattr(getattr(self, "shot", None), "processed", 0)
            return (f"{m} BTC: run {self.meta.get('runs', 0)} · key {'View only' if self.mode == 'shadow' else 'armed'} "
                    f"· reads ok · {done} close{'s' if done != 1 else ''} processed · {n} order"
                    f"{'s' if n != 1 else ''} it {'would send' if self.mode == 'shadow' else 'sent'} · "
                    + ("its save is refused (the reason is on your phone)" if self.save_refused_why else "state saved")
                    + (" · kills.json can't be read: entry_kill on" if self.kills_read and self.kills_read[1] else "")
                    + (" · a file looks like a kill switch but isn't kills.json: entry_kill on" if self.kills_strays
                       else ""))
        if not self.changed:
            why = self.public_why or self.problem or "error"
            return f"{m} BTC: refused before reading the account: {why} (nothing read or sent; the state is unchanged)"
        why = self.public_why or self.problem or "error"
        return f"{m} BTC: stopped: {why} (details on your phone and in the encrypted board)"


def run_shadow(state_dir, env, **kw) -> tuple[int, Run]:
    r = Run(state_dir, env, mode="shadow", **kw)
    return r.execute(), r


# ---------------------------------------------------------------- LIVE: the same run, on the client arm() unlocks

class LiveRun(Run):
    """LIVE on Coinbase, one check per call: SHADOW's run on CoinbaseExchange itself, through the client
    coinbase_live.arm() unlocks after every arming check (the go-live gate, DESK_LIVE_CONFIRM, DESK_LIVE_COINBASE,
    --max-notional, SHADOW's record, a View + Trade key that can't move money, the same portfolio, a flat start). LOCKED:
    nothing in this repository runs it but the tests, against their local fake Coinbase; no workflow arms it."""

    def __init__(self, state_dir, env, *, max_notional: float | None, gate: list, force_unproven: bool = False, **kw):
        super().__init__(state_dir, env, mode="live", **kw)
        self.cap, self.gate, self.force = max_notional, list(gate or []), force_unproven
        self.password: str | None = None
        self.arm_lines: list[str] = []
        self.safety_said: list[str] = []
        self.proof: dict | None = None
        self.proof_read = False

    def max_notional(self):
        return self.cap

    def preflight(self) -> tuple[str, str]:
        password, raw = super().preflight()
        self.password = password
        return password, raw

    def counted(self, bundle: dict | None) -> dict | None:
        """A LIVE state with no armed_at is what a LIVE attempt that never armed left (before LIVE saved only once
        armed): no LIVE start. This run starts LIVE as a first start (the go-live gate and SHADOW's record checked),
        and its state replaces that one only if it arms."""
        try:
            armed = armed_at(bundle)
        except ValueError:
            return bundle               # the run's own read refuses it ("state")
        if bundle is not None and not armed:
            self.replaces_leftover = True
            self.log("the saved LIVE state is a LIVE attempt's that never armed: no LIVE start, so this run is a first "
                     "start")
            return None
        return bundle

    def saves_from_the_start(self) -> bool:
        return bool(self.meta.get("armed_at"))

    def read_shadow_proof(self) -> dict | None:
        if self.proof_read:
            return self.proof
        self.proof_read = True
        try:
            b = open_bundle(self.dir / "state-shadow.enc.json", self.password, "shadow")
        except statebox.BoxError:
            return None
        if b is None:
            return None
        files = b["files"]
        try:
            account = json.loads(files.get("account.json") or "null")
            meta = (json.loads(files.get("desk.json") or "null") or {}).get("meta")
        except ValueError:
            return None
        self.proof = shadow_proof(account, meta)
        return self.proof

    def client_for(self, key):
        from . import coinbase_live

        out: list[str] = []
        got = coinbase_live.arm(key, env=self.env, max_notional=self.cap, gate=self.gate, force_unproven=self.force,
                                shadow_proof=self.read_shadow_proof(), live_state={"armed_at": self.meta.get("armed_at")},
                                base_url=self.base_url, clock=self.clock, sleep=self.sleep, out=out.append,
                                deadline_s=self.clock() + self.deadline_s, emit=self.mask_emit)
        self.arm_lines = out
        for ln in out:
            self.log(ln.strip())
        if got is None:
            self.watchdog(key)
            fail = next((ln.split("Not going live on Coinbase:", 1)[1].strip() for ln in out
                         if "Not going live on Coinbase:" in ln), "an arming check failed")
            raise Stop("refused", f"not armed: {fail}")
        self.meta["armed_at"] = self.meta.get("armed_at") or int(self.clock() * 1000)
        if not self.changed:
            self.begin_saving()         # LIVE's first start: from its arming on, the run is saved
        return got[0]

    def stopped(self, why: str) -> None:
        """A LIVE run that stopped before it ever armed saves nothing (no state, board, status or kill), so the branch
        is as it was: SHADOW carries on and the next LIVE run is a first start again (not_armed_push)."""
        if self.changed:
            return super().stopped(why)
        self.not_armed_push(why)

    def refused_push(self, s: Stop) -> None:
        """Refused before the state was opened (DESK_STATE_KEY not set, say). A LIVE that has armed (its public status
        says so): as SHADOW, once every 6 hours. One that never armed: the same push as any never-armed refusal."""
        st = self.saved_status() or {}
        if (st.get("mode") == "LIVE" and st.get("armed") is True) or (
                not st and f"{COIN}/state-live.enc.json" in self.branch_loaded().tree()):
            return super().refused_push(s)      # armed (or a LIVE state with no status to say it never armed)
        self.not_armed_push(s.text)

    def shadow_last_run(self) -> int:
        """When SHADOW last ran on this account: its encrypted record's (DESK_STATE_KEY opens it), else its public
        status's "updated" (readable without the key: a refusal over DESK_STATE_KEY), else 0."""
        if self.password:
            last = int((self.read_shadow_proof() or {}).get("last_run_ts") or 0)
            if last:
                return last
        st = self.saved_status() or {}
        try:
            return int(st.get("updated") or 0) if st.get("mode") == "SHADOW" else 0
        except (TypeError, ValueError):
            return 0

    def not_armed_push(self, why: str) -> None:
        """With no state to remember a push in: at every never-armed refusal while SHADOW's last run is fresh enough
        to arm on (under 2 hours), then once every 6 hours by the clock."""
        from .coinbase_live import SHADOW_FRESH_MS

        last = self.shadow_last_run()
        fresh = last > 0 and 0 <= self.now - last < SHADOW_FRESH_MS
        if not fresh and self.now % PUSH_EVERY_MS >= 15 * 60_000:
            return
        stale = "" if fresh else (
            " SHADOW's last run is over 2 hours old, so LIVE can't arm until SHADOW has run again: put the SHADOW run "
            "step back (docs/coinbase-setup.md, Part 2)." if last else
            " SHADOW has no record here that DESK_STATE_KEY opens, so LIVE can't arm until SHADOW has run on this "
            "account (docs/coinbase-setup.md, Part 1).")
        self.push(f"{COIN} LIVE: not armed",
                  f"LIVE didn't arm: {why.removeprefix('not armed: ')}. No order was sent, and nothing was saved: the "
                  f"coinbase-state branch and SHADOW's state are as they were, and the next LIVE run is a first start "
                  f"again.{stale} Paper is unaffected.", "high", "warning")

    def public_line(self) -> str:
        if self.code != 0 and not self.changed and not self.refused_before_state:
            why = self.public_why or ("an arming check failed" if self.problem == "refused" else self.problem or "error")
            return (f"LIVE BTC: not armed: {why} (no order sent; nothing saved: the branch and SHADOW's state are "
                    f"as they were)")
        return super().public_line()

    def watchdog(self, key) -> None:
        """A run that refuses still looks, read only: a stop resting with no position (it would open one: Coinbase has
        no reduce-only), a position with no stop. Each is pushed, urgently."""
        from .brokers.coinbase import CoinbaseExchange
        from .brokers.coinbase_rest import CoinbaseError, CoinbaseREST

        try:
            ex = CoinbaseExchange(CoinbaseREST(key, base_url=self.base_url, clock=self.clock, sleep=self.sleep,
                                               deadline_s=self.clock() + 60, emit=self.mask_emit, env=self.env),
                                  on_alert=lambda text: None)
            found = ex.watchdog()
        except CoinbaseError as exc:
            self.log(f"watchdog: {type(exc).__name__}")
            return
        for kind, text in found:
            if kind in ("stray", "unprotected"):
                self.push(f"{COIN} LIVE: STOP WITH NO POSITION" if kind == "stray" else f"{COIN} LIVE: NO STOP",
                          f"{text}. LIVE didn't run this check (it refused), so nothing else will fix it.", "urgent",
                          "rotating_light")
            self.alerts.append(text)

    def check_permissions(self, perms: dict) -> None:
        ok = perms["can_view"] and perms["can_trade"] and not perms["can_transfer"]
        self.checks.add("view_only", "Key is View + Trade, not Transfer", ok)
        if not ok:
            raise Stop("permissions", "LIVE only runs with a View + Trade key that can't move money")

    # ---- the safety pass: the position's protection first, before any market read
    def _say_safe(self, text: str) -> None:
        self.log(f"safety pass: {text}")
        self.safety_said.append(text)

    def safety_pass(self, work: Path) -> None:
        """Right after arming and the key's permissions, before the product, the candles or the balance are read (any
        of which can fail, or a product change refuse the market): the position and the open orders, read fresh.
          flat      every desk order left resting is cancelled (Coinbase has no reduce-only: it would open a
                    position); one that won't cancel, or one that isn't the desk's, is pushed urgently
          flat_kill the stop cancelled (confirmed), then close_position
          no stop   one placed: at the saved stop, else the stop distance from the entry at the last ATR seen; the
                    price already through it: closed at market. None placed: the urgent push
        It needs only the product's id. What it can't read, it leaves to the rest of the run (which reads it all)."""
        from .brokers import coinbase_orders as co
        from .brokers.coinbase import CancelFailed, StopAlreadyFilled, StopThroughMarket
        from .brokers.coinbase_rest import CoinbaseError
        from .brokers.live import STOP_ATTEMPTS, STOP_BACKOFF_S

        real = self.real
        path = work / COIN / "live_position.json"
        saved = _read(work, "live_position.json") or {}
        kills = self.kills_now()
        try:
            pos, orders = real._fresh()
        except CoinbaseError as exc:
            self._say_safe(f"the position and orders couldn't be read ({type(exc).__name__}); the run reads them again")
            return

        def save(**kw) -> None:
            if not path.exists():
                return
            d = _read(work, "live_position.json") or {}
            d.update(kw)
            _write(work, "live_position.json", d)

        def seq() -> int:
            n = int((_read(work, "live_position.json") or {}).get("order_seq") or 0)
            save(order_seq=n + 1)
            return n

        def cancel(o) -> bool:
            for attempt in range(STOP_ATTEMPTS):
                if attempt:
                    self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
                try:
                    real.cancel_order(o["id"], PRODUCT)
                    return True
                except StopAlreadyFilled:
                    return True
                except (CoinbaseError, CancelFailed):
                    continue
            return False

        others = real.foreign_on_product()
        if pos is None:
            left = [o for o in orders if o["status"] == "open" and not cancel(o)]
            if left:
                ids = ", ".join(str(o["id"]) for o in left)
                self.on_urgent("stray", f"STOP WITH NO POSITION: order {ids} rests on {PRODUCT} with no position and "
                                        f"won't cancel: Coinbase has no reduce-only, so it would open a position if it "
                                        f"fired. Cancel order {ids} in Coinbase now")
            elif orders:
                self._say_safe(f"flat: cancelled {len(orders)} order(s) left resting")
            for o in others:
                self.on_urgent("stray-foreign", f"STOP WITH NO POSITION: order {o['id']} rests on {PRODUCT} with no "
                                                f"position, and it isn't the desk's (the desk never touches it): "
                                                f"Coinbase has no reduce-only, so it would open a position if it "
                                                f"fired. Cancel it in Coinbase now")
            return
        sign = 1 if pos["side"] == "long" else -1
        closing = "sell" if sign > 0 else "buy"
        n = int(pos["contracts"])
        sp = saved.get("position") or {}
        ets = sp.get("entry_ts") if sp.get("side") == pos["side"] else "safety"
        stops = [o for o in orders if o["side"] == closing and o.get("triggerPrice") and o["status"] == "open"]

        def close(reason: str) -> None:
            for o in [o for o in orders if o["side"] == closing and o["status"] == "open"]:
                if not cancel(o):
                    self._say_safe(f"the {reason} close isn't sent: the stop {o['id']} won't cancel first (no "
                                   f"reduce-only); the rest of the run tries again")
                    return
            save(close_wanted=reason)
            try:
                real.create_order(PRODUCT, "market", closing, n, None,
                                  {"reduceOnly": True, "clientOrderId": real.client_order_id("close-safety", ets,
                                                                                             seq())})
                self._say_safe(f"closed the {pos['side']} {n} contract(s) at market ({reason.replace('_', ' ')})")
            except CoinbaseError as exc:
                self.on_urgent("no_stop", f"NO STOP: the {pos['side']} {n} contract(s) on {PRODUCT} had their stop "
                                          f"cancelled for a {reason.replace('_', ' ')} close, and the close didn't go "
                                          f"through ({type(exc).__name__}). Close it or place its stop in Coinbase "
                                          f"now")

        with real.protecting():
            if kills.flat_kill:
                close("flat_kill")
                return
            if stops:
                return
            level = None
            if sp.get("side") == pos["side"] and sp.get("stop"):
                level = float(sp["stop"])
            elif self.meta.get("last_atr") and pos.get("entry"):
                level = float(pos["entry"]) - sign * self.p.strategy.stop_atr * float(self.meta["last_atr"])
            if level is None or level <= 0:
                self.on_urgent("no_stop", f"NO STOP: the {pos['side']} {n} contract(s) on {PRODUCT} have no stop "
                                          f"resting and the desk has no level for one yet; it places one later this "
                                          f"run if it can read the market. Check it in Coinbase")
                return
            try:
                last = real.last_trade()
            except CoinbaseError:
                last = None
            if last is not None and (last[0] - level) * sign <= 0:
                self._say_safe(f"no stop resting, and {COIN} at {last[0]:,.2f} is already through its level "
                               f"{level:,.2f}: closing at market")
                close("stop")
                return
            trig = float(co.round_price(level))
            err = ""
            for attempt in range(STOP_ATTEMPTS):
                if attempt:
                    self.sleep(STOP_BACKOFF_S * 2 ** (attempt - 1))
                try:
                    real.create_order(PRODUCT, "market", closing, n, trig,
                                      {"stopLossPrice": trig,
                                       "clientOrderId": real.client_order_id("stop-safety", ets, trig, seq())})
                    self._say_safe(f"no stop was resting on the {pos['side']} {n} contract(s): placed one at "
                                   f"{trig:,.0f}")
                    return
                except StopThroughMarket:
                    close("stop")
                    return
                except CoinbaseError as exc:
                    err = f"{type(exc).__name__}: {exc}"
            self.on_urgent("no_stop", f"NO STOP: the {pos['side']} {n} contract(s) on {PRODUCT} have no stop resting "
                                      f"and one couldn't be placed ({err}). Place it or close the position in Coinbase "
                                      f"now; the desk tries again later this run and at the next one")

    def exchange(self, rules, feed):
        return self.real

    def push_trades(self) -> None:
        if self.desk is None:
            return
        new = [e for e in self.desk.events if e.ts >= self.now and e.agent in ("fills", "risk")]
        if new:
            self.push(f"{COIN} LIVE: TRADE", "\n".join(f"{e.agent.upper()}: {e.text}" for e in new), "high",
                      "chart_with_upwards_trend")


def run_live_once(state_dir, env, *, max_notional: float | None, gate: list, force_unproven: bool = False,
                  **kw) -> tuple[int, LiveRun]:
    r = LiveRun(state_dir, env, max_notional=max_notional, gate=gate, force_unproven=force_unproven, **kw)
    return r.execute(), r


# ---------------------------------------------------------------- the CLI's side: nothing private reaches the log

TEST_URL = re.compile(r"http://127\.0\.0\.1:\d{1,5}")


def base_url_from(env) -> str | None:
    """DESK_COINBASE_TEST_URL points a run at a local fake Coinbase (the tests, a dry run of the workflow). Only
    http://127.0.0.1:<port> is taken: it can't send the key's signatures anywhere else."""
    u = (env.get("DESK_COINBASE_TEST_URL") or "").strip()
    if not u:
        return None
    if not TEST_URL.fullmatch(u):
        raise SystemExit("DESK_COINBASE_TEST_URL must be http://127.0.0.1:<port> (a local fake Coinbase)")
    return u


def captured(fn, public_out):
    """Run `fn(mask_emit)` with stdout and stderr swapped for a private buffer from its first line: LiveBroker and
    the runner log amounts, and a traceback would print an exception's text. Only GitHub's add-mask lines (to the
    real stdout, as GitHub reads them) and the run's one public line get out. Returns (code, line, the run or None)."""
    import io
    import sys

    real_out, real_err = sys.stdout, sys.stderr
    buf = io.StringIO()

    def mask_emit(line: str) -> None:
        assert line.startswith("::add-mask::") and "\n" not in line and "\r" not in line
        real_out.write(line + "\n")
        real_out.flush()

    run = None
    sys.stdout = sys.stderr = buf
    try:
        code, run = fn(mask_emit, buf)
        line = run.public_line()
    except BaseException as exc:        # noqa: BLE001 (the whole point: nothing escapes to the public log)
        code, line = (exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1), \
            f"stopped: {type(exc).__name__} (nothing was sent)"
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    public_out(line)
    return code, line, run
