"""Session statistics tracking - tracks performance since bot startup.

Separate from daily stats which reset at midnight.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from core.logging_utils import get_logger

logger = get_logger(__name__)

SESSION_FILE = Path("data/session_stats.json")


@dataclass
class HourlyStats:
    """Stats for a single hour."""
    hour: str  # "2025-12-21T15:00"
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    start_balance: float = 0.0
    end_balance: float = 0.0


@dataclass
class SessionStats:
    """Session statistics since bot startup."""
    session_start: str = ""  # ISO timestamp
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    start_balance: float = 0.0
    current_balance: float = 0.0
    peak_balance: float = 0.0
    trough_balance: float = float('inf')
    hourly_data: List[dict] = field(default_factory=list)
    _current_hour: str = ""
    
    def __post_init__(self):
        if not self.session_start:
            self.session_start = datetime.now(timezone.utc).isoformat()
    
    def record_trade(self, pnl: float, is_win: bool):
        """Record a completed trade."""
        self.trades += 1
        self.total_pnl += pnl
        
        if is_win:
            self.wins += 1
            if pnl > self.biggest_win:
                self.biggest_win = pnl
        else:
            self.losses += 1
            if pnl < self.biggest_loss:
                self.biggest_loss = pnl
        
        # Update hourly data
        self._update_hourly(pnl, is_win)
        self.save()
    
    def update_balance(self, balance: float):
        """Update current balance and track peaks/troughs."""
        if self.start_balance == 0:
            self.start_balance = balance
        
        self.current_balance = balance
        
        if balance > self.peak_balance:
            self.peak_balance = balance
        if balance < self.trough_balance and balance > 0:
            self.trough_balance = balance
        
        # Update hourly end balance
        hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        if self.hourly_data:
            last_hour = self.hourly_data[-1]
            if last_hour.get("hour") == hour_key:
                last_hour["end_balance"] = balance
    
    def _update_hourly(self, pnl: float, is_win: bool):
        """Update hourly stats."""
        hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        
        # Find or create hour entry
        hour_entry = None
        for h in self.hourly_data:
            if h.get("hour") == hour_key:
                hour_entry = h
                break
        
        if not hour_entry:
            hour_entry = {
                "hour": hour_key,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "start_balance": self.current_balance,
                "end_balance": self.current_balance,
            }
            self.hourly_data.append(hour_entry)
            # Keep only last 24 hours
            if len(self.hourly_data) > 24:
                self.hourly_data = self.hourly_data[-24:]
        
        hour_entry["trades"] += 1
        hour_entry["pnl"] += pnl
        if is_win:
            hour_entry["wins"] += 1
        else:
            hour_entry["losses"] += 1
    
    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return (self.wins / self.trades) * 100
    
    @property
    def session_return(self) -> float:
        if self.start_balance == 0:
            return 0.0
        return ((self.current_balance / self.start_balance) - 1) * 100
    
    @property
    def max_drawdown(self) -> float:
        if self.peak_balance == 0:
            return 0.0
        return ((self.trough_balance / self.peak_balance) - 1) * 100
    
    def get_chart_data(self) -> List[dict]:
        """Get data formatted for charting."""
        return [
            {
                "hour": h["hour"],
                "pnl": round(h["pnl"], 2),
                "cumulative_pnl": round(sum(x["pnl"] for x in self.hourly_data[:i+1]), 2),
                "trades": h["trades"],
                "win_rate": round((h["wins"] / h["trades"] * 100) if h["trades"] > 0 else 0, 1),
            }
            for i, h in enumerate(self.hourly_data)
        ]
    
    def save(self):
        """Persist to disk."""
        try:
            data = {
                "session_start": self.session_start,
                "trades": self.trades,
                "wins": self.wins,
                "losses": self.losses,
                "total_pnl": self.total_pnl,
                "biggest_win": self.biggest_win,
                "biggest_loss": self.biggest_loss,
                "start_balance": self.start_balance,
                "current_balance": self.current_balance,
                "peak_balance": self.peak_balance,
                "trough_balance": self.trough_balance if self.trough_balance != float('inf') else 0,
                "hourly_data": self.hourly_data,
            }
            SESSION_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("[SESSION] Save failed: %s", e)
    
    @classmethod
    def load(cls) -> "SessionStats":
        """Load from disk or create new."""
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text())
                stats = cls(
                    session_start=data.get("session_start", ""),
                    trades=data.get("trades", 0),
                    wins=data.get("wins", 0),
                    losses=data.get("losses", 0),
                    total_pnl=data.get("total_pnl", 0.0),
                    biggest_win=data.get("biggest_win", 0.0),
                    biggest_loss=data.get("biggest_loss", 0.0),
                    start_balance=data.get("start_balance", 0.0),
                    current_balance=data.get("current_balance", 0.0),
                    peak_balance=data.get("peak_balance", 0.0),
                    trough_balance=data.get("trough_balance", float('inf')),
                    hourly_data=data.get("hourly_data", []),
                )
                return stats
            except Exception as e:
                logger.warning("[SESSION] Load failed: %s", e)
        return cls()
    
    @classmethod
    def new_session(cls) -> "SessionStats":
        """Start a fresh session."""
        stats = cls()
        stats.save()
        return stats


# Singleton
_session: Optional[SessionStats] = None


def get_session() -> SessionStats:
    """Get current session stats."""
    global _session
    if _session is None:
        _session = SessionStats.load()
    return _session


def start_new_session() -> SessionStats:
    """Start a new session (clears old stats)."""
    global _session
    _session = SessionStats.new_session()
    return _session


def record_session_trade(pnl: float, is_win: bool):
    """Record a trade in session stats."""
    get_session().record_trade(pnl, is_win)


def update_session_balance(balance: float):
    """Update current balance in session."""
    get_session().update_balance(balance)
