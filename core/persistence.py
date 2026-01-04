"""Position persistence facade that delegates to mode-specific backends."""

from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from core.logging_utils import get_logger
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.models import Position, PositionState, Side
from core.asset_class import get_risk_profile
from core.paper_persistence import PaperPositionPersistence
from core.live_persistence import LivePositionPersistence

logger = get_logger(__name__)


def _get_backend(mode: Optional[TradingMode] = None):
    resolved_mode = mode or ConfigurationManager.get_trading_mode()
    return _get_backend_cached(resolved_mode)


@lru_cache(maxsize=2)
def _get_backend_cached(resolved_mode: TradingMode):
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
            logger.warning("[SYNC] Could not get portfolio UUID")
            return positions

        breakdown_resp = client.get_portfolio_breakdown(portfolio_uuid)
        breakdown = getattr(breakdown_resp, "breakdown", breakdown_resp)
        spot_positions = getattr(breakdown, "spot_positions", [])

        if not quiet:
            logger.info("[SYNC] Portfolio has %d spot positions", len(spot_positions))

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

            # Skip dust positions that are below trading minimums
            from core.helpers import is_dust
            if is_dust(value_usd):
                logger.info(
                    "[SYNC] Skipping dust position %s ($%.4f < $%.2f)",
                    symbol,
                    value_usd,
                    settings.position_min_usd,
                )
                continue

            # SELF-HEALING: Handle invalid or missing entry prices
            current_price_per_unit = value_usd / qty if qty > 0 else 0
            
            if entry_price <= 0:
                # Invalid entry price from exchange - use current price as fallback
                logger.warning(
                    "[SYNC] %s has invalid entry_price=0, using current price $%.4f",
                    symbol, current_price_per_unit
                )
                entry_price = current_price_per_unit
            
            # Derive a reasonable time stop for synced holdings so they can't hang forever.
            try:
                risk_profile = get_risk_profile(symbol)
                time_stop_min = int(risk_profile.max_hold_hours * 60)
            except Exception:
                time_stop_min = int(getattr(settings, "max_hold_minutes", 120) or 120)

            # SELF-HEALING: Detect underwater synced positions
            # If current price is significantly below entry, the stop would trigger immediately
            # Instead, set a REALISTIC stop based on current price to give position room to recover
            pnl_pct = ((current_price_per_unit / entry_price) - 1) * 100 if entry_price > 0 else 0
            original_stop = entry_price * (1 - settings.fixed_stop_pct)
            
            if pnl_pct < -settings.fixed_stop_pct * 100:
                # Position is ALREADY underwater beyond normal stop level
                # Use current price for stop calculation to avoid immediate exit
                effective_stop = current_price_per_unit * (1 - settings.fixed_stop_pct)
                logger.warning(
                    "[SYNC] %s is %.1f%% underwater (entry=$%.4f, now=$%.4f). "
                    "Setting realistic stop at $%.4f instead of $%.4f",
                    symbol, pnl_pct, entry_price, current_price_per_unit,
                    effective_stop, original_stop
                )
                stop_price = effective_stop
                # Also adjust TPs to be realistic from current price
                tp1_price = current_price_per_unit * (1 + settings.tp1_pct)
                tp2_price = current_price_per_unit * (1 + settings.tp2_pct)
                strategy_id = "sync_underwater"  # Mark for special handling
            else:
                stop_price = original_stop
                tp1_price = entry_price * (1 + settings.tp1_pct)
                tp2_price = entry_price * (1 + settings.tp2_pct)
                strategy_id = "sync"

            position = Position(
                symbol=symbol,
                side=Side.BUY,
                entry_price=entry_price,
                entry_time=datetime.now(timezone.utc),
                size_usd=value_usd,
                size_qty=qty,
                stop_price=stop_price,
                tp1_price=tp1_price,
                tp2_price=tp2_price,
                time_stop_min=time_stop_min,
                state=PositionState.OPEN,
                strategy_id=strategy_id,
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
        updated = 0
        for symbol, exchange_pos in real_holdings.items():
            if symbol not in positions:
                # New position from exchange - add it
                positions[symbol] = exchange_pos
                added += 1
            else:
                # Matched position - PRESERVE local metadata, update exchange data
                local_pos = positions[symbol]
                
                # Preserve these from local (they don't exist on exchange)
                exchange_pos.strategy_id = local_pos.strategy_id or exchange_pos.strategy_id
                exchange_pos.tier = getattr(local_pos, "tier", "normal")
                exchange_pos.entry_score = getattr(local_pos, "entry_score", 0.0)
                exchange_pos.flags = getattr(local_pos, "flags", "")
                exchange_pos.source_strategy = getattr(local_pos, "source_strategy", "") or local_pos.strategy_id
                exchange_pos.entry_confidence = local_pos.entry_confidence
                exchange_pos.current_confidence = local_pos.current_confidence
                exchange_pos.peak_confidence = local_pos.peak_confidence
                exchange_pos.ml_score_entry = local_pos.ml_score_entry
                exchange_pos.ml_score_current = local_pos.ml_score_current
                exchange_pos.stack_count = getattr(local_pos, "stack_count", 0)
                exchange_pos.stop_order_id = getattr(local_pos, "stop_order_id", None)
                exchange_pos.entry_order_id = getattr(local_pos, "entry_order_id", None)
                exchange_pos.last_modified = getattr(local_pos, "last_modified", None)
                exchange_pos.last_stop_update = getattr(local_pos, "last_stop_update", None)
                exchange_pos.entry_time = local_pos.entry_time  # Preserve original entry time
                
                # Keep local stop/TP if they were already set (don't overwrite trailing stops)
                if local_pos.stop_price > 0:
                    exchange_pos.stop_price = local_pos.stop_price
                if local_pos.tp1_price > 0:
                    exchange_pos.tp1_price = local_pos.tp1_price
                if local_pos.tp2_price > 0:
                    exchange_pos.tp2_price = local_pos.tp2_price
                
                positions[symbol] = exchange_pos
                updated += 1

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
