"""
Persistent Candle Storage

Writes all candles (WS + REST) to disk for:
- Restart recovery (cache rehydration)
- Future backtesting
- Historical analysis

Format: logs/candles/{symbol}/{tf}.jsonl
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict
import threading

from core.models import Candle
from core.mode_paths import get_logs_dir
from core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class StoredCandle:
    """Candle with metadata for storage."""
    ts: str              # ISO timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    tf: str              # "1m" or "5m"
    source: str          # "ws" or "rest"
    
    @classmethod
    def from_candle(cls, candle: Candle, tf: str, source: str) -> "StoredCandle":
        return cls(
            ts=candle.timestamp.isoformat(),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            tf=tf,
            source=source
        )
    
    def to_candle(self) -> Candle:
        ts = datetime.fromisoformat(self.ts.replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return Candle(
            timestamp=ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume
        )


class CandleStore:
    """
    Persistent candle storage with append-only JSONL files.
    
    Directory structure:
    logs/<mode>/candles/{symbol}/1m.jsonl
    logs/<mode>/candles/{symbol}/5m.jsonl
    """
    
    def __init__(self, base_dir: str | Path | None = None):
        # Resolve base dir lazily so TRADING_MODE overrides are respected
        if base_dir:
            resolved = Path(base_dir)
            self._base_dir_func = lambda: resolved
        else:
            self._base_dir_func = lambda: get_logs_dir() / "candles"
        self._write_lock = threading.Lock()
        self._write_buffer: Dict[str, List[str]] = {}  # symbol -> lines
        self._buffer_size = 10  # Flush every N candles
        
        # Stats
        self.candles_written = 0
        self.candles_loaded = 0

    @property
    def base_dir(self) -> Path:
        """Current base directory (mode-aware)."""
        path = self._base_dir_func()
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def _get_file_path(self, symbol: str, tf: str) -> Path:
        """Get file path for symbol/timeframe."""
        # Sanitize symbol for filesystem
        safe_symbol = symbol.replace("/", "-").replace(":", "-")
        symbol_dir = self.base_dir / safe_symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        return symbol_dir / f"{tf}.jsonl"
    
    def write_candle(self, symbol: str, candle: Candle, tf: str, source: str = "ws"):
        """Write a single candle to storage."""
        stored = StoredCandle.from_candle(candle, tf, source)
        line = json.dumps(asdict(stored))
        
        with self._write_lock:
            key = f"{symbol}_{tf}"
            if key not in self._write_buffer:
                self._write_buffer[key] = []
            self._write_buffer[key].append(line)
            
            # Flush if buffer full
            if len(self._write_buffer[key]) >= self._buffer_size:
                self._flush_buffer(symbol, tf)
    
    def write_candles(self, symbol: str, candles: List[Candle], tf: str, source: str = "rest"):
        """Write multiple candles to storage."""
        if not candles:
            return
        
        lines = []
        for candle in candles:
            stored = StoredCandle.from_candle(candle, tf, source)
            lines.append(json.dumps(asdict(stored)))
        
        file_path = self._get_file_path(symbol, tf)
        
        with self._write_lock:
            with open(file_path, 'a') as f:
                f.write('\n'.join(lines) + '\n')
            self.candles_written += len(candles)
    
    def _flush_buffer(self, symbol: str, tf: str):
        """Flush write buffer to disk."""
        key = f"{symbol}_{tf}"
        if key not in self._write_buffer or not self._write_buffer[key]:
            return
        
        file_path = self._get_file_path(symbol, tf)
        lines = self._write_buffer[key]
        
        with open(file_path, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        
        self.candles_written += len(lines)
        self._write_buffer[key] = []
    
    def flush_all(self):
        """Flush all buffers to disk."""
        with self._write_lock:
            for key in list(self._write_buffer.keys()):
                symbol, tf = key.rsplit('_', 1)
                self._flush_buffer(symbol, tf)
    
    def load_candles(
        self, 
        symbol: str, 
        tf: str, 
        max_age_hours: int = 24,
        max_count: int = 500
    ) -> List[Candle]:
        """Load candles from storage."""
        file_path = self._get_file_path(symbol, tf)
        
        if not file_path.exists():
            return []
        
        candles = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        stored = StoredCandle(**data)
                        candle = stored.to_candle()
                        
                        # Filter by age
                        if candle.timestamp >= cutoff:
                            candles.append(candle)
                    except (json.JSONDecodeError, TypeError, KeyError):
                        continue
            
            # Sort by timestamp and deduplicate
            candles.sort(key=lambda c: c.timestamp)
            candles = self._deduplicate(candles)
            
            # Limit count (keep most recent)
            if len(candles) > max_count:
                candles = candles[-max_count:]
            
            self.candles_loaded += len(candles)
            return candles
            
        except Exception as e:
            logger.warning("[STORE] Error loading %s/%s: %s", symbol, tf, e)
            return []
    
    def _deduplicate(self, candles: List[Candle]) -> List[Candle]:
        """Remove duplicate candles by timestamp."""
        seen = set()
        unique = []
        for candle in candles:
            ts_key = candle.timestamp.isoformat()
            if ts_key not in seen:
                seen.add(ts_key)
                unique.append(candle)
        return unique
    
    def rehydrate_buffers(
        self, 
        symbols: List[str],
        max_age_hours: int = 4
    ) -> Dict[str, Dict[str, List[Candle]]]:
        """
        Load candles for multiple symbols on startup.
        
        Returns: {symbol: {"1m": [candles], "5m": [candles]}}
        """
        result = {}
        
        for symbol in symbols:
            candles_1m = self.load_candles(symbol, "1m", max_age_hours)
            candles_5m = self.load_candles(symbol, "5m", max_age_hours)
            
            if candles_1m or candles_5m:
                result[symbol] = {
                    "1m": candles_1m,
                    "5m": candles_5m
                }
        
        total = sum(
            len(d["1m"]) + len(d["5m"]) 
            for d in result.values()
        )
        logger.info("[STORE] Rehydrated %d candles for %d symbols", total, len(result))
        
        return result
    
    def cleanup_old_files(self, max_age_days: int = 7):
        """Remove old candle files to save disk space."""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        removed = 0
        
        for symbol_dir in self.base_dir.iterdir():
            if not symbol_dir.is_dir():
                continue
            
            for file_path in symbol_dir.iterdir():
                if file_path.suffix != '.jsonl':
                    continue
                
                try:
                    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if mtime < cutoff:
                        file_path.unlink()
                        removed += 1
                except Exception:
                    pass
        
        if removed:
            logger.info("[STORE] Cleaned up %d old candle files", removed)
    
    def get_stats(self) -> dict:
        """Get storage statistics."""
        total_files = 0
        total_size = 0
        symbols = set()
        
        for symbol_dir in self.base_dir.iterdir():
            if not symbol_dir.is_dir():
                continue
            symbols.add(symbol_dir.name)
            
            for file_path in symbol_dir.iterdir():
                if file_path.suffix == '.jsonl':
                    total_files += 1
                    total_size += file_path.stat().st_size
        
        return {
            "symbols": len(symbols),
            "files": total_files,
            "size_mb": total_size / (1024 * 1024),
            "candles_written": self.candles_written,
            "candles_loaded": self.candles_loaded,
        }


# Singleton instance
candle_store = CandleStore()
