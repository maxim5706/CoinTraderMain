"""Portfolio balance history tracking over time.

Tracks portfolio value at intervals and calculates:
- 1 hour change
- 1 day change  
- 5 day change
- All-time high/low
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from core.logging_utils import get_logger

logger = get_logger(__name__)

HISTORY_FILE = Path("data/portfolio_history.json")
SNAPSHOT_INTERVAL_MINUTES = 5  # Take snapshot every 5 minutes


@dataclass
class BalanceSnapshot:
    """Single point-in-time portfolio snapshot."""
    timestamp: str  # ISO format
    total_usd: float
    cash_usd: float
    crypto_usd: float
    position_count: int


@dataclass 
class PortfolioHistory:
    """Portfolio balance history with time-based calculations."""
    snapshots: List[dict] = field(default_factory=list)
    all_time_high: float = 0.0
    all_time_low: float = float('inf')
    _last_snapshot_time: Optional[datetime] = None
    
    def record(self, total_usd: float, cash_usd: float, crypto_usd: float, position_count: int):
        """Record current balance if enough time has passed."""
        now = datetime.now(timezone.utc)
        
        # Only snapshot every N minutes to avoid bloat
        if self._last_snapshot_time:
            elapsed = (now - self._last_snapshot_time).total_seconds() / 60
            if elapsed < SNAPSHOT_INTERVAL_MINUTES:
                return
        
        snapshot = {
            "timestamp": now.isoformat(),
            "total_usd": round(total_usd, 2),
            "cash_usd": round(cash_usd, 2),
            "crypto_usd": round(crypto_usd, 2),
            "position_count": position_count,
        }
        
        self.snapshots.append(snapshot)
        self._last_snapshot_time = now
        
        # Track all-time high/low
        if total_usd > self.all_time_high:
            self.all_time_high = total_usd
        if total_usd < self.all_time_low and total_usd > 0:
            self.all_time_low = total_usd
        
        # Prune old snapshots (keep 7 days max)
        self._prune_old_snapshots()
        
        # Persist
        self.save()
    
    def _prune_old_snapshots(self):
        """Remove snapshots older than 7 days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        cutoff_str = cutoff.isoformat()
        self.snapshots = [s for s in self.snapshots if s["timestamp"] > cutoff_str]
    
    def _get_snapshot_at(self, hours_ago: float) -> Optional[dict]:
        """Get closest snapshot to N hours ago."""
        if not self.snapshots:
            return None
        
        target_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        target_str = target_time.isoformat()
        
        # Find closest snapshot at or before target time
        closest = None
        for snap in reversed(self.snapshots):
            if snap["timestamp"] <= target_str:
                closest = snap
                break
        
        return closest
    
    def get_change_1h(self, current_total: float) -> Optional[float]:
        """Get portfolio change over last hour (percentage)."""
        snap = self._get_snapshot_at(1.0)
        if snap and snap["total_usd"] > 0:
            return ((current_total / snap["total_usd"]) - 1) * 100
        return None
    
    def get_change_1d(self, current_total: float) -> Optional[float]:
        """Get portfolio change over last 24 hours (percentage)."""
        snap = self._get_snapshot_at(24.0)
        if snap and snap["total_usd"] > 0:
            return ((current_total / snap["total_usd"]) - 1) * 100
        return None
    
    def get_change_5d(self, current_total: float) -> Optional[float]:
        """Get portfolio change over last 5 days (percentage)."""
        snap = self._get_snapshot_at(24.0 * 5)
        if snap and snap["total_usd"] > 0:
            return ((current_total / snap["total_usd"]) - 1) * 100
        return None
    
    def get_oldest_snapshot(self) -> Optional[dict]:
        """Get the oldest snapshot we have."""
        return self.snapshots[0] if self.snapshots else None
    
    def get_summary(self, current_total: float) -> dict:
        """Get summary of portfolio changes."""
        return {
            "current": round(current_total, 2),
            "change_1h": self.get_change_1h(current_total),
            "change_1d": self.get_change_1d(current_total),
            "change_5d": self.get_change_5d(current_total),
            "all_time_high": round(self.all_time_high, 2),
            "all_time_low": round(self.all_time_low, 2) if self.all_time_low != float('inf') else None,
            "snapshot_count": len(self.snapshots),
        }
    
    def save(self):
        """Persist history to disk."""
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "snapshots": self.snapshots[-2000:],  # Keep last 2000 snapshots max
                "all_time_high": self.all_time_high,
                "all_time_low": self.all_time_low if self.all_time_low != float('inf') else 0,
            }
            HISTORY_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("[HISTORY] Failed to save: %s", e)
    
    @classmethod
    def load(cls) -> "PortfolioHistory":
        """Load history from disk."""
        history = cls()
        
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text())
                history.snapshots = data.get("snapshots", [])
                history.all_time_high = data.get("all_time_high", 0)
                history.all_time_low = data.get("all_time_low", float('inf'))
                if history.all_time_low == 0:
                    history.all_time_low = float('inf')
                logger.info("[HISTORY] Loaded %d snapshots, ATH: $%.2f", 
                           len(history.snapshots), history.all_time_high)
            except Exception as e:
                logger.warning("[HISTORY] Failed to load: %s", e)
        
        return history


# Singleton instance
_history: Optional[PortfolioHistory] = None


def get_history() -> PortfolioHistory:
    """Get singleton portfolio history instance."""
    global _history
    if _history is None:
        _history = PortfolioHistory.load()
    return _history


def record_balance(total_usd: float, cash_usd: float, crypto_usd: float, position_count: int):
    """Record current portfolio balance."""
    get_history().record(total_usd, cash_usd, crypto_usd, position_count)


def get_portfolio_summary(current_total: float) -> dict:
    """Get portfolio change summary."""
    return get_history().get_summary(current_total)
