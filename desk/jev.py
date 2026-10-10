"""JEV — TypeSafe AI's System One model, used as a second opinion on entries.

Jev doesn't write text: you hand it a state and typed questions and it returns
probabilities. The desk asks it three things about a breakout that already passed
every other gate (so it's called a few times a month, not every bar):

    momentum_already_spent  (noul)    has most of this move already happened?
    follow_through          (noul)    will this breakout keep going?
    regime                  (choice)  trend / chop / reversal

The state we send is relative numbers only — no dates, no absolute prices — so
that when the backtest replays history through Jev it can't simply recognise
"this is the November 2021 top". That reduces, but cannot eliminate, the chance
the model has memorised the past; treat any backtest improvement from Jev with
suspicion until paper trading confirms it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .models import GateResult, Side, Verdict
from .thresholds import Params

URL = "https://api.typesafe.ai/v1/systemone"

QUESTIONS = {
    "momentum_already_spent": {
        "type": "noul",
        "instructions": "Yes if most of this move has probably already happened, so entering in the "
                        "breakout direction now would be chasing a move that is likely to stall or reverse.",
    },
    "follow_through": {
        "type": "noul",
        "instructions": "Yes if this breakout is likely to keep moving in the breakout direction over the "
                        "next several bars rather than failing back into its prior range.",
    },
    "regime": {
        "type": "choice",
        "criteria": {
            "trend": "price is in a sustained directional trend",
            "chop": "price is ranging back and forth without direction",
            "reversal": "an existing trend is exhausting and turning the other way",
        },
    },
}


def describe(snapshot: dict) -> str:
    """Turn the desk's numbers into the plain-language state Jev reads."""
    d = snapshot
    side = "LONG (upside breakout)" if d["direction"] > 0 else "SHORT (downside breakout)"
    return (
        f"{d.get('coin', 'BTC')} perpetual futures, {d['interval']} bars. Candidate entry: {side}.\n"
        f"Close just broke the prior {d['breakout_bars']}-bar extreme by {d['breakout_atr']:.2f} ATR.\n"
        f"Move over last 3 bars: {d['ret_3']:+.2f}%. Over last 18 bars: {d['ret_18']:+.2f}%. "
        f"Over last 90 bars: {d['ret_90']:+.2f}%.\n"
        f"Close is {d['dist_fast_atr']:+.2f} ATR from the {d['ema_fast']}-bar EMA; "
        f"{d['ema_fast']}-EMA is {d['ema_spread_atr']:+.2f} ATR from the {d['ema_slow']}-EMA.\n"
        f"Efficiency ratio (net move / path, {d['breakout_bars']} bars): {d['efficiency']:.2f}.\n"
        f"ATR is {d['atr_pct']:.2f}% of price; ATR vs its 90-bar average: {d['atr_vs_avg']:.2f}x.\n"
        f"Funding (3-day avg, annualised): {d['funding_apr']:+.1f}%. Positive = longs pay shorts."
    )


class JevClient:
    def __init__(self, api_key: str | None = None, model: str = "jev-latest", timeout_s: float = 3.0,
                 cache_path: str | Path | None = None):
        # None = read JEV_API_KEY from the environment; "" = explicitly no key
        self.api_key = os.environ.get("JEV_API_KEY", "") if api_key is None else api_key
        self.model = model
        self.timeout_s = timeout_s
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        self.last_error = ""
        if self.cache_path and self.cache_path.exists():
            for line in self.cache_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._cache[row["key"]] = row["answers"]

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def ask(self, state: str, questions: dict = QUESTIONS) -> dict | None:
        """Returns Jev's `answers` dict, or None if Jev is unavailable (the gate then SKIPs, never blocks)."""
        key = hashlib.sha256(json.dumps([self.model, state, questions], sort_keys=True).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        if not self.enabled:
            return None
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                answers = json.loads(resp.read())["answers"]
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            self.errors += 1
            self.last_error = str(exc)[:200]
            return None
        self.calls += 1
        with self._lock:
            self._cache[key] = answers
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with self.cache_path.open("a") as fh:
                    fh.write(json.dumps({"key": key, "answers": answers}) + "\n")
        return answers


def gates_from_answers(answers: dict | None, p: Params) -> list[GateResult]:
    """Map Jev's answers onto soft gates. In 'advisory' mode a failure is reported as MARK, never DROP."""
    j = p.jev
    fail = Verdict.DROP if j.mode == "gate" else Verdict.MARK
    if answers is None:
        return [GateResult("jev", Verdict.SKIP, owner="jev", note="jev unavailable — not consulted")]
    spent = answers.get("momentum_already_spent", {}).get("noul")
    follow = answers.get("follow_through", {}).get("noul")
    trend = answers.get("regime", {}).get("probabilities", {}).get("trend")
    out = []
    for name, value, thr, op in (
        ("jev_momentum_spent", spent, j.max_momentum_spent, "max"),
        ("jev_follow_through", follow, j.min_follow_through, "min"),
        ("jev_regime_trend", trend, j.min_regime_trend, "min"),
    ):
        if value is None:
            out.append(GateResult(name, Verdict.SKIP, owner="jev", note="no answer"))
            continue
        ok = value <= thr if op == "max" else value >= thr
        out.append(GateResult(name, Verdict.PASS if ok else fail, value, thr, op, owner="jev"))
    return out


def snapshot(features, i: int, side: Side, p: Params) -> dict:
    """Relative-only description of bar i for Jev (see module docstring for why)."""
    bars = features.bars
    c = bars[i].close
    a = features.atr[i] or 1e-9
    d = side.sign

    def ret(n: int) -> float:
        j = max(0, i - n)
        return (c / bars[j].close - 1) * 100

    level = features.don_hi[i] if d > 0 else features.don_lo[i]
    window = [x for x in features.atr[max(0, i - 90):i + 1] if x]
    return {
        "coin": p.market.symbol, "interval": p.market.interval, "direction": d,
        "breakout_bars": p.strategy.breakout_bars,
        "breakout_atr": (c - level) / a * d if level else 0.0,
        "ret_3": ret(3), "ret_18": ret(18), "ret_90": ret(90),
        "ema_fast": p.strategy.ema_fast, "ema_slow": p.strategy.ema_slow,
        "dist_fast_atr": (c - features.ema_fast[i]) / a,
        "ema_spread_atr": (features.ema_fast[i] - features.ema_slow[i]) / a,
        "efficiency": features.efficiency[i] or 0.0,
        "atr_pct": a / c * 100,
        "atr_vs_avg": a / (sum(window) / len(window)) if window else 1.0,
        "funding_apr": features.funding_apr[i],
    }
