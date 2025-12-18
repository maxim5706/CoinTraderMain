"""Base persistence with atomic writes and error recovery."""

import json
import os
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.logging_utils import get_logger
from core.models import Position, PositionState, Side
from core.trading_interfaces import IPositionPersistence

logger = get_logger(__name__)


class BasePositionPersistence(ABC, IPositionPersistence):
    """
    Base class for position persistence with:
    - Atomic writes (write to temp, then rename)
    - Automatic backup before write
    - Corruption recovery from backup
    - Proper error logging (no silent failures)
    """

    def __init__(self, path: Path):
        self.positions_file = path
        self.backup_file = path.with_suffix(".json.bak")

    def _ensure_dir(self) -> None:
        self.positions_file.parent.mkdir(parents=True, exist_ok=True)

    def _create_backup(self) -> None:
        """Create backup of current file before writing."""
        if self.positions_file.exists():
            try:
                import shutil
                shutil.copy2(self.positions_file, self.backup_file)
            except Exception as e:
                logger.warning("[PERSIST] Failed to create backup: %s", e)

    def _atomic_write(self, data: dict) -> bool:
        """
        Write data atomically: write to temp file, then rename.
        Returns True on success, False on failure.
        """
        self._ensure_dir()
        self._create_backup()

        # Write to temp file in same directory (ensures same filesystem for rename)
        temp_fd = None
        temp_path = None
        try:
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.positions_file.parent,
                prefix=".positions_",
                suffix=".tmp"
            )
            with os.fdopen(temp_fd, "w") as f:
                temp_fd = None  # fdopen takes ownership
                json.dump(data, f, indent=2)

            # Atomic rename (on POSIX systems)
            os.replace(temp_path, self.positions_file)
            temp_path = None  # Successfully moved
            return True

        except Exception as e:
            logger.error("[PERSIST] Atomic write failed: %s", e)
            return False

        finally:
            # Cleanup temp file if it still exists
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except Exception:
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    def _safe_read(self) -> Optional[dict]:
        """
        Read positions file with fallback to backup on corruption.
        Returns None only if both main and backup are unreadable.
        """
        # Try main file first
        if self.positions_file.exists():
            try:
                with open(self.positions_file, "r") as f:
                    content = f.read().strip()
                    if not content:
                        logger.warning("[PERSIST] Main file is empty")
                    else:
                        return json.loads(content)
            except json.JSONDecodeError as e:
                logger.error("[PERSIST] Main file corrupted: %s", e)
            except Exception as e:
                logger.error("[PERSIST] Failed to read main file: %s", e)

        # Fallback to backup
        if self.backup_file.exists():
            logger.info("[PERSIST] Attempting recovery from backup")
            try:
                with open(self.backup_file, "r") as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        logger.info("[PERSIST] Recovered %d positions from backup", len(data))
                        # Restore backup to main file
                        self._atomic_write(data)
                        return data
            except Exception as e:
                logger.error("[PERSIST] Backup recovery failed: %s", e)

        return None

    def _serialize_position(self, pos: Position) -> dict:
        """Serialize a Position to dict."""
        return {
            "symbol": pos.symbol,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time.isoformat(),
            "size_usd": pos.size_usd,
            "size_qty": pos.size_qty,
            "stop_price": pos.stop_price,
            "tp1_price": pos.tp1_price,
            "tp2_price": pos.tp2_price,
            "time_stop_min": pos.time_stop_min,
            "state": pos.state.value,
            "strategy_id": getattr(pos, "strategy_id", "unknown"),
            "realized_pnl": pos.realized_pnl,
            "partial_closed": pos.partial_closed,
            "entry_confidence": getattr(pos, "entry_confidence", 0.0),
            "current_confidence": getattr(pos, "current_confidence", 0.0),
            "peak_confidence": getattr(pos, "peak_confidence", 0.0),
            "ml_score_entry": getattr(pos, "ml_score_entry", 0.0),
            "ml_score_current": getattr(pos, "ml_score_current", 0.0),
        }

    def _deserialize_position(self, pos_data: dict) -> Position:
        """Deserialize dict to Position with validation."""
        entry_time = datetime.fromisoformat(pos_data["entry_time"])
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)

        return Position(
            symbol=pos_data["symbol"],
            side=Side(pos_data["side"]),
            entry_price=float(pos_data["entry_price"]),
            entry_time=entry_time,
            size_usd=float(pos_data["size_usd"]),
            size_qty=float(pos_data["size_qty"]),
            stop_price=float(pos_data["stop_price"]),
            tp1_price=float(pos_data["tp1_price"]),
            tp2_price=float(pos_data["tp2_price"]),
            time_stop_min=pos_data.get("time_stop_min", 30),
            state=PositionState(pos_data["state"]),
            strategy_id=pos_data.get("strategy_id", "unknown"),
            realized_pnl=float(pos_data.get("realized_pnl", 0.0)),
            partial_closed=bool(pos_data.get("partial_closed", False)),
            entry_confidence=float(pos_data.get("entry_confidence", 70.0)),
            current_confidence=float(pos_data.get("current_confidence", 70.0)),
            peak_confidence=float(pos_data.get("peak_confidence", 70.0)),
            ml_score_entry=float(pos_data.get("ml_score_entry", 0.0)),
            ml_score_current=float(pos_data.get("ml_score_current", 0.0)),
        )

    def save_positions(self, positions: dict[str, Position]) -> None:
        """Persist positions atomically."""
        data = {symbol: self._serialize_position(pos) for symbol, pos in positions.items()}
        if self._atomic_write(data):
            logger.debug("[PERSIST] Saved %d positions", len(positions))
        else:
            logger.error("[PERSIST] FAILED to save %d positions", len(positions))

    def load_positions(self) -> dict[str, Position]:
        """Load positions with corruption recovery."""
        self._ensure_dir()
        
        data = self._safe_read()
        if data is None:
            return {}

        positions = {}
        for symbol, pos_data in data.items():
            try:
                positions[symbol] = self._deserialize_position(pos_data)
            except Exception as e:
                logger.error("[PERSIST] Failed to deserialize %s: %s", symbol, e)
                # Continue loading other positions

        logger.debug("[PERSIST] Loaded %d positions", len(positions))
        return positions

    def clear_position(self, symbol: str) -> None:
        """Remove a single position atomically."""
        data = self._safe_read()
        if data is None:
            return

        if symbol in data:
            del data[symbol]
            if self._atomic_write(data):
                logger.debug("[PERSIST] Cleared position %s", symbol)
            else:
                logger.error("[PERSIST] Failed to clear position %s", symbol)
