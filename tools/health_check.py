"""
Health Check - Quick "green/red early" validation.

Run after 1-3 minutes of bot operation to verify system is behaving correctly.

Green signs:
✅ Universe rebuild succeeds
✅ WS stable (no reconnect storms)
✅ ML fresh counts stay high
✅ Zero duplicate compute warnings
✅ Entry candidates passing warmth + spread gates normally

Red signs:
❌ WS reconnect loop
❌ ML mostly stale
❌ Universe collapsing to tiny set
❌ Repeated partial-fill warnings
"""

import os
import sys
import json
from pathlib import Path

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple
from dataclasses import dataclass, field
from core.mode_paths import get_status_path

# Ensure project root is on sys.path when executed as a script
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@dataclass
class HealthStatus:
    """Health check results."""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Overall
    is_healthy: bool = True
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    # Components
    ws_healthy: bool = True
    ws_reconnects: int = 0
    ws_last_msg_age_s: float = 0.0
    
    ml_fresh_count: int = 0
    ml_stale_count: int = 0
    ml_health_pct: float = 100.0
    
    universe_total: int = 0
    universe_eligible: int = 0
    universe_warm: int = 0
    universe_cold: int = 0
    
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    
    # Filters
    spread_rejections: int = 0
    warmth_rejections: int = 0
    regime_rejections: int = 0
    
    def add_issue(self, msg: str):
        self.issues.append(msg)
        self.is_healthy = False
    
    def add_warning(self, msg: str):
        self.warnings.append(msg)
    
    def summary(self) -> str:
        """Generate summary report."""
        lines = []
        lines.append("=" * 60)
        lines.append(f"HEALTH CHECK @ {self.timestamp.strftime('%H:%M:%S UTC')}")
        lines.append("=" * 60)
        
        status = "🟢 HEALTHY" if self.is_healthy else "🔴 ISSUES DETECTED"
        lines.append(f"\nStatus: {status}")
        
        # WebSocket
        ws_status = "🟢" if self.ws_healthy else "🔴"
        lines.append(f"\n📡 WebSocket: {ws_status}")
        lines.append(f"   Last message: {self.ws_last_msg_age_s:.1f}s ago")
        lines.append(f"   Reconnects: {self.ws_reconnects}")
        
        # ML
        ml_status = "🟢" if self.ml_health_pct >= 70 else "🟡" if self.ml_health_pct >= 50 else "🔴"
        lines.append(f"\n🤖 ML Cache: {ml_status}")
        lines.append(f"   Fresh: {self.ml_fresh_count}, Stale: {self.ml_stale_count}")
        lines.append(f"   Health: {self.ml_health_pct:.0f}%")
        
        # Universe
        univ_status = "🟢" if self.universe_eligible >= 20 else "🟡" if self.universe_eligible >= 10 else "🔴"
        lines.append(f"\n🌐 Universe: {univ_status}")
        lines.append(f"   Total: {self.universe_total}, Eligible: {self.universe_eligible}")
        lines.append(f"   Warm: {self.universe_warm}, Cold: {self.universe_cold}")
        lines.append(f"   Tiers: T1={self.tier1_count}, T2={self.tier2_count}, T3={self.tier3_count}")
        
        # Filters
        lines.append(f"\n🚫 Rejections:")
        lines.append(f"   Spread: {self.spread_rejections}")
        lines.append(f"   Warmth: {self.warmth_rejections}")
        lines.append(f"   Regime: {self.regime_rejections}")
        
        # Issues
        if self.issues:
            lines.append(f"\n❌ ISSUES:")
            for issue in self.issues:
                lines.append(f"   • {issue}")
        
        # Warnings
        if self.warnings:
            lines.append(f"\n⚠️ WARNINGS:")
            for warn in self.warnings:
                lines.append(f"   • {warn}")
        
        lines.append("\n" + "=" * 60)
        return "\n".join(lines)


def run_health_check() -> HealthStatus:
    """Run health check on current system state."""
    status = HealthStatus()
    
    try:
        # Optional snapshot from running bot
        snapshot_path = get_status_path()
        if snapshot_path.exists():
            try:
                snap = json.loads(snapshot_path.read_text())
                uni = snap.get("universe", {})
                status.universe_eligible = uni.get("eligible", status.universe_eligible)
                status.universe_warm = uni.get("warm", status.universe_warm)
                status.universe_cold = uni.get("cold", status.universe_cold)
                status.tier1_count = uni.get("tier1", status.tier1_count)
                status.tier2_count = uni.get("tier2", status.tier2_count)
                status.tier3_count = uni.get("tier3", status.tier3_count)
                status.ws_last_msg_age_s = snap.get("ws_last_age", status.ws_last_msg_age_s)
                status.ws_healthy = snap.get("ws_ok", status.ws_healthy)
                status.ws_reconnects = snap.get("ws_reconnect_count", status.ws_reconnects)
                
                # ML from snapshot
                ml_snap = snap.get("ml", {})
                if ml_snap:
                    status.ml_health_pct = ml_snap.get("fresh_pct", status.ml_health_pct)
                    status.ml_fresh_count = int(ml_snap.get("total_cached", 0) * ml_snap.get("fresh_pct", 0) / 100)
                    status.ml_stale_count = ml_snap.get("total_cached", 0) - status.ml_fresh_count
                
                rej = snap.get("rejections", {})
                status.spread_rejections = rej.get("spread", status.spread_rejections)
                status.warmth_rejections = rej.get("warmth", status.warmth_rejections)
                status.regime_rejections = rej.get("regime", status.regime_rejections)
                mode = snap.get("mode", None)
                if mode:
                    status.warnings.append(f"Mode: {mode} (profile: {snap.get('profile', 'prod')})")
            except Exception:
                pass

        # Only check live state if no snapshot found (bot not running)
        if not snapshot_path.exists():
            from logic.intelligence import intelligence
            from logic.live_features import feature_engine
            from datafeeds.universe import tier_scheduler
            
            # ML cache status from live state
            total_ml = len(intelligence.live_ml)
            fresh_count = 0
            stale_count = 0
            
            for symbol, ml in intelligence.live_ml.items():
                if ml.is_stale():
                    stale_count += 1
                else:
                    fresh_count += 1
            
            status.ml_fresh_count = fresh_count
            status.ml_stale_count = stale_count
            status.ml_health_pct = (fresh_count / total_ml * 100) if total_ml > 0 else 100
            
            # Tier scheduler status from live state
            tier_stats = tier_scheduler.get_stats()
            status.tier1_count = tier_stats.get("tier1_ws", 0)
            status.tier2_count = tier_stats.get("tier2_fast", 0)
            status.tier3_count = tier_stats.get("tier3_slow", 0)
            status.universe_warm = tier_stats.get("warm", 0)
            status.universe_cold = tier_stats.get("cold", 0)
            status.universe_eligible = status.tier1_count + status.tier2_count + status.tier3_count
        
        # Validation checks
        # ML check only matters if there's actually ML data
        total_ml_cached = status.ml_fresh_count + status.ml_stale_count
        if total_ml_cached > 0:
            if status.ml_health_pct < 50:
                status.add_issue(f"ML cache degraded: {status.ml_health_pct:.0f}% fresh")
            elif status.ml_health_pct < 70:
                status.add_warning(f"ML cache aging: {status.ml_health_pct:.0f}% fresh")
        
        if status.universe_eligible < 10:
            status.add_issue(f"Universe too small: {status.universe_eligible} symbols")
        elif status.universe_eligible < 20:
            status.add_warning(f"Universe limited: {status.universe_eligible} symbols")
        
        if status.universe_cold > status.universe_warm and status.universe_cold > 0:
            status.add_warning(f"More cold than warm: {status.universe_cold} cold vs {status.universe_warm} warm")
        
    except Exception as e:
        status.add_issue(f"Health check error: {e}")
    
    return status


def check_startup_health(bot_state, min_runtime_s: int = 60) -> HealthStatus:
    """
    Check health after bot has been running for min_runtime_s.
    Pass in the bot's state object.
    """
    status = HealthStatus()
    
    # WebSocket health
    if bot_state.ws_last_msg_time:
        status.ws_last_msg_age_s = bot_state.ws_last_age
        status.ws_healthy = status.ws_last_msg_age_s < 10
        
        if status.ws_last_msg_age_s > 30:
            status.add_issue(f"WS stale: {status.ws_last_msg_age_s:.0f}s since last message")
        elif status.ws_last_msg_age_s > 10:
            status.add_warning(f"WS slow: {status.ws_last_msg_age_s:.1f}s since last message")
    else:
        status.add_issue("WS never connected")
        status.ws_healthy = False
    
    # Universe health
    status.universe_warm = bot_state.warm_symbols
    status.universe_cold = bot_state.cold_symbols
    status.tier1_count = bot_state.tier1_count
    status.tier2_count = bot_state.tier2_count
    status.tier3_count = bot_state.tier3_count
    
    if status.tier1_count < 10:
        status.add_warning(f"Low Tier 1 count: {status.tier1_count}")
    
    # ML health
    from logic.intelligence import intelligence
    total = len(intelligence.live_ml)
    fresh = sum(1 for ml in intelligence.live_ml.values() if not ml.is_stale())
    status.ml_fresh_count = fresh
    status.ml_stale_count = total - fresh
    status.ml_health_pct = (fresh / total * 100) if total > 0 else 0
    
    if status.ml_health_pct < 50 and total > 5:
        status.add_issue(f"ML mostly stale: {status.ml_health_pct:.0f}%")
    
    # Candle flow
    if bot_state.candles_last_5s == 0 and min_runtime_s > 60:
        status.add_warning("No candles in last 5s")
    
    return status


if __name__ == "__main__":
    # Quick test
    status = run_health_check()
    print(status.summary())
