"""Entry gate checks for order validation.

Extracted from order_router.py - contains 21 gate checks that must pass
before any order is placed.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger
from core.models import Intent, Signal, SignalType
from core.helpers import is_warm, GateReason
from core.asset_class import get_risk_profile
from core.config_manager import get_config_manager
from datafeeds.universe import tier_scheduler
from logic.intelligence import intelligence, EntryScore
from logic.predictive_ranker import predictive_ranker

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
    trace: list = field(default_factory=list)
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class GateCheck:
    """Single gate evaluation for trace output."""
    name: str
    passed: bool
    reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass 
class SizingResult:
    """Result of position sizing calculation."""
    size_usd: float
    tier: str  # Display tier with emoji
    score: float
    confluence: int
    available_budget: float
    min_order_usd: float
    max_trade_usd: float
    current_exposure: float
    session_mult: float
    tier_code: str = "normal"  # Clean tier code: scout/normal/strong/whale


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

    def _record_trace(self, trace: list, name: str, passed: bool, reason: str = "", details: Optional[dict] = None):
        trace.append(GateCheck(
            name=name,
            passed=passed,
            reason=reason,
            details=details or {},
        ))
    
    def check_all_gates(
        self,
        signal: Signal | Intent,
        skip_position_registry: bool = False,
    ) -> Tuple[GateResult, Optional[EntryScore]]:
        """
        Run all 21 gate checks on a signal.
        
        Returns:
            Tuple of (GateResult, EntryScore or None)
        """
        symbol = signal.symbol
        trace: list[GateCheck] = []

        # Gate 1: Daily loss limit
        if self.daily_stats.should_stop:
            details = {
                "total_pnl": self.daily_stats.total_pnl,
                "limit_usd": settings.daily_max_loss_usd,
            }
            self._record_trace(trace, "daily_loss_limit", False, "daily_loss_limit", details)
            return GateResult(False, "daily_loss_limit", GateReason.RISK, details, trace=trace), None
        self._record_trace(trace, "daily_loss_limit", True, details={
            "total_pnl": self.daily_stats.total_pnl,
            "limit_usd": settings.daily_max_loss_usd,
        })

        # Gate 1b: Manual pause (dashboard pause entries)
        if not self.is_test:
            try:
                paused = bool(get_config_manager().pause_new_entries)
                if paused:
                    details = {"reason": "pause_new_entries"}
                    self._record_trace(trace, "pause_new_entries", False, "pause_new_entries", details)
                    return GateResult(
                        False,
                        "pause_new_entries",
                        GateReason.RISK,
                        details,
                        trace=trace,
                    ), None
                self._record_trace(trace, "pause_new_entries", True)
            except Exception:
                # If config manager fails for any reason, do not hard-fail trading.
                self._record_trace(trace, "pause_new_entries", True, details={"skipped": True})

        # Gate 2: Circuit breaker
        if not self.circuit_breaker.can_trade():
            self._record_trace(trace, "circuit_breaker", False, "circuit_breaker_open", {
                "state": getattr(self.circuit_breaker, "state", "open"),
            })
            return GateResult(False, "circuit_breaker_open", GateReason.CIRCUIT, trace=trace), None
        self._record_trace(trace, "circuit_breaker", True, details={
            "state": getattr(self.circuit_breaker, "state", "closed"),
        })

        # Gate 3: Signal type check
        allowed_types = [SignalType.FLAG_BREAKOUT, SignalType.FAST_BREAKOUT]
        if signal.type not in allowed_types:
            details = {"signal_type": getattr(signal.type, "value", str(signal.type))}
            self._record_trace(trace, "signal_type", False, "invalid_signal_type", details)
            return GateResult(False, "invalid_signal_type", GateReason.SCORE, details, trace=trace), None
        self._record_trace(trace, "signal_type", True, details={
            "signal_type": getattr(signal.type, "value", str(signal.type)),
        })

        # Gate 4: No duplicate positions
        if symbol in self.positions:
            self._record_trace(trace, "duplicate_position", False, "already_have_position")
            return GateResult(False, "already_have_position", GateReason.LIMITS, trace=trace), None
        self._record_trace(trace, "duplicate_position", True)

        # Gate 5: Stablecoin filter
        base = symbol.split("-")[0] if "-" in symbol else symbol
        if base in self.STABLECOINS:
            details = {"reason": "stablecoin", "base": base}
            self._record_trace(trace, "stablecoin_filter", False, "stablecoin", details)
            return GateResult(False, "stablecoin", GateReason.LIMITS, details, trace=trace), None
        self._record_trace(trace, "stablecoin_filter", True, details={"base": base})

        # Gate 6: Exchange holdings check (with stacking support)
        if symbol in self.exchange_holdings:
            holding_value = self.exchange_holdings.get(symbol, 0)
            # Ignore dust positions (< $1) - allow fresh entry
            dust_threshold = getattr(settings, "min_position_usd", 1.0)
            if holding_value < dust_threshold:
                logger.debug("[GATE] %s: ignoring dust holding ($%.2f < $%.2f)", symbol, holding_value, dust_threshold)
                self._record_trace(trace, "exchange_holdings", True, "dust_ignored", {"value": holding_value})
            else:
                # Check if stacking is allowed
                can_stack, stack_reason = self._check_stacking_allowed(symbol)
                if not can_stack:
                    details = {"reason": "already_holding", "value": holding_value, "stack_blocked": stack_reason}
                    self._record_trace(trace, "exchange_holdings", False, "already_holding", details)
                    return GateResult(False, "already_holding", GateReason.LIMITS, details, trace=trace), None
                # Stacking allowed - continue with gates
                self._record_trace(trace, "exchange_holdings", True, "stacking_allowed", {"reason": stack_reason})
        else:
            self._record_trace(trace, "exchange_holdings", True)

        # Gate 7: Cooldown check
        cooldown_result = self._check_cooldown(symbol)
        self._record_trace(
            trace,
            "cooldown",
            cooldown_result.passed,
            cooldown_result.reason,
            cooldown_result.details,
        )
        if not cooldown_result.passed:
            cooldown_result.trace = trace
            return cooldown_result, None

        # Gate 8: Warmth check (skip in test)
        if not self.is_test:
            warmth_result = self._check_warmth(symbol)
            self._record_trace(
                trace,
                "warmth",
                warmth_result.passed,
                warmth_result.reason,
                warmth_result.details,
            )
            if not warmth_result.passed:
                warmth_result.trace = trace
                return warmth_result, None

        # Gate 9: Symbol exposure limit
        if not self.is_test:
            exposure_result = self._check_symbol_exposure(symbol)
            self._record_trace(
                trace,
                "symbol_exposure",
                exposure_result.passed,
                exposure_result.reason,
                exposure_result.details,
            )
            if not exposure_result.passed:
                exposure_result.trace = trace
                return exposure_result, None

        # Gate 10: Intelligence position limits
        intelligence.update_sector_counts(self.positions)
        allowed, limit_reason = intelligence.check_position_limits(
            symbol, 15.0, self.positions
        )
        if not allowed:
            details = {"reason": limit_reason}
            self._record_trace(trace, "position_limits", False, limit_reason, details)
            return GateResult(False, limit_reason, GateReason.LIMITS, details, trace=trace), None
        self._record_trace(trace, "position_limits", True)

        # Gate 11: Spread filter
        signal_spread = getattr(signal, "spread_bps", 0.0)
        if not self.is_test and signal_spread > settings.spread_max_bps:
            details = {"spread_bps": signal_spread, "max_spread_bps": settings.spread_max_bps}
            self._record_trace(trace, "spread_filter", False, "spread_too_high", details)
            return GateResult(False, "spread_too_high", GateReason.SPREAD, details, trace=trace), None
        self._record_trace(trace, "spread_filter", True, details={
            "spread_bps": signal_spread,
            "max_spread_bps": settings.spread_max_bps,
        })

        # Gate 12: Whitelist gate
        if not self.is_test and settings.use_whitelist:
            whitelist = [s.strip() for s in settings.symbol_whitelist.split(",")]
            if symbol not in whitelist:
                self._record_trace(trace, "whitelist", False, "not_in_whitelist", {"symbol": symbol})
                return GateResult(False, "not_in_whitelist", GateReason.WHITELIST, trace=trace), None
            self._record_trace(trace, "whitelist", True)

        # Gate 13-15: Entry score check
        entry_score = self._score_entry(signal)

        logger.debug(
            "[SCORE] %s: total=%.0f, should_enter=%s, regime=%s, conf=%.2f",
            symbol,
            entry_score.total_score,
            entry_score.should_enter,
            entry_score.btc_regime,
            float(getattr(signal, "confidence", 0.0) or 0.0),
        )

        # Gate 13: Spread-adjusted score
        if not self.is_test and signal_spread > settings.spread_max_bps * 0.7:
            if entry_score.total_score < settings.entry_score_min + 5:
                details = {"spread_bps": signal_spread, "score": entry_score.total_score}
                self._record_trace(trace, "spread_score", False, "spread_requires_higher_score", details)
                return GateResult(False, "spread_requires_higher_score", GateReason.SPREAD, details, trace=trace), entry_score
        self._record_trace(trace, "spread_score", True, details={
            "spread_bps": signal_spread,
            "score": entry_score.total_score,
        })

        # Gate 14: Entry score threshold
        if not entry_score.should_enter:
            gate = self._categorize_score_rejection(entry_score)
            details = {"score": entry_score.total_score, "min_score": settings.entry_score_min}
            self._record_trace(trace, "entry_score", False, "score_too_low", details)
            return GateResult(False, "score_too_low", gate, details, trace=trace), entry_score
        self._record_trace(trace, "entry_score", True, details={
            "score": entry_score.total_score,
            "min_score": settings.entry_score_min,
        })

        # Gate 15: Trading halted check
        is_halted, halt_reason = intelligence.is_trading_halted()
        if is_halted:
            details = {"reason": "trading_halted", "message": halt_reason}
            self._record_trace(trace, "trading_halted", False, halt_reason, details)
            return GateResult(False, halt_reason, GateReason.RISK, details, trace=trace), entry_score
        self._record_trace(trace, "trading_halted", True)

        # Gate 15b: Predictive timing gate (avoid chasing / bad timing)
        if not self.is_test:
            try:
                should_wait, wait_reason = predictive_ranker.should_wait_for_entry(symbol)
                if should_wait:
                    details = {"reason": wait_reason}
                    self._record_trace(trace, "predictive_timing", False, wait_reason, details)
                    return GateResult(False, wait_reason, GateReason.SCORE, details, trace=trace), entry_score
                self._record_trace(trace, "predictive_timing", True)
            except Exception:
                # If ranker fails for any reason, do not block trading.
                self._record_trace(trace, "predictive_timing", True, details={"skipped": True})

        # Gate 16: Position registry limits (size checked later with actual sizing)
        if not skip_position_registry:
            estimated_size = settings.max_trade_usd
            can_open, limit_reason = self.position_registry.can_open_position(
                signal.strategy_id or "default",
                estimated_size
            )
            if not can_open:
                details = {"reason": limit_reason, "estimated_size": estimated_size}
                self._record_trace(trace, "registry_limits", False, limit_reason, details)
                return GateResult(False, limit_reason, GateReason.LIMITS, details, trace=trace), entry_score
            self._record_trace(trace, "registry_limits", True, details={"estimated_size": estimated_size})

        # All gates passed
        return GateResult(True, trace=trace), entry_score
    
    def _check_cooldown(self, symbol: str) -> GateResult:
        """Gate 7: Check symbol cooldown."""
        if symbol not in self.order_cooldown:
            return GateResult(True, details={
                "elapsed": None,
                "remaining": 0,
                "min_seconds": settings.order_cooldown_min_seconds,
                "cooldown_seconds": self.cooldown_seconds,
            })
        
        elapsed = (datetime.now(timezone.utc) - self.order_cooldown[symbol]).total_seconds()
        min_cooldown = settings.order_cooldown_min_seconds
        
        if elapsed < min_cooldown:
            return GateResult(
                False, "hard_cooldown",
                details={
                    "elapsed": int(elapsed),
                    "remaining": int(min_cooldown - elapsed),
                    "min_seconds": min_cooldown,
                }
            )
        
        if elapsed < self.cooldown_seconds:
            return GateResult(
                False, "cooldown",
                details={
                    "elapsed": int(elapsed),
                    "remaining": int(self.cooldown_seconds - elapsed),
                    "cooldown_seconds": self.cooldown_seconds,
                }
            )
        
        return GateResult(True, details={
            "elapsed": int(elapsed),
            "remaining": 0,
            "cooldown_seconds": self.cooldown_seconds,
        })
    
    def _check_warmth(self, symbol: str) -> GateResult:
        """Gate 8: Check candle buffer warmth."""
        buffer = self.get_candle_buffer(symbol)
        have_1m = len(buffer.candles_1m) if buffer else 0
        have_5m = len(buffer.candles_5m) if buffer else 0
        min_1m = getattr(tier_scheduler.config, "min_candles_1m", 0)
        min_5m = getattr(tier_scheduler.config, "min_candles_5m", 0)
        if not is_warm(symbol, buffer, tier_scheduler):
            return GateResult(
                False, "not_warm", GateReason.WARMTH,
                {"have_1m": have_1m, "have_5m": have_5m, "min_1m": min_1m, "min_5m": min_5m}
            )
        return GateResult(True, details={
            "have_1m": have_1m,
            "have_5m": have_5m,
            "min_1m": min_1m,
            "min_5m": min_5m,
        })
    
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
        return GateResult(True, details={
            "current": current_exposure,
            "limit": 15.0,
        })
    
    def _check_stacking_allowed(self, symbol: str) -> tuple[bool, str]:
        """Check if stacking (adding to winner) is allowed for this position.
        
        Returns:
            Tuple of (can_stack: bool, reason: str)
        """
        # Check if stacking is enabled
        if not getattr(settings, "stacking_enabled", False):
            return False, "stacking_disabled"
        
        # Get position from tracked positions
        position = self.positions.get(symbol)
        
        # If no tracked position but we have exchange holdings, check if we can stack
        # This handles "recovered" positions that exist on exchange but not in our dict
        if not position:
            holding_value = self.exchange_holdings.get(symbol, 0)
            if holding_value > 0:
                # Can't stack without knowing entry price/pnl - need tracked position
                # But we CAN allow stacking if the holding is profitable based on current price
                # For now, block stacking on untracked positions (would need price data)
                return False, "untracked_position"
            return False, "no_position"
        
        # Check profit threshold (+2% default)
        min_profit = getattr(settings, "stacking_min_profit_pct", 2.0)
        current_pnl_pct = position.pnl_pct if hasattr(position, 'pnl_pct') else 0
        if current_pnl_pct < min_profit:
            return False, f"profit_{current_pnl_pct:.1f}%_below_{min_profit}%"
        
        # Check max adds (default 1 add = 2x total)
        max_adds = getattr(settings, "stacking_max_adds", 1)
        current_adds = getattr(position, "stack_count", 0)
        if current_adds >= max_adds:
            return False, f"max_adds_{current_adds}/{max_adds}"
        
        # Check positive incline (green candles)
        green_required = getattr(settings, "stacking_green_candles", 3)
        buffer = self.get_candle_buffer(symbol) if self.get_candle_buffer else None
        if buffer and hasattr(buffer, 'candles_1m') and len(buffer.candles_1m) >= green_required:
            recent = buffer.candles_1m[-green_required:]
            green_count = sum(1 for c in recent if c.close > c.open)
            if green_count < green_required:
                return False, f"incline_{green_count}/{green_required}_green"
        else:
            return False, "no_candle_data"
        
        # All checks passed - stacking allowed
        logger.info("[STACK] %s: Stacking allowed (+%.1f%%, %d green candles)", 
                   symbol, current_pnl_pct, green_count)
        return True, f"profit_{current_pnl_pct:.1f}%_incline_ok"
    
    def _score_entry(self, signal: Signal | Intent) -> EntryScore:
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
        signal: Signal | Intent,
        portfolio_value: float,
    ) -> SizingResult:
        """
        Calculate position size using tiered sizing.

        Precedence:
        1) Tier USD (score-based)
        2) Clamp to max_trade_usd
        3) Clamp to exposure remaining
        4) Enforce minimum order size
        
        Returns:
            SizingResult with size_usd and tier info
        """
        confluence_count = getattr(signal, 'confluence_count', 1)
        raw_score = entry_score.total_score if hasattr(entry_score, 'total_score') else entry_score
        # Normalize score to 0-100 scale (signals come as 0-1 floats)
        score = raw_score * 100 if raw_score <= 1.0 else raw_score
        
        # Count current position tiers
        whale_threshold = settings.whale_trade_usd * 0.8
        strong_threshold = settings.strong_trade_usd * 0.8
        scout_threshold = settings.scout_trade_usd * 0.8
        
        whale_count = sum(1 for p in self.positions.values()
                         if getattr(p, 'entry_cost_usd', 0) >= whale_threshold)
        strong_count = sum(1 for p in self.positions.values()
                          if strong_threshold <= getattr(p, 'entry_cost_usd', 0) < whale_threshold)
        scout_count = sum(1 for p in self.positions.values()
                         if scout_threshold <= getattr(p, 'entry_cost_usd', 0) < strong_threshold)
        
        # Determine tier and calculate size based on portfolio percentage
        is_whale = score >= settings.whale_score_min and confluence_count >= settings.whale_confluence_min
        is_strong = score >= settings.strong_score_min
        is_normal = score >= settings.entry_score_min
        is_scout = settings.scout_score_min <= score < settings.entry_score_min
        
        # Get portfolio value for percentage-based sizing
        pv = portfolio_value if portfolio_value > 0 else 500.0
        min_trade = getattr(settings, "min_trade_usd", 5.0)  # Absolute floor to avoid dust
        
        if is_whale and whale_count < settings.whale_max_positions:
            # Use percentage if available, fallback to fixed USD
            pct = getattr(settings, "whale_trade_pct", 0.020)
            size_usd = max(pv * pct, settings.whale_trade_usd) if pv * pct >= min_trade else settings.whale_trade_usd
            tier = "🐋 WHALE"
            tier_code = "whale"
        elif is_strong and strong_count < settings.strong_max_positions:
            pct = getattr(settings, "strong_trade_pct", 0.016)
            size_usd = max(pv * pct, settings.strong_trade_usd) if pv * pct >= min_trade else settings.strong_trade_usd
            tier = "💪 STRONG"
            tier_code = "strong"
        elif is_normal:
            pct = getattr(settings, "normal_trade_pct", 0.013)
            size_usd = max(pv * pct, settings.normal_trade_usd) if pv * pct >= min_trade else settings.normal_trade_usd
            tier = "📊 NORMAL"
            tier_code = "normal"
        elif is_scout and scout_count < settings.scout_max_positions:
            pct = getattr(settings, "scout_trade_pct", 0.010)
            size_usd = max(pv * pct, settings.scout_trade_usd) if pv * pct >= min_trade else settings.scout_trade_usd
            tier = "🔍 SCOUT"
            tier_code = "scout"
        else:
            pct = getattr(settings, "normal_trade_pct", 0.013)
            size_usd = max(pv * pct, settings.normal_trade_usd) if pv * pct >= min_trade else settings.normal_trade_usd
            tier = "📊 NORMAL"
            tier_code = "normal"
        
        # Apply time-of-day multiplier
        session_mult = intelligence.get_size_multiplier()
        if session_mult < 1.0:
            size_usd *= session_mult

        # Portfolio guardrails (percent-based)
        min_pct_size = pv * settings.position_min_pct
        max_pct_size = pv * settings.position_max_pct
        size_usd = max(min_pct_size, min(max_pct_size, size_usd))

        # Clamp to max trade
        max_trade_usd = getattr(self.config, "max_trade_usd", settings.max_trade_usd)
        size_usd = min(size_usd, max_trade_usd)

        # Clamp to exposure remaining
        current_exposure = sum(p.cost_basis for p in self.positions.values())
        budget = pv * getattr(
            self.config, "portfolio_max_exposure_pct", settings.portfolio_max_exposure_pct
        )
        available = max(0.0, budget - current_exposure)
        if available > 0:
            size_usd = min(size_usd, available)
        else:
            size_usd = 0.0

        # Enforce minimum order size
        min_order_usd = max(
            getattr(self.config, "min_position_usd", 1.0),
            getattr(settings, "position_min_usd", 1.0),
        )
        
        return SizingResult(
            size_usd=size_usd,
            tier=tier,
            tier_code=tier_code,
            score=int(score),  # Ensure integer for logging
            confluence=confluence_count,
            available_budget=available,
            min_order_usd=min_order_usd,
            max_trade_usd=max_trade_usd,
            current_exposure=current_exposure,
            session_mult=session_mult,
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
        
        bot_budget = portfolio_value * getattr(
            self.config, "portfolio_max_exposure_pct", settings.portfolio_max_exposure_pct
        )
        current_exposure = sum(p.cost_basis for p in self.positions.values())
        available = bot_budget - current_exposure
        
        return size_usd <= available, available


def calculate_stops(
    price: float,
    is_fast: bool,
    config,
    symbol: str = ""
) -> Tuple[float, float, float, int]:
    """
    Calculate stop and target prices using dynamic asset-class parameters.
    
    Large caps (BTC, ETH) get wider stops for swing trading.
    Small/micro caps get tighter stops to cut losses quickly.
    
    Returns:
        Tuple of (stop_price, tp1_price, tp2_price, time_stop_min)
    """
    if is_fast:
        stop_price = price * (1 - settings.fast_stop_pct / 100)
        tp1_price = price * (1 + settings.fast_tp1_pct / 100)
        tp2_price = price * (1 + settings.fast_tp2_pct / 100)
        time_stop_min = settings.fast_time_stop_min
    elif symbol:
        # Use dynamic asset-class stops
        risk_profile = get_risk_profile(symbol)
        stop_pct = risk_profile.stop_loss_pct
        tp_pct = risk_profile.take_profit_pct
        max_hold = risk_profile.max_hold_hours * 60  # Convert to minutes
        
        stop_price = price * (1 - stop_pct)
        tp1_price = price * (1 + tp_pct)
        tp2_price = price * (1 + tp_pct * 1.5)
        time_stop_min = max_hold
        
        logger.debug("[STOPS] %s (%s): stop=%.1f%%, tp=%.1f%%, max_hold=%dh",
                    symbol, risk_profile.tier.value, stop_pct * 100, tp_pct * 100, 
                    risk_profile.max_hold_hours)
    else:
        # Fallback to config defaults
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
