"""Risk management components - daily stats, circuit breaker, cooldowns."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import settings
from core.logging_utils import get_logger
from core.mode_configs import TradingMode

logger = get_logger(__name__)


@dataclass
class DailyStats:
    """Daily trading statistics with compounding metrics and persistence."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
    
    # For avg win/loss calculation
    total_win_pnl: float = 0.0
    total_loss_pnl: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    
    # Track the date these stats are for
    stats_date: str = ""
    
    # Persistence path
    _persist_path: str = "data/daily_stats.json"
    
    def check_reset(self):
        """Reset stats if it's a new day (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.stats_date != today:
            logger.info("[STATS] New day detected (%s → %s), resetting daily stats", self.stats_date, today)
            self.trades = 0
            self.wins = 0
            self.losses = 0
            self.total_pnl = 0.0
            self.max_drawdown = 0.0
            self.peak_pnl = 0.0
            self.total_win_pnl = 0.0
            self.total_loss_pnl = 0.0
            self.biggest_win = 0.0
            self.biggest_loss = 0.0
            self.stats_date = today
    
    def record_trade(self, pnl: float):
        self.trades += 1
        if pnl > 0:
            self.wins += 1
            self.total_win_pnl += pnl
            self.biggest_win = max(self.biggest_win, pnl)
        elif pnl < 0:
            # Only count actual losses, not breakeven trades
            self.losses += 1
            self.total_loss_pnl += abs(pnl)
            self.biggest_loss = max(self.biggest_loss, abs(pnl))
        # pnl == 0 is breakeven, counted in trades but not wins/losses
        self.total_pnl += pnl
        self.peak_pnl = max(self.peak_pnl, self.total_pnl)
        self.max_drawdown = min(self.max_drawdown, self.total_pnl - self.peak_pnl)
        # Persist after each trade
        self.save()
    
    def save(self):
        """Persist daily stats to disk."""
        import os
        import tempfile
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "trades": self.trades,
                "wins": self.wins,
                "losses": self.losses,
                "total_pnl": self.total_pnl,
                "max_drawdown": self.max_drawdown,
                "peak_pnl": self.peak_pnl,
                "total_win_pnl": self.total_win_pnl,
                "total_loss_pnl": self.total_loss_pnl,
                "biggest_win": self.biggest_win,
                "biggest_loss": self.biggest_loss,
                "stats_date": self.stats_date,
            }
            # Atomic write
            temp_fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".stats_", suffix=".tmp")
            try:
                with os.fdopen(temp_fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_path, path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            logger.warning("[STATS] Failed to save daily stats: %s", e)
    
    @classmethod
    def load(cls, mode: TradingMode = None) -> "DailyStats":
        """Load daily stats from disk or create new."""
        prefix = "live" if mode != TradingMode.PAPER else "paper"
        path = Path(f"data/{prefix}_daily_stats.json")
        stats = cls(_persist_path=str(path))
        
        if path.exists():
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                stats.trades = data.get("trades", 0)
                stats.wins = data.get("wins", 0)
                stats.losses = data.get("losses", 0)
                stats.total_pnl = data.get("total_pnl", 0.0)
                stats.max_drawdown = data.get("max_drawdown", 0.0)
                stats.peak_pnl = data.get("peak_pnl", 0.0)
                stats.total_win_pnl = data.get("total_win_pnl", 0.0)
                stats.total_loss_pnl = data.get("total_loss_pnl", 0.0)
                stats.biggest_win = data.get("biggest_win", 0.0)
                stats.biggest_loss = data.get("biggest_loss", 0.0)
                stats.stats_date = data.get("stats_date", "")
                logger.info("[STATS] Loaded daily stats: %d trades, %d W / %d L, $%.2f PnL",
                           stats.trades, stats.wins, stats.losses, stats.total_pnl)
            except Exception as e:
                logger.warning("[STATS] Failed to load daily stats: %s", e)
        
        # Check for day reset
        stats.check_reset()
        return stats
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0
    
    @property
    def avg_win(self) -> float:
        return self.total_win_pnl / self.wins if self.wins > 0 else 0.0
    
    @property
    def avg_loss(self) -> float:
        return self.total_loss_pnl / self.losses if self.losses > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        """Sum of wins / sum of losses. >1.0 is profitable."""
        return self.total_win_pnl / self.total_loss_pnl if self.total_loss_pnl > 0 else float('inf')
    
    @property
    def avg_r(self) -> float:
        """Average R per trade (avg_win / avg_loss ratio weighted by win rate)."""
        if self.avg_loss <= 0:
            return 0.0
        return (self.avg_win * self.win_rate - self.avg_loss * (1 - self.win_rate)) / self.avg_loss
    
    @property
    def should_stop(self) -> bool:
        return self.total_pnl <= -settings.daily_max_loss_usd
    
    @property
    def loss_limit_pct(self) -> float:
        """How close to daily loss limit (0-100%)."""
        if self.total_pnl >= 0:
            return 0.0
        if settings.daily_max_loss_usd <= 0:
            return 100.0  # No loss limit configured means always at limit
        return min(100.0, abs(self.total_pnl) / settings.daily_max_loss_usd * 100)


@dataclass
class CircuitBreaker:
    """
    Circuit breaker that pauses trading after consecutive API failures.
    
    States:
    - CLOSED: Normal operation, trades allowed
    - OPEN: Too many failures, trades blocked
    - HALF_OPEN: Testing if API recovered (allows 1 trade)
    """
    max_consecutive_failures: int = settings.circuit_breaker_max_failures
    reset_after_seconds: int = settings.circuit_breaker_reset_seconds
    
    consecutive_failures: int = 0
    last_failure_time: Optional[datetime] = None
    state: str = "closed"  # closed, open, half_open
    
    def record_success(self):
        """API call succeeded - reset failure count."""
        self.consecutive_failures = 0
        self.state = "closed"
        self.last_failure_time = None
    
    def record_failure(self):
        """API call failed - increment counter and potentially trip breaker."""
        self.consecutive_failures += 1
        self.last_failure_time = datetime.now(timezone.utc)
        
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.state = "open"
            logger.error(
                "[CIRCUIT] Breaker OPEN after %d consecutive failures - blocking trades for %ds",
                self.consecutive_failures,
                self.reset_after_seconds,
            )
    
    def can_trade(self) -> bool:
        """Check if trading is allowed."""
        if self.state == "closed":
            return True
        
        if self.state == "open":
            # Check if enough time has passed to try again
            if self.last_failure_time:
                elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()
                if elapsed >= self.reset_after_seconds:
                    self.state = "half_open"
                    logger.info("[CIRCUIT] Breaker HALF-OPEN - allowing test trade")
                    return True
            return False
        
        # half_open: allow one trade to test
        return True
    
    @property
    def is_tripped(self) -> bool:
        return self.state == "open"


class CooldownPersistence:
    """Persists order cooldowns to survive restarts with atomic writes."""
    
    def __init__(self, mode: TradingMode):
        prefix = "paper" if mode == TradingMode.PAPER else "live"
        self.file_path = Path(f"data/{prefix}_cooldowns.json")
    
    def save(self, cooldowns: dict[str, datetime]) -> None:
        """Save cooldowns to disk atomically."""
        import os
        import tempfile
        
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            data = {sym: ts.isoformat() for sym, ts in cooldowns.items()}
            
            # Atomic write: temp file then rename
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.file_path.parent,
                prefix=".cooldowns_",
                suffix=".tmp"
            )
            try:
                with os.fdopen(temp_fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_path, self.file_path)
            except Exception:
                # Cleanup temp file on failure
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise
                
        except Exception as e:
            logger.warning("[COOLDOWN] Failed to save cooldowns: %s", e)
    
    def load(self) -> dict[str, datetime]:
        """Load cooldowns from disk, filtering out expired ones."""
        if not self.file_path.exists():
            return {}
        
        try:
            with open(self.file_path, "r") as f:
                data = json.load(f)
            
            cooldowns = {}
            now = datetime.now(timezone.utc)
            cooldown_seconds = settings.order_cooldown_seconds
            
            for sym, ts_str in data.items():
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                
                # Only keep if not expired
                if (now - ts).total_seconds() < cooldown_seconds:
                    cooldowns[sym] = ts
            
            if cooldowns:
                logger.info("[COOLDOWN] Loaded %d active cooldowns from disk", len(cooldowns))
            return cooldowns
            
        except Exception as e:
            logger.warning("[COOLDOWN] Failed to load cooldowns: %s", e)
            return {}
