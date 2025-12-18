"""Live executor implementation that wraps Coinbase client calls."""

import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple

from core.logging_utils import get_logger
from core.mode_configs import LiveModeConfig
from core.models import Position, PositionState, Side, TradeResult
from core.trading_interfaces import IExecutor
from execution.order_utils import (
    OrderResult,
    calculate_limit_buy_price,
    parse_order_response,
    rate_limiter,
)
from logic.intelligence import intelligence

logger = get_logger(__name__)


class LiveExecutor(IExecutor):
    """Executes real orders on Coinbase via the Advanced Trade client."""
    
    # Products that fail with "account not available" - skip these
    _untradeable_products: set = set()

    def __init__(self, config: LiveModeConfig, portfolio=None, stop_manager=None):
        from core.live_portfolio import LivePortfolioManager
        from execution.live_stops import LiveStopManager

        self.config = config
        self.portfolio = portfolio or LivePortfolioManager(config)
        self.stop_manager = stop_manager or LiveStopManager(config, getattr(self.portfolio, "client", None))
        if hasattr(self.stop_manager, "bind_client"):
            self.stop_manager.bind_client(getattr(self.portfolio, "client", None))

    def can_execute_order(self, size_usd: float, symbol: str | None = None) -> Tuple[bool, str]:
        self.portfolio.update_portfolio_state()
        if symbol:
            info = self.portfolio.get_product_info(symbol)
            if size_usd < info.get("quote_min", 0):
                return False, f"Order below minimum ${info.get('quote_min', 0):.2f}"
        if size_usd > self.config.max_trade_usd:
            return False, f"Trade size too large: ${size_usd:.2f} > ${self.config.max_trade_usd:.2f}"
        if size_usd > self.portfolio.get_available_balance():
            return False, "Insufficient balance"
        return True, "OK"

    async def open_position(
        self,
        symbol: str,
        size_usd: float,
        price: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        max_retries: int = 3,
    ) -> Optional[Position]:
        client = getattr(self.portfolio, "client", None)
        if client is None:
            logger.error("[LIVE] No live client available")
            return None

        order_id = f"ct_{symbol}_{int(datetime.now().timestamp())}"
        qty = size_usd / price if price else 0.0
        result: OrderResult | None = None
        last_error: Exception | None = None

        # Skip products known to be untradeable on this account
        if symbol in self._untradeable_products:
            logger.warning("[LIVE] Skipping %s - marked as untradeable", symbol)
            return None
        
        for attempt in range(max_retries):
            try:
                rate_limiter.wait_if_needed()
                # Get portfolio UUID from portfolio manager
                portfolio_uuid = getattr(self.portfolio, '_portfolio_uuid', None)
                logger.info("[LIVE] Placing order for %s with portfolio_uuid=%s", symbol, portfolio_uuid)
                
                if self.config.use_limit_orders:
                    limit_price = calculate_limit_buy_price(price, buffer_pct=self.config.limit_buffer_pct)
                    base_size = size_usd / limit_price
                    
                    # Round to product precision to avoid INVALID_PRICE_PRECISION errors
                    product_info = self.portfolio.get_product_info(symbol) if self.portfolio else {}
                    price_inc = float(product_info.get('price_increment', 0.01) or 0.01)
                    base_inc = float(product_info.get('base_increment', 0.0001) or 0.0001)
                    
                    # Round down to increment
                    import math
                    price_decimals = max(0, -int(math.log10(price_inc))) if price_inc > 0 else 2
                    base_decimals = max(0, -int(math.log10(base_inc))) if base_inc > 0 else 4
                    limit_price = round(limit_price, price_decimals)
                    base_size = round(base_size, base_decimals)
                    
                    # Format to avoid scientific notation (e.g. 2.2e-05)
                    base_str = f"{base_size:.8f}".rstrip('0').rstrip('.')
                    price_str = f"{limit_price:.8f}".rstrip('0').rstrip('.')
                    logger.info("[LIVE] Limit order: %s base_size=%s limit_price=%s", symbol, base_str, price_str)
                    order = client.limit_order_gtc_buy(
                        client_order_id=order_id,
                        product_id=symbol,
                        base_size=base_str,
                        limit_price=price_str,
                        retail_portfolio_id=portfolio_uuid  # Use correct portfolio
                    )
                    logger.info("[LIVE] Order response: %s", order)
                    result = parse_order_response(order, expected_qty=qty, market_price=price)
                    logger.info("[LIVE] Parsed: success=%s error=%s fill_qty=%s", result.success, result.error, result.fill_qty)
                else:
                    order = client.market_order_buy(
                        client_order_id=order_id,
                        product_id=symbol,
                        quote_size=str(size_usd),
                        retail_portfolio_id=portfolio_uuid  # Use correct portfolio
                    )
                    logger.info("[LIVE] Order response for %s: %s", symbol, order)
                    result = parse_order_response(order, expected_quote=size_usd, market_price=price)
                    logger.info("[LIVE] Parsed result: success=%s, error=%s, fill_qty=%s", result.success, result.error, result.fill_qty)

                if result.success:
                    break
                raise Exception(result.error or "Order failed")
            except Exception as e:
                last_error = e
                # Detect untradeable products and blacklist them
                if "account is not available" in str(e).lower():
                    self._untradeable_products.add(symbol)
                    # Also persist to scanner's untradeable list
                    try:
                        from datafeeds.universe.symbol_scanner import SymbolScanner
                        SymbolScanner.add_untradeable_product(symbol)
                    except Exception:
                        pass
                    logger.warning("[LIVE] %s marked as untradeable (account not available)", symbol)
                    return None
                if attempt < max_retries - 1:
                    delay = 0.5 * (2**attempt)
                    logger.warning("[LIVE] Retry %s/%s: %s", attempt + 1, max_retries, e)
                    await asyncio.sleep(delay)

        if result is None or not result.success or result.fill_qty <= 0:
            logger.error("[LIVE] Failed after %s attempts: %s", max_retries, last_error)
            return None

        fill_qty = result.fill_qty or 0.0
        fill_price = result.fill_price or price

        # Adjust stops/targets relative to actual fill
        adjusted_stop = stop_price * (fill_price / price) if price else stop_price
        adjusted_tp1 = tp1_price * (fill_price / price) if price else tp1_price
        adjusted_tp2 = tp2_price * (fill_price / price) if price else tp2_price

        stop_id = self.stop_manager.place_stop_order(symbol, fill_qty, adjusted_stop)
        if stop_id:
            logger.info("[LIVE] Real stop-loss on exchange @ $%s", f"{adjusted_stop:.4f}")
        else:
            logger.warning("[LIVE] Stop order failed - position unprotected!")

        entry_cost = fill_price * fill_qty
        entry_conf = getattr(intelligence, "_last_entry_confidence", 0.0) or 0.0
        ml_result = intelligence.get_live_ml(symbol)
        ml_score = ml_result.raw_score if ml_result and not ml_result.is_stale() else 0.0

        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=fill_price,
            entry_time=datetime.now(timezone.utc),
            size_usd=entry_cost,
            size_qty=fill_qty,
            stop_price=adjusted_stop,
            tp1_price=adjusted_tp1,
            tp2_price=adjusted_tp2,
            time_stop_min=0,
            state=PositionState.OPEN,
            strategy_id="live",
            entry_cost_usd=entry_cost,
            entry_confidence=entry_conf,
            current_confidence=entry_conf,
            peak_confidence=entry_conf,
            ml_score_entry=ml_score,
            ml_score_current=ml_score,
        )

    async def close_position(self, position: Position, price: float, reason: str) -> TradeResult:
        client = getattr(self.portfolio, "client", None)
        if client is None:
            raise RuntimeError("Live client unavailable")

        # Cancel stop before closing
        self.stop_manager.cancel_stop_order(position.symbol)
        # Get portfolio UUID
        portfolio_uuid = getattr(self.portfolio, '_portfolio_uuid', None)
        order = client.market_order_sell(
            client_order_id=f"ct_sell_{position.symbol}_{int(datetime.now().timestamp())}",
            product_id=position.symbol,
            base_size=str(position.size_qty),
            retail_portfolio_id=portfolio_uuid,
        )
        success = getattr(order, "success", None) or (order.get("success") if isinstance(order, dict) else True)
        if not success:
            logger.error("[LIVE] Sell order failed: %s", order)

        gross_pnl = (price - position.entry_price) * position.size_qty + position.realized_pnl
        entry_fee_rate = 0.006 if self.config.use_limit_orders else 0.012
        entry_fee = position.entry_price * position.size_qty * entry_fee_rate
        exit_fee = price * position.size_qty * 0.012
        pnl = gross_pnl - (entry_fee + exit_fee)
        pnl_pct = ((price / position.entry_price) - 1) * 100 - ((entry_fee_rate + 0.012) * 100)

        return TradeResult(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=price,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc),
            size_usd=position.size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
        )
