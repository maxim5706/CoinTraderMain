"""Paper execution helper to mirror live execution shape without hitting APIs."""

from datetime import datetime, timezone
from core.models import Position, PositionState, Side


class PaperExecution:
    """Simulates fills/stops/TPs for paper mode."""

    def open_buy(
        self,
        symbol: str,
        price: float,
        size_usd: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        time_stop_min: int,
        strategy_id: str = "",
    ) -> Position:
        qty = size_usd / price if price > 0 else 0.0
        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=price,
            entry_time=datetime.now(timezone.utc),
            size_usd=size_usd,
            size_qty=qty,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            time_stop_min=time_stop_min,
            state=PositionState.OPEN,
            strategy_id=strategy_id,
        )
