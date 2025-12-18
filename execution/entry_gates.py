"""Entry gate checks for order validation.

Extracted from order_router.py - contains 21 gate checks that must pass
before any order is placed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger
from core.models import Signal, SignalType
from core.helpers import is_warm, GateReason
from datafeeds.universe import tier_scheduler
from logic.intelligence import intelligence, EntryScore

if TYPE_CHECKING:
    from core.models import Position
    from core.position_registry import PositionRegistry
    from execution.risk import DailyStats, CircuitBreaker

logger = get_logger(__name__)


@dataclass
class GateResult:
    """Result of gate check."""
    passed: bool
    reason: str = ""
    gate: GateReason = GateReason.SCORE
    details: dict = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass 
class SizingResult:
    """Result of position sizing calculation."""
    size_usd: float
    tier: str
    score: float
    confluence: int


class EntryGateChecker:
    """Validates signals against 21 gate checks before order placement."""
    
    # Stablecoins to skip
    STABLECOINS = {"USDT", "USDC", "DAI", "USD", "EURC", "FDUSD", "PYUSD", "GUSD", "TUSD"}
    
    def __init__(
        self,
        positions: dict,
        position_registry: "PositionRegistry",
        daily_stats: "DailyStats",
        circuit_breaker: "CircuitBreaker",
        order_cooldown: dict,
        exchange_holdings: dict,
        cooldown_seconds: int,
        get_candle_buffer_func,
        is_test: bool = False,
    ):
        self.positions = positions
        self.position_registry = position_registry
        self.daily_stats = daily_stats
        self.circuit_breaker = circuit_breaker
        self.order_cooldown = order_cooldown
        self.exchange_holdings = exchange_holdings
        self.cooldown_seconds = cooldown_seconds
        self.get_candle_buffer = get_candle_buffer_func
        self.is_test = is_test
    
    def check_all_gates(self, signal: Signal) -> Tuple[GateResult, Optional[EntryScore]]:
        """
        Run all 21 gate checks on a signal.
        
        Returns:
            Tuple of (GateResult, EntryScore or None)
        """
        symbol = signal.symbol
        
        # Gate 1: Daily loss limit
        if self.daily_stats.should_stop:
            return GateResult(False, "daily_loss_limit", GateReason.RISK), None
        
        # Gate 2: Circuit breaker
        if not self.circuit_breaker.can_trade():
            return GateResult(False, "circuit_breaker_open", GateReason.CIRCUIT_BREAKER), None
        
        # Gate 3: Signal type check
        if signal.type not in [SignalType.FLAG_BREAKOUT, SignalType.FAST_BREAKOUT]:
            return GateResult(False, "invalid_signal_type", GateReason.SCORE), None
        
        # Gate 4: No duplicate positions
        if symbol in self.positions:
            return GateResult(False, "already_have_position", GateReason.LIMITS), None
        
        # Gate 5: Stablecoin filter
        base = symbol.split("-")[0] if "-" in symbol else symbol
        if base in self.STABLECOINS:
            return GateResult(False, "stablecoin", GateReason.LIMITS, {"reason": "stablecoin"}), None
        
        # Gate 6: Exchange holdings check
        if symbol in self.exchange_holdings:
            return GateResult(
                False, "already_holding", GateReason.LIMITS,
                {"reason": "already_holding", "value": self.exchange_holdings[symbol]}
            ), None
        
        # Gate 7: Cooldown check
        cooldown_result = self._check_cooldown(symbol)
        if not cooldown_result.passed:
            return cooldown_result, None
        
        # Gate 8: Warmth check (skip in test)
        if not self.is_test:
            warmth_result = self._check_warmth(symbol)
            if not warmth_result.passed:
                return warmth_result, None
        
        # Gate 9: Symbol exposure limit
        if not self.is_test:
            exposure_result = self._check_symbol_exposure(symbol)
            if not exposure_result.passed:
                return exposure_result, None
        
        # Gate 10: Intelligence position limits
        intelligence.update_sector_counts(self.positions)
        allowed, limit_reason = intelligence.check_position_limits(
            symbol, 15.0, self.positions
        )
        if not allowed:
            return GateResult(False, limit_reason, GateReason.LIMITS, {"reason": limit_reason}), None
        
        # Gate 11: Spread filter
        signal_spread = getattr(signal, "spread_bps", 0.0)
        if not self.is_test and signal_spread > settings.spread_max_bps:
            return GateResult(
                False, "spread_too_high", GateReason.SPREAD,
                {"spread_bps": signal_spread}
            ), None
        
        # Gate 12: Whitelist gate
        if not self.is_test and settings.use_whitelist:
            whitelist = [s.strip() for s in settings.symbol_whitelist.split(",")]
            if symbol not in whitelist:
                return GateResult(False, "not_in_whitelist", GateReason.WHITELIST), None
        
        # Gate 13-15: Entry score check
        entry_score = self._score_entry(signal)
        
        # Gate 13: Spread-adjusted score
        if not self.is_test and signal_spread > settings.spread_max_bps * 0.7:
            if entry_score.total_score < settings.entry_score_min + 5:
                return GateResult(
                    False, "spread_requires_higher_score", GateReason.SPREAD,
                    {"spread_bps": signal_spread, "score": entry_score.total_score}
                ), entry_score
        
        # Gate 14: Entry score threshold
        if not entry_score.should_enter:
            gate = self._categorize_score_rejection(entry_score)
            return GateResult(
                False, "score_too_low", gate,
                {"score": entry_score.total_score, "reasons": entry_score.reasons[:3]}
            ), entry_score
        
        # Gate 15: Trading halted check
        is_halted, halt_reason = intelligence.is_trading_halted()
        if is_halted:
            return GateResult(False, halt_reason, GateReason.RISK, {"reason": "trading_halted"}), entry_score
        
        # Gate 16: Position registry limits
        estimated_size = settings.max_trade_usd
        can_open, limit_reason = self.position_registry.can_open_position(
            signal.strategy_id or "default",
            estimated_size
        )
        if not can_open:
            return GateResult(False, limit_reason, GateReason.LIMITS, {"reason": limit_reason}), entry_score
        
        # All gates passed
        return GateResult(True), entry_score
    
    def _check_cooldown(self, symbol: str) -> GateResult:
        """Gate 7: Check symbol cooldown."""
        if symbol not in self.order_cooldown:
            return GateResult(True)
        
        elapsed = (datetime.now(timezone.utc) - self.order_cooldown[symbol]).total_seconds()
        min_cooldown = settings.order_cooldown_min_seconds
        
        if elapsed < min_cooldown:
            return GateResult(
                False, "hard_cooldown",
                details={"remaining": int(min_cooldown - elapsed)}
            )
        
        if elapsed < self.cooldown_seconds:
            return GateResult(
                False, "cooldown",
                details={"remaining": int(self.cooldown_seconds - elapsed)}
            )
        
        return GateResult(True)
    
    def _check_warmth(self, symbol: str) -> GateResult:
        """Gate 8: Check candle buffer warmth."""
        buffer = self.get_candle_buffer(symbol)
        if not is_warm(symbol, buffer, tier_scheduler):
            have_1m = len(buffer.candles_1m) if buffer else 0
            have_5m = len(buffer.candles_5m) if buffer else 0
            return GateResult(
                False, "not_warm", GateReason.WARMTH,
                {"have_1m": have_1m, "have_5m": have_5m}
            )
        return GateResult(True)
    
    def _check_symbol_exposure(self, symbol: str) -> GateResult:
        """Gate 9: Check symbol exposure limit."""
        current_exposure = sum(
            p.cost_basis for p in self.positions.values()
            if p.symbol == symbol
        )
        if current_exposure >= 15.0:
            return GateResult(
                False, "symbol_exposure", GateReason.LIMITS,
                {"reason": "symbol_exposure", "current": current_exposure}
            )
        return GateResult(True)
    
    def _score_entry(self, signal: Signal) -> EntryScore:
        """Score entry using intelligence layer."""
        burst_metrics = {
            "vol_spike": getattr(signal, "vol_spike", 1.0),
            "range_spike": getattr(signal, "range_spike", 1.0),
            "trend_15m": getattr(signal, "trend_15m", 0.0),
            "vwap_distance": getattr(signal, "vwap_distance", 0.0),
            "spread_bps": getattr(signal, "spread_bps", 50.0),
            "tier": getattr(signal, "tier", "unknown"),
        }
        return intelligence.score_entry(signal, burst_metrics, self.positions)
    
    def _categorize_score_rejection(self, entry_score: EntryScore) -> GateReason:
        """Determine whether rejection was regime-driven or score-driven."""
        regime = intelligence._market_regime
        if regime != "normal" and not entry_score.btc_trend_ok:
            return GateReason.REGIME
        return GateReason.SCORE


class PositionSizer:
    """Calculates position size based on score, confluence, and portfolio."""
    
    def __init__(self, positions: dict, config):
        self.positions = positions
        self.config = config
    
    def calculate_size(
        self,
        entry_score: EntryScore,
        signal: Signal,
        portfolio_value: float,
    ) -> SizingResult:
        """
        Calculate position size using tiered sizing.
        
        Returns:
            SizingResult with size_usd and tier info
        """
        confluence_count = getattr(signal, 'confluence_count', 1)
        score = entry_score.total_score if hasattr(entry_score, 'total_score') else entry_score
        
        # Count current position tiers
        whale_threshold = settings.whale_trade_usd * 0.8
        strong_threshold = settings.strong_trade_usd * 0.8
        scout_threshold = settings.scout_trade_usd * 0.8
        
        whale_count = sum(1 for p in self.positions.values()
                         if getattr(p, 'entry_cost_usd', 0) >= whale_threshold)
        strong_count = sum(1 for p in self.positions.values()
                          if strong_threshold <= getattr(p, 'entry_cost_usd', 0) < whale_threshold)
        scout_count = sum(1 for p in self.positions.values()
                         if scout_threshold <= getattr(p, 'entry_cost_usd', 0) < strong_threshold * 0.8)
        
        # Determine tier
        is_whale = score >= settings.whale_score_min and confluence_count >= settings.whale_confluence_min
        is_strong = score >= settings.strong_score_min or confluence_count >= settings.whale_confluence_min
        is_scout = score >= settings.scout_score_min
        
        if is_whale and whale_count < settings.whale_max_positions:
            size_usd = settings.whale_trade_usd
            tier = "🐋 WHALE"
        elif is_strong and strong_count < settings.strong_max_positions:
            size_usd = settings.strong_trade_usd
            tier = "💪 STRONG"
        elif is_scout and scout_count < settings.scout_max_positions:
            size_usd = settings.scout_trade_usd
            tier = "🔍 SCOUT"
        else:
            size_usd = settings.normal_trade_usd
            tier = "📊 NORMAL"
        
        # Apply time-of-day multiplier
        session_mult = intelligence.get_size_multiplier()
        if session_mult < 1.0:
            size_usd *= session_mult
        
        # Clamp to portfolio limits
        pv = portfolio_value if portfolio_value > 0 else 500.0
        min_size = pv * settings.position_min_pct
        max_size = pv * settings.position_max_pct
        size_usd = max(min_size, min(max_size, size_usd))
        
        # Cap at max_trade_usd
        if size_usd > settings.max_trade_usd:
            size_usd = settings.max_trade_usd
        
        return SizingResult(
            size_usd=size_usd,
            tier=tier,
            score=score,
            confluence=confluence_count
        )
    
    def check_budget(
        self,
        size_usd: float,
        portfolio_value: float,
        is_test: bool = False
    ) -> Tuple[bool, float]:
        """
        Check if budget is available for position.
        
        Returns:
            Tuple of (has_budget, available_budget)
        """
        if is_test:
            return True, 10000.0
        
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        current_exposure = sum(p.cost_basis for p in self.positions.values())
        available = bot_budget - current_exposure
        
        return size_usd <= available, available


def calculate_stops(
    price: float,
    is_fast: bool,
    config
) -> Tuple[float, float, float, int]:
    """
    Calculate stop and target prices.
    
    Returns:
        Tuple of (stop_price, tp1_price, tp2_price, time_stop_min)
    """
    if is_fast:
        stop_price = price * (1 - settings.fast_stop_pct / 100)
        tp1_price = price * (1 + settings.fast_tp1_pct / 100)
        tp2_price = price * (1 + settings.fast_tp2_pct / 100)
        time_stop_min = settings.fast_time_stop_min
    else:
        stop_price = price * (1 - config.fixed_stop_pct)
        tp1_price = price * (1 + config.tp1_pct)
        tp2_price = price * (1 + config.tp2_pct)
        time_stop_min = getattr(settings, 'max_hold_minutes', 120)
    
    return stop_price, tp1_price, tp2_price, time_stop_min


def validate_rr_ratio(
    price: float,
    stop_price: float,
    tp1_price: float,
    min_rr_ratio: float,
    is_test: bool = False
) -> Tuple[bool, float, str]:
    """
    Validate reward-to-risk ratio.
    
    Returns:
        Tuple of (valid, rr_ratio, reason)
    """
    risk_per_share = price - stop_price
    reward_to_tp1 = tp1_price - price
    
    if risk_per_share <= 0:
        return False, 0.0, "invalid_stop"
    
    rr_ratio = reward_to_tp1 / risk_per_share
    
    if not is_test and rr_ratio < min_rr_ratio:
        return False, rr_ratio, "rr_too_low"
    
    return True, rr_ratio, ""
