"""Shared state for dashboard and components."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Deque, Tuple
from core.models import Signal, SignalType, Position, CandleBuffer
from core.events import OrderEvent


@dataclass
class BurstCandidate:
    """Coin in the burst leaderboard."""
    symbol: str
    price: float = 0.0
    burst_score: float = 0.0
    vol_spike: float = 0.0
    range_spike: float = 0.0
    trend_5m: float = 0.0      # 15m trend as %
    trend_slope: float = 0.0   # Linear slope
    spread_bps: float = 0.0    # Spread in basis points
    vwap_dist: float = 0.0     # Distance from VWAP as %
    daily_move: float = 0.0    # Abnormality vs daily ATR
    tier: str = "unknown"      # large/mid/small/micro
    rank: int = 0
    entry_score: int = 0       # Quality entry score (0-100+)


@dataclass
class FocusCoinState:
    """Detailed state for the #1 candidate coin."""
    symbol: str = ""
    price: float = 0.0
    spread_bps: float = 0.0  # Current spread in basis points
    warmup_1m: int = 0       # How many 1m candles we have
    warmup_5m: int = 0       # How many 5m candles we have
    warmup_ready: bool = False  # Ready for full strategy checks
    
    # Impulse leg
    impulse_move: float = 0.0
    impulse_high: float = 0.0
    impulse_low: float = 0.0
    impulse_age_min: float = 0.0
    impulse_atr: float = 0.0
    impulse_green_candles: int = 0
    
    # Flag pattern
    flag_retracement: float = 0.0
    flag_age_min: float = 0.0
    flag_slope: float = 0.0
    flag_vol_decay: float = 0.0
    flag_high: float = 0.0
    flag_low: float = 0.0
    flag_upper_trendline: float = 0.0
    
    # Traps
    triple_top: bool = False
    head_shoulders: bool = False
    skip_reason: str = ""
    
    # Stage
    stage: str = "waiting"  # waiting, burst, impulse, flag, breakout, trap
    
    # Quick metrics for display
    vol_spike: float = 0.0
    trend_5m: float = 0.0


@dataclass 
class CurrentSignal:
    """Current trading signal state."""
    action: str = "WAIT"  # WAIT, ENTER_LONG, EXIT, SKIP_TRAP
    confidence: float = 0.0
    reason: str = ""
    entry_price: float = 0.0
    stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    time_stop_deadline: str = ""


@dataclass
class PositionDisplay:
    """Position data formatted for display."""
    symbol: str
    units: float
    size_usd: float
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    unrealized_pnl: float
    unrealized_pct: float
    age_min: float


@dataclass
class UniverseState:
    """State for symbol universe and scanner."""
    total_symbols: int = 0
    eligible_symbols: int = 0
    spicy_smallcaps: int = 0
    
    # Tier counts
    large_caps: int = 0
    mid_caps: int = 0
    small_caps: int = 0
    micro_caps: int = 0
    
    # Last refresh
    last_universe_refresh: Optional[datetime] = None
    last_burst_update: Optional[datetime] = None
    
    # Streaming status
    symbols_streaming: int = 0


@dataclass
class BotState:
    """Global bot state for dashboard."""
    
    # Mode + startup
    mode: str = "paper"
    profile: str = "prod"
    startup_time: Optional[datetime] = None
    boot_phase: bool = True
    boot_started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # API status
    api_ok: bool = False
    api_msg: str = ""
    
    # WebSocket status
    ws_ok: bool = False
    ws_last_msg_time: Optional[datetime] = None
    ws_last_age: float = 0.0
    ws_reconnect_count: int = 0
    
    # Time
    local_time: datetime = field(default_factory=datetime.now)
    last_candle_time: Optional[datetime] = None
    
    # Portfolio (real exchange balances)
    portfolio_value: float = 0.0      # Total value (cash + holdings)
    cash_balance: float = 0.0         # USD/USDC available
    holdings_value: float = 0.0       # Value of crypto holdings
    
    # Paper trading balance (simulated)
    paper_balance: float = 1000.0     # Simulated paper balance
    paper_positions_value: float = 0.0  # Value in paper positions
    
    # PnL
    daily_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    
    # Kill switch
    kill_switch: bool = False
    kill_reason: str = ""
    
    # Universe/Scanner state
    universe: UniverseState = field(default_factory=UniverseState)
    
    # Burst leaderboard
    burst_leaderboard: list[BurstCandidate] = field(default_factory=list)
    
    # Focus coin
    focus_coin: FocusCoinState = field(default_factory=FocusCoinState)
    
    # Current signal
    current_signal: CurrentSignal = field(default_factory=CurrentSignal)
    
    # Positions
    positions: list[PositionDisplay] = field(default_factory=list)
    
    # Stats
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    
    # Compounding metrics
    profit_factor: float = 0.0         # Sum(wins) / Sum(losses)
    avg_r: float = 0.0                 # Average R per trade
    avg_win: float = 0.0               # Average win $
    avg_loss: float = 0.0              # Average loss $
    biggest_win: float = 0.0           # Best trade today
    biggest_loss: float = 0.0          # Worst trade today
    max_drawdown: float = 0.0          # Max DD from peak
    loss_limit_pct: float = 0.0        # % toward daily loss limit
    
    # Bot Budget & Exposure tracking
    starting_portfolio_value: float = 0.0  # Portfolio value at session start
    actual_pnl: float = 0.0            # REAL PnL = current - starting
    bot_budget_usd: float = 0.0        # 25% of portfolio allocated to bot
    bot_exposure_usd: float = 0.0      # $ currently in bot positions
    bot_available_usd: float = 0.0     # $ available for new trades
    exposure_pct: float = 0.0          # % of budget used
    max_exposure_pct: float = 25.0     # Limit from config
    
    # Balances
    paper_balance: float = 1000.0
    paper_positions_value: float = 0.0
    paper_balance_usd: float = 0.0
    live_balance_usd: float = 0.0
    daily_loss_limit_usd: float = 0.0
    
    # Truth/sync (live)
    portfolio_snapshot_age_s: float = 999.0   # Age of last live snapshot
    sync_paused: bool = False                 # Pause entries until fresh truth
    truth_stale: bool = False                 # Snapshot missing/old

    # Live log: deque of (timestamp, level, message)
    live_log: Deque[Tuple[datetime, str, str]] = field(
        default_factory=lambda: deque(maxlen=12)
    )
    # Recent order lifecycle events (open/partial/close)
    recent_orders: Deque[OrderEvent] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    
    # Live population counters (reset every 5s)
    ticks_last_5s: int = 0
    candles_last_5s: int = 0
    events_last_5s: int = 0
    
    # Tier system stats
    tier1_count: int = 0          # WS real-time symbols
    tier2_count: int = 0          # REST fast (15s) symbols
    tier3_count: int = 0          # REST slow (60s) symbols
    warm_symbols: int = 0         # Symbols with enough history
    cold_symbols: int = 0         # Symbols needing warmup
    pending_backfills: int = 0    # Symbols in backfill queue
    
    # BTC regime
    btc_regime: str = "normal"    # normal, caution, risk_off
    btc_trend_1h: float = 0.0     # BTC 1h trend %
    
    # REST poller stats
    rest_polls_tier2: int = 0     # Tier 2 polls completed
    rest_polls_tier3: int = 0     # Tier 3 polls completed
    rest_rate_degraded: bool = False  # Rate limit degraded
    
    # Candle store stats
    candles_persisted: int = 0    # Total candles written to disk
    
    # ML/Live indicators for focus symbol
    ml_score: Optional[float] = None     # ML raw score (-1 to 1)
    ml_confidence: Optional[float] = None # ML confidence (0 to 1)
    ml_fresh_pct: float = 0.0            # % of ML cache that is fresh
    ml_total_cached: int = 0             # Total symbols with ML cache
    is_choppy: bool = False              # Current chop state
    rsi: float = 50.0                    # RSI 14
    vol_regime: str = "normal"           # quiet/normal/hot/crashy
    
    # Filter rejection tracking (for health monitoring)
    rejections_spread: int = 0           # Rejected due to spread
    rejections_warmth: int = 0           # Rejected due to cold symbol
    rejections_regime: int = 0           # Rejected due to BTC regime
    rejections_score: int = 0            # Rejected due to low score
    rejections_rr: int = 0               # Rejected due to R:R
    rejections_limits: int = 0           # Rejected due to position limits
    
    # Blocked signals (for dashboard display)
    blocked_signals: list = field(default_factory=list)  # Recent blocked signals with reasons
    
    # Heartbeats (for "ethernet lights")
    heartbeat_ws: Optional[datetime] = None
    heartbeat_candles_1m: Optional[datetime] = None
    heartbeat_candles_5m: Optional[datetime] = None
    heartbeat_features: Optional[datetime] = None
    heartbeat_ml: Optional[datetime] = None
    heartbeat_scanner: Optional[datetime] = None
    heartbeat_order_router: Optional[datetime] = None
    heartbeat_dashboard: Optional[datetime] = None
    
    # ML freshness
    ml_fresh_count: int = 0
    ml_total_count: int = 0
    
    def log(self, msg: str, level: str = "INFO"):
        """Add entry to live log."""
        ts = datetime.now(timezone.utc)
        self.live_log.appendleft((ts, level, msg))
        self.events_last_5s += 1
    
    def update_ws_age(self):
        """Update WebSocket message age."""
        if self.ws_last_msg_time:
            self.ws_last_age = (datetime.now(timezone.utc) - self.ws_last_msg_time).total_seconds()
        else:
            self.ws_last_age = 999.0
    
    @property
    def win_rate(self) -> float:
        if self.trades_today == 0:
            return 0.0
        return self.wins_today / self.trades_today
