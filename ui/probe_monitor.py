"""Probe monitoring - track REST probes and their performance."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List
from collections import deque


@dataclass
class ProbeResult:
    """Result of a REST probe."""
    symbol: str
    timestamp: datetime
    price: float
    spread_bps: float
    vol_spike: float
    trend_1m: float = 0.0  # 1-minute trend
    price_1m_ago: float = 0.0  # For calculating % change
    
    @property
    def pct_change_1m(self) -> float:
        """Calculate % change since probe."""
        if self.price_1m_ago > 0:
            return ((self.price - self.price_1m_ago) / self.price_1m_ago) * 100
        return 0.0
    
    @property
    def age_seconds(self) -> float:
        """Age of probe in seconds."""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


class ProbeMonitor:
    """Monitor REST probes and their outcomes."""
    
    def __init__(self, max_probes: int = 50):
        self.max_probes = max_probes
        self.probes: deque[ProbeResult] = deque(maxlen=max_probes)
        self.price_history: Dict[str, List[tuple[datetime, float]]] = {}
        
    def add_probe(self, symbol: str, price: float, spread_bps: float, vol_spike: float, trend_1m: float = 0.0):
        """Add a new probe result."""
        # Get price from 1 minute ago if available
        price_1m_ago = 0.0
        if symbol in self.price_history:
            now = datetime.now(timezone.utc)
            one_min_ago = now - timedelta(minutes=1)
            # Find closest price to 1 min ago
            for ts, p in reversed(self.price_history[symbol]):
                if ts <= one_min_ago:
                    price_1m_ago = p
                    break
        
        probe = ProbeResult(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            price=price,
            spread_bps=spread_bps,
            vol_spike=vol_spike,
            trend_1m=trend_1m,
            price_1m_ago=price_1m_ago
        )
        
        self.probes.append(probe)
        
        # Update price history
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        self.price_history[symbol].append((probe.timestamp, price))
        
        # Keep only last 10 minutes of history per symbol
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.price_history[symbol] = [
            (ts, p) for ts, p in self.price_history[symbol] if ts > cutoff
        ]
    
    def get_recent_probes(self, limit: int = 50) -> List[ProbeResult]:
        """Get most recent probes."""
        return list(self.probes)[-limit:]
    
    def get_stats(self) -> dict:
        """Get probe statistics."""
        if not self.probes:
            return {
                "total_probes": 0,
                "avg_spread": 0,
                "avg_vol_spike": 0,
                "winners": 0,
                "losers": 0,
                "avg_gain": 0
            }
        
        recent = list(self.probes)[-50:]
        
        # Calculate stats
        spreads = [p.spread_bps for p in recent]
        vol_spikes = [p.vol_spike for p in recent]
        changes = [p.pct_change_1m for p in recent if p.price_1m_ago > 0]
        
        winners = sum(1 for c in changes if c > 0)
        losers = sum(1 for c in changes if c < 0)
        avg_gain = sum(changes) / len(changes) if changes else 0
        
        return {
            "total_probes": len(recent),
            "avg_spread": sum(spreads) / len(spreads) if spreads else 0,
            "avg_vol_spike": sum(vol_spikes) / len(vol_spikes) if vol_spikes else 0,
            "winners": winners,
            "losers": losers,
            "avg_gain": avg_gain
        }


# Global instance
probe_monitor = ProbeMonitor(max_probes=50)
