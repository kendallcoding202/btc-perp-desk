"""thresholds.py — every number the desk believes.

Nothing else in the codebase hard-codes a trading number. If you want to change
how the desk behaves, change it here, re-run the backtest, and look at the
report before you let it near real money.

HARD gates are arithmetic: cheap, objective, and any failure blocks a new entry.
SOFT gates are the agents' judgement scores: a failure blocks the entry, and a
pass that is close to the line is flagged MARK so you can see it on the board.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace


@dataclass(frozen=True)
class Market:
    symbol: str = "BTC"                 # the only thing this desk trades
    interval: str = "4h"                # bar size the desk thinks in
    venue: str = "hyperliquid"          # where live prices / orders go (see brokers/)


@dataclass(frozen=True)
class Strategy:
    # SCAN — finding what moves. Trend-following breakout on a Donchian channel.
    breakout_bars: int = 30             # close beyond the prior 30-bar high/low = breakout
    ema_fast: int = 50                  # trend filter: fast EMA must agree with direction
    ema_slow: int = 200
    atr_bars: int = 14                  # volatility yardstick for stops and sizing
    allow_shorts: bool = False          # shorts added nothing 2020-23 and doubled drawdown (see reports/)

    # RISK — the stop.
    stop_atr: float = 3.0               # initial stop distance in ATRs
    trail_atr: float = 3.0              # chandelier trail: best close since entry minus N ATRs
    exit_on_trend_flip: bool = True     # close when fast EMA crosses back through slow EMA
    take_profit_atr: float | None = None  # sell outright once up N ATRs. Off: tested, it cuts the big winners
                                          # this strategy lives on (see the take-profit table in reports/)


@dataclass(frozen=True)
class Hard:
    """HARD — arithmetic, costs nothing. Fail = DROP."""
    min_history_bars: int = 250         # enough bars to warm up the slow EMA
    max_data_age_bars: float = 1.5      # last closed bar older than this many bars = stale feed
    min_atr_pct: float = 0.20           # ATR as % of price; below = dead tape, fees eat the edge
    max_atr_pct: float = 6.00           # above = disorderly market, stand aside
    max_abs_funding_apr: float = 150.0  # |funding| annualised %; beyond = squeeze territory
    max_spread_bps: float = 5.0         # live/paper only: top-of-book spread
    min_depth_usd: float = 250_000.0    # live/paper only: resting size within 10 bps each side


@dataclass(frozen=True)
class Soft:
    """SOFT — the agents' answers, each a 0..1 score. Fail = DROP, near the line = MARK.

    None = the score is still computed and shown on the board, but never blocks a trade.
    The first three were blocking in the original design; tested on 2020-2023 only, each one
    cost money (trend_strength worst: it rejects breakouts before the EMAs have spread, which is
    exactly when entries pay best). Crowding was the only soft gate that helped, so it alone blocks.
    """
    min_trend_strength: float | None = None   # SCAN: how cleanly price is trending (EMA spread vs ATR); was 0.50
    min_efficiency: float | None = None       # VET: Kaufman efficiency ratio, is the move real or noise; was 0.15
    max_extension: float | None = None        # VET: momentum already spent (distance from fast EMA); was 0.80
    max_crowding: float | None = 0.85         # CROWD: funding pressure on our side (1 = everyone's paying to be with us)
    mark_margin: float = 0.10                 # within 10% of a soft line = MARK


@dataclass(frozen=True)
class Sizing:
    """SIZE — how much."""
    starting_equity: float = 50.0       # paper (and backtest) start with $50
    goal_equity: float = 100_000.0      # the board tracks progress toward this
    risk_per_trade: float = 0.02        # lose 2% of equity if the stop is hit. At $50, 1% would make about
                                        # 1 in 7 tickets smaller than the exchange minimum below
    max_leverage: float = 3.0           # notional never exceeds 3x equity
    min_notional_usd: float = 10.0      # Hyperliquid's minimum order value; smaller tickets are skipped


@dataclass(frozen=True)
class Risk:
    """RISK — the desk-level brakes."""
    daily_loss_limit: float = 0.03      # down 3% on the UTC day = flatten, no entries until tomorrow
    max_drawdown_halt: float = 0.25     # down 25% from peak = flatten and HALT (manual reset)
    max_consecutive_losses: int = 5     # after 5 losers in a row...
    cooldown_bars: int = 6              # ...sit out 6 bars


@dataclass(frozen=True)
class Costs:
    """What the exchange takes. Backtests that ignore this are fiction."""
    taker_fee_bps: float = 4.5          # Hyperliquid base tier taker = 0.045%
    slippage_bps: float = 3.0           # assumed adverse fill vs reference price


@dataclass(frozen=True)
class Promotion:
    """Gate between paper and real money. `python -m desk live` refuses to start unless this passes."""
    min_backtest_years: float = 3.0
    min_oos_profit_factor: float = 1.15  # out-of-sample gross wins / gross losses
    min_oos_return: float = 0.0          # out-of-sample must be net profitable after all costs
    max_backtest_drawdown: float = 0.35
    min_paper_days: float = 14.0
    min_paper_return: float = 0.0


@dataclass(frozen=True)
class Jev:
    """Optional second opinion from TypeSafe's Jev (needs JEV_API_KEY). Asked only when every other gate passed."""
    mode: str = "advisory"               # "off" | "advisory" (shown, never blocks) | "gate" (can DROP)
                                         # advisory by default: replayed over 94 historical entries, Jev's answers
                                         # had ~0 correlation with trade outcome (see reports/), so it gets no veto.
    model: str = "jev-latest"
    timeout_s: float = 3.0               # Jev normally answers in ~0.3s; on timeout the gate is SKIPPED
    max_momentum_spent: float = 0.70     # noul: "has most of this move already happened?"
    min_follow_through: float = 0.40     # noul: "will this breakout follow through?"
    min_regime_trend: float = 0.40       # choice: probability the regime is "trend"


@dataclass(frozen=True)
class Params:
    market: Market = field(default_factory=Market)
    strategy: Strategy = field(default_factory=Strategy)
    hard: Hard = field(default_factory=Hard)
    soft: Soft = field(default_factory=Soft)
    sizing: Sizing = field(default_factory=Sizing)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    promotion: Promotion = field(default_factory=Promotion)
    jev: Jev = field(default_factory=Jev)

    def with_(self, **sections) -> "Params":
        """Override fields per section: params.with_(strategy={"stop_atr": 2.5})."""
        updates = {}
        for name, overrides in sections.items():
            updates[name] = replace(getattr(self, name), **overrides)
        return replace(self, **updates)

    def as_dict(self) -> dict:
        return {
            f.name: {g.name: getattr(getattr(self, f.name), g.name) for g in fields(getattr(self, f.name))}
            for f in fields(self)
        }


DEFAULT = Params()

INTERVAL_SECONDS = {
    "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400,
}
