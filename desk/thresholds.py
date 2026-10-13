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
    symbol: str = "BTC"                 # Hyperliquid coin name; see COINS below for the paper accounts
    interval: str = "2h"                # bar size the desk thinks in. 2h beat 4h on 2020-23 (+157% vs +104%
                                        # at 2% risk) and held up out-of-sample; 1h was worse (fees, noise)
    venue: str = "hyperliquid"          # where live prices / orders go (see brokers/); a venue in VENUES below
                                        # also brings its own rules (whole contracts, sessions, margin, fees)


@dataclass(frozen=True)
class Strategy:
    # SCAN — finding what moves. Trend-following breakout on a Donchian channel.
    breakout_bars: int = 60             # close beyond the prior 60-bar (5-day) high/low = breakout
    ema_fast: int = 100                 # trend filter: fast EMA must agree with direction (~8 days)
    ema_slow: int = 400                 # (~33 days)
    atr_bars: int = 28                  # volatility yardstick for stops and sizing (~2.3 days)
    allow_shorts: bool = False          # shorts added nothing 2020-23 and doubled drawdown (see reports/)
    short_mode: str = "breakdown"       # with allow_shorts: "breakdown" (mirror of the long breakout),
                                        # "failed_rally" (downtrend bounce rejected at the fast EMA),
                                        # "failed_breakout" (bull trap: new high, then back below it), "both_failed"
    trap_window_bars: int = 6           # failed_breakout: the breakout happened within the last 6 bars (12h)...
    trap_atr: float = 0.0               # ...and price just closed this many ATRs back below the breakout level
    rally_window_bars: int = 6          # failed_rally: a high touched the fast EMA within the last 6 bars...
    rally_reject_atr: float = 0.5       # ...and price just closed this many ATRs back below it
    short_stop_atr: float | None = None  # shorts' stop/trail width in ATRs (None = same as stop_atr/trail_atr)
    entry_mode: str = "breakout"        # "breakout" | "pullback" (buy dips in the trend) | "both"
    pullback_atr: float = 1.0           # pullback = close at least this many ATRs on the wrong side of the fast EMA...
    pullback_hold_slow: bool = True     # ...while still on the right side of the slow EMA (trend intact)

    # RISK — the stop.
    stop_atr: float = 3.0               # initial stop distance in ATRs
    trail_atr: float = 3.0              # chandelier trail: best close since entry minus N ATRs
    exit_on_trend_flip: bool = True     # close when fast EMA crosses back through slow EMA
    take_profit_atr: float | None = None  # sell outright once up N ATRs. Off: tested, it cuts the big winners
                                          # this strategy lives on (see the take-profit table in reports/)


@dataclass(frozen=True)
class Hard:
    """HARD — arithmetic, costs nothing. Fail = DROP."""
    min_history_bars: int = 450         # enough bars to warm up the slow EMA
    max_data_age_bars: float = 1.5      # last closed bar older than this many bars = stale feed
    min_atr_pct: float = 0.14           # ATR as % of price; below = dead tape, fees eat the edge
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
    starting_equity: float = 150.0      # paper (and backtest) start with $150: below ~$150 the ETH/SOL tickets
                                        # (≈0.3-0.5x equity) start to fall under Hyperliquid's $10 minimum order
    goal_equity: float = 100_000.0      # the board tracks progress toward this
    risk_per_trade: float = 0.05        # AGGRESSIVE: lose 5% of equity if the stop is hit. Position size =
                                        # risk / stop distance, so a tight setup gets more leverage, a wild one
                                        # less (2020-26: median 1.7x, max 4x). 2% = +21%/yr, 12% max drawdown;
                                        # 5% = +50%/yr, 28%; 8% = +78%/yr, 41% (an 8-loss streak ≈ -49%)
    max_leverage: float = 5.0           # notional never exceeds 5x equity
    min_notional_usd: float = 10.0      # Hyperliquid's minimum order value; smaller tickets are skipped


@dataclass(frozen=True)
class Risk:
    """RISK — the desk-level brakes."""
    daily_loss_limit: float = 0.10      # down 10% on the UTC day = flatten, no entries until tomorrow (2 stop-outs)
    max_drawdown_halt: float = 0.40     # down 40% from peak = flatten and HALT (manual reset)
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


@dataclass(frozen=True)
class VolTrend:
    """VT — the vol-trend basket, a 4th paper account (desk/voltrend.py). PAPER ONLY: it has no live mode.

    One account, three sleeves (BTC, ETH, SOL), a third of the book each. At every 00:00 UTC daily close, each coin
    gets a size in "units": 0.40 / its 10-day realised vol (capped at 2) while the close is above its 50-day
    average, else 0. Orders fill at the next day's open. Frozen: these exact numbers passed the October 2026 edge
    hunt's pre-set bar and then two independent checks (research/edge-hunt-2026-10/candidates/voltgt/). Don't tune them.
    """
    coins: tuple[str, ...] = ("BTC", "ETH", "SOL")   # one sleeve each; a coin with no history yet is a cash sleeve
    interval: str = "1d"                # decided at the 00:00 UTC close, filled at the next daily bar's open
    vol_lookback: int = 10              # realised vol = sample std of the last 10 daily returns x sqrt(365)
    target_vol: float = 0.40            # units = 0.40 / realised vol: a sleeve at 1 unit runs about 40%/yr vol...
    max_units: float = 2.0              # ...capped at 2 units, so a very quiet coin can't take an outsized position
    sma_bars: int = 50                  # trend gate: hold a coin only while its close is above its 50-day average
                                        # (the mean of the last 50 closes, today's included). The gate carries the
                                        # edge: without it the same sizing made OOS Sharpe 0.61 with a 40% drop
    band: float = 0.25                  # no-trade band: keep the held units unless the new size is >25% away
                                        # (entries and exits always go through)
    book_leverage: float = 0.65         # notional per sleeve = equity x 0.65 / 3 x units: 2020-23 max drawdown 26%
    min_order_usd: float = 10.0         # Hyperliquid's minimum order: smaller entries and resizes are skipped
                                        # (a skipped entry is retried at the next close; exits always go through)
    starting_equity: float = 500.0      # entries are ~0.14x equity and resizes ~0.06x, so below ~$250 the $10
                                        # minimum starts skipping ETH/SOL orders (a fifth to a third of them at
                                        # $150) and at $50 it blocks most of them (fresh starts since 2024: $50 has
                                        # 351 orders blocked, $250 28, $500 3). Yours to change: the backtest's
                                        # minimum-capital table (reports/backtest-VT.md) shows what each size costs
    goal_equity: float = 100_000.0      # the board tracks progress toward this, like the desk accounts


VOLTREND = VolTrend()


@dataclass(frozen=True)
class ContractVenue:
    """A venue with its own rules: whole contracts, a trading session, margin, a fee schedule. These are the
    exchange's rules, not the strategy's: the desk decides exactly as before, and desk/venue.py decides what of it this
    venue lets through and what it costs. Used by the backtest and the paper step alike.

    The one there is: Coinbase's US-regulated nano BTC perp (Coinbase Financial Markets, "BTC PERP", product
    BIP-20DEC30-CDE), the venue the BTC account is planned to trade with real money. The numbers below were read from
    Coinbase's public API on 2026-10-13 (GET api.coinbase.com/api/v3/brokerage/market/products/BIP-20DEC30-CDE).
    """
    name: str = "coinbase"
    product: str = "BIP-20DEC30-CDE"
    display: str = "COINBASE · BTC PERP"
    contract_size: float = 0.01         # BTC per contract (future_product_details.contract_size)
    min_contracts: int = 1              # whole contracts only: base_increment 1, base_min_size 1...
    max_contracts: int = 5000           # ...base_max_size 5000
    max_leverage: float = 4.0           # the desk's cap: overnight margin (~21%) allows at most ~4.76x; 4x leaves room
    # Margin, a fraction of notional (future_product_details.*_margin_rate). Paper reads these every run (falling back to
    # the last value it saved); the backtest uses these. The entry check uses the overnight rate: equity minus the loss
    # at the stop must cover contracts x contract notional x this.
    overnight_long_margin: float = 0.21025
    overnight_short_margin: float = 0.256625
    intraday_long_margin: float = 0.100079
    intraday_short_margin: float = 0.0999811
    # FEE ESTIMATE, to be replaced by the real fee tier: per side, max(this x contracts, costs.taker_fee_bps of the
    # notional). Coinbase's all-in rate isn't confirmed: an exchange fee of ~$0.10/side/contract, marketing that says
    # "as low as 0.02%, $0.15 minimum" (2025), an older footnote that says 0.05% with a $0.20 minimum. The desk charges
    # the higher, older one (BTC's costs.taker_fee_bps is 5.0), plus the usual 3 bps slippage.
    fee_min_per_contract: float = 0.20
    # Sessions. 24/7 except a weekly break, Friday 17:00-18:00 New York time (21:00-22:00 UTC in summer, 22:00-23:00
    # in winter; Coinbase's own 5-minute candles show it), and listed maintenance windows (fcm_trading_session_details,
    # recorded by paper at every run). No entry is decided while the fill would fall in one; Coinbase has no candles
    # while it's closed, so a stop isn't checked then and a gap fills at the reopen.
    weekly_break: tuple[int, int, int] | None = (4, 17, 60)   # (weekday, Mon=0: Friday; New York hour; minutes)
    # Closures seen in Coinbase's own 15-minute candles since BIP listed (Saturday maintenance, about quarterly), as
    # [start, end) UTC. The backtest drops the Binance bars that fall wholly inside one, as Coinbase has none.
    known_closures: tuple[tuple[str, str], ...] = (
        ("2025-10-25T10:00", "2025-10-25T21:00"), ("2026-01-24T15:00", "2026-01-24T18:00"),
        ("2026-04-02T21:00", "2026-04-02T22:00"),   # the weekly break, moved to Thursday for Good Friday
        ("2026-04-18T14:00", "2026-04-18T17:00"), ("2026-07-25T14:00", "2026-07-25T17:00"))
    listed: str | None = "2025-07-18T13:00"     # BIP's listing (new_at, 2026-10-13): a later new_at isn't trusted to
                                                 # cut the history short unless Coinbase has no candles before it
    funding_interval_hours: int = 1     # funding_interval 3600s; no funding history API: paper logs each run's rate
    funding_log_days: float = 8.0       # how far back the ledger keeps those snapshots (the 3-day APR needs 3)
    history_note: str = ("Binance BTCUSDT perp prices and funding prints, the proxy for Coinbase's "
                         "(BIP listed 2025-07-18)")


COINBASE = ContractVenue()
VENUES: dict[str, ContractVenue] = {"coinbase": COINBASE}


def venue_for(p: "Params") -> ContractVenue | None:
    """The venue rules this account trades under, or None for a venue with no rules of its own (Hyperliquid:
    fractional sizes, 24/7, the taker fee in costs)."""
    return VENUES.get(p.market.venue)


# The paper accounts the desk runs: BTC $500, ETH and SOL $150 each. Same strategy for every coin; only what's
# listed here differs.
COINS = ("BTC", "ETH", "SOL")
# Every paper account `python -m desk step` runs: the three desk coins, plus the VT basket.
ACCOUNTS = (*COINS, "VT")
COIN_OVERRIDES: dict[str, dict] = {
    # BTC is the account planned for real money first, on Coinbase's US nano BTC perp: whole contracts of 0.01 BTC
    # (about $830 at $83k) with ~21% overnight margin, so $150 can't hold even one. Its paper account starts at the
    # planned $500 and simulates Coinbase (COINBASE above): whole contracts, at most 4x, the overnight margin check,
    # sessions and the fee estimate (5 bps a side, $0.20 a contract at least). The strategy is BTC's as before.
    "BTC": {"market": {"venue": "coinbase"}, "sizing": {"starting_equity": 500.0, "max_leverage": 4.0},
            "costs": {"taker_fee_bps": 5.0}},
    # ETH and SOL swing harder than BTC: at BTC's 5% risk both hit the 40% drawdown halt in the
    # 2020-26 backtest (ETH in 2024, SOL in 2021). At 2%: ETH +65% (26% worst drop), SOL +40% (17%).
    # ETH also shorts breakdowns (a close below the 60-bar low while the 100-EMA is under the 400-EMA).
    # PAPER EXPERIMENT, switched on by the user: it missed the pre-set bar (short PF >= 1.15 in 2020-23
    # and in 2024+; it made 1.34 / 1.10) but was the only short design that made money in both periods.
    "ETH": {"sizing": {"risk_per_trade": 0.02}, "strategy": {"allow_shorts": True, "short_mode": "breakdown"}},
    "SOL": {"sizing": {"risk_per_trade": 0.02}},
}

# Shown on the board so an experiment never passes for a validated setting.
EXPERIMENTS: dict[str, str] = {
    "ETH": "breakdown shorts on: paper experiment, missed the pre-set bar",
    "VT": "paper experiment from the Oct 2026 edge hunt: passed the pre-set bar and two independent checks; "
          "expect a Sharpe nearer 0.5-0.7 than the backtest's",
}


def params_for(coin: str, base: Params = DEFAULT) -> Params:
    coin = coin.upper()
    p = base.with_(market={"symbol": coin})
    return p.with_(**COIN_OVERRIDES[coin]) if coin in COIN_OVERRIDES else p


def report_name(coin: str) -> str:
    """reports/<name>.md — BTC keeps the original `backtest` name."""
    return "backtest" if coin.upper() == "BTC" else f"backtest-{coin.upper()}"

INTERVAL_SECONDS = {
    "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400,
}
