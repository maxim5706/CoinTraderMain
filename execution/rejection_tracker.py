"""Gate rejection tracking and statistics.

Extracted from order_router.py - tracks why signals are rejected
for dashboard display and post-analysis.
"""

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.logging_utils import get_logger
from core.helpers import GateReason, make_signal_event
from core.logger import log_rejection, utc_iso_str

if TYPE_CHECKING:
    from core.state import BotState
    from logic.intelligence import EntryScore

logger = get_logger(__name__)


class RejectionTracker:
    """Tracks gate rejections for analysis and dashboard display."""
    
    def __init__(self, state: Optional["BotState"] = None):
        self.state = state

        # De-spam recent_signals: remember the last (symbol, gate, detail) emitted.
        self._last_display_evt: dict[tuple[str, str, str], datetime] = {}
        self._display_dedupe_seconds: float = 8.0
        self._maxpos_dedupe_seconds: float = 60.0
        
        # Local counters (in case state is None)
        self._rejections = {
            "warmth": 0,
            "regime": 0,
            "score": 0,
            "rr": 0,
            "limits": 0,
            "spread": 0,
            "circuit_breaker": 0,
            "whitelist": 0,
            "truth": 0,
            "risk": 0,
        }
    
    def record(
        self,
        reason: str | GateReason,
        symbol: str = "",
        details: dict = None
    ):
        """
        Track rejection counter and log for analysis.
        
        Args:
            reason: Gate reason (string or GateReason enum)
            symbol: Symbol that was rejected
            details: Additional context
        """
        if details is None:
            details = {}
        
        # Normalize reason to enum
        reason_enum = GateReason.from_value(reason) if isinstance(reason, (GateReason, str)) else GateReason.SCORE
        reason_value = reason_enum.value
        
        # Update local counter
        if reason_value in self._rejections:
            self._rejections[reason_value] += 1
        
        # Update state counters
        if self.state:
            self._update_state_counter(reason_enum)
            self._update_state_display(symbol, reason_value, details)
        
        # Log for post-analysis
        self._log_rejection(symbol, reason_value, details)
    
    def _update_state_counter(self, reason: GateReason):
        """Update the appropriate counter on BotState."""
        if reason == GateReason.WARMTH:
            self.state.rejections_warmth += 1
        elif reason == GateReason.REGIME:
            self.state.rejections_regime += 1
        elif reason == GateReason.SCORE:
            self.state.rejections_score += 1
        elif reason == GateReason.RR:
            self.state.rejections_rr += 1
        elif reason == GateReason.LIMITS:
            self.state.rejections_limits += 1
        elif reason == GateReason.SPREAD:
            self.state.rejections_spread += 1
    
    def _update_state_display(self, symbol: str, reason: str, details: dict):
        """Update state for TUI display."""
        try:
            detail_str = details.get("reason", reason) if details else reason
            self.state.last_rejection = (symbol, reason, str(detail_str))

            # Rate-limit identical gate events to avoid confusing UI spam.
            now = datetime.now(timezone.utc)
            key = (symbol or "", reason or "", str(detail_str))
            last = self._last_display_evt.get(key)
            dedupe_seconds = self._display_dedupe_seconds
            if "Max positions" in str(detail_str):
                dedupe_seconds = self._maxpos_dedupe_seconds
            if last and (now - last).total_seconds() < dedupe_seconds:
                return
            self._last_display_evt[key] = now
            
            # Track in recent signals stream as blocked event
            evt = make_signal_event(
                datetime.now(timezone.utc),
                symbol,
                "gate",
                details.get("score", 0) if details else 0,
                details.get("spread_bps", 0.0) if details else 0.0,
                False,
                str(detail_str),
            )
            self.state.recent_signals.appendleft(evt)
        except Exception:
            pass
        
        # Log important rejections to TUI (skip noisy score rejections)
        if reason in ("spread", "rr", "limits", "regime") and symbol:
            sym_short = symbol.replace("-USD", "")
            detail = details.get("reason", reason) if details else reason
            self.state.log(f"⛔ {sym_short} {detail}", "GATE")
    
    def _log_rejection(self, symbol: str, reason: str, details: dict):
        """Log rejection to JSONL for post-analysis."""
        record = {
            "ts": utc_iso_str(),
            "symbol": symbol,
            "gate": reason,
        }
        if details:
            record.update(details)
        log_rejection(record)
    
    def get_stats(self) -> dict:
        """Get rejection statistics."""
        total = sum(self._rejections.values())
        return {
            "total": total,
            "by_gate": dict(self._rejections),
            "top_gate": max(self._rejections, key=self._rejections.get) if total > 0 else None,
        }
    
    def reset_stats(self):
        """Reset local counters."""
        for key in self._rejections:
            self._rejections[key] = 0


def categorize_score_rejection(entry_score: "EntryScore", market_regime: str) -> GateReason:
    """
    Determine whether rejection was regime-driven or score-driven.
    
    Args:
        entry_score: EntryScore from intelligence layer
        market_regime: Current market regime string
        
    Returns:
        GateReason.REGIME or GateReason.SCORE
    """
    if market_regime != "normal" and not entry_score.btc_trend_ok:
        return GateReason.REGIME
    return GateReason.SCORE
