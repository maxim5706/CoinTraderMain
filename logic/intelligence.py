"""Intelligence Layer - Smart entry filtering and position management.

Coordinator module that orchestrates:
- regime.py: BTC regime detection, sessions
- limits.py: Position limits, sector classification
- ml_cache.py: Live indicators and ML cache
- scoring.py: Entry scoring

Refactored from 1,153 lines to ~200 lines.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Dict, Optional

from core.config import settings
from core.models import Signal
from core.logging_utils import get_logger

from logic.regime import regime_detector, session_detector, RegimeDetector
from logic.limits import limit_checker, LimitChecker, PositionLimits, SECTOR_MAP, CORRELATION_GROUPS
from logic.ml_cache import indicator_cache, IndicatorCache
from logic.scoring import EntryScore, EntryScorer, CANONICAL_GATE_ORDER
from logic.sector_tracker import sector_tracker, SectorTracker

logger = get_logger(__name__)

__all__ = [
    'intelligence',
    'IntelligenceLayer',
    'EntryScore',
    'PositionLimits',
    'SECTOR_MAP',
    'CORRELATION_GROUPS',
    'CANONICAL_GATE_ORDER',
]


class IntelligenceLayer:
    """Smart decision layer coordinating regime, limits, cache, and scoring."""
    
    def __init__(self):
        self.regime = regime_detector
        self.limits = limit_checker
        self.cache = indicator_cache
        self.sectors = sector_tracker
        self.scorer = EntryScorer(self.regime, self.cache, self.limits)
        
        self._daily_realized_pnl: float = 0.0
        self._daily_reset_date: Optional[date] = None
        self._trading_halted: bool = False
        self._strategy_stats: Dict[str, Dict] = {}
    
    # Regime delegation
    @property
    def _market_regime(self) -> str:
        return self.regime.regime
    
    @property
    def _btc_trend_1h(self) -> float:
        return self.regime.btc_trend_1h
    
    @property
    def is_safe_to_trade(self) -> bool:
        return self.regime.is_safe_to_trade
    
    @property
    def regime_status(self) -> str:
        return self.regime.get_status_string()
    
    def update_btc_trend(self, trend_1h: float, trend_15m: float = 0.0, price: float = 0.0):
        self.regime.update_btc_trend(trend_1h, trend_15m, price)
    
    def fetch_btc_trend(self) -> bool:
        return self.regime.fetch_btc_trend()
    
    def fetch_fear_greed(self) -> Optional[int]:
        return self.regime.fetch_fear_greed()
    
    def get_fear_greed(self) -> Optional[dict]:
        return self.regime.get_fear_greed()
    
    def get_session_info(self) -> dict:
        return session_detector.get_session_info()
    
    def get_size_multiplier(self) -> float:
        return session_detector.get_size_multiplier()
    
    # Limits delegation
    def get_sector(self, symbol: str) -> str:
        return self.limits.get_sector(symbol)
    
    def update_sector_counts(self, positions: dict):
        self.limits.update_sector_counts(positions)
    
    # Sector tracking delegation
    def update_symbol_trend(self, symbol: str, trend_1h: float, trend_5m: float = 0.0, price: float = 0.0):
        """Update trend data for sector tracking."""
        self.sectors.update_symbol(symbol, trend_1h, trend_5m, price)
        # Also update BTC trend for divergence detection
        if symbol in ("BTC-USD", "BTC"):
            self.sectors.update_btc_trend(trend_1h)
    
    def get_hot_sectors(self):
        """Get sectors with positive momentum."""
        return self.sectors.get_hot_sectors()
    
    def get_diverging_sectors(self):
        """Get sectors diverging from BTC - rare opportunities."""
        return self.sectors.get_diverging_sectors()
    
    def get_rotation_opportunities(self):
        """Find rotation opportunities."""
        return self.sectors.get_rotation_opportunities()
    
    def is_high_conviction_setup(self, symbol: str) -> tuple[bool, str]:
        """Check if this is a high conviction setup based on sector rotation."""
        # In hot sector
        if self.sectors.is_symbol_in_hot_sector(symbol):
            return True, "hot_sector"
        
        # Diverging from BTC (rare)
        if self.sectors.is_symbol_diverging(symbol):
            return True, "btc_divergence"
        
        # Best performer in its sector
        base = symbol.split("-")[0] if "-" in symbol else symbol
        sector = SECTOR_MAP.get(base, "other")
        if self.sectors.get_best_in_sector(sector) == symbol:
            return True, "sector_leader"
        
        return False, ""
    
    def get_weakest_position(self, positions: dict) -> Optional[str]:
        """Find the weakest position to potentially rotate out of."""
        if not positions:
            return None
        
        weakest = None
        worst_score = float('inf')
        
        for symbol, pos in positions.items():
            # Skip staked or very new positions
            if hasattr(pos, 'hold_duration_minutes') and pos.hold_duration_minutes() < 30:
                continue
            
            # Score based on PnL and sector strength
            pnl_pct = getattr(pos, 'pnl_pct', 0) or 0
            sector_strength = 0
            
            base = symbol.split("-")[0] if "-" in symbol else symbol
            sector = SECTOR_MAP.get(base, "other")
            stats = self.sectors._sector_stats.get(sector)
            if stats:
                sector_strength = stats.strength_score
            
            # Combined score: negative PnL + cold sector = weak
            score = pnl_pct + (sector_strength / 10)
            
            if score < worst_score:
                worst_score = score
                weakest = symbol
        
        return weakest
    
    def should_rotate_position(self, weak_symbol: str, strong_signal_symbol: str, 
                                positions: dict) -> tuple[bool, str]:
        """Determine if we should close weak position to open strong one."""
        weak_pos = positions.get(weak_symbol)
        if not weak_pos:
            return False, "no_weak_position"
        
        weak_pnl_pct = getattr(weak_pos, 'pnl_pct', 0) or 0
        
        # Only rotate if weak position is losing
        if weak_pnl_pct > -2.0:
            return False, "weak_not_losing_enough"
        
        # Check if strong signal is high conviction
        is_high_conv, conv_reason = self.is_high_conviction_setup(strong_signal_symbol)
        if not is_high_conv:
            return False, "signal_not_high_conviction"
        
        return True, f"rotate_{conv_reason}"
    
    def get_sector_summary(self) -> Dict:
        """Get sector status for dashboard."""
        return self.sectors.get_status_summary()
    
    def check_position_limits(self, symbol: str, size_usd: float, 
                               current_positions: dict) -> tuple[bool, str]:
        return self.limits.check_limits(symbol, size_usd, current_positions)
    
    def record_trade(self):
        self.limits.record_trade()
    
    # Cache delegation
    def update_live_indicators(self, symbol: str, indicators):
        self.cache.update_indicators(symbol, indicators)
    
    def get_live_ml(self, symbol: str, max_stale_seconds: float = 180):
        return self.cache.get_ml(symbol, max_stale_seconds)
    
    def get_live_indicators(self, symbol: str, max_stale_seconds: float = 120):
        return self.cache.get_indicators(symbol, max_stale_seconds)
    
    @property
    def live_indicators(self):
        return self.cache.live_indicators
    
    @property
    def live_ml(self):
        return self.cache.live_ml
    
    # Scoring delegation
    def score_entry(self, signal: Signal, burst_metrics: dict, 
                    current_positions: dict) -> EntryScore:
        return self.scorer.score(signal, burst_metrics, current_positions)
    
    def get_position_size(self, base_size: float, score: EntryScore) -> float:
        return self.scorer.get_position_size(base_size, score)
    
    # Daily loss tracking
    def _check_daily_reset(self):
        today = date.today()
        if self._daily_reset_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_reset_date = today
            self._trading_halted = False
    
    def record_trade_result(self, pnl: float, strategy_id: str = "unknown", is_win: bool = False):
        self._check_daily_reset()
        self._daily_realized_pnl += pnl
        
        if self._daily_realized_pnl <= -self.limits.limits.daily_loss_limit_usd:
            self._trading_halted = True
        
        if strategy_id not in self._strategy_stats:
            self._strategy_stats[strategy_id] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        
        stats = self._strategy_stats[strategy_id]
        if is_win:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        stats["total_pnl"] += pnl
    
    def is_trading_halted(self) -> tuple[bool, str]:
        self._check_daily_reset()
        if self._trading_halted:
            return True, f"Daily loss limit hit: ${self._daily_realized_pnl:.2f}"
        return False, ""
    
    def get_daily_pnl(self) -> float:
        self._check_daily_reset()
        return self._daily_realized_pnl
    
    # Logging
    def log_trade_entry(self, symbol: str, score: EntryScore, burst_metrics: dict):
        from core.logger import log_trade, utc_iso_str
        log_trade({
            "ts": utc_iso_str(),
            "type": "ml_entry",
            "symbol": symbol,
            "score_total": score.total_score,
            "score_trend": score.trend_score,
            "score_volume": score.volume_score,
            "vol_spike": burst_metrics.get("vol_spike", 0),
            "trend_15m": burst_metrics.get("trend_15m", 0),
            "btc_trend": self._btc_trend_1h,
            "market_regime": self._market_regime,
            "sector": self.get_sector(symbol),
        })
    
    def log_trade_exit(self, symbol: str, pnl: float, pnl_pct: float,
                       exit_reason: str, hold_minutes: float, strategy_id: str = "unknown"):
        from core.logger import log_trade, utc_iso_str
        is_win = pnl > 0
        log_trade({
            "ts": utc_iso_str(),
            "type": "ml_exit",
            "symbol": symbol,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "hold_minutes": hold_minutes,
            "is_win": is_win,
            "btc_trend_at_exit": self._btc_trend_1h,
            "strategy_id": strategy_id,
        })
        self.record_trade_result(pnl, strategy_id, is_win)
    
    # Strategy stats
    def get_strategy_stats(self) -> Dict[str, Dict]:
        result = {}
        for strategy_id, stats in self._strategy_stats.items():
            wins = stats["wins"]
            losses = stats["losses"]
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            result[strategy_id] = {
                "wins": wins,
                "losses": losses,
                "total": total,
                "win_rate": win_rate,
                "total_pnl": stats["total_pnl"],
            }
        return result
    
    def get_strategy_summary(self) -> str:
        stats = self.get_strategy_stats()
        if not stats:
            return "No trades recorded yet"
        lines = ["Strategy Performance:"]
        for strat, data in sorted(stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            pnl_str = f"+${data['total_pnl']:.2f}" if data['total_pnl'] >= 0 else f"-${abs(data['total_pnl']):.2f}"
            lines.append(f"  {strat}: {data['wins']}W/{data['losses']}L ({data['win_rate']:.0f}%) {pnl_str}")
        return "\n".join(lines)


intelligence = IntelligenceLayer()
