"""WebSocket candle collector using Coinbase Advanced Trade API."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, Optional
import websockets

from core.config import settings
from core.logging_utils import get_logger
from core.models import Candle, CandleBuffer
from core.logger import log_raw, utc_iso_str

logger = get_logger(__name__)


class CandleCollector:
    """Collects real-time candle data from Coinbase WebSocket."""
    
    WS_URL = "wss://advanced-trade-ws.coinbase.com"
    
    # Reconnection settings
    RECONNECT_BASE_DELAY = 1.0   # Start with 1 second
    RECONNECT_MAX_DELAY = 60.0   # Max 60 seconds
    RECONNECT_MULTIPLIER = 2.0   # Double each time
    MAX_RECONNECT_ATTEMPTS = 10  # Give up after 10 consecutive failures
    
    def __init__(self, symbols: list[str], on_candle: Optional[Callable] = None):
        self.symbols = list(symbols)
        self.on_candle = on_candle
        self.buffers: dict[str, CandleBuffer] = {
            sym: CandleBuffer(symbol=sym) for sym in symbols
        }
        self._running = False
        self._connected = False
        self._ws = None
        self._last_trades: dict[str, list] = {sym: [] for sym in symbols}
        self._current_minute: dict[str, Optional[Candle]] = {sym: None for sym in symbols}
        self._last_message_time: Optional[datetime] = None
        
        # Reconnection state
        self._reconnect_attempts = 0
        self._reconnect_delay = self.RECONNECT_BASE_DELAY
        self._last_connect_time: Optional[datetime] = None
        self._total_reconnects = 0
        
        # Callbacks
        self.on_connect: Optional[Callable] = None  # Called when WS connects
        self.on_tick: Optional[Callable] = None     # Called on every price update
        self.on_disconnect: Optional[Callable] = None  # Called when WS disconnects
    
    def _get_fresh_jwt(self) -> str:
        """Get fresh JWT from settings (regenerated each call)."""
        return settings.get_ws_jwt()
    
    def _get_subscribe_message(self, channel: str) -> dict:
        """Build subscription message."""
        msg = {
            "type": "subscribe",
            "product_ids": self.symbols,
            "channel": channel,
        }
        
        # Add fresh JWT auth if configured (regenerated each subscribe)
        if settings.is_configured:
            jwt_token = self._get_fresh_jwt()
            if jwt_token:
                msg["jwt"] = jwt_token
        
        return msg
    
    async def _handle_ticker(self, data: dict):
        """Handle ticker updates and build candles."""
        self._last_message_time = datetime.now(timezone.utc)
        
        events = data.get("events", [])
        for event in events:
            tickers = event.get("tickers", [])
            for ticker in tickers:
                symbol = ticker.get("product_id")
                if symbol not in self.symbols:
                    continue
                
                try:
                    price = float(ticker.get("price", 0))
                    bid = ticker.get("best_bid")
                    ask = ticker.get("best_ask")
                    
                    if price <= 0:
                        continue
                    
                    # Log raw tick
                    tick_record = {
                        "ts": utc_iso_str(),
                        "type": "tick",
                        "symbol": symbol,
                        "price": price,
                        "src": "ws:ticker"
                    }
                    if bid:
                        tick_record["bid"] = float(bid)
                    if ask:
                        tick_record["ask"] = float(ask)
                    if bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        if mid > 0:
                            tick_record["spread_bps"] = round((float(ask) - float(bid)) / mid * 10000, 2)
                    log_raw(tick_record)
                    
                    # Compute spread_bps for callback
                    spread_bps = None
                    if bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        if mid > 0:
                            spread_bps = round((float(ask) - float(bid)) / mid * 10000, 2)
                    
                    # Call tick callback for real-time updates (with spread)
                    if self.on_tick:
                        self.on_tick(symbol, price, spread_bps=spread_bps)
                    
                    now = datetime.now(timezone.utc)
                    minute_key = now.replace(second=0, microsecond=0)
                    
                    current = self._current_minute.get(symbol)
                    
                    if current is None or current.timestamp != minute_key:
                        # Close previous candle
                        if current is not None:
                            self.buffers[symbol].add_1m(current)
                            if self.on_candle:
                                self.on_candle(symbol, current)
                        
                        # Start new candle
                        self._current_minute[symbol] = Candle(
                            timestamp=minute_key,
                            open=price,
                            high=price,
                            low=price,
                            close=price,
                            volume=0.0  # Estimated from trades
                        )
                    else:
                        # Update current candle
                        current.high = max(current.high, price)
                        current.low = min(current.low, price)
                        current.close = price
                        
                except (ValueError, TypeError):
                    continue
    
    async def _handle_trades(self, data: dict):
        """Handle trade updates for volume."""
        self._last_message_time = datetime.now(timezone.utc)
        
        events = data.get("events", [])
        for event in events:
            trades = event.get("trades", [])
            for trade in trades:
                symbol = trade.get("product_id")
                if symbol not in self.symbols:
                    continue
                
                try:
                    price = float(trade.get("price", 0))
                    size = float(trade.get("size", 0))
                    side = trade.get("side", "")
                    
                    # Log raw trade
                    trade_record = {
                        "ts": utc_iso_str(),
                        "type": "trade",
                        "symbol": symbol,
                        "price": price,
                        "size": size,
                        "src": "ws:market_trades"
                    }
                    if side:
                        trade_record["side"] = side
                    log_raw(trade_record)
                    
                    current = self._current_minute.get(symbol)
                    if current is not None:
                        current.volume += size
                        current.high = max(current.high, price)
                        current.low = min(current.low, price)
                        current.close = price
                except (ValueError, TypeError):
                    continue
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected
    
    @property
    def is_receiving(self) -> bool:
        """Check if actively receiving data."""
        if not self._connected:
            return False
        if self._last_message_time is None:
            return self._connected  # Just connected, no data yet
        age = (datetime.now(timezone.utc) - self._last_message_time).total_seconds()
        return age < 30  # More lenient - 30 seconds
    
    @property
    def last_message_age(self) -> float:
        """Get age of last message in seconds."""
        if self._last_message_time is None:
            return 0.0 if self._connected else 999.0
        return (datetime.now(timezone.utc) - self._last_message_time).total_seconds()
    
    async def _listen(self):
        """Main WebSocket listener loop."""
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_connect_time = datetime.now(timezone.utc)
                    
                    # Reset reconnect state on successful connection
                    if self._reconnect_attempts > 0:
                        logger.info(
                            "[WS] ✅ Reconnected after %s attempts",
                            self._reconnect_attempts,
                        )
                    self._reconnect_attempts = 0
                    self._reconnect_delay = self.RECONNECT_BASE_DELAY
                    
                    # Subscribe to heartbeats first (connection health)
                    heartbeat_sub = self._get_subscribe_message("heartbeats")
                    await ws.send(json.dumps(heartbeat_sub))
                    
                    # Subscribe to ticker channel
                    ticker_sub = self._get_subscribe_message("ticker")
                    await ws.send(json.dumps(ticker_sub))
                    
                    # Subscribe to market trades for volume
                    trades_sub = self._get_subscribe_message("market_trades")
                    await ws.send(json.dumps(trades_sub))
                    
                    logger.info(
                        "[WS] Connected, subscribed to %s symbols (ticker + trades + heartbeats)",
                        len(self.symbols),
                    )
                    
                    # Notify connection callback
                    if self.on_connect:
                        self.on_connect()
                    
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            
                            # Handle error messages from Coinbase
                            msg_type = data.get("type")
                            if msg_type == "error":
                                logger.error("[WS] ERROR: %s", data.get("message", data))
                                continue
                            
                            channel = data.get("channel")
                            
                            if channel == "ticker":
                                await self._handle_ticker(data)
                            elif channel == "market_trades":
                                await self._handle_trades(data)
                            elif channel == "heartbeats":
                                # Heartbeat keeps connection alive and confirms health
                                self._last_message_time = datetime.now(timezone.utc)
                                log_raw({
                                    "ts": utc_iso_str(),
                                    "type": "heartbeat",
                                    "src": "ws:heartbeats"
                                })
                                
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.exception("[WS] Error processing message: %s", e)
                            
            except websockets.ConnectionClosed as e:
                self._connected = False
                self._reconnect_attempts += 1
                self._total_reconnects += 1
                
                if self.on_disconnect:
                    self.on_disconnect()
                
                if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "[WS] ❌ Max reconnect attempts (%s) reached, giving up",
                        self.MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                
                logger.warning(
                    "[WS] Connection closed (code: %s), reconnecting in %.1fs... (attempt %s)",
                    e.code,
                    self._reconnect_delay,
                    self._reconnect_attempts,
                )
                await asyncio.sleep(self._reconnect_delay)
                
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * self.RECONNECT_MULTIPLIER,
                    self.RECONNECT_MAX_DELAY
                )
                
            except Exception as e:
                self._connected = False
                self._reconnect_attempts += 1
                self._total_reconnects += 1
                
                if self.on_disconnect:
                    self.on_disconnect()
                
                if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "[WS] ❌ Max reconnect attempts (%s) reached, giving up",
                        self.MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                
                logger.warning(
                    "[WS] Error: %s, reconnecting in %.1fs... (attempt %s)",
                    e,
                    self._reconnect_delay,
                    self._reconnect_attempts,
                    exc_info=True,
                )
                await asyncio.sleep(self._reconnect_delay)
                
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * self.RECONNECT_MULTIPLIER,
                    self.RECONNECT_MAX_DELAY
                )
    
    async def start(self):
        """Start the collector."""
        self._running = True
        await self._listen()
    
    def stop(self):
        """Stop the collector."""
        self._running = False
        if self._ws:
            asyncio.create_task(self._ws.close())
    
    async def update_symbols(self, symbols: list[str]):
        """
        Update the set of streamed symbols. Triggers a reconnect with new subscriptions.
        Safe to call while running.
        """
        # Deduplicate and keep order
        seen = set()
        new_symbols = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                new_symbols.append(s)
        
        if set(new_symbols) == set(self.symbols):
            return  # No change
        
        self.symbols = new_symbols
        
        # Ensure buffers/state for any newly added symbols
        for sym in new_symbols:
            if sym not in self.buffers:
                self.buffers[sym] = CandleBuffer(symbol=sym)
                self._last_trades[sym] = []
                self._current_minute[sym] = None
        
        # Trigger reconnect to resubscribe
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("WebSocket close failed during symbol update", exc_info=True)
    
    def get_buffer(self, symbol: str) -> Optional[CandleBuffer]:
        """Get candle buffer for a symbol."""
        return self.buffers.get(symbol)
    
    def get_last_price(self, symbol: str) -> float:
        """Get last price for a symbol (checks current forming candle first)."""
        # First check current forming candle (most recent data)
        current = self._current_minute.get(symbol)
        if current and current.close > 0:
            return current.close
        # Fall back to completed candles
        buffer = self.buffers.get(symbol)
        if buffer and buffer.last_price > 0:
            return buffer.last_price
        return 0.0
    
    def has_any_data(self) -> bool:
        """Check if we have any price data at all."""
        for symbol in self.symbols:
            if self.get_last_price(symbol) > 0:
                return True
        return False


class MockCollector:
    """Mock collector for paper trading without API connection."""
    
    def __init__(self, symbols: list[str], on_candle: Optional[Callable] = None):
        self.symbols = list(symbols)
        self.on_candle = on_candle
        self.buffers: dict[str, CandleBuffer] = {
            sym: CandleBuffer(symbol=sym) for sym in symbols
        }
        self._running = False
        self._prices: dict[str, float] = {
            "BTC-USD": 95000.0,
            "ETH-USD": 3500.0,
            "SOL-USD": 240.0,
            "AVAX-USD": 45.0,
            "LINK-USD": 18.0,
            "DOGE-USD": 0.42,
        }
    
    async def start(self):
        """Generate mock candles for testing."""
        import random
        self._running = True
        
        logger.info("[MOCK] Starting mock data generator")
        
        while self._running:
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            
            for symbol in self.symbols:
                base_price = self._prices.get(symbol, 100.0)
                
                # Random walk with occasional bursts
                change_pct = random.gauss(0, 0.002)  # Normal moves
                if random.random() < 0.05:  # 5% chance of burst
                    change_pct = random.choice([-1, 1]) * random.uniform(0.01, 0.03)
                
                new_price = base_price * (1 + change_pct)
                self._prices[symbol] = new_price
                
                high = new_price * (1 + abs(random.gauss(0, 0.001)))
                low = new_price * (1 - abs(random.gauss(0, 0.001)))
                
                candle = Candle(
                    timestamp=now,
                    open=base_price,
                    high=max(base_price, new_price, high),
                    low=min(base_price, new_price, low),
                    close=new_price,
                    volume=random.uniform(100, 10000) * (3 if abs(change_pct) > 0.01 else 1)
                )
                
                self.buffers[symbol].add_1m(candle)
                if self.on_candle:
                    self.on_candle(symbol, candle)
            
            await asyncio.sleep(60)  # 1 minute candles
    
    def stop(self):
        self._running = False
    
    async def update_symbols(self, symbols: list[str]):
        """Update mock stream set (adds new symbols with synthetic prices)."""
        seen = set()
        new_symbols = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                new_symbols.append(s)
        if set(new_symbols) == set(self.symbols):
            return
        self.symbols = new_symbols
        for sym in new_symbols:
            if sym not in self.buffers:
                self.buffers[sym] = CandleBuffer(symbol=sym)
            if sym not in self._prices:
                self._prices[sym] = 100.0
        # No reconnect needed for mock; next loop will generate data
    
    def get_buffer(self, symbol: str) -> Optional[CandleBuffer]:
        return self.buffers.get(symbol)
    
    def get_last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)
