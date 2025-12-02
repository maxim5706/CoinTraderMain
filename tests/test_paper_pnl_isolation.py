from datetime import datetime, timezone

from core.config import settings
from core.models import Position, PositionState, Side
from execution.order_router import OrderRouter
from run_v2 import TradingBotV2


def test_paper_ignores_live_snapshot_for_pnl():
    orig_mode = settings.trading_mode
    settings.trading_mode = "paper"

    bot = TradingBotV2()
    router = OrderRouter(get_price_func=lambda *_: 10.0, state=bot.state)
    bot.router = router

    # Inject position and bogus live snapshot
    pos = Position(
        symbol="TEST-USD",
        side=Side.BUY,
        entry_price=10.0,
        entry_time=datetime.now(timezone.utc),
        size_usd=10.0,
        size_qty=1.0,
        stop_price=9.0,
        tp1_price=11.0,
        tp2_price=12.0,
        time_stop_min=30,
        state=PositionState.OPEN,
    )
    router.positions["TEST-USD"] = pos
    router._portfolio_snapshot = type("Snap", (), {"total_unrealized_pnl": 999, "total_value": 9999, "total_cash": 9000, "total_crypto": 999})()
    router._usd_balance = 500.0

    bot._get_price = lambda symbol: 10.0
    bot._update_positions_state()

    assert bot.state.portfolio_value == router._usd_balance + pos.size_qty * 10.0
    assert bot.state.unrealized_pnl == pos.unrealized_pnl(10.0)

    settings.trading_mode = orig_mode
