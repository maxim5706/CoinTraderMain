"""Paper mode position persistence."""

import json
from datetime import datetime, timezone
from pathlib import Path

from core.models import Position, PositionState, Side
from core.trading_interfaces import IPositionPersistence


class PaperPositionPersistence(IPositionPersistence):
    """Stores paper positions in a dedicated file."""

    def __init__(self, path: Path | None = None):
        self.positions_file = path or Path("data/paper_positions.json")

    def _ensure_dir(self) -> None:
        self.positions_file.parent.mkdir(parents=True, exist_ok=True)

    def save_positions(self, positions: dict[str, Position]) -> None:
        self._ensure_dir()
        data = {}
        for symbol, pos in positions.items():
            data[symbol] = {
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
                "realized_pnl": pos.realized_pnl,
                "partial_closed": pos.partial_closed,
                "entry_confidence": getattr(pos, "entry_confidence", 0.0),
                "current_confidence": getattr(pos, "current_confidence", 0.0),
                "peak_confidence": getattr(pos, "peak_confidence", 0.0),
                "ml_score_entry": getattr(pos, "ml_score_entry", 0.0),
                "ml_score_current": getattr(pos, "ml_score_current", 0.0),
            }

        with open(self.positions_file, "w") as f:
            json.dump(data, f, indent=2)

    def load_positions(self) -> dict[str, Position]:
        self._ensure_dir()
        if not self.positions_file.exists():
            return {}

        try:
            with open(self.positions_file, "r") as f:
                data = json.load(f)
        except Exception:
            return {}

        positions = {}
        for symbol, pos_data in data.items():
            entry_time = datetime.fromisoformat(pos_data["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)

            positions[symbol] = Position(
                symbol=pos_data["symbol"],
                side=Side(pos_data["side"]),
                entry_price=pos_data["entry_price"],
                entry_time=entry_time,
                size_usd=pos_data["size_usd"],
                size_qty=pos_data["size_qty"],
                stop_price=pos_data["stop_price"],
                tp1_price=pos_data["tp1_price"],
                tp2_price=pos_data["tp2_price"],
                time_stop_min=pos_data.get("time_stop_min", 30),
                state=PositionState(pos_data["state"]),
                realized_pnl=pos_data.get("realized_pnl", 0.0),
                partial_closed=pos_data.get("partial_closed", False),
                entry_confidence=pos_data.get("entry_confidence", 70.0),
                current_confidence=pos_data.get("current_confidence", 70.0),
                peak_confidence=pos_data.get("peak_confidence", 70.0),
                ml_score_entry=pos_data.get("ml_score_entry", 0.0),
                ml_score_current=pos_data.get("ml_score_current", 0.0),
            )

        return positions

    def clear_position(self, symbol: str) -> None:
        if not self.positions_file.exists():
            return

        try:
            with open(self.positions_file, "r") as f:
                data = json.load(f)
            if symbol in data:
                del data[symbol]
                with open(self.positions_file, "w") as f:
                    json.dump(data, f, indent=2)
        except Exception:
            return
