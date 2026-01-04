"""Paper account state persistence for deterministic restarts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class PaperAccountState:
    balance: float
    realized_pnl: float
    start_balance: float
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "start_balance": self.start_balance,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict, start_balance: float) -> "PaperAccountState":
        return cls(
            balance=float(data.get("balance", start_balance)),
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            start_balance=float(data.get("start_balance", start_balance)),
            updated_at=data.get("updated_at"),
        )

    def save(self, path: Path) -> None:
        """Persist state atomically to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = self.to_dict()

        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".paper_state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(temp_path, path)
        except Exception as e:
            logger.warning("[PAPER] Failed to persist account state: %s", e)
            try:
                os.unlink(temp_path)
            except Exception:
                pass


def load_paper_state(path: Path, start_balance: float, reset: bool = False) -> PaperAccountState:
    """Load paper account state, optionally resetting to start balance."""
    if reset:
        state = PaperAccountState(
            balance=start_balance,
            realized_pnl=0.0,
            start_balance=start_balance,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        state.save(path)
        return state

    if not path.exists():
        state = PaperAccountState(
            balance=start_balance,
            realized_pnl=0.0,
            start_balance=start_balance,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        state.save(path)
        return state

    try:
        with open(path, "r") as f:
            data = json.load(f)
        return PaperAccountState.from_dict(data, start_balance)
    except Exception as e:
        logger.warning("[PAPER] Failed to load account state, resetting: %s", e)
        return PaperAccountState(
            balance=start_balance,
            realized_pnl=0.0,
            start_balance=start_balance,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


def should_reset_paper_state() -> bool:
    """Check for an explicit reset flag (env)."""
    value = (os.getenv("PAPER_RESET_STATE") or "").strip().lower()
    return value in {"1", "true", "yes"}
