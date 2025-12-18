"""Shared state for dashboard and system components.

Central state container providing real-time visibility into bot operation.
Used by: TUI dashboard, web dashboard, order router, scanner.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Deque
from core.events import OrderEvent


@dataclass
class BurstCandidate:
    """Symbol ranking in burst leaderboard."""
    symbol: str
    price: float = 0.0
    burst_score: float = 0.0
    vol_spike: float = 1.0
    range_spike: float = 1.0
    trend_5m: float = 0.0
    trend_slope: float = 0.0
    spread_bps: float = 0.0
    vwap_dist: float = 0.0
    daily_move: float = 0.0
    tier: str = "unknown"
    rank: int = 0
    entry_score: int = 0


@dataclass
class FocusCoinState:
    """Detailed state for top candidate under analysis."""
    symbol: str = ""
    price: float = 0.0
    spread_bps: float = 0.0
    warmup_1m: int = 0
    warmup_5m: int = 0
    warmup_ready: bool = False
    impulse_move: float = 0.0
    impulse_high: float = 0.0
    impulse_low: float = 0.0
    impulse_age_min: float = 0.0
    impulse_atr: float = 0.0
    impulse_green_candles: int = 0
    flag_retracement: float = 0.0
    flag_age_min: float = 0.0
    flag_slope: float = 0.0
    flag_vol_decay: float = 0.0
    flag_high: float = 0.0
    flag_low: float = 0.0
    flag_upper_trendline: float = 0.0
    triple_top: bool = False
    head_shoulders: bool = False
    skip_reason: str = ""
    stage: str = "waiting"
    vol_spike: float = 0.0
    trend_5m: float = 0.0


@dataclass
class CurrentSignal:
    """Active trading signal state."""
    action: str = "WAIT"
    symbol: str = ""
    confidence: float = 0.0
    reason: str = ""
    entry_price: float = 0.0
    stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    time_stop_deadline: str = ""


@dataclass
class PositionDisplay:
    """Position formatted for dashboard display."""
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
    """Symbol universe and tier distribution."""
    total_symbols: int = 0
    eligible_symbols: int = 0
    spicy_smallcaps: int = 0
    large_caps: int = 0
    mid_caps: int = 0
    small_caps: int = 0
    micro_caps: int = 0
    last_universe_refresh: Optional[datetime] = None
    last_burst_update: Optional[datetime] = None
    symbols_streaming: int = 0


@dataclass
class BotState:
    """Global bot state container.
    
    Sections:
        - Identity: mode, profile, startup
        - Connectivity: API, WebSocket status
        - Portfolio: balances, PnL, exposure
        - Trading: positions, signals, rejections
        - Engine: tiers, heartbeats, throughput
        - ML: scores, freshness, regime
    """
    
    # Identity
    mode: str = "paper"
    profile: str = "prod"
    startup_time: Optional[datetime] = None
    boot_phase: bool = True
    boot_started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Connectivity
    api_ok: bool = False
    api_msg: str = ""
    ws_ok: bool = False
    ws_last_msg_time: Optional[datetime] = None
    ws_last_age: float = 0.0
    ws_reconnect_count: int = 0
    
    # Time
    local_time: datetime = field(default_factory=datetime.now)
    last_candle_time: Optional[datetime] = None
    
    # Portfolio - Live
    portfolio_value: float = 0.0
    cash_balance: float = 0.0
    holdings_value: float = 0.0
    starting_portfolio_value: float = 0.0
    actual_pnl: float = 0.0
    
    # Portfolio - Paper
    paper_balance: float = 1000.0
    paper_positions_value: float = 0.0
    paper_balance_usd: float = 0.0
    
    # PnL
    daily_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    
    # Budget & Exposure
    bot_budget_usd: float = 0.0
    bot_exposure_usd: float = 0.0
    bot_available_usd: float = 0.0
    exposure_pct: float = 0.0
    max_exposure_pct: float = 25.0
    live_balance_usd: float = 0.0
    daily_loss_limit_usd: float = 0.0
    
    # Kill Switch
    kill_switch: bool = False
    kill_reason: str = ""
    
    # Exchange Sync
    portfolio_snapshot_age_s: float = 999.0
    sync_paused: bool = False
    truth_stale: bool = False
    
    # Trading Stats
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    profit_factor: float = 0.0
    avg_r: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    max_drawdown: float = 0.0
    loss_limit_pct: float = 0.0
    
    # Rejections
    rejections_spread: int = 0
    rejections_warmth: int = 0
    rejections_regime: int = 0
    rejections_score: int = 0
    rejections_rr: int = 0
    rejections_limits: int = 0
    last_rejection: Optional[tuple[str, str, str]] = None
    blocked_signals: list = field(default_factory=list)
    
    # Tier System
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    warm_symbols: int = 0
    cold_symbols: int = 0
    pending_backfills: int = 0
    
    # REST Poller
    rest_polls_tier2: int = 0
    rest_polls_tier3: int = 0
    rest_rate_degraded: bool = False
    rest_requests: int = 0
    rest_429s: int = 0
    
    # Throughput (reset every 5s)
    ticks_last_5s: int = 0
    candles_last_5s: int = 0
    events_last_5s: int = 0
    candles_persisted: int = 0
    
    # BTC Regime
    btc_regime: str = "normal"
    btc_trend_1h: float = 0.0
    
    # ML State
    ml_score: Optional[float] = None
    ml_confidence: Optional[float] = None
    ml_fresh_pct: float = 0.0
    ml_fresh_count: int = 0
    ml_total_count: int = 0
    ml_total_cached: int = 0
    is_choppy: bool = False
    rsi: float = 50.0
    vol_regime: str = "normal"
    
    # Heartbeats
    heartbeat_ws: Optional[datetime] = None
    heartbeat_candles_1m: Optional[datetime] = None
    heartbeat_candles_5m: Optional[datetime] = None
    heartbeat_features: Optional[datetime] = None
    heartbeat_ml: Optional[datetime] = None
    heartbeat_scanner: Optional[datetime] = None
    heartbeat_order_router: Optional[datetime] = None
    heartbeat_dashboard: Optional[datetime] = None
    
    # Nested State Objects
    universe: UniverseState = field(default_factory=UniverseState)
    focus_coin: FocusCoinState = field(default_factory=FocusCoinState)
    current_signal: CurrentSignal = field(default_factory=CurrentSignal)
    
    # Collections
    burst_leaderboard: list[BurstCandidate] = field(default_factory=list)
    positions: list[PositionDisplay] = field(default_factory=list)
    positions_display: list[PositionDisplay] = field(default_factory=list)
    live_log: Deque[tuple[datetime, str, str]] = field(
        default_factory=lambda: deque(maxlen=100)
    )
    recent_signals: Deque[tuple[datetime, str, str, int, float, bool, str]] = field(
        default_factory=lambda: deque(maxlen=30)
    )
    recent_orders: Deque[OrderEvent] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    
    def log(self, msg: str, level: str = "INFO"):
        """Append to live log deque."""
        self.live_log.appendleft((datetime.now(timezone.utc), level, msg))
        self.events_last_5s += 1
    
    def update_ws_age(self):
        """Refresh WebSocket message age in seconds."""
        if self.ws_last_msg_time:
            self.ws_last_age = (datetime.now(timezone.utc) - self.ws_last_msg_time).total_seconds()
        else:
            self.ws_last_age = 999.0
    
    def touch_heartbeat(self, component: str):
        """Update heartbeat timestamp for a component."""
        now = datetime.now(timezone.utc)
        attr = f"heartbeat_{component}"
        if hasattr(self, attr):
            setattr(self, attr, now)
    
    @property
    def win_rate(self) -> float:
        """Calculate win rate as decimal (0.0 to 1.0)."""
        if self.trades_today == 0:
            return 0.0
        return self.wins_today / self.trades_today
    
    @property
    def uptime_seconds(self) -> float:
        """Seconds since startup."""
        if not self.startup_time:
            return 0.0
        return (datetime.now(timezone.utc) - self.startup_time).total_seconds()
    
    def reset_daily_stats(self):
        """Reset daily counters (call at midnight or session start)."""
        self.trades_today = 0
        self.wins_today = 0
        self.losses_today = 0
        self.daily_pnl = 0.0
        self.realized_pnl = 0.0
        self.biggest_win = 0.0
        self.biggest_loss = 0.0
        self.max_drawdown = 0.0
        self.rejections_spread = 0
        self.rejections_warmth = 0
        self.rejections_regime = 0
        self.rejections_score = 0
        self.rejections_rr = 0
        self.rejections_limits = 0
    
    def reset_throughput(self):
        """Reset 5-second throughput counters."""
        self.ticks_last_5s = 0
        self.candles_last_5s = 0
        self.events_last_5s = 0
