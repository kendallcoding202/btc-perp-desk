"""Money in and money out: deposits and withdrawals. Never profit, never a loss, never a brake trip.

The plan lives in deposits.json at the repo root (on main, so it can be edited from the GitHub app):

    {"BTC": {"monthly": {"amount": 100, "day": 1, "first": "2026-11-01", "count": 60},
             "extra": [{"date": "2026-12-15", "amount": 250, "note": "bonus"},
                       {"date": "2027-03-01", "amount": -50, "note": "a withdrawal"}]}}

`monthly` is one plan or a list of plans (to change the amount later: end the first plan with its count, start the
next the month after). `extra` amounts below zero are withdrawals. Keys starting with "_" are comments (strings).

Each paper step applies, in date order and once, every flow whose date's 00:00 UTC has come and that the ledger
hasn't recorded yet. Ids are fixed by the file: "monthly-2026-11", "extra-2026-12-15-0" (the first extra that day, in
file order). A flow takes effect at the open of the first bar that opens at or after 00:00 UTC on its date, so a run
that comes late (or a missed run) gives exactly what a run on time gives. A flow already recorded can't be removed
or changed: the step stops, loudly, until the file agrees with the ledger again.

Accounting is by units, like a fund. The account starts with `start_equity` units at $1 each. A flow buys (or
sells) units at the NAV it finds, marked equity / units, so:

    performance = NAV - 1                      unchanged by any flow (time-weighted)
    P&L ($)     = equity - start - net flows   unchanged by any flow
    brakes      peak equity and the day's start equity are scaled by (E + F) / E at the flow, so the drawdown and
                the day's loss read the same percentage just before and just after it

A flow changes nothing else: trading only sees it as the bigger (or smaller) equity it sizes from next time.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

FILE = "deposits.json"
MAX_USD = 1_000_000.0          # one flow, either way: a typo guard
MAX_MONTHS = 600               # one plan: 50 years
DAY_MS = 86_400_000
YEAR_MS = 365.25 * DAY_MS
EXTRA_KEYS = {"date", "amount", "note", "future"}
FUTURE_DAYS = 366              # an extra dated further ahead than this is taken for a typo unless it says "future": true
MONTHLY_KEYS = {"amount", "day", "first", "count", "note"}
SECTION_KEYS = {"monthly", "extra"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FlowsError(ValueError):
    """deposits.json, or what a ledger recorded from it, is wrong. That account's step stops until it's fixed: a
    deposit or withdrawal must never be skipped, guessed at, or counted twice."""


@dataclass(frozen=True)
class Flow:
    id: str             # "monthly-2026-11", "extra-2026-12-15-0"
    date: str           # YYYY-MM-DD
    ts: int             # its date's 00:00 UTC, epoch ms
    amount: float       # dollars in (+) or out (-)
    kind: str           # "plan" or "extra" (live flows read from the exchange are recorded as "exchange")
    note: str = ""


# ---------------------------------------------------------------- the file

def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _strict_json(text: str, source: str):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise FlowsError(f"{source}: the key {k!r} appears twice in one object; JSON keeps only the last, "
                                 f"so one of them would be silently dropped")
            out[k] = v
        return out

    def constant(name):
        raise FlowsError(f"{source}: {name} is not a number of dollars")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        hint = _hint(text, exc.pos)
        raise FlowsError(f"{source} is not valid JSON{': ' + hint if hint else ''} (line {exc.lineno}, column "
                         f"{exc.colno}: {exc.msg}). Every account's step waits until it's fixed, so no deposit or "
                         f"withdrawal is skipped") from None


def _hint(text: str, pos: int) -> str:
    """The fix, in plain words, for the slips a phone keyboard makes (JSON's own message rarely says)."""
    before, at = text[:pos].rstrip(), text[pos:].lstrip()
    if any(q in text for q in "\u201c\u201d\u2018\u2019"):
        return "your keyboard typed curly quotes (\u201c \u201d); use straight \" quotes (turn off smart punctuation)"
    if (before.endswith(",") and at[:1] in ("]", "}")) or (at[:1] == "," and at[1:].lstrip()[:1] in ("]", "}")):
        return "remove the comma after the last entry"         # (Python 3.12 points past the comma, 3.13 at it)
    if re.search(r"\[\s*\]$", before) and at[:1] == "{":           # an empty list, the entry typed after it
        return "put the entry inside the list's brackets: \"extra\": [ {...} ], not after them"
    if before[-1:] in ("}", "]") and at[:1] in ("{", "\""):
        return "add a comma between two entries"
    if at[:1] == "$" or (at[:1] == "-" and at[1:2].isspace()) or (re.search(r"\d,$", before) and at[:1].isdigit()):
        return "write amounts as plain numbers: 250 or -50 (no $, no space after the minus, no thousands commas)"
    return ""


def _comments_ok(obj: dict, where: str) -> dict:
    """Keys starting with "_" are comments: allowed anywhere, as long as they hold text (so an entry can't be hidden
    by renaming its key)."""
    for k, v in obj.items():
        if isinstance(k, str) and k.startswith("_") and not isinstance(v, str):
            raise FlowsError(f"{where}.{k} is a comment key (it starts with \"_\") but holds {type(v).__name__}, not "
                             f"text. Comments must be strings; to drop an entry, delete it")
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def _keys(obj, allowed: set, required: set, where: str) -> dict:
    if not isinstance(obj, dict):
        raise FlowsError(f"{where} must be an object {{...}}, not {type(obj).__name__}")
    obj = _comments_ok(obj, where)
    extra = set(obj) - allowed
    if extra:
        raise FlowsError(f"{where} has unknown key(s) {sorted(extra)}; allowed: {sorted(allowed)}")
    missing = required - set(obj)
    if missing:
        raise FlowsError(f"{where} is missing {sorted(missing)}")
    return obj


def _date(v, where: str) -> date:
    if not isinstance(v, str) or not _DATE.match(v):
        raise FlowsError(f"{where} is {v!r}; it must be a date written \"YYYY-MM-DD\", e.g. \"2026-12-15\"")
    try:
        d = date.fromisoformat(v)
    except ValueError:
        raise FlowsError(f"{where} is {v!r}, which is not a real date") from None
    if not 2000 <= d.year <= 2100:
        raise FlowsError(f"{where} is {v!r}; the year must be between 2000 and 2100")
    return d


def _amount(v, where: str, positive: bool) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FlowsError(f"{where} is {v!r} ({type(v).__name__}); it must be a number of dollars, e.g. 250"
                         + ("" if positive else " (or -50 for a withdrawal)"))
    x = float(v)
    if not math.isfinite(x) or x == 0 or (positive and x < 0):
        raise FlowsError(f"{where} is {v!r}; it must be " + ("more than 0" if positive else
                                                           "a non-zero number (below 0 = a withdrawal)"))
    if abs(x) > MAX_USD:
        raise FlowsError(f"{where} is {v!r}; one flow can't be more than ${MAX_USD:,.0f} either way (a typo guard)")
    if round(x, 2) != x:
        raise FlowsError(f"{where} is {v!r}; amounts are dollars and cents, at most 2 decimals")
    return x


def _int(v, where: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise FlowsError(f"{where} is {v!r}; it must be a whole number from {lo} to {hi}")
    return v


def _note(obj: dict, where: str, default: str) -> str:
    n = obj.get("note", default)
    if not isinstance(n, str) or len(n) > 200:
        raise FlowsError(f"{where}.note must be text of at most 200 characters")
    return n.strip()


def _monthly(plans, where: str) -> list[Flow]:
    plans = plans if isinstance(plans, list) else [plans]
    if not plans:
        raise FlowsError(f"{where} is an empty list; leave it out instead")
    out, seen = [], {}
    for k, plan in enumerate(plans):
        w = f"{where}[{k}]" if len(plans) > 1 else where
        plan = _keys(plan, MONTHLY_KEYS, MONTHLY_KEYS - {"note"}, w)
        amount = _amount(plan["amount"], f"{w}.amount", positive=True)
        day = _int(plan["day"], f"{w}.day", 1, 28)
        first = _date(plan["first"], f"{w}.first")
        if first.day != day:
            raise FlowsError(f"{w}.first is {plan['first']!r} but {w}.day is {day}: the first deposit must fall on "
                             f"the plan's day of the month")
        count = _int(plan["count"], f"{w}.count", 1, MAX_MONTHS)
        note = _note(plan, w, "monthly plan")
        for m in range(count):
            y, mo = divmod(first.month - 1 + m, 12)
            d = date(first.year + y, mo + 1, day)
            fid = f"monthly-{d.year:04d}-{d.month:02d}"
            if fid in seen:
                raise FlowsError(f"{w} and {seen[fid]} both have a deposit in {d:%Y-%m}: plans must not overlap "
                                 f"(end one with its count, start the next the month after)")
            seen[fid] = w
            out.append(Flow(fid, d.isoformat(), _ms(d), amount, "plan", note))
    return out


def _extras(items, where: str, today: date | None = None) -> list[Flow]:
    if not isinstance(items, list):
        raise FlowsError(f"{where} must be a list [...] of {{\"date\", \"amount\", \"note\"}} entries")
    out, per_day = [], {}
    for k, e in enumerate(items):
        w = f"{where}[{k}]"
        e = _keys(e, EXTRA_KEYS, {"date", "amount"}, w)
        d = _date(e["date"], f"{w}.date")
        if "future" in e and not isinstance(e["future"], bool):
            raise FlowsError(f"{w}.future is {e['future']!r}; it can only be true (or left out)")
        if today is not None and (d - today).days > FUTURE_DAYS and e.get("future") is not True:
            # a year typo (2062 for 2026) would otherwise wait, silently, for decades
            raise FlowsError(f"{w}.date is {e['date']!r}, more than a year from today ({today}): a typo in the year? "
                             f"If it really is planned that far ahead, add \"future\": true to the entry")
        amount = _amount(e["amount"], f"{w}.amount", positive=False)
        n = per_day.get(d, 0)
        per_day[d] = n + 1
        out.append(Flow(f"extra-{d.isoformat()}-{n}", d.isoformat(), _ms(d), amount, "extra", _note(e, w, "")))
    return out


def parse(text: str, accounts, source: str = FILE, now_ms: int | None = None) -> dict:
    """{account: [Flow, ...] in the order they apply, or the FlowsError for that account's entry}. A problem with the
    file as a whole (bad JSON, an account name it doesn't know) raises: it can't be pinned on one account, so every
    account's step stops. With `now_ms`, an extra dated more than a year ahead is refused as a likely typo."""
    today = None if now_ms is None else datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    data = _strict_json(text, source)
    if not isinstance(data, dict):
        raise FlowsError(f"{source} must be one object {{\"BTC\": {{...}}, ...}}, not {type(data).__name__}")
    data = _comments_ok(data, source)
    unknown = sorted(set(data) - set(accounts))
    if unknown:
        raise FlowsError(f"{source} names {unknown}, which isn't an account (accounts: {', '.join(accounts)}; "
                         f"capitals, as written here)")
    out = {}
    for acc, section in data.items():
        try:
            section = _keys(section, SECTION_KEYS, set(), f"{source}: {acc}")
            flows = []
            if "monthly" in section:
                flows += _monthly(section["monthly"], f"{source}: {acc}.monthly")
            if "extra" in section:
                flows += _extras(section["extra"], f"{source}: {acc}.extra", today)
            order = {"plan": 0, "extra": 1}
            out[acc] = sorted(flows, key=lambda f: (f.ts, order[f.kind], f.id))
        except FlowsError as exc:
            out[acc] = exc
    return out


def load(path: str | Path = FILE, accounts=None, now_ms: int | None = None) -> dict:
    """parse() of the file, as of `now_ms` (default: now); {} when there is none (no account has flows)."""
    import time

    from .thresholds import ACCOUNTS

    path = Path(path)
    if not path.exists():
        return {}
    return parse(path.read_text(), accounts or ACCOUNTS, source=path.name,
                 now_ms=int(time.time() * 1000) if now_ms is None else now_ms)


def schedule(plans: dict, account: str) -> list[Flow]:
    """One account's flows from load(); raises that account's FlowsError."""
    got = plans.get(account, [])
    if isinstance(got, Exception):
        raise got
    return got


def due(flows: list[Flow], recorded: list[dict], now_ms: int, account: str = "") -> list[Flow]:
    """The flows to apply now: their date has come (00:00 UTC <= now) and the ledger hasn't recorded them. Raises if
    the file no longer lists a flow the ledger recorded, or lists it with another date or amount."""
    by_id = {f.id: f for f in flows}
    who = f"{account}: " if account else ""
    for r in recorded:
        if r.get("kind") not in ("plan", "extra"):
            continue
        f = by_id.get(r["id"])
        if f is None or f.date != r["date"] or abs(f.amount - r["amount"]) > 1e-9:
            now = "no longer lists it" if f is None else f"now says {f.amount:+,.2f} on {f.date}"
            # the likeliest way to get here without editing anything: a new extra put above one on the same day
            # (entries on one day are numbered in file order)
            same_day = r["kind"] == "extra" and any(g.kind == "extra" and g.date == r["date"] for g in flows)
            fix = (f"if you added an entry dated {r['date']}, move it below the one already there. Otherwise " if
                   same_day else "")
            raise FlowsError(f"{who}{fix}put back what was recorded: a recorded deposit or withdrawal can't be "
                             f"removed or changed (correct it with a new extra entry; below 0 takes money out). The "
                             f"ledger recorded {r['id']} ({r['amount']:+,.2f} on {r['date']}) but {FILE} {now}")
    done = {r["id"] for r in recorded}
    return [f for f in flows if f.ts <= now_ms and f.id not in done]


# ---------------------------------------------------------------- the accounting

def units(start_equity: float, recorded: list[dict]) -> float:
    """Units outstanding: start_equity of them at $1 (NAV0 = 1), plus every flow's."""
    return start_equity + sum(r["units"] for r in recorded)


def record(f: Flow, ts: int, equity: float, cash: float, start_equity: float, recorded: list[dict]) -> dict:
    """The ledger's entry for `f`, taking effect at `ts` on an account worth `equity` (marked) with `cash` realised."""
    if not equity > 0:
        raise FlowsError(f"{f.id}: the account is worth ${equity:,.2f}; a flow needs a positive balance to price units")
    if equity + f.amount <= 0:
        raise FlowsError(f"{f.id}: a withdrawal of ${-f.amount:,.2f} is more than the account's ${equity:,.2f}")
    nav = equity / units(start_equity, recorded)
    return {"id": f.id, "date": f.date, "ts": ts, "amount": f.amount, "kind": f.kind, "note": f.note,
            "equity_before": equity, "cash_before": cash, "nav": nav, "units": f.amount / nav}


def rescale_brakes(risk, equity: float, amount: float) -> None:
    """The drawdown halt and the daily loss lock read the same percentage just before and just after a flow: a
    deposit can't hide a drawdown, a withdrawal can't trip one. (`risk` is engine.RiskState.)"""
    s = (equity + amount) / equity
    risk.peak_equity *= s
    risk.day_start_equity *= s


def withdrawal_wait(amount: float, equity: float, cash: float, notional: float, max_leverage: float) -> str | None:
    """Why a withdrawal can't go out now (None: it can), the way an exchange keeps back what an open position needs:
    the realised cash must stay above 0, and the position within `max_leverage` of what's left. Flat, anything up to
    the whole account can go (more than that is refused by record())."""
    if amount >= 0 or notional <= 0:
        return None
    left = equity + amount
    if cash + amount > 0 and left > 0 and notional / left <= max_leverage:
        return None
    most = max(0.0, min(cash, equity - notional / max_leverage))
    return (f"the open position needs the money: ${notional:,.2f} held, at most {max_leverage:g}x the account, so at "
            f"most ${most:,.2f} of the ${equity:,.2f} can come out now")


def _waiting(say, events, f: Flow, ts: int, why: str) -> None:
    """Said once (the board and the phone), however many runs it waits."""
    head = f"WITHDRAWAL ${-f.amount:,.2f} waiting ({_label(f.kind, f.note)}, {f.date})"
    if not any(e.agent == "bank" and e.text.startswith(head) for e in events):
        say(ts, "bank", f"{head}: {why}. It goes out at the first bar open where it fits; nothing has been taken "
                        f"out yet")


def apply_desk(desk, f: Flow, ts: int, price: float, start_equity: float, recorded: list[dict]) -> dict | None:
    """A desk account's flow (paper.step), at `ts` with the position marked at `price`: the cash moves, the brakes are
    scaled, the units are bought or sold, and the board and the phone are told. Nothing else changes. None (and
    nothing changes) for a withdrawal the open position still needs: it waits."""
    eq, pos = desk.broker.equity(price), desk.broker.position
    why = withdrawal_wait(f.amount, eq, desk.broker.cash, pos.notional(price) if pos else 0.0,
                          desk.p.sizing.max_leverage)
    if why:
        _waiting(desk.say, desk.events, f, ts, why)
        return None
    rec = record(f, ts, eq, desk.broker.cash, start_equity, recorded)
    desk.broker.cash += f.amount
    rescale_brakes(desk.risk, eq, f.amount)
    recorded.append(rec)
    desk.say(ts, "bank", describe(rec))
    return rec


def apply_book(book, f: Flow, ts: int, marks: dict, start_equity: float, recorded: list[dict]) -> dict | None:
    """The VT basket's flow (paper.step_basket), at `ts` with each coin marked at `marks` (else its last close). VT
    has no brakes: the cash and the units move, and the board and the phone are told. None for a withdrawal the
    holdings still need (more than the rules ever hold: book_leverage x max_units): it waits."""
    eq = book.equity(marks)
    held = sum(s.qty * marks.get(c, s.mark) for c, s in book.sleeves.items())
    why = withdrawal_wait(f.amount, eq, book.cash, held, book.vt.book_leverage * book.vt.max_units)
    if why:
        _waiting(book.say, book.events, f, ts, why)
        return None
    rec = record(f, ts, eq, book.cash, start_equity, recorded)
    book.cash += f.amount
    recorded.append(rec)
    book.say(ts, "bank", describe(rec))
    return rec


def _label(kind: str, note: str) -> str:
    label = {"plan": "monthly plan", "extra": "extra", "exchange": "on the exchange"}.get(kind, kind)
    return f"{label}: {note}" if note and note != label else label


def describe(rec: dict) -> str:
    """The event the board shows and the phone gets (with the flow's date: a desk board's events show the time only)."""
    a, e = rec["amount"], rec["equity_before"]
    label = f"{_label(rec['kind'], rec.get('note', ''))}, {rec['date']}"
    if a > 0:
        return (f"DEPOSIT ${a:,.2f} recorded ({label}) — money in, not profit. bank ${e:,.2f} → ${e + a:,.2f}; "
                f"performance unchanged")
    return (f"WITHDRAWAL ${-a:,.2f} recorded ({label}) — money out, not a loss. bank ${e:,.2f} → ${e + a:,.2f}; "
            f"performance unchanged")


def realised_return(start_equity: float, recorded: list[dict], cash: float) -> float:
    """The go-live gate's "up overall": the realised balance (cash) over the units, i.e. the realised NAV - 1. Each
    flow bought or sold its units at the marked NAV it found, so neither a deposit nor a withdrawal moves it, whether
    a position was open or not. With no flows it is cash / start - 1, as before."""
    return cash / units(start_equity, recorded) - 1


def years_to(target: float, equity: float, rate: float | None, future: list[tuple[float, float]]) -> float | None:
    """Years until `equity` reaches `target`, growing at `rate` a year with the flows in `future` ([(years from now,
    dollars)], ascending) added as they come. None if it never gets there."""
    if equity >= target:
        return 0.0
    if rate is None or rate <= -1:
        return None
    g = math.log(1 + rate)
    t, e = 0.0, equity
    for at, amount in future:
        at = max(at, t)
        if g > 0 and e > 0 and t + math.log(target / e) / g <= at:
            return t + math.log(target / e) / g
        e = e * math.exp(g * (at - t)) + amount
        t = at
        if e >= target:
            return t
        if e <= 0:
            return None
    return t + math.log(target / e) / g if g > 0 and e > 0 else None


@dataclass
class State:
    """What the board needs about an account's flows: what was recorded, and the plan still to come."""
    start: float
    recorded: list[dict]
    planned: list[Flow] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Boards change only once a flow has been recorded: until then they're byte for byte what they were."""
        return bool(self.recorded)

    @property
    def net(self) -> float:
        return sum(r["amount"] for r in self.recorded)

    def units(self) -> float:
        return units(self.start, self.recorded)

    def nav(self, equity: float) -> float:
        return equity / self.units()

    def future(self, now_ms: int) -> list[tuple[float, float]]:
        return [(max(0.0, (f.ts - now_ms) / YEAR_MS), f.amount) for f in self.planned]

    def summary(self, equity: float, now_ms: int) -> dict:
        rec = self.recorded
        nxt = self.planned[0] if self.planned else None
        return {
            "net": self.net, "deposited": sum(r["amount"] for r in rec if r["amount"] > 0),
            "withdrawn": -sum(r["amount"] for r in rec if r["amount"] < 0), "count": len(rec),
            "units": self.units(), "nav": self.nav(equity), "twr": self.nav(equity) - 1,
            "pnl": equity - self.start - self.net, "start": self.start,
            "marks": [[r["ts"], r["amount"], r["kind"]] for r in rec[-100:]],     # the equity chart marks each one
            "recent": [{k: r[k] for k in ("id", "date", "ts", "amount", "kind", "note")} for r in rec[-10:]],
            "planned": {"count": len(self.planned), "usd": sum(f.amount for f in self.planned),
                        "next": None if nxt is None else {"id": nxt.id, "date": nxt.date, "amount": nxt.amount},
                        # extras still to come, by date: one dated by mistake far ahead shows here
                        "extras": [{"id": f.id, "date": f.date, "amount": f.amount, "note": f.note}
                                   for f in self.planned if f.kind == "extra"][:5]},
        }
