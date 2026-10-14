"""A venue's own rules, for an account that trades whole contracts on a session (thresholds.ContractVenue): today,
Coinbase's US nano BTC perp. The backtest and the paper step use this one module, so they apply the same rules:

    SIZE      the desk's risk-based size, cut DOWN to whole contracts of 0.01 BTC at the bar's price, at most 4x the
              account. 0 contracts (one would risk more than the budget, or be more than 4x) = no entry. Then the
              overnight margin check: equity minus the loss at the stop must cover contracts x contract notional x the
              overnight margin rate; contracts are cut until it does, and none left = no entry.
    SESSION   no entry is decided while its fill (the next bar's open) falls in a closure: the weekly break, Friday
              17:00-18:00 New York time, or a maintenance window (known, or listed by Coinbase and recorded by paper),
              nor while Coinbase reports its session closed (paper's newest bar only, like the spread and depth gates,
              and only when that close is decided on time: a late one goes by the schedule, as in the backtest) or its
              answer has no session details (closed for entries, saying why). Coinbase has no candles while it's
              closed, so a stop isn't checked then and a gap fills at the reopen; the backtest drops the Binance bars
              that fall wholly inside a closure to match, and paper waits for a candle missing outside one (missing).
    FEES      per side, max($0.20 x contracts, 5 bps of notional): an ESTIMATE until the real fee tier is known.
    FUNDING   Coinbase funds hourly and has no funding history API: paper keeps a log of each run's rate
              (FundingLog) and charges each hour the nearest rate it knows. The backtest uses Binance's prints. With
              no new rate for a day the funding gate WAITs (paper's newest bar only).

Nothing here touches an account whose venue has no rules of its own (ETH, SOL, VT on Hyperliquid).
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from .models import FundingEvent, GateResult, Verdict
from .thresholds import ContractVenue

HOUR_MS = 3600_000
DAY_MS = 86400_000
VENUE_GATES = ("session", "contracts", "margin")
FUNDING_STALE_MS = 3 * HOUR_MS      # no new hourly rate for 3 funding hours: paper says so, once (an estimate gets older)
FUNDING_UNKNOWN_MS = 24 * HOUR_MS   # a day: the funding gate WAITs rather than judge an entry on that old a rate
WINDOW_KEEP_MS = 200 * DAY_MS       # a recorded maintenance window explains its missing candles this long (the 1,560-bar
                                    # window paper reads is 130 days)


def _ms(iso: str) -> int:
    """'2026-10-24T10:00' or '2026-10-24T10:00:00Z' (UTC) -> epoch ms."""
    s = iso.strip().replace("Z", "")
    d = datetime.fromisoformat(s)
    return int(d.replace(tzinfo=timezone.utc).timestamp() * 1000) if d.tzinfo is None else int(d.timestamp() * 1000)


def _hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")


def _when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%a %b %d %H:%M")


# ---------------------------------------------------------------- sessions

def new_york_utc_offset(d: date) -> int:
    """New York's UTC offset in hours on date `d` (-4 in daylight time, -5 otherwise), by the US rule since 2007: from
    the second Sunday of March to the first Sunday of November. Exact for any day but the two changeover Sundays
    themselves, which the weekly break (a Friday) never falls on."""
    def nth_sunday(month: int, n: int) -> date:
        first = date(d.year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return -4 if nth_sunday(3, 2) <= d < nth_sunday(11, 1) else -5


class Sessions:
    """When the venue is closed: its weekly break, plus maintenance windows as [start, end) epoch ms."""

    def __init__(self, venue: ContractVenue, windows=()):
        self.weekday, self.hour, self.minutes = venue.weekly_break or (None, 0, 0)    # None: no weekly break
        known = [(_ms(a), _ms(b)) for a, b in venue.known_closures]
        self.windows = sorted({(int(a), int(b)) for a, b in [*known, *windows] if int(b) > int(a)})

    def weekly(self, ts: int) -> tuple[int, int] | None:
        """The weekly break on ts's UTC date, if that date is its weekday (it ends by 23:00 UTC: never across
        midnight)."""
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if d.weekday() != self.weekday:
            return None
        start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000) + \
            (self.hour - new_york_utc_offset(d)) * HOUR_MS
        return start, start + self.minutes * 60_000

    def closure(self, ts: int) -> tuple[int, int, str] | None:
        """(start, end, why) of the closure `ts` falls in, or None while the venue is open."""
        for a, b in self.windows:
            if a <= ts < b:
                return a, b, "maintenance"
        w = self.weekly(ts)
        if w and w[0] <= ts < w[1]:
            return w[0], w[1], "weekly break"
        return None

    def closed_through(self, t0: int, t1: int) -> bool:
        """The venue is closed for all of [t0, t1): no trade, so no candle."""
        t = t0
        while t < t1:
            c = self.closure(t)
            if c is None:
                return False
            t = c[1]
        return True

    def next_closure(self, ts: int, within_ms: int = 8 * DAY_MS) -> tuple[int, int, str] | None:
        """The next closure starting after `ts` (or the one it's in), for the board."""
        cur = self.closure(ts)
        if cur:
            return cur
        cands = [(a, b, "maintenance") for a, b in self.windows if ts < a <= ts + within_ms]
        for k in range(8):
            w = self.weekly(ts + k * DAY_MS)
            if w and ts < w[0] <= ts + within_ms:
                cands.append((w[0], w[1], "weekly break"))
        return min(cands) if cands else None


# ---------------------------------------------------------------- the rules

class Rules:
    """One venue's rules as they stand for a run: the spec (thresholds.ContractVenue), the closures known, and the
    margin rates (paper passes what Coinbase answered this run; the backtest the spec's)."""

    def __init__(self, venue: ContractVenue, windows=(), margin: dict | None = None):
        self.spec = venue
        self.sessions = Sessions(venue, windows)
        m = margin or {}
        self.margin = {"overnight_long": m.get("overnight_long") or venue.overnight_long_margin,
                       "overnight_short": m.get("overnight_short") or venue.overnight_short_margin,
                       "intraday_long": m.get("intraday_long") or venue.intraday_long_margin,
                       "intraday_short": m.get("intraday_short") or venue.intraday_short_margin}

    @property
    def name(self) -> str:
        return self.spec.name

    # ---- contracts and fees
    def contracts(self, qty: float) -> int:
        return int(round(qty / self.spec.contract_size))

    def qty(self, n: int) -> float:
        return round(n * self.spec.contract_size, 10)

    def describe(self, qty: float, coin: str = "BTC") -> str:
        n = self.contracts(qty)
        return f"{n} contract{'s' if n != 1 else ''} ({qty:.2f} {coin})"

    def fee(self, qty: float, price: float, fee_rate: float) -> float:
        """One side's fee (an ESTIMATE): max(the per-contract minimum x contracts, the rate x notional)."""
        return max(self.spec.fee_min_per_contract * self.contracts(qty), qty * price * fee_rate)

    def overnight_margin(self, side_sign: int) -> float:
        return self.margin["overnight_long"] if side_sign > 0 else self.margin["overnight_short"]

    def margin_in_use(self, qty: float, price: float, side_sign: int) -> float:
        return qty * price * self.overnight_margin(side_sign)

    # ---- the gates
    def session_gate(self, ts_fill: int, ctx=None) -> GateResult:
        """No entry while its fill would fall in a closure, or (paper's newest bar, decided on time) while Coinbase says
        it's closed, or while its answer doesn't say (ctx.session_note: fail closed, saying why)."""
        c = self.sessions.closure(ts_fill)
        if c:
            a, b, why = c
            return GateResult("session", Verdict.DROP, owner="fills",
                              note=f"Coinbase closed at the fill ({why}, reopens {_hhmm(b)} UTC)")
        if ctx is not None and getattr(ctx, "session_open", None) is False:
            return GateResult("session", Verdict.DROP, owner="fills",
                              note=getattr(ctx, "session_note", None) or "Coinbase reports its session closed")
        return GateResult("session", Verdict.PASS, owner="fills", note="Coinbase open at the fill")

    def funding_gate(self, ctx=None) -> GateResult | None:
        """Paper's newest bar only (ctx.funding_age_ms: how old the newest logged funding rate is): no entry judged on a
        funding rate a day old or older (Coinbase stopped answering it). None when there's nothing to judge (the
        backtest, a catch-up bar)."""
        age = getattr(ctx, "funding_age_ms", None) if ctx is not None else None
        if age is None:
            return None
        if age >= FUNDING_UNKNOWN_MS:
            return GateResult("funding_known", Verdict.WAIT, round(age / HOUR_MS, 1), FUNDING_UNKNOWN_MS / HOUR_MS,
                              "max", "crowd", f"funding unknown: no new Coinbase rate for {age / HOUR_MS:.0f}h")
        return GateResult("funding_known", Verdict.PASS, round(age / HOUR_MS, 1), FUNDING_UNKNOWN_MS / HOUR_MS, "max",
                          "crowd", "hours since Coinbase's last funding rate")

    def size(self, qty: float, price: float, stop: float, equity: float, side_sign: int,
             risk_budget: float) -> tuple[float, list[GateResult]]:
        """The desk's size cut down to whole contracts, at most max_leverage, then cut to what overnight margin
        allows. Returns the quantity to trade (0.0 = no entry) and the gates that say why."""
        v = self.spec
        cs = v.contract_size
        one = cs * price                                   # one contract's notional
        loss1 = cs * abs(price - stop)                     # one contract's loss at the stop
        want = qty / cs if cs > 0 else 0.0
        n = math.floor(want + 1e-9)
        cap = math.floor(equity * v.max_leverage / one + 1e-9) if equity > 0 and one > 0 else 0
        n = max(0, min(n, cap, v.max_contracts))
        if n < v.min_contracts:
            risk1, lev1 = (loss1 / equity, one / equity) if equity > 0 else (math.inf, math.inf)
            if risk1 > risk_budget * (1 + 1e-9):
                why = f"one contract ({_usd(one)}) would risk {risk1:.1%} at this stop (budget {risk_budget:.0%})"
            elif lev1 > v.max_leverage:
                why = f"one contract ({_usd(one)}) is {lev1:.1f}x the account (cap {v.max_leverage:g}x)"
            else:
                why = f"the size comes to {want:.2f} contracts; whole contracts only"
            return 0.0, [GateResult("contracts", Verdict.DROP, round(want, 4), float(v.min_contracts), "min", "size",
                                    why)]
        gates = [GateResult("contracts", Verdict.PASS, float(n), float(v.min_contracts), "min", "size",
                            f"{n} x {cs:g} BTC, cut down from {want:.2f}")]
        rate = self.overnight_margin(side_sign)
        first = n
        while n >= 1 and equity - n * loss1 < n * one * rate:
            n -= 1
        if n == 0:
            need, left = first * one * rate, equity - first * loss1
            gates.append(GateResult("margin", Verdict.DROP, round(left, 2), round(need, 2), "min", "size",
                                    f"{first} contract{'s' if first != 1 else ''} need {_usd(need)} overnight margin "
                                    f"({rate:.1%}); the stop would leave {_usd(left)}"))
            return 0.0, gates
        need, left = n * one * rate, equity - n * loss1
        note = f"{_usd(need)} overnight margin ({rate:.1%}) of {_usd(left)} left at the stop"
        if n < first:
            note = f"cut from {first} to {n} contracts: " + note
        gates.append(GateResult("margin", Verdict.PASS, round(left, 2), round(need, 2), "min", "size", note))
        return self.qty(n), gates

    def min_equity_for_one(self, price: float, stop: float, side_sign: int, risk_budget: float,
                           desk_max_leverage: float | None = None) -> float:
        """The smallest account that holds one contract at `price` with this stop, as size() decides: the loss at the
        stop within the risk budget, one contract's notional within the leverage caps, and the equity left at the
        stop covering its overnight margin. (What the board shows an account too small to trade.)"""
        one = self.spec.contract_size * price
        loss1 = self.spec.contract_size * abs(price - stop)
        need = [loss1 / risk_budget if risk_budget > 0 else math.inf, one / self.spec.max_leverage,
                loss1 + one * self.overnight_margin(side_sign)]
        if desk_max_leverage:
            need.append(one / desk_max_leverage)
        return max(need)

    def as_dict(self) -> dict:
        return {"spec": asdict(self.spec), "margin": dict(self.margin), "windows": self.sessions.windows}


def _usd(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def skip_note(gates: list[GateResult]) -> GateResult | None:
    """The venue gate that stopped an entry everything else let through (only venue gates dropped it), or None."""
    blocking = [g for g in gates if g.verdict in (Verdict.DROP, Verdict.WAIT)]
    if blocking and all(g.name in VENUE_GATES for g in blocking):
        return blocking[0]
    return None


def tradable(bars, sessions: Sessions, bar_ms: int):
    """The bars a closed venue would have: none wholly inside a closure (Binance trades through Coinbase's)."""
    return [b for b in bars if not sessions.closed_through(b.ts, b.ts + bar_ms)]


def missing(bar_ts, sessions: Sessions, first_ms: int, last_ms: int, bar_ms: int, gaps=()) -> list[int]:
    """The bar opens in [first_ms, last_ms] (on the bar grid) the venue should have a candle for and the answer
    (`bar_ts`, its candles' opens) hasn't: not wholly inside a closure, and not a gap already accepted (`gaps`: candles
    Coinbase never published)."""
    have, skip = set(bar_ts), {int(g) for g in gaps}
    t = -(-first_ms // bar_ms) * bar_ms
    out = []
    while t <= last_ms:
        if t not in have and t not in skip and not sessions.closed_through(t, t + bar_ms):
            out.append(t)
        t += bar_ms
    return out


# ---------------------------------------------------------------- funding, from snapshots

class FundingLog:
    """Coinbase's hourly funding rate, as each run read it: [[funding_time_ms, rate], ...], oldest first. There is no
    funding history to ask for, so this is the history. Each hour is charged the rate of the nearest snapshot in time
    (an ESTIMATE between runs); the 3-day funding APR gate reads the same events."""

    def __init__(self, rows=None, keep_days: float = 8.0):
        self.keep_ms = int(keep_days * DAY_MS)
        self.rows: list[list] = sorted([[int(t), float(r)] for t, r in (rows or [])], key=lambda x: x[0])

    def add(self, ts: int | None, rate: float | None) -> bool:
        """Record one snapshot (Coinbase's funding_time and funding_rate). A time already logged is updated."""
        if ts is None or rate is None or not math.isfinite(rate):
            return False
        ts = int(ts)
        for row in self.rows:
            if row[0] == ts:
                row[1] = float(rate)
                return True
        self.rows.append([ts, float(rate)])
        self.rows.sort(key=lambda x: x[0])
        return True

    def trim(self, now_ms: int, keep_from_ms: int | None = None) -> None:
        """Keep keep_days of it, or back to `keep_from_ms` if that's earlier (a run catching up after a longer outage
        still charges those hours their nearest rate), plus the newest snapshot before that, the nearest for the hours
        just after it. Never the newest snapshot of all."""
        bound = now_ms - self.keep_ms if keep_from_ms is None else min(now_ms - self.keep_ms, keep_from_ms)
        older = [r for r in self.rows if r[0] < bound]
        keep = older[-1:] + [r for r in self.rows if r[0] >= bound]
        self.rows = keep or self.rows[-1:]

    def rate_at(self, ts: int) -> float | None:
        if not self.rows:
            return None
        best = min(self.rows, key=lambda r: (abs(r[0] - ts), -r[0]))   # nearest; a tie goes to the later one
        return best[1]

    def events(self, start_ms: int, end_ms: int, every_ms: int = HOUR_MS) -> list[FundingEvent]:
        """One event per funding hour in (start_ms, end_ms], at the nearest snapshot's rate."""
        if not self.rows:
            return []
        out = []
        t = (start_ms // every_ms + 1) * every_ms
        while t <= end_ms:
            out.append(FundingEvent(t, self.rate_at(t)))
            t += every_ms
        return out

    def to_list(self) -> list[list]:
        return [list(r) for r in self.rows]


# ---------------------------------------------------------------- what paper remembers about the venue

def remember(saved: dict | None, details: dict | None, now_ms: int, venue: ContractVenue,
             keep_from_ms: int | None = None) -> dict:
    """The ledger's "venue" record after this run: the margin rates and session Coinbase answered (or the last ones
    saved, if this answer had none), the maintenance windows it has listed (kept WINDOW_KEEP_MS, as their missing
    candles stay in the history paper reads that long), and the funding log with this run's snapshot, kept back to
    `keep_from_ms` if a catch-up needs that. `details` = CoinbaseFeed.product()'s parse, or None (no answer this run)."""
    s = dict(saved or {})
    margin = dict(s.get("margin") or {})
    windows = [list(w) for w in s.get("windows") or []]
    log = FundingLog(s.get("funding_log"), venue.funding_log_days)
    if details:
        for k, v in (details.get("margin") or {}).items():
            if v:
                margin[k] = v
        w = details.get("maintenance")
        if w and w not in windows:
            windows.append(list(w))
        log.add(details.get("funding_time"), details.get("funding_rate"))
        s["session"] = {"open": details.get("session_open"), "state": details.get("session_state"),
                        "read_ts": now_ms}
        s["read_ts"] = now_ms
    windows = sorted(w for w in windows if w[1] >= now_ms - WINDOW_KEEP_MS)[-40:]
    log.trim(now_ms, keep_from_ms)
    s.update({"name": venue.name, "product": venue.product, "margin": margin, "windows": windows,
              "funding_log": log.to_list()})
    return s
