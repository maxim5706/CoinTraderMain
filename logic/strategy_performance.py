"""
Strategy Performance Tracker - Live learning system.

Tracks which strategies are winning/losing in real-time.
Allows the bot to:
1. See what's working TODAY
2. Adapt position sizing based on recent performance
3. Discover new patterns that work
4. Kill strategies that don't work

This is the "learning" part of BEAST MODE.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from collections import defaultdict, deque


@dataclass
class StrategyTrade:
    """Individual trade result for a strategy."""
    strategy_id: str
    symbol: str
    entry_time: datetime
    exit_time: datetime
    size_usd: float
    pnl_usd: float
    pnl_pct: float
    score: int
    hold_time_min: int
    exit_reason: str  # "tp1", "tp2", "stop", "time", "thesis_break"


@dataclass
class StrategyStats:
    """Performance stats for a strategy."""
    strategy_id: str
    
    # Counts
    signals_generated: int = 0
    signals_taken: int = 0
    trades_completed: int = 0
    
    # Win/Loss
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    
    # P&L
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    
    # Quality metrics
    avg_score: float = 0.0
    avg_hold_time_min: float = 0.0
    
    # Exit breakdown
    exits_tp1: int = 0
    exits_tp2: int = 0
    exits_stop: int = 0
    exits_time: int = 0
    exits_thesis: int = 0
    
    # Rolling stats (last 10 trades)
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=10))
    
    @property
    def win_rate(self) -> float:
        """Win percentage."""
        if self.trades_completed == 0:
            return 0.0
        return self.wins / self.trades_completed * 100
    
    @property
    def avg_pnl(self) -> float:
        """Average P&L per trade."""
        if self.trades_completed == 0:
            return 0.0
        return self.total_pnl / self.trades_completed
    
    @property
    def profit_factor(self) -> float:
        """Gross profit / Gross loss."""
        total_wins = sum(t.pnl_usd for t in self.recent_trades if t.pnl_usd > 0)
        total_losses = abs(sum(t.pnl_usd for t in self.recent_trades if t.pnl_usd < 0))
        if total_losses == 0:
            return float('inf') if total_wins > 0 else 0.0
        return total_wins / total_losses
    
    @property
    def recent_win_rate(self) -> float:
        """Win rate of last 10 trades."""
        if not self.recent_trades:
            return 0.0
        wins = sum(1 for t in self.recent_trades if t.pnl_usd > 0)
        return wins / len(self.recent_trades) * 100
    
    @property
    def is_profitable(self) -> bool:
        """Is strategy profitable overall?"""
        return self.total_pnl > 0
    
    @property
    def confidence_score(self) -> float:
        """
        0-100 confidence that strategy has edge.
        
        Based on:
        - Win rate
        - Profit factor
        - Sample size
        - Recent performance
        """
        if self.trades_completed < 5:
            return 50.0  # Neutral until proven
        
        score = 50.0
        
        # Win rate component (30 points)
        if self.win_rate >= 70:
            score += 30
        elif self.win_rate >= 60:
            score += 20
        elif self.win_rate >= 50:
            score += 10
        elif self.win_rate < 40:
            score -= 20
        
        # Profit factor component (30 points)
        if self.profit_factor >= 2.0:
            score += 30
        elif self.profit_factor >= 1.5:
            score += 20
        elif self.profit_factor >= 1.0:
            score += 10
        elif self.profit_factor < 0.8:
            score -= 20
        
        # Sample size component (20 points)
        if self.trades_completed >= 30:
            score += 20
        elif self.trades_completed >= 20:
            score += 15
        elif self.trades_completed >= 10:
            score += 10
        
        # Recent trend (20 points)
        if self.recent_win_rate >= 70:
            score += 20
        elif self.recent_win_rate >= 50:
            score += 10
        elif self.recent_win_rate < 30:
            score -= 20
        
        return max(0, min(100, score))


class StrategyPerformanceTracker:
    """
    Tracks live performance of all strategies.
    
    This is the learning engine - figures out what works.
    """
    
    def __init__(self):
        self.stats: Dict[str, StrategyStats] = defaultdict(StrategyStats)
        self.all_trades: List[StrategyTrade] = []
        self._signal_log: Dict[str, List[dict]] = defaultdict(list)  # Track all signals
    
    def record_signal(self, strategy_id: str, symbol: str, score: int, taken: bool):
        """Record when a strategy generates a signal."""
        stats = self.stats[strategy_id]
        stats.strategy_id = strategy_id
        stats.signals_generated += 1
        
        if taken:
            stats.signals_taken += 1
        
        # Log for analysis
        self._signal_log[strategy_id].append({
            'timestamp': datetime.now(timezone.utc),
            'symbol': symbol,
            'score': score,
            'taken': taken
        })
    
    def record_trade(
        self,
        strategy_id: str,
        symbol: str,
        entry_time: datetime,
        exit_time: datetime,
        size_usd: float,
        pnl_usd: float,
        score: int,
        exit_reason: str
    ):
        """Record completed trade."""
        stats = self.stats[strategy_id]
        stats.strategy_id = strategy_id
        stats.trades_completed += 1
        
        # Calculate metrics
        pnl_pct = (pnl_usd / size_usd) * 100 if size_usd > 0 else 0
        hold_time = (exit_time - entry_time).total_seconds() / 60  # minutes
        
        # Create trade record
        trade = StrategyTrade(
            strategy_id=strategy_id,
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            size_usd=size_usd,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            score=score,
            hold_time_min=int(hold_time),
            exit_reason=exit_reason
        )
        
        # Update stats
        stats.total_pnl += pnl_usd
        
        if pnl_usd > 0.05:  # Win (>$0.05 to avoid fees)
            stats.wins += 1
            stats.avg_win = (stats.avg_win * (stats.wins - 1) + pnl_usd) / stats.wins
            stats.largest_win = max(stats.largest_win, pnl_usd)
        elif pnl_usd < -0.05:  # Loss
            stats.losses += 1
            stats.avg_loss = (stats.avg_loss * (stats.losses - 1) + pnl_usd) / stats.losses
            stats.largest_loss = min(stats.largest_loss, pnl_usd)
        else:  # Breakeven
            stats.breakevens += 1
        
        # Update averages
        total = stats.trades_completed
        stats.avg_score = (stats.avg_score * (total - 1) + score) / total
        stats.avg_hold_time_min = (stats.avg_hold_time_min * (total - 1) + hold_time) / total
        
        # Exit reason tracking
        if exit_reason == "tp1":
            stats.exits_tp1 += 1
        elif exit_reason == "tp2":
            stats.exits_tp2 += 1
        elif exit_reason == "stop":
            stats.exits_stop += 1
        elif exit_reason == "time":
            stats.exits_time += 1
        elif exit_reason == "thesis_break":
            stats.exits_thesis += 1
        
        # Add to recent trades
        stats.recent_trades.append(trade)
        self.all_trades.append(trade)
    
    def get_performance_summary(self, timeframe_hours: int = 24) -> Dict[str, StrategyStats]:
        """Get performance summary for all strategies."""
        return dict(self.stats)
    
    def get_best_strategies(self, n: int = 3) -> List[str]:
        """Get top N performing strategies by confidence score."""
        sorted_strats = sorted(
            self.stats.items(),
            key=lambda x: x[1].confidence_score,
            reverse=True
        )
        return [strat_id for strat_id, _ in sorted_strats[:n]]
    
    def get_worst_strategies(self, n: int = 3) -> List[str]:
        """Get bottom N performing strategies."""
        sorted_strats = sorted(
            self.stats.items(),
            key=lambda x: x[1].confidence_score
        )
        return [strat_id for strat_id, _ in sorted_strats[:n]]
    
    def should_trade_strategy(self, strategy_id: str) -> bool:
        """Should we still trade this strategy?"""
        stats = self.stats.get(strategy_id)
        if not stats or stats.trades_completed < 10:
            return True  # Not enough data, keep testing
        
        # Kill strategy if:
        # 1. Win rate < 30% after 20+ trades
        if stats.trades_completed >= 20 and stats.win_rate < 30:
            return False
        
        # 2. Losing money after 30+ trades
        if stats.trades_completed >= 30 and not stats.is_profitable:
            return False
        
        # 3. Recent performance terrible (0% win in last 10)
        if len(stats.recent_trades) >= 10 and stats.recent_win_rate == 0:
            return False
        
        return True
    
    def get_size_multiplier(self, strategy_id: str) -> float:
        """
        Adjust position size based on strategy performance.
        
        Returns multiplier 0.5-1.5×:
        - Hot strategies get 1.5× size
        - Cold strategies get 0.5× size
        """
        stats = self.stats.get(strategy_id)
        if not stats or stats.trades_completed < 10:
            return 1.0  # Neutral until proven
        
        confidence = stats.confidence_score
        
        if confidence >= 80:
            return 1.5  # Proven winner, bet more
        elif confidence >= 60:
            return 1.2
        elif confidence <= 30:
            return 0.5  # Underperforming, bet less
        elif confidence <= 50:
            return 0.8
        
        return 1.0


# Global tracker instance
performance_tracker = StrategyPerformanceTracker()
