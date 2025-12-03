"""Position persistence facade that delegates to mode-specific backends."""

from datetime import datetime, timezone
from typing import Optional

from core.logging_utils import get_logger
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.models import Position, PositionState, Side
from core.paper_persistence import PaperPositionPersistence
from core.live_persistence import LivePositionPersistence

logger = get_logger(__name__)


def _get_backend(mode: Optional[TradingMode] = None):
    resolved_mode = mode or ConfigurationManager.get_trading_mode()
    if resolved_mode == TradingMode.PAPER:
        return PaperPositionPersistence()
    return LivePositionPersistence()


def save_positions(positions: dict[str, Position], mode: Optional[TradingMode] = None):
    """Persist positions using the correct backend."""
    backend = _get_backend(mode)
    backend.save_positions(positions)


def load_positions(mode: Optional[TradingMode] = None) -> dict[str, Position]:
    """Load positions for the given trading mode."""
    backend = _get_backend(mode)
    return backend.load_positions()


def clear_position(symbol: str, mode: Optional[TradingMode] = None):
    """Remove a single position from persistence."""
    backend = _get_backend(mode)
    backend.clear_position(symbol)


def sync_with_exchange(
    client,
    positions: dict[str, Position],
    quiet: bool = True,
    mode: Optional[TradingMode] = None,
) -> dict[str, Position]:
    """
    Sync local positions with actual exchange holdings using Portfolio API.
    Only runs for live mode.
    
    Reconciliation:
    - Adds positions found on exchange but not in local storage (orphans)
    - Removes positions in local storage but not on exchange (stale)
    - Logs detailed report of discrepancies
    """
    from core.config import settings

    resolved_mode = mode or ConfigurationManager.get_trading_mode()

    if resolved_mode != TradingMode.LIVE:
        logger.info("[SYNC] Paper mode - skipping exchange sync")
        return positions

    if client is None:
        logger.warning("[SYNC] No client, skipping exchange sync")
        return positions

    try:
        portfolios = client.get_portfolios()
        portfolio_list = getattr(portfolios, "portfolios", [])

        portfolio_uuid = None
        for p in portfolio_list:
            ptype = getattr(p, "type", "") or p.get("type", "")
            if ptype == "DEFAULT":
                portfolio_uuid = getattr(p, "uuid", None) or p.get("uuid")
                break
        if not portfolio_uuid and portfolio_list:
            portfolio_uuid = getattr(portfolio_list[0], "uuid", None) or portfolio_list[0].get("uuid")

        if not portfolio_uuid:
            print("[SYNC] Could not get portfolio UUID")
            return positions

        breakdown_resp = client.get_portfolio_breakdown(portfolio_uuid)
        breakdown = getattr(breakdown_resp, "breakdown", breakdown_resp)
        spot_positions = getattr(breakdown, "spot_positions", [])

        if not quiet:
            print(f"[SYNC] Portfolio has {len(spot_positions)} spot positions")

        real_holdings = {}

        for pos in spot_positions:
            if isinstance(pos, dict):
                asset = pos.get("asset", "")
                qty = float(pos.get("total_balance_crypto", 0) or 0)
                value_usd = float(pos.get("total_balance_fiat", 0) or 0)
                entry_obj = pos.get("average_entry_price", {})
                cost_obj = pos.get("cost_basis", {})
                pnl_val = pos.get("unrealized_pnl", 0)
                is_cash = pos.get("is_cash", False)
            else:
                asset = getattr(pos, "asset", "")
                qty = float(getattr(pos, "total_balance_crypto", 0) or 0)
                value_usd = float(getattr(pos, "total_balance_fiat", 0) or 0)
                entry_obj = getattr(pos, "average_entry_price", {})
                cost_obj = getattr(pos, "cost_basis", {})
                pnl_val = getattr(pos, "unrealized_pnl", 0)
                is_cash = getattr(pos, "is_cash", False)

            if not asset:
                continue

            symbol = f"{asset}-USD"
            
            # Skip ignored/delisted symbols
            if symbol in settings.ignored_symbol_set:
                logger.debug("[SYNC] Skipping ignored symbol: %s", symbol)
                continue

            if isinstance(entry_obj, dict):
                entry_price = float(entry_obj.get("value", 0) or 0)
            else:
                entry_price = float(getattr(entry_obj, "value", 0) or 0)

            if isinstance(cost_obj, dict):
                cost_basis = float(cost_obj.get("value", 0) or 0)
            else:
                cost_basis = float(getattr(cost_obj, "value", 0) or 0)

            unrealized_pnl = float(pnl_val) if pnl_val else 0.0

            if is_cash or asset in ["USD", "USDC"]:
                continue

            position = Position(
                symbol=symbol,
                side=Side.BUY,
                entry_price=entry_price,
                entry_time=datetime.now(timezone.utc),
                size_usd=value_usd,
                size_qty=qty,
                stop_price=entry_price * (1 - settings.fixed_stop_pct),
                tp1_price=entry_price * (1 + settings.tp1_pct),
                tp2_price=entry_price * (1 + settings.tp2_pct),
                state=PositionState.OPEN,
                strategy_id="sync",
                entry_cost_usd=cost_basis,
                realized_pnl=0.0,
                partial_closed=False,
                entry_confidence=70.0,
                current_confidence=70.0,
                peak_confidence=70.0,
                ml_score_entry=0.0,
                ml_score_current=0.0,
            )

            real_holdings[symbol] = position

        # Reconciliation: find discrepancies
        local_symbols = set(positions.keys())
        exchange_symbols = set(real_holdings.keys())
        
        orphaned = exchange_symbols - local_symbols  # On exchange, not in local
        stale = local_symbols - exchange_symbols     # In local, not on exchange
        matched = local_symbols & exchange_symbols   # In both
        
        # Log detailed reconciliation report
        if orphaned or stale:
            logger.warning(
                "[SYNC] Position discrepancy detected: %d orphaned, %d stale, %d matched",
                len(orphaned), len(stale), len(matched)
            )
            
            for symbol in orphaned:
                pos = real_holdings[symbol]
                logger.warning(
                    "[SYNC] ORPHANED: %s found on exchange ($%.2f) but not tracked locally - adding",
                    symbol, pos.size_usd
                )
            
            for symbol in stale:
                pos = positions[symbol]
                logger.warning(
                    "[SYNC] STALE: %s tracked locally ($%.2f) but not on exchange - removing",
                    symbol, pos.size_usd
                )
        else:
            logger.info(
                "[SYNC] Positions in sync: %d positions match exchange",
                len(matched)
            )
        
        # Apply reconciliation
        added = 0
        for symbol, pos in real_holdings.items():
            if symbol not in positions:
                positions[symbol] = pos
                added += 1

        removed = 0
        for symbol in list(positions.keys()):
            if symbol not in real_holdings:
                del positions[symbol]
                removed += 1

        if added or removed:
            logger.info("[SYNC] Reconciled positions: +%d added, -%d removed", added, removed)
            # Persist the reconciled positions
            backend = _get_backend(resolved_mode)
            backend.save_positions(positions)

        return positions

    except Exception as e:
        logger.error("[SYNC] Reconciliation failed: %s", e, exc_info=True)
        return positions
