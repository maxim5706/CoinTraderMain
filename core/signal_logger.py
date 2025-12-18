"""
Signal Logger - Log all signals to JSONL for ML training.

Logs every signal generated with:
- Strategy details
- Features at signal time
- Outcome (win/loss/pending)
- Timing metrics

This creates training data for future ML models.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import asdict


class SignalLogger:
    """
    Logs all signals to JSONL for ML training and validation.
    
    Ensures we can:
    1. Prove signals are real (not fake)
    2. Train ML models on historical signals
    3. Backtest strategy performance
    4. Debug why signals did/didn't work
    """
    
    def __init__(self, mode: str = "live"):
        self.mode = mode
        self.log_dir = Path(f"logs/{mode}/signals")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Current day's log file
        self._current_file = None
        self._signals_logged = 0
    
    def log_signal(
        self,
        signal,
        features: dict,
        taken: bool,
        rejection_reason: str = None
    ):
        """
        Log a signal to JSONL.
        
        Args:
            signal: StrategySignal object
            features: Dict of features at signal time
            taken: Was the signal acted on?
            rejection_reason: If not taken, why?
        """
        try:
            # Get log file for today
            log_file = self._get_log_file()
            
            # Build record
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": signal.symbol,
                "strategy_id": signal.strategy_id,
                "direction": signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction),
                "score": signal.edge_score_base,
                "trend_score": signal.trend_score,
                "reasons": signal.reasons if hasattr(signal, 'reasons') else [],
                "taken": taken,
                "rejection_reason": rejection_reason,
                
                # Features at signal time (critical for ML)
                "features": {
                    "price": features.get('price', 0),
                    "trend_1h": features.get('trend_1h', 0),
                    "trend_15m": features.get('trend_15m', 0),
                    "trend_5m": features.get('trend_5m', 0),
                    "vol_spike_5m": features.get('vol_spike_5m', 1.0),
                    "vwap_distance": features.get('vwap_distance', 0),
                    "spread_bps": features.get('spread_bps', 0),
                },
                
                # Signal metadata
                "confluence_count": getattr(signal, 'confluence_count', 1),
                "is_valid": bool(signal.is_valid),  # Convert to native bool for JSON
                
                # For tracking outcomes later
                "signal_id": f"{signal.symbol}_{int(datetime.now(timezone.utc).timestamp())}",
            }
            
            # Write to JSONL (one line per signal)
            with open(log_file, 'a') as f:
                safe_record = self._sanitize_for_json(record)
                f.write(json.dumps(safe_record) + '\n')
            
            self._signals_logged += 1
            
            # Log milestone
            if self._signals_logged % 100 == 0:
                logger.info("[SIGNAL_LOG] %d signals logged to %s", self._signals_logged, log_file.name)
        
        except Exception as e:
            logger.warning("[SIGNAL_LOG] Error logging signal: %s", e)
    
    def log_outcome(
        self,
        signal_id: str,
        outcome: str,  # "win", "loss", "breakeven"
        pnl_usd: float,
        pnl_pct: float,
        hold_time_min: int,
        exit_reason: str
    ):
        """
        Log outcome of a signal (after trade closes).
        
        This completes the training data loop.
        """
        try:
            outcome_file = self.log_dir / f"outcomes_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
            
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signal_id": signal_id,
                "outcome": outcome,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "hold_time_min": hold_time_min,
                "exit_reason": exit_reason,
            }
            
            with open(outcome_file, 'a') as f:
                f.write(json.dumps(record) + '\n')
        
        except Exception as e:
            logger.warning("[SIGNAL_LOG] Error logging outcome: %s", e)

    def _sanitize_for_json(self, obj):
        """Convert numpy/datetime/iterables to JSON-safe primitives."""
        try:
            import numpy as np  # type: ignore
        except Exception:  # pragma: no cover - numpy may not be installed in tests
            np = None

        # Fast-path for primitives
        if obj is None or isinstance(obj, (str, int, float, bool)):
            if isinstance(obj, float) and not math.isfinite(obj):
                return 0.0
            return obj

        if np and isinstance(obj, np.generic):
            return self._sanitize_for_json(obj.item())

        if isinstance(obj, datetime):
            return obj.isoformat()

        if isinstance(obj, dict):
            return {str(k): self._sanitize_for_json(v) for k, v in obj.items()}

        if isinstance(obj, (list, tuple, set)):
            return [self._sanitize_for_json(v) for v in obj]

        # Fallback: string representation to avoid hard failure
        return str(obj)
    
    def _get_log_file(self) -> Path:
        """Get log file for current day."""
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        log_file = self.log_dir / f"signals_{date_str}.jsonl"
        
        # Update current file reference
        if self._current_file != log_file:
            self._current_file = log_file
            logger.info("[SIGNAL_LOG] Logging signals to: %s", log_file)
        
        return log_file
    
    def get_stats(self) -> dict:
        """Get logging stats."""
        return {
            "signals_logged_today": self._signals_logged,
            "log_file": str(self._current_file) if self._current_file else "none",
            "log_dir": str(self.log_dir),
        }
    
    def validate_signals(self, min_signals: int = 10) -> bool:
        """
        Validate that we're logging real signals (not empty/fake).
        
        Returns True if we have enough real signals logged.
        """
        if not self._current_file or not self._current_file.exists():
            return False
        
        try:
            with open(self._current_file, 'r') as f:
                lines = f.readlines()
            
            if len(lines) < min_signals:
                return False
            
            # Validate signal structure
            for line in lines[-min_signals:]:
                record = json.loads(line)
                
                # Check required fields
                required = ['symbol', 'strategy_id', 'score', 'features', 'taken']
                if not all(k in record for k in required):
                    return False
                
                # Check features are real (not all zeros)
                features = record['features']
                if all(v == 0 for v in features.values()):
                    return False  # Fake features
            
            return True
        
        except Exception as e:
            logger.warning("[SIGNAL_LOG] Validation error: %s", e)
            return False


# Global logger instance
signal_logger = SignalLogger()
