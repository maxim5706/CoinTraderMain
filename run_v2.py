#!/usr/bin/env python3
"""
Main entry point for CoinTrader bot - V2 with enhanced dashboard.

Three-clock architecture:
- Clock A: Real-time WebSocket stream (always on)
- Clock B: Rolling intraday context (every minute)
- Clock C: Background slow context (every 30 min)

Usage:
  python run_v2.py                    # Use env TRADING_MODE
  python run_v2.py --paper           # Force paper mode
  python run_v2.py --live            # Force live mode
  python run_v2.py --mode=paper      # Explicit mode setting
"""

import asyncio
import argparse
import json
import os
import signal as sig
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.logging_utils import get_logger, setup_logging
from core.config import settings
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.profiles import apply_profile
from core.models import Signal, SignalType, CandleBuffer
from core.logger import log_candle_1m, log_burst, log_signal, utc_iso_str
from core.trading_container import TradingContainer
from core.events import MarketEventBus, TickEvent, CandleEvent, OrderEvent

setup_logging()
logger = get_logger(__name__)

# Apply profile overrides BEFORE anything else uses settings
apply_profile(settings.profile, settings)
from core.state import (
    BotState, BurstCandidate, FocusCoinState, 
    CurrentSignal, PositionDisplay
)

from datafeeds.collectors import CandleCollector, DynamicBackfill, MockCollector, RestPoller
from datafeeds.coinbase_fetcher import fetch_history_windowed
from datafeeds.universe import SymbolScanner, tier_scheduler
from services.candle_store import candle_store
from logic.strategies.orchestrator import StrategyOrchestrator
from execution.order_router import OrderRouter
from apps.dashboard.dashboard_v2 import DashboardV2
from tools.preflight import test_api_keys


class TradingBotV2:
    """Main trading bot orchestrator with three-clock architecture."""
    
    def __init__(self):
        self.dashboard = DashboardV2()
        self.mode = ConfigurationManager.get_trading_mode()
        self.config = ConfigurationManager.get_config_for_mode(self.mode)
        self.orchestrator = StrategyOrchestrator()
        self.events = MarketEventBus(self.mode)
        self.scanner = SymbolScanner()
        self.collector: Optional[CandleCollector | MockCollector] = None
        self.router: Optional[OrderRouter] = None
        self._last_5m_counts: dict[str, int] = {}
        self._last_strategy_signals: dict[str, any] = {}  # Raw StrategySignal per symbol
        
        # Track latest spread per symbol for FAST mode
        self._latest_spreads: dict[str, float] = {}
        self._running = False
        self.stream_limit = 75  # Max symbols to stream at once (was 50, Coinbase allows more)
        self._last_hot_leader: Optional[str] = None
        self._last_rest_probe: Optional[datetime] = None
        self._focus_rotation_secs = 15
        self._focus_rotation_pool = 3
        self._focus_index = 0
        self._last_focus_switch: Optional[datetime] = None
        self._focus_symbol: Optional[str] = None
        self._last_history_probe: Optional[datetime] = None
        self._strategy_pool = 15  # Analyze top 15 symbols each loop to catch bursts
        
        # Task handles
        self._clock_a_task: Optional[asyncio.Task] = None  # WebSocket
        self._clock_b_task: Optional[asyncio.Task] = None  # Every minute
        self._clock_c_task: Optional[asyncio.Task] = None  # Every 30 min
        self._display_task: Optional[asyncio.Task] = None
        self._poller_task: Optional[asyncio.Task] = None   # REST poller
        self._backfill_task: Optional[asyncio.Task] = None # Dynamic backfill
        
        # Tiered polling system
        self.rest_poller: Optional[RestPoller] = None
        self.backfill_service: Optional[DynamicBackfill] = None
        
        # Shared state for dashboard
        self.state = self.dashboard.state
        self.state.mode = self.mode.value
        self.state.profile = getattr(settings, "profile", "prod")
        self.state.daily_loss_limit_usd = settings.daily_max_loss_usd
        self.state.startup_time = datetime.now(timezone.utc)
        self.state.paper_balance = getattr(settings, "paper_start_balance", 1000.0)
        self.events.on_order(self._on_order_event)
    
    def _get_price(self, symbol: str) -> float:
        """Price getter for order router."""
        if self.collector:
            return self.collector.get_last_price(symbol)
        return 0.0

    def _on_order_event(self, event: OrderEvent) -> None:
        """Capture order lifecycle events for dashboard recent strip."""
        try:
            self.state.recent_orders.appendleft(event)
        except Exception:
            pass
    
    async def start(self):
        """Start the bot with three-clock architecture."""
        
        # === PREFLIGHT CHECK ===
        api_ok, api_msg = test_api_keys()
        self.state.api_ok = api_ok
        self.state.api_msg = api_msg
        
        self.dashboard.print_startup(api_ok, api_msg)
        
        if not api_ok and self.mode == TradingMode.LIVE:
            logger.error(
                "Cannot run in LIVE mode without valid API keys. "
                "Fix .env or switch to TRADING_MODE=paper."
            )
            return
        
        if not api_ok and self.mode == TradingMode.PAPER:
            logger.warning("Running in paper mode with mock data (no API keys)")
        
        self._running = True
        
        # === CLOCK C: Initial universe discovery ===
        logger.info("[CLOCK C] Refreshing symbol universe...")
        await self.scanner.refresh_universe()
        self._update_universe_state()
        
        # Get symbols to stream (eligible symbols or defaults)
        stream_symbols = self.scanner.get_eligible_symbols()
        if not stream_symbols:
            stream_symbols = settings.coins
        
        # Limit initial stream to top symbols to avoid overload
        stream_symbols = stream_symbols[: self.stream_limit]
        logger.info("[CLOCK A] Streaming %s symbols (initial)", len(stream_symbols))
        self.state.universe.symbols_streaming = len(stream_symbols)
        
        # Initialize collector
        if settings.is_configured and api_ok:
            logger.info("[BOT] Using live WebSocket connection")
            self.collector = CandleCollector(
                symbols=stream_symbols,
                on_candle=self._on_candle
            )
            self.collector.on_tick = self._on_tick
            self.collector.on_connect = self._on_ws_connect
        else:
            logger.info("[BOT] Using mock data generator")
            self.collector = MockCollector(
                symbols=stream_symbols,
                on_candle=self._on_candle
            )
            # Mock is always "connected"
            self.state.ws_ok = True
        
        # Backfill recent history to skip warmup lag
        self._backfill_initial(stream_symbols)
        
        # Rehydrate candles from persistent storage
        self._rehydrate_from_store(stream_symbols)
    
        # Initialize order router with mode-specific dependencies
        container = TradingContainer(self.mode, self.config)
        self.router = OrderRouter(
            get_price_func=self._get_price,
            state=self.state,
            mode=self.mode,
            config=self.config,
            executor=container.get_executor(),
            portfolio=container.get_portfolio_manager(),
            persistence=container.get_persistence(),
            stop_manager=container.get_stop_manager(),
            event_bus=self.events,
        )
        
        # Connect candle collector for thesis invalidation checks
        if self.collector:
            self.router.set_candle_collector(self.collector)
        
        # Check if we need to auto-rebalance (over budget)
        await self._check_and_rebalance()
        
        # Initialize tiered polling system
        self._init_tiered_system(stream_symbols)
        
        # Print startup summary
        self._print_startup_summary(stream_symbols)
        
        # Start all clocks
        self._clock_a_task = asyncio.create_task(self.collector.start())  # WebSocket
        self._clock_b_task = asyncio.create_task(self._clock_b_loop())    # Every minute
        self._clock_c_task = asyncio.create_task(self._clock_c_loop())    # Every 30 min
        self._display_task = asyncio.create_task(self._display_loop())
        
        # Start tiered polling services
        if self.rest_poller:
            self._poller_task = asyncio.create_task(
                self.rest_poller.start(tier_scheduler)
            )
        if self.backfill_service:
            self._backfill_task = asyncio.create_task(
                self.backfill_service.start()
            )
        
        try:
            await asyncio.gather(
                self._clock_a_task,
                self._clock_b_task,
                self._clock_c_task,
                self._display_task
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop the bot gracefully - positions are KEPT open."""
        if not self._running:
            return
        
        self._running = False
        logger.info("[BOT] Shutting down...")
        
        # IMPORTANT: Do NOT auto-sell positions on shutdown!
        # Positions stay open on the exchange
        if self.router and self.router.positions:
            logger.info(
                "[BOT] Keeping %s positions open on exchange",
                len(self.router.positions)
            )
            # Save positions for next restart
            from core.persistence import save_positions
            save_positions(self.router.positions)
        
        if self.collector:
            self.collector.stop()
        
        # Stop tiered polling services
        if self.rest_poller:
            await self.rest_poller.stop()
        if self.backfill_service:
            await self.backfill_service.stop()
        
        # Flush candle store to disk
        candle_store.flush_all()
        logger.info(
            "[STORE] Flushed %s candles to disk",
            candle_store.candles_written
        )
        
        self.dashboard.print_shutdown()
    
    async def _check_and_rebalance(self):
        """Check if over budget and offer to rebalance at startup."""
        if self.mode == TradingMode.PAPER or not self.router:
            return
        
        # Wait for portfolio snapshot
        await asyncio.sleep(2)
        
        portfolio_value = self.router._portfolio_value
        if self.router._portfolio_snapshot:
            portfolio_value = self.router._portfolio_snapshot.total_value
        
        if portfolio_value <= 0:
            return
        
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        # Use cost_basis (original entry cost) not current value for budget!
        current_exposure = sum(p.cost_basis for p in self.router.positions.values())
        available = bot_budget - current_exposure
        
        if available >= 0:
            logger.info(
                "[BUDGET] OK: $%s/$%s used, $%s available",
                f"{current_exposure:.0f}",
                f"{bot_budget:.0f}",
                f"{available:.0f}",
            )
            return
        
        over_by = abs(available)
        logger.warning(
            "[BUDGET] OVER-ALLOCATED by $%s | Exposure: $%s | Budget: $%s | Positions: %s",
            f"{over_by:.0f}",
            f"{current_exposure:.0f}",
            f"{bot_budget:.0f}",
            list(self.router.positions.keys()),
        )
        
        # DISABLED: Auto-rebalance was selling at losses aggressively
        # Instead, just block new trades until positions close naturally
        # To manually rebalance, sell positions in Coinbase UI
        logger.info("[BUDGET] Trading paused - will resume when positions close")
        logger.info("[BUDGET] Tip: Manually close losing positions in Coinbase if needed")
    
    def _print_startup_summary(self, stream_symbols: list):
        """Print clean startup summary after all initialization."""
        # Gather stats
        stats = {
            "eligible": len(self.scanner.get_eligible_symbols()),
            "ws_count": len(stream_symbols),
            "rest_count": len(self.scanner.get_eligible_symbols()) - len(stream_symbols),
            "candles_1m": sum(len(self.collector.get_buffer(s).candles_1m) for s in stream_symbols if self.collector and self.collector.get_buffer(s)),
            "candles_5m": sum(len(self.collector.get_buffer(s).candles_5m) for s in stream_symbols if self.collector and self.collector.get_buffer(s)),
            "candles_1h": sum(len(getattr(self.collector.get_buffer(s), 'candles_1h', [])) for s in stream_symbols if self.collector and self.collector.get_buffer(s)),
            "candles_1d": sum(len(getattr(self.collector.get_buffer(s), 'candles_1d', [])) for s in stream_symbols if self.collector and self.collector.get_buffer(s)),
            "portfolio": self.router._portfolio_value if self.router else 0,
            "positions": len(self.router.positions) if self.router else 0,
            "available": 0,
        }
        
        # Calculate available budget (use cost_basis, not current value!)
        if self.router and self.router._portfolio_snapshot:
            portfolio = self.router._portfolio_snapshot.total_value
            exposure = sum(p.cost_basis for p in self.router.positions.values())
            stats["portfolio"] = portfolio
            stats["available"] = portfolio * settings.portfolio_max_exposure_pct - exposure
        
        self.dashboard.print_startup_complete(stats)
    
    def _on_candle(self, symbol: str, candle):
        """Callback when new candle forms (Clock A)."""
        self.state.ws_ok = True
        self.state.ws_last_msg_time = datetime.now(timezone.utc)
        self.state.last_candle_time = candle.timestamp
        self.state.candles_last_5s += 1
        self.state.heartbeat_candles_1m = datetime.now(timezone.utc)
        
        # Log completed 1m candle to file
        buffer = self.collector.get_buffer(symbol) if self.collector else None
        candle_record = {
            "ts": utc_iso_str(candle.timestamp),
            "type": "candle_1m",
            "symbol": symbol,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume
        }
        if buffer and len(buffer.candles_1m) >= 30:
            candle_record["vwap_30"] = round(buffer.vwap(30), 6)
        if buffer and len(buffer.candles_5m) >= 20:
            candle_record["ema20_5m"] = round(buffer.ema(20, "5m"), 6)
        if buffer and len(buffer.candles_1m) >= 14:
            candle_record["atr14_1m"] = round(buffer.atr(14, "1m"), 6)
        log_candle_1m(candle_record, candle.timestamp)
        
        # Persist to candle store (WS source)
        candle_store.write_candle(symbol, candle, "1m", source="ws")

        # Emit normalized event for downstream consumers
        if self.events:
            self.events.emit_candle(
                CandleEvent(symbol=symbol, candle=candle, tf="1m", source="ws")
            )
        
        # Incremental update of live indicators (O(1) per candle)
        from features.live import feature_engine
        from logic.intelligence import intelligence
        
        vwap = buffer.vwap(30) if buffer and len(buffer.candles_1m) >= 30 else 0.0
        spread_bps = self._latest_spreads.get(symbol, 0.0)
        
        indicators = feature_engine.update(symbol, candle, spread_bps, vwap)
        intelligence.update_live_indicators(symbol, indicators)
        if indicators:
            self.state.heartbeat_features = datetime.now(timezone.utc)
            ml = intelligence.get_live_ml(symbol)
            if ml and not ml.is_stale():
                self.state.heartbeat_ml = datetime.now(timezone.utc)
        
        # 5m heartbeat when aggregation advances
        if buffer:
            prev_count = self._last_5m_counts.get(symbol, 0)
            curr_count = len(buffer.candles_5m)
            if curr_count > prev_count:
                self.state.heartbeat_candles_5m = datetime.now(timezone.utc)
            self._last_5m_counts[symbol] = curr_count
        
        # Only log to live dashboard for focus symbol to avoid spam
        if symbol == self.scanner.get_focus_symbol():
            self.state.log(f"Candle {symbol} close={candle.close:.4f}", "DATA")
    
    def _on_tick(self, symbol: str, price: float, spread_bps: float = None):
        """Callback on every price update (Clock A - real-time)."""
        self.state.ws_ok = True
        self.state.ws_last_msg_time = datetime.now(timezone.utc)
        self.state.ws_last_age = 0.0  # Just got data
        self.state.ticks_last_5s += 1
        self.state.heartbeat_ws = datetime.now(timezone.utc)
        
        # Store latest spread for FAST mode decisions
        if spread_bps is not None:
            self._latest_spreads[symbol] = spread_bps

        # Emit normalized tick event
        if self.events:
            self.events.emit_tick(
                TickEvent(symbol=symbol, price=price, spread_bps=spread_bps, source="ws")
            )
    
    def _on_ws_connect(self):
        """Callback when WebSocket connects."""
        # Suppressed: print("[WS] Dashboard notified of connection")
        self.state.ws_ok = True
        self.state.ws_last_age = 0.0
        self.state.log("WebSocket connected ✅", "WS")
    
    def _update_universe_state(self):
        """Update dashboard state with universe info."""
        u = self.state.universe
        prev_stream = u.symbols_streaming
        prev_eligible = u.eligible_symbols
        u.total_symbols = len(self.scanner.universe)
        u.eligible_symbols = len(self.scanner.get_eligible_symbols())
        u.spicy_smallcaps = len(self.scanner.get_spicy_smallcaps())
        u.large_caps = len(self.scanner.get_tier_symbols("large"))
        u.mid_caps = len(self.scanner.get_tier_symbols("mid"))
        u.small_caps = len(self.scanner.get_tier_symbols("small"))
        u.micro_caps = len(self.scanner.get_tier_symbols("micro"))
        u.last_universe_refresh = self.scanner._last_universe_refresh
        u.symbols_streaming = len(self.collector.symbols) if self.collector else 0
        self.state.symbols_streaming = u.symbols_streaming
        self.state.symbols_eligible = u.eligible_symbols
        self.state.warm_symbols = self.state.warm_symbols  # keep existing count
        if (
            u.symbols_streaming != prev_stream
            or u.eligible_symbols != prev_eligible
        ):
            self.state.log(
                f"Universe refreshed: eligible={u.eligible_symbols}, stream={u.symbols_streaming}",
                "UNIV",
            )
    
    async def _manage_streams(self):
        """Adjust streamed symbols based on hot list and open positions."""
        if not self.collector:
            return
        
        desired: list[str] = []
        
        # Always stream open positions
        if self.router and self.router.positions:
            desired.extend(list(self.router.positions.keys()))
        
        # Add current hot list leaders
        desired.extend([m.symbol for m in self.scanner.hot_list.symbols[: self.stream_limit]])
        
        # Fill the rest with eligible symbols to keep discovery running
        for sym in self.scanner.get_eligible_symbols():
            if sym not in desired:
                desired.append(sym)
            if len(desired) >= self.stream_limit:
                break
        
        desired = desired[: self.stream_limit]
        
        if set(desired) != set(self.collector.symbols):
            await self.collector.update_symbols(desired)
            self.state.universe.symbols_streaming = len(desired)
            self.state.log(f"Stream set → {len(desired)} symbols", "UNIV")
            logger.info("[STREAM] Streaming %s symbols", len(desired))
    
    def _log_hot_leader_change(self):
        """Log when the #1 hot symbol changes."""
        top = self.scanner.get_focus_symbol()
        if top and top != self._last_hot_leader:
            self._last_hot_leader = top
            metrics = next((m for m in self.scanner.hot_list.symbols if m.symbol == top), None)
            msg = f"Hot #1 {top}"
            if metrics:
                msg += f" score={metrics.burst_score:.2f} vol={metrics.vol_spike:.1f}x rng={metrics.range_spike:.1f}x"
            self.state.log(msg, "FOCUS")
            logger.info("[HOT] %s", msg)
    
    def _rest_probe(self, limit: int = 5):
        """
        Periodically fetch spread snapshots for non-streamed symbols using REST
        to improve eligibility and rotation decisions.
        """
        if not self.scanner._init_client():
            return
        
        now = datetime.now()
        if self._last_rest_probe and (now - self._last_rest_probe) < timedelta(seconds=60):
            return
        
        exclude = set(self.collector.symbols) if self.collector else set()
        candidates = [
            s for s in self.scanner.get_eligible_symbols()
            if s not in exclude
        ][:limit]
        
        if not candidates:
            return
        
        try:
            self.scanner.refresh_spread_snapshots(candidates)
            self._last_rest_probe = now
            self.state.log(f"REST probe {len(candidates)} symbols", "UNIV")
        except Exception as e:
            logger.warning("[REST] Probe error: %s", e, exc_info=True)
    
    def _probe_unstreamed_history(self, limit: int = 3, lookback_minutes: int = 30):
        """
        Pull lightweight history for a few non-streamed symbols to surface
        off-stream bursts into the hot list.
        """
        if not self.collector or not self.scanner._init_client():
            return
        
        now = datetime.now(timezone.utc)
        if self._last_history_probe and (now - self._last_history_probe).total_seconds() < 20:
            return
        
        exclude = set(self.collector.symbols)
        candidates = [
            s for s in self.scanner.get_eligible_symbols()
            if s not in exclude
        ][:limit]
        
        if not candidates:
            return
        
        for sym in candidates:
            history = self.scanner.fetch_history(sym, granularity_s=60, lookback_minutes=lookback_minutes)
            if len(history) < 10:
                continue
            buf = CandleBuffer(symbol=sym)
            for c in history:
                buf.add_1m(c)
            self.scanner.update_burst_metrics(
                symbol=sym,
                candles_1m=buf.candles_1m,
                candles_5m=buf.candles_5m,
                vwap=buf.vwap(30),
                atr_24h=0.0
            )
        self._last_history_probe = now
    
    def _backfill_initial(self, symbols: list[str], minutes_1m: int = 60, minutes_5m: int = 120):
        """Pull recent history via REST and seed candle buffers to avoid warmup lag."""
        if not self.collector:
            return
        
        # Only attempt if REST client is available
        if not self.scanner._init_client():
            logger.warning("[BACKFILL] No REST client; skipping")
            return
        
        total_1m = 0
        total_5m = 0
        total_1h = 0
        total_1d = 0
        success_count = 0
        
        # Process symbols with progress tracking
        num_symbols = len(symbols)
        for idx, sym in enumerate(symbols):
            buffer = self.collector.get_buffer(sym)
            if buffer is None:
                continue
            
            # Fetch 1m candles (most important for live trading)
            history_1m = self.scanner.fetch_history(sym, granularity_s=60, lookback_minutes=minutes_1m)
            for candle in history_1m:
                buffer.add_1m(candle)
                total_1m += 1
            
            # Fetch 5m candles directly (faster than aggregating from 1m)
            history_5m = self.scanner.fetch_history(sym, granularity_s=300, lookback_minutes=minutes_5m)
            for candle in history_5m:
                buffer.add_5m_direct(candle)
                total_5m += 1
            
            # Fetch 1H candles (48 hours = 2880 minutes)
            history_1h = self.scanner.fetch_history(sym, granularity_s=3600, lookback_minutes=48*60)
            if history_1h:
                buffer.candles_1h = history_1h[-48:]  # Keep last 48 hours
                total_1h += len(history_1h)
                # Store to disk
                candle_store.write_candles(sym, history_1h, "1h", source="backfill")
            
            # Fetch 1D candles (30 days = 43200 minutes)
            history_1d = self.scanner.fetch_history(sym, granularity_s=86400, lookback_minutes=30*24*60)
            if history_1d:
                buffer.candles_1d = history_1d[-30:]  # Keep last 30 days
                total_1d += len(history_1d)
                # Store to disk
                candle_store.write_candles(sym, history_1d, "1d", source="backfill")
            
            # Update feature engine with higher TF data
            if history_1h or history_1d:
                from features.live import feature_engine
                feature_engine.update_higher_tf(sym, history_1h or [], history_1d or [])
            
            if history_1m or history_5m:
                success_count += 1
            
            # Progress indicator every 10 symbols
            if (idx + 1) % 10 == 0:
                print(f"\r[BACKFILL] Progress: {idx + 1}/{num_symbols} symbols...", end="", flush=True)
        
        # Clear progress line
        if num_symbols >= 10:
            print()
        
        if total_1m > 0 or total_5m > 0:
            logger.info(
                "[BACKFILL] Loaded %s 1m + %s 5m + %s 1h + %s 1d candles for %s/%s symbols",
                total_1m,
                total_5m,
                total_1h,
                total_1d,
                success_count,
                len(symbols),
            )
            self.state.log(f"Backfill: {total_1m} 1m, {total_5m} 5m, {total_1h} 1h, {total_1d} 1d candles", "DATA")
            
            # Mark symbols as warm in tier scheduler
            for sym in symbols:
                buffer = self.collector.get_buffer(sym) if self.collector else None
                if buffer:
                    tier_scheduler.update_candle_counts(
                        sym,
                        len(buffer.candles_1m),
                        len(buffer.candles_5m)
                    )
    
    def _rehydrate_from_store(self, symbols: list[str]):
        """Rehydrate candle buffers from persistent storage on startup."""
        try:
            stored = candle_store.rehydrate_buffers(symbols, max_age_hours=4)
            
            for sym, data in stored.items():
                buffer = self.collector.get_buffer(sym) if self.collector else None
                if buffer is None:
                    continue
                
                # Add stored candles to buffer
                for candle in data.get("1m", []):
                    buffer.add_1m(candle)
                for candle in data.get("5m", []):
                    buffer.add_5m_direct(candle)
                
                # Update tier scheduler
                tier_scheduler.update_candle_counts(
                    sym,
                    len(buffer.candles_1m),
                    len(buffer.candles_5m)
                )
        except Exception as e:
            logger.warning("[STORE] Rehydrate error: %s", e, exc_info=True)
    
    def _init_tiered_system(self, ws_symbols: list[str]):
        """Initialize the tiered polling system."""
        # Set up tier scheduler
        all_eligible = self.scanner.get_eligible_symbols()
        
        # Assign initial tiers
        # WS symbols are Tier 1, rest distributed to Tier 2/3
        ranked = ws_symbols + [s for s in all_eligible if s not in ws_symbols]
        tier_scheduler.reassign_tiers(ranked)
        
        # Set up callbacks for tier changes
        tier_scheduler.on_ws_add = self._on_symbol_promoted_to_ws
        tier_scheduler.on_ws_remove = self._on_symbol_demoted_from_ws
        
        # Initialize REST poller
        if self.scanner._init_client():
            self.rest_poller = RestPoller(
                fetch_candles_func=fetch_history_windowed,
                fetch_spread_func=None  # Optional
            )
            self.rest_poller.on_candles = self._on_rest_candles
        
        # Initialize backfill service
        if self.scanner._init_client():
            self.backfill_service = DynamicBackfill(
                fetch_candles_func=fetch_history_windowed,
                min_candles_1m=20,
                min_candles_5m=10
            )
            self.backfill_service.on_candles = self._on_backfill_candles
            self.backfill_service.on_warmup_complete = self._on_symbol_warmed
        
        logger.info(
            "[TIER] Initialized: %s WS, %s REST",
            len(ws_symbols),
            len(all_eligible) - len(ws_symbols),
        )
    
    def _on_symbol_promoted_to_ws(self, symbol: str):
        """Called when symbol is promoted to Tier 1 (WebSocket)."""
        logger.info("[TIER] %s promoted to WS - queuing backfill", symbol)
        if self.backfill_service:
            self.backfill_service.queue_backfill(symbol)
    
    def _on_symbol_demoted_from_ws(self, symbol: str):
        """Called when symbol is demoted from Tier 1."""
        logger.info("[TIER] %s demoted from WS", symbol)
    
    def _on_symbol_warmed(self, symbol: str):
        """Called when symbol backfill completes."""
        self.state.log(f"{symbol} warmed up", "TIER")
    
    def _on_rest_candles(self, symbol: str, candles_1m: list, candles_5m: list):
        """Called when REST poller fetches new candles."""
        # Store to disk
        if candles_1m:
            candle_store.write_candles(symbol, candles_1m, "1m", source="rest")
        if candles_5m:
            candle_store.write_candles(symbol, candles_5m, "5m", source="rest")
        
        # Update burst metrics if we have a buffer
        if candles_1m and len(candles_1m) >= 10:
            buf = CandleBuffer(symbol=symbol)
            for c in candles_1m:
                buf.add_1m(c)
            for c in candles_5m:
                buf.add_5m_direct(c)
            
            self.scanner.update_burst_metrics(
                symbol=symbol,
                candles_1m=buf.candles_1m,
                candles_5m=buf.candles_5m,
                vwap=buf.vwap(30),
                atr_24h=0.0
            )
    
    def _on_backfill_candles(self, symbol: str, candles_1m: list, candles_5m: list, 
                              candles_1h: list = None, candles_1d: list = None):
        """Called when backfill service fetches candles for a symbol."""
        buffer = self.collector.get_buffer(symbol) if self.collector else None
        
        if buffer:
            for candle in candles_1m:
                buffer.add_1m(candle)
            for candle in candles_5m:
                buffer.add_5m_direct(candle)
            # Store higher timeframe candles in buffer
            if candles_1h:
                buffer.candles_1h = candles_1h[-48:]  # Keep last 48 hours
            if candles_1d:
                buffer.candles_1d = candles_1d[-30:]  # Keep last 30 days
        
        # Store to disk
        if candles_1m:
            candle_store.write_candles(symbol, candles_1m, "1m", source="backfill")
        if candles_5m:
            candle_store.write_candles(symbol, candles_5m, "5m", source="backfill")
        if candles_1h:
            candle_store.write_candles(symbol, candles_1h, "1h", source="backfill")
        if candles_1d:
            candle_store.write_candles(symbol, candles_1d, "1d", source="backfill")
        
        # Update feature engine with higher TF data
        if candles_1h or candles_1d:
            from features.live import feature_engine
            feature_engine.update_higher_tf(symbol, candles_1h or [], candles_1d or [])
        
        # Update tier scheduler
        tier_scheduler.update_candle_counts(
            symbol,
            len(candles_1m),
            len(candles_5m)
        )
    
    def _select_focus_symbol(self) -> Optional[str]:
        """Rotate focus across top hot-list symbols when not in a trade."""
        # Always prioritize an open position if one exists
        if self.router and self.router.positions:
            for sym in self.router.positions.keys():
                return sym
        
        hot_symbols = [m.symbol for m in self.scanner.hot_list.symbols[: self._focus_rotation_pool]]
        # Fallback to whatever we have on the radar (live-price placeholders)
        if not hot_symbols and self.state.burst_leaderboard:
            hot_symbols = [self.state.burst_leaderboard[0].symbol]
        if not hot_symbols:
            return None
        
        now = datetime.now(timezone.utc)
        
        # If current focus fell out of the pool, reset to top
        if self._focus_symbol not in hot_symbols:
            self._focus_symbol = hot_symbols[0]
            self._focus_index = 0
            self._last_focus_switch = now
            return self._focus_symbol
        
        # Rotate on timer
        if self._last_focus_switch is None:
            self._last_focus_switch = now
        elif (now - self._last_focus_switch).total_seconds() >= self._focus_rotation_secs:
            self._focus_index = (self._focus_index + 1) % len(hot_symbols)
            self._focus_symbol = hot_symbols[self._focus_index]
            self._last_focus_switch = now
            self.state.log(f"Focus → {self._focus_symbol}", "FOCUS")
        
        return self._focus_symbol
    
    async def _clock_c_loop(self):
        """Clock C: Background slow context (every 30 min)."""
        while self._running:
            try:
                await asyncio.sleep(30 * 60)  # 30 minutes
                
                if not self._running:
                    break
                
                from core.logger import log_universe, utc_iso_str
                
                logger.info("[CLOCK C] Refreshing universe...")
                await self.scanner.refresh_universe()
                self._update_universe_state()
                
                # Log ranked universe
                ranked = self.scanner.get_ranked_universe(top_k=50)
                if ranked:
                    log_universe({
                        "ts": utc_iso_str(),
                        "count": len(ranked),
                        "symbols": ranked[:20]  # Log top 20 in detail
                    })
                
                self.state.log(
                    f"Universe: {self.state.universe.eligible_symbols} eligible, "
                    f"{self.state.universe.small_caps} smallcaps",
                    "UNIV"
                )
                
            except Exception as e:
                logger.exception("[CLOCK C] Error: %s", e)
                await asyncio.sleep(60)
    
    async def _clock_b_loop(self):
        """Clock B: Rolling intraday context (every 5 seconds)."""
        from logic.intelligence import intelligence
        
        # Wait for initial data
        await asyncio.sleep(3)
        
        # Track BTC regime update
        last_btc_check = datetime.now(timezone.utc) - timedelta(minutes=5)
        
        while self._running:
            try:
                # Reset population counters for this cycle
                self.state.ticks_last_5s = 0
                self.state.candles_last_5s = 0
                self.state.events_last_5s = 0
                
                # Update BTC regime every 2 minutes
                if (datetime.now(timezone.utc) - last_btc_check).total_seconds() >= 120:
                    intelligence.fetch_btc_trend()
                    last_btc_check = datetime.now(timezone.utc)
                    self.state.log(f"Market: {intelligence.regime_status}", "INTEL")
                
                # Always show current prices, even without full data
                self._update_live_prices()
                
                # Adjust streaming set based on hot list + positions
                await self._manage_streams()
                
                # Update burst metrics for all streaming symbols
                for symbol in self.collector.symbols:
                    buffer = self.collector.get_buffer(symbol)
                    if buffer is None:
                        continue
                    
                    # Need at least some candles for metrics (lowered threshold)
                    if len(buffer.candles_1m) < 3:
                        continue
                    
                    # Get universe info for daily baseline
                    info = self.scanner.universe.get(symbol)
                    atr_24h = info.atr_24h if info else 0
                    
                    # Update scanner burst metrics
                    self.scanner.update_burst_metrics(
                        symbol=symbol,
                        candles_1m=buffer.candles_1m,
                        candles_5m=buffer.candles_5m,
                        vwap=buffer.vwap(30),
                        atr_24h=atr_24h
                    )
                self.state.heartbeat_scanner = datetime.now(timezone.utc)
                
                # ML freshness counts (cached only, no recompute)
                from logic.intelligence import intelligence
                total_ml = len(intelligence.live_ml)
                fresh_ml = sum(1 for ml in intelligence.live_ml.values() if not ml.is_stale())
                self.state.ml_total_count = total_ml
                self.state.ml_fresh_count = fresh_ml
                
                # Probe a few non-streamed symbols with REST history to surface bursts
                self._probe_unstreamed_history(limit=3, lookback_minutes=30)
                
                # Compute hot list (side-effect updates scanner.hot_list)
                self.scanner.compute_hot_list(top_n=10)
                self._log_hot_leader_change()
                
                # REST probe a few non-streamed symbols to improve spreads/eligibility
                self._rest_probe(limit=5)
                
                # Log burst metrics for hot list (Layer C)
                now_utc = datetime.now(timezone.utc)
                for rank, metrics in enumerate(self.scanner.hot_list.symbols[:10], 1):
                    info = self.scanner.universe.get(metrics.symbol)
                    burst_record = {
                        "ts": utc_iso_str(now_utc),
                        "type": "burst_metrics",
                        "symbol": metrics.symbol,
                        "price": metrics.price,
                        "vol_spike": metrics.vol_spike,
                        "range_spike": metrics.range_spike,
                        "trend_15m": metrics.trend_15m,
                        "trend_slope": metrics.trend_slope,
                        "burst_score": metrics.burst_score,
                        "vwap_distance": metrics.vwap_distance,
                        "daily_move": metrics.daily_move,
                        "rank": rank
                    }
                    if info:
                        burst_record["tier"] = info.tier
                    log_burst(burst_record, now_utc)
                
                # Update dashboard burst leaderboard
                self._update_burst_leaderboard()
                
                # Run strategy analysis on hot symbols
                await self._run_strategy_analysis()
                
                # Refresh real portfolio from Coinbase (every 30 seconds) - skip in PAPER
                if not hasattr(self, '_last_portfolio_refresh'):
                    self._last_portfolio_refresh = datetime.now(timezone.utc)
                    self._last_pnl_log = datetime.now(timezone.utc)
                if not hasattr(self, '_last_status_write'):
                    self._last_status_write = datetime.now(timezone.utc)
                if (datetime.now(timezone.utc) - self._last_portfolio_refresh).total_seconds() >= 15:
                    if self.mode == TradingMode.LIVE and settings.is_configured:
                        from core.portfolio import portfolio_tracker
                        from core.logger import log_pnl_snapshot, utc_iso_str as pnl_utc_iso_str
                        from core.persistence import sync_with_exchange
                        
                        try:
                            snap = portfolio_tracker.get_snapshot()
                            self.router._portfolio_snapshot = snap
                            self.router._last_snapshot_at = datetime.now(timezone.utc)
                            self.router._sync_degraded = False
                            self._last_portfolio_refresh = datetime.now(timezone.utc)
                            
                            # Sync positions with exchange (detect manual trades)
                            if self.router._client:
                                old_count = len(self.router.positions)
                                self.router.positions = sync_with_exchange(
                                    self.router._client, 
                                    self.router.positions
                                )
                                new_count = len(self.router.positions)
                                if new_count != old_count:
                                    logger.info(
                                        "[SYNC] Positions changed: %s → %s",
                                        old_count,
                                        new_count,
                                    )
                                
                                # Also update _exchange_holdings from synced positions
                                # This ensures we don't buy something we already hold
                                self.router._exchange_holdings = {
                                    p.symbol: p.size_usd 
                                    for p in self.router.positions.values()
                                    if p.size_usd >= 1.0
                                }
                            
                            # Log PnL snapshot every 5 minutes
                            if (datetime.now(timezone.utc) - self._last_pnl_log).total_seconds() >= 300:
                                if snap:
                                    log_pnl_snapshot({
                                        "ts": pnl_utc_iso_str(),
                                        "equity": snap.total_value,
                                        "cash": snap.total_cash,
                                        "crypto": snap.total_crypto,
                                        "unrealized_pnl": snap.total_unrealized_pnl,
                                        "realized_pnl": self.router.daily_stats.total_pnl,
                                        "position_count": snap.position_count,
                                    })
                                self._last_pnl_log = datetime.now(timezone.utc)
                        except Exception:
                            logger.warning("Portfolio refresh failed", exc_info=True)
                    else:
                        self._last_portfolio_refresh = datetime.now(timezone.utc)
                
                # Periodic status snapshot for health_check when bot not attached
                if (datetime.now(timezone.utc) - self._last_status_write).total_seconds() >= 30:
                    self._write_status_snapshot()
                    self._write_health_log()
                    self._last_status_write = datetime.now(timezone.utc)
                
                # Update positions state
                self._update_positions_state()
                
                # Update WS status from collector (use is_receiving for data flow, not just socket open)
                if hasattr(self.collector, 'is_receiving'):
                    self.state.ws_ok = self.collector.is_receiving
                    self.state.ws_last_age = self.collector.last_message_age
                elif hasattr(self.collector, 'is_connected'):
                    self.state.ws_ok = self.collector.is_connected
                
                # Update streaming count
                self.state.universe.symbols_streaming = len(self.collector.symbols)
                
                await asyncio.sleep(2)  # Faster loop for more responsive signals
                
            except Exception as e:
                logger.exception("[CLOCK B] Error: %s", e)
                await asyncio.sleep(5)
    
    def _update_live_prices(self):
        """Show live prices immediately, even before burst data is ready."""
        # Check if scanner has computed burst scores yet
        has_burst_data = any(
            m.burst_score > 0 for m in self.scanner.hot_list.symbols
        ) if self.scanner.hot_list.symbols else False
        
        # If we have real burst data, use that instead
        if has_burst_data:
            return
        
        # Otherwise show raw prices
        candidates = []
        for symbol in self.collector.symbols:
            price = self.collector.get_last_price(symbol)
            if price > 0:
                info = self.scanner.universe.get(symbol)
                candidates.append(BurstCandidate(
                    symbol=symbol,
                    price=price,
                    burst_score=0,  # No data yet
                    vol_spike=0,
                    range_spike=0,
                    trend_5m=0,
                    vwap_dist=0,
                    tier=info.tier if info else "unknown",
                    rank=len(candidates) + 1
                ))
        
        if candidates:
            # Sort by price for now (just to show something)
            candidates.sort(key=lambda x: x.price, reverse=True)
            for i, c in enumerate(candidates):
                c.rank = i + 1
            
            self.state.burst_leaderboard = candidates[:10]
    
    def _update_burst_leaderboard(self):
        """Update dashboard burst leaderboard from scanner."""
        from logic.intelligence import intelligence
        
        candidates = []
        
        for m in self.scanner.hot_list.symbols:
            info = self.scanner.universe.get(m.symbol)
            
            # Get entry score - quick estimate from burst metrics
            entry_score = 40  # Base score
            try:
                # Price bonus (low price = more volatile)
                if m.price < 0.10:
                    entry_score += 15
                elif m.price < 1.0:
                    entry_score += 10
                elif m.price < 10.0:
                    entry_score += 5
                
                # Trend bonus
                if m.trend_15m > 1.0:
                    entry_score += 15
                elif m.trend_15m > 0.5:
                    entry_score += 10
                elif m.trend_15m > 0:
                    entry_score += 5
                
                # Volume spike bonus
                if m.vol_spike > 10:
                    entry_score += 15
                elif m.vol_spike > 5:
                    entry_score += 10
                elif m.vol_spike > 2:
                    entry_score += 5
                
                # Tier bonus
                tier_name = info.tier if info else "unknown"
                if tier_name == "micro":
                    entry_score += 15
                elif tier_name == "small":
                    entry_score += 10
            except Exception as e:
                logger.warning("[RADAR] Failed to score burst candidate %s: %s", m.symbol, e, exc_info=True)
            
            candidates.append(BurstCandidate(
                symbol=m.symbol,
                price=m.price,
                burst_score=m.burst_score,
                vol_spike=m.vol_spike,
                range_spike=m.range_spike,
                trend_5m=m.trend_15m,
                trend_slope=m.trend_slope,
                vwap_dist=m.vwap_distance,
                daily_move=m.daily_move,
                tier=info.tier if info else "unknown",
                rank=m.rank,
                entry_score=entry_score
            ))
        
        # If we have no scored candidates yet, keep whatever live-price
        # candidates were populated by _update_live_prices so the radar
        # is not blank during warmup.
        if candidates:
            self.state.burst_leaderboard = candidates

    def _write_status_snapshot(self):
        """Write lightweight BotState snapshot for external health checks."""
        try:
            from core.mode_paths import get_status_path

            status_path = get_status_path(self.mode)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "ts": utc_iso_str(),
                "ws_ok": self.state.ws_ok,
                "ws_last_age": getattr(self.state, "ws_last_age", 0.0),
                "ws_reconnect_count": self.state.ws_reconnect_count,
                "mode": self.state.mode,
                "profile": getattr(settings, "profile", "prod"),
                "universe": {
                    "eligible": self.state.universe.eligible_symbols,
                    "warm": self.state.warm_symbols,
                    "cold": self.state.cold_symbols,
                    "tier1": self.state.tier1_count,
                    "tier2": self.state.tier2_count,
                    "tier3": self.state.tier3_count,
                },
                "ml": {
                    "fresh_pct": self.state.ml_fresh_pct,
                    "total_cached": self.state.ml_total_cached,
                },
                "rejections": {
                    "spread": self.state.rejections_spread,
                    "warmth": self.state.rejections_warmth,
                    "regime": self.state.rejections_regime,
                    "score": self.state.rejections_score,
                    "rr": self.state.rejections_rr,
                    "limits": self.state.rejections_limits,
                },
                "positions": len(self.router.positions) if self.router else 0,
            }
            status_path.write_text(json.dumps(snapshot, indent=2))
        except Exception:
            logger.warning("Failed to write status snapshot", exc_info=True)
    
    def _write_health_log(self):
        """Write periodic health snapshot to JSONL for post-analysis."""
        try:
            from core.logger import log_health, utc_iso_str
            from logic.intelligence import intelligence
            
            total_ml = len(intelligence.live_ml)
            fresh_ml = sum(1 for ml in intelligence.live_ml.values() if not ml.is_stale())
            
            log_health({
                "ts": utc_iso_str(),
                "universe_eligible": self.state.universe.eligible_symbols,
                "warm_symbols": self.state.warm_symbols,
                "cold_symbols": self.state.cold_symbols,
                "ml_fresh_pct": (fresh_ml / total_ml * 100) if total_ml > 0 else 0,
                "ml_total": total_ml,
                "ws_ok": self.state.ws_ok,
                "ws_age": self.state.ws_last_age,
                "btc_regime": self.state.btc_regime,
                "rejections": {
                    "warmth": self.state.rejections_warmth,
                    "regime": self.state.rejections_regime,
                    "score": self.state.rejections_score,
                    "rr": self.state.rejections_rr,
                    "spread": self.state.rejections_spread,
                    "limits": self.state.rejections_limits,
                },
                "positions": len(self.router.positions) if self.router else 0,
                "daily_pnl": self.state.daily_pnl,
            })
        except Exception:
            logger.warning("Failed to write health snapshot", exc_info=True)
    
    async def _run_strategy_analysis(self):
        """Run strategy analysis on focus coin and manage trades."""
        # Check kill switch
        if self.router.daily_stats.should_stop:
            self.state.kill_switch = True
            return
        
        # Get focus symbol (rotating among top candidates when no position)
        prev_focus = self.state.focus_coin.symbol
        focus_symbol = self._select_focus_symbol()
        market_context = self._build_market_context()
        
        # Build a small pool to analyze (focus + top N)
        candidates: list[str] = []
        if focus_symbol:
            candidates.append(focus_symbol)
        candidates.extend([m.symbol for m in self.scanner.hot_list.symbols[: self._focus_rotation_pool]])
        candidates.extend([m.symbol for m in self.scanner.hot_list.symbols[: self._strategy_pool]])
        # Preserve order, dedupe
        seen = set()
        ordered = []
        for sym in candidates:
            if sym not in seen:
                seen.add(sym)
                ordered.append(sym)
        candidates = ordered
        
        if not candidates:
            return
        
        # Always update focus coin state (even without signal)
        if focus_symbol:
            focus_buffer = self.collector.get_buffer(focus_symbol)
            if focus_buffer:
                self._update_focus_coin_basic(focus_symbol, focus_buffer)
        
        # Analyze each candidate; focus updates only for focus symbol
        for symbol in candidates:
            buffer = self.collector.get_buffer(symbol)
            if buffer is None:
                continue
            
            features = self._build_features(symbol, buffer)
            strat_signal = self.orchestrator.analyze(symbol, buffer, features, market_context)
            if strat_signal is None:
                self._last_strategy_signals.pop(symbol, None)
                # Track as "no signal" - strategy didn't find entry opportunity
                self.state.rejections_score += 1
                continue
            
            signal = self._adapt_strategy_signal(symbol, strat_signal, features, market_context, buffer)
            if signal is None:
                self._last_strategy_signals.pop(symbol, None)
                self.state.rejections_score += 1
                continue
            
            self._last_strategy_signals[symbol] = strat_signal
            
            # Log signal to file (Layer D)
            signal_record = {
                "ts": utc_iso_str(signal.timestamp),
                "type": "signal",
                "symbol": signal.symbol,
                "strategy_id": signal.strategy_id,
                "signal_type": signal.type.value,
                "price": signal.price,
                "confidence": signal.confidence,
                "reason": signal.reason
            }
            if signal.stop_price:
                signal_record["stop_price"] = signal.stop_price
            if signal.tp1_price:
                signal_record["tp1_price"] = signal.tp1_price
            if signal.tp2_price:
                signal_record["tp2_price"] = signal.tp2_price
            if signal.impulse:
                signal_record["impulse"] = {
                    "start_time": utc_iso_str(signal.impulse.start_time),
                    "end_time": utc_iso_str(signal.impulse.end_time),
                    "low": signal.impulse.low,
                    "high": signal.impulse.high,
                    "pct_move": signal.impulse.pct_move,
                    "green_candles": signal.impulse.green_candles
                }
            if signal.flag:
                signal_record["flag"] = {
                    "start_time": utc_iso_str(signal.flag.start_time),
                    "high": signal.flag.high,
                    "low": signal.flag.low,
                    "retrace_pct": signal.flag.retrace_pct,
                    "duration_minutes": signal.flag.duration_minutes
                }
            log_signal(signal_record, signal.timestamp)
            
            # Only update dashboard focus state for the focus symbol
            if symbol == focus_symbol:
                old_stage = self.state.focus_coin.stage
                self._update_focus_coin(symbol, buffer)
                self._update_signal_state(signal)
                if focus_symbol != prev_focus and prev_focus:
                    self.state.log(f"Focus → {focus_symbol}", "FOCUS")
                if self.state.focus_coin.stage != old_stage:
                    self.state.log(f"{focus_symbol}: {old_stage} → {self.state.focus_coin.stage}", "STRAT")
            
            # Check for entry (both normal and FAST breakouts)
            if signal.type in [SignalType.FLAG_BREAKOUT, SignalType.FAST_BREAKOUT]:
                if self.router.has_position(symbol):
                    # Already holding - track as limit rejection
                    self.state.rejections_limits += 1
                else:
                    position = await self.router.open_position(signal)
                    if position:
                        self.orchestrator.reset(symbol)
                        if symbol == focus_symbol:
                            self.state.focus_coin.stage = "breakout"
                        is_fast = signal.type == SignalType.FAST_BREAKOUT
                        mode = "⚡ FAST LONG" if is_fast else "🎯 LONG"
                        logger.info(
                            "[TRADE] %s %s @ $%s",
                            mode,
                            symbol,
                            f"{signal.price:.4f}",
                        )
                        self.state.log(f"{'FAST ' if is_fast else ''}OPEN LONG {symbol} @ {signal.price:.4f}", "TRADE")
        
        # Update confidence for all active plays
        self.router.update_all_position_confidence()
        
        # Check for exits on all positions
        for symbol in list(self.router.positions.keys()):
            result = await self.router.check_exits(symbol)
            if result:
                self.orchestrator.reset(symbol)
                emoji = "✅" if result.pnl >= 0 else "❌"
                logger.info(
                    "[TRADE] %s Closed %s @ $%s (%s) PnL: $%s",
                    emoji,
                    symbol,
                    f"{result.exit_price:.4f}",
                    result.exit_reason,
                    f"{result.pnl:+.2f}",
                )
                self.state.log(f"CLOSE {symbol} {result.exit_reason} pnl={result.pnl:+.2f}", "TRADE")
                self._last_strategy_signals.pop(symbol, None)
    
    def _build_features(self, symbol: str, buffer: CandleBuffer) -> dict:
        """Build feature dict for strategy orchestrator from live indicators."""
        from logic.intelligence import intelligence
        
        ind = intelligence.get_live_indicators(symbol)
        features = {
            "trend_5m": 0.0,
            "trend_1h": 0.0,
            "trend_15m": 0.0,
            "vol_ratio": 1.0,
            "vwap_pct": 0.0,
            "vwap_distance": 0.0,
        }
        
        if ind and getattr(ind, "is_ready", False):
            features.update({
                "trend_5m": ind.trend_5m,
                "trend_1h": ind.trend_15m,  # 15m as proxy for 1h until added
                "trend_15m": ind.trend_15m,
                "vol_ratio": ind.volume_ratio,
                "vwap_pct": ind.vwap_distance,
                "vwap_distance": ind.vwap_distance,
            })
        else:
            # Fallback to buffer-derived VWAP distance when indicators are cold
            try:
                vwap = buffer.vwap(30)
                if vwap > 0 and buffer.last_price > 0:
                    features["vwap_pct"] = (buffer.last_price - vwap) / vwap * 100
                    features["vwap_distance"] = features["vwap_pct"]
            except Exception:
                logger.debug("VWAP fallback failed for %s", symbol, exc_info=True)
        
        return features
    
    def _build_market_context(self) -> dict:
        """Build market context shared across strategies."""
        from logic.intelligence import intelligence
        return {
            "btc_regime": intelligence._market_regime,
            "vol_regime": getattr(self.state, "vol_regime", "normal"),
        }
    
    def _adapt_strategy_signal(
        self,
        symbol: str,
        strat_signal,
        features: dict,
        market_context: dict,
        buffer: CandleBuffer,
    ) -> Optional[Signal]:
        """Adapt StrategySignal to core Signal for routing."""
        if strat_signal.entry_price <= 0 or strat_signal.stop_price <= 0:
            return None
        
        spread_bps = self._latest_spreads.get(symbol, 999.0)
        
        # Pull burst metrics from scanner if available for scoring gates
        bm = self.scanner.burst_metrics.get(symbol)
        vol_spike = bm.vol_spike if bm else max(1.0, features.get("vol_ratio", 1.0))
        range_spike = bm.range_spike if bm else 1.0
        trend_15m = bm.trend_15m if bm else features.get("trend_15m", 0.0)
        vwap_distance = bm.vwap_distance if bm else features.get("vwap_distance", 0.0)
        
        tier = "unknown"
        info = self.scanner.universe.get(symbol)
        if info:
            tier = info.tier
        
        confidence = min(max(strat_signal.edge_score_base / 100, 0.0), 1.0)
        
        return Signal(
            symbol=symbol,
            strategy_id=strat_signal.strategy_id,
            type=SignalType.FLAG_BREAKOUT,
            timestamp=datetime.now(timezone.utc),
            price=strat_signal.entry_price,
            confidence=confidence,
            stop_price=strat_signal.stop_price,
            tp1_price=strat_signal.tp1_price,
            tp2_price=strat_signal.tp2_price,
            reason=strat_signal.reason or f"{strat_signal.strategy_id} setup",
            vol_spike=vol_spike,
            range_spike=range_spike,
            trend_15m=trend_15m,
            vwap_distance=vwap_distance,
            spread_bps=spread_bps,
            tier=tier,
        )
    
    def _update_focus_coin_basic(self, symbol: str, buffer: CandleBuffer):
        """Update basic focus coin info (always called, even without signal)."""
        fc = self.state.focus_coin
        fc.symbol = symbol
        fc.price = buffer.last_price
        fc.spread_bps = self._latest_spreads.get(symbol, 0.0)
        fc.warmup_1m = len(buffer.candles_1m)
        fc.warmup_5m = len(buffer.candles_5m)
        fc.warmup_ready = fc.warmup_1m >= 10 and fc.warmup_5m >= 3
        
        # Set stage based on what we know
        strat_signal = self._last_strategy_signals.get(symbol)
        if not fc.warmup_ready:
            fc.stage = "warmup"
        elif strat_signal:
            fc.stage = strat_signal.strategy_id
        else:
            fc.stage = "scanning"
    
    def _update_focus_coin(self, symbol: str, buffer: CandleBuffer):
        """Update focus coin state from strategy analysis."""
        fc = self.state.focus_coin
        fc.symbol = symbol
        fc.price = buffer.last_price
        fc.spread_bps = self._latest_spreads.get(symbol, 0.0)
        fc.warmup_1m = len(buffer.candles_1m)
        fc.warmup_5m = len(buffer.candles_5m)
        # Keep warmup short so dashboard shows data quickly
        fc.warmup_ready = fc.warmup_1m >= 10 and fc.warmup_5m >= 3
        
        strat_signal = self._last_strategy_signals.get(symbol)
        
        if not fc.warmup_ready:
            fc.stage = "warmup"
        elif strat_signal:
            fc.stage = strat_signal.strategy_id
            fc.flag_retracement = getattr(strat_signal, "flag_retrace_pct", 0) or strat_signal.risk_pct
            fc.flag_age_min = getattr(strat_signal, "timing_score", 0)
            fc.flag_high = strat_signal.tp1_price
            fc.flag_low = strat_signal.stop_price
            fc.impulse_move = strat_signal.edge_score_base
        else:
            fc.stage = "waiting"
            fc.flag_retracement = 0
            fc.flag_age_min = 0
            fc.flag_high = 0
            fc.flag_low = 0
            fc.impulse_move = 0
        
        # Reset unused legacy fields
        fc.flag_slope = 0
        fc.flag_vol_decay = 0
        fc.impulse_high = 0
        fc.impulse_low = 0
        fc.impulse_age_min = 0
        fc.impulse_atr = 0
        fc.impulse_green_candles = 0
        fc.triple_top = False
        fc.head_shoulders = False
        fc.skip_reason = ""
    
    def _update_signal_state(self, signal):
        """Update current signal state from strategy signal."""
        sig = self.state.current_signal
        fc = self.state.focus_coin
        
        if signal.type == SignalType.NONE:
            sig.action = "WAIT"
            sig.reason = signal.reason
        elif signal.type == SignalType.BURST_DETECTED:
            sig.action = "WAIT"
            sig.reason = "Burst detected, monitoring..."
        elif signal.type == SignalType.IMPULSE_FOUND:
            sig.action = "WAIT"
            sig.reason = f"Impulse +{signal.impulse.pct_move:.1f}%, waiting for flag"
        elif signal.type == SignalType.FLAG_FORMING:
            sig.action = "WAIT"
            sig.reason = f"Flag forming ({signal.flag.duration_minutes}m)"
        elif signal.type == SignalType.FLAG_BREAKOUT:
            sig.action = "ENTER_LONG"
            sig.reason = signal.reason
            sig.entry_price = signal.price
            # Always use fixed geometry (strategies can output tiny stops)
            sig.stop_price = signal.price * (1 - settings.fixed_stop_pct)
            sig.tp1_price = signal.price * (1 + settings.tp1_pct)
            sig.tp2_price = signal.price * (1 + settings.tp2_pct)
            sig.time_stop_deadline = (datetime.now(timezone.utc) + timedelta(minutes=settings.max_hold_minutes)).strftime("%H:%M")
        elif signal.type == SignalType.FAST_BREAKOUT:
            sig.action = "ENTER_LONG_FAST"
            sig.reason = signal.reason
            sig.entry_price = signal.price
            # FAST mode: 2.5% stop, 4% TP1, 7% TP2
            sig.stop_price = signal.price * (1 - settings.fast_stop_pct / 100)
            sig.tp1_price = signal.price * (1 + settings.fast_tp1_pct / 100)
            sig.tp2_price = signal.price * (1 + settings.fast_tp2_pct / 100)
            sig.time_stop_deadline = (datetime.now(timezone.utc) + timedelta(minutes=settings.fast_time_stop_min)).strftime("%H:%M")
        elif signal.type in [SignalType.TRAP_TRIPLE_TOP, SignalType.TRAP_HEAD_SHOULDERS]:
            sig.action = "SKIP_TRAP"
            sig.reason = signal.reason
        
        # Make warmup status explicit for visibility
        if sig.action == "WAIT" and not fc.warmup_ready:
            sig.reason = (
                f"Warming up candles: "
                f"{fc.warmup_1m}/10 (1m), {fc.warmup_5m}/3 (5m)"
            )
        
        sig.confidence = signal.confidence
    
    def _update_positions_state(self):
        """Update positions for dashboard."""
        positions = []
        
        for symbol, pos in self.router.positions.items():
            price = self._get_price(symbol)
            positions.append(PositionDisplay(
                symbol=symbol,
                units=pos.size_qty,
                size_usd=pos.size_usd,
                entry_price=pos.entry_price,
                stop_price=pos.stop_price,
                tp1_price=pos.tp1_price,
                tp2_price=pos.tp2_price,
                unrealized_pnl=pos.unrealized_pnl(price),
                unrealized_pct=((price / pos.entry_price) - 1) * 100 if pos.entry_price > 0 else 0,
                age_min=pos.hold_duration_minutes()
            ))
        
        self.state.positions = positions
        
        # Update PnL from REAL Coinbase Portfolio API
        self.state.realized_pnl = self.router.daily_stats.total_pnl
        
        # Use real portfolio snapshot if available
        use_snapshot = (self.mode == TradingMode.LIVE) and bool(self.router._portfolio_snapshot)
        
        # Truth/snapshot freshness for dashboard
        if self.mode == TradingMode.LIVE:
            if self.router._last_snapshot_at:
                age = (datetime.now(timezone.utc) - self.router._last_snapshot_at).total_seconds()
                self.state.portfolio_snapshot_age_s = age
                self.state.truth_stale = age > 20
            else:
                self.state.portfolio_snapshot_age_s = 999.0
                self.state.truth_stale = True
            self.state.sync_paused = getattr(self.router, "_sync_degraded", False)
        else:
            self.state.portfolio_snapshot_age_s = 0.0
            self.state.sync_paused = False
            self.state.truth_stale = False
        
        if use_snapshot:
            snap = self.router._portfolio_snapshot
            self.state.unrealized_pnl = snap.total_unrealized_pnl
            self.state.portfolio_value = snap.total_value
            self.state.cash_balance = snap.total_cash
            self.state.holdings_value = snap.total_crypto
        else:
            # Fallback to calculated values
            unrealized = sum(p.unrealized_pnl for p in positions)
            self.state.unrealized_pnl = unrealized
            holdings_value = sum(
                p.size_qty * self._get_price(p.symbol) 
                for p in self.router.positions.values()
                if self._get_price(p.symbol) > 0
            )
            self.state.cash_balance = self.router._usd_balance
            self.state.holdings_value = holdings_value
            self.state.portfolio_value = self.state.cash_balance + holdings_value
        
        # Track starting portfolio value (first time we get a snapshot)
        if self.state.starting_portfolio_value == 0 and self.state.portfolio_value > 0:
            self.state.starting_portfolio_value = self.state.portfolio_value
            logger.info(
                "[PORTFOLIO] Starting value: $%s",
                f"{self.state.starting_portfolio_value:.2f}",
            )
        
        # ACTUAL PnL = current portfolio - starting portfolio (THE REAL NUMBER)
        if self.state.starting_portfolio_value > 0:
            self.state.actual_pnl = self.state.portfolio_value - self.state.starting_portfolio_value
        
        # Legacy daily_pnl (keep for compatibility but use actual_pnl for display)
        self.state.daily_pnl = self.state.actual_pnl
        
        # Check for daily reset (new day in UTC)
        self.router.daily_stats.check_reset()
        
        # Update stats from DailyStats
        ds = self.router.daily_stats
        self.state.trades_today = ds.trades
        self.state.wins_today = ds.wins
        self.state.losses_today = ds.losses
        
        # Compounding metrics
        self.state.profit_factor = ds.profit_factor if ds.profit_factor != float('inf') else 99.9
        self.state.avg_r = ds.avg_r
        self.state.avg_win = ds.avg_win
        self.state.avg_loss = ds.avg_loss
        self.state.biggest_win = ds.biggest_win
        self.state.biggest_loss = ds.biggest_loss
        self.state.max_drawdown = ds.max_drawdown
        self.state.loss_limit_pct = ds.loss_limit_pct
        
        # Bot Budget & Exposure tracking (use cost_basis = original entry cost!)
        bot_exposure = sum(p.cost_basis for p in self.router.positions.values())
        bot_budget = self.state.portfolio_value * settings.portfolio_max_exposure_pct
        
        self.state.bot_budget_usd = bot_budget
        self.state.bot_exposure_usd = bot_exposure
        self.state.bot_available_usd = max(0, bot_budget - bot_exposure)
        self.state.exposure_pct = (bot_exposure / bot_budget * 100) if bot_budget > 0 else 0
        self.state.max_exposure_pct = settings.portfolio_max_exposure_pct * 100
        
        # Balances for UI separation
        if self.mode == TradingMode.PAPER:
            self.state.paper_balance_usd = self.router._usd_balance
            self.state.live_balance_usd = 0.0
        else:
            self.state.live_balance_usd = self.router._usd_balance
        
        # Tier system stats
        tier_stats = tier_scheduler.get_stats()
        self.state.tier1_count = tier_stats.get("tier1_ws", 0)
        self.state.tier2_count = tier_stats.get("tier2_fast", 0)
        self.state.tier3_count = tier_stats.get("tier3_slow", 0)
        self.state.warm_symbols = tier_stats.get("warm", 0)
        self.state.cold_symbols = tier_stats.get("cold", 0)
        
        # Backfill stats
        if self.backfill_service:
            self.state.pending_backfills = self.backfill_service.get_pending_count()
        
        # REST poller stats
        if self.rest_poller:
            poller_stats = self.rest_poller.get_stats()
            self.state.rest_polls_tier2 = poller_stats.get("polls_tier2", 0)
            self.state.rest_polls_tier3 = poller_stats.get("polls_tier3", 0)
            self.state.rest_rate_degraded = poller_stats.get("is_degraded", False)
        
        # BTC regime from intelligence
        from logic.intelligence import intelligence
        self.state.btc_regime = intelligence._market_regime
        self.state.btc_trend_1h = intelligence._btc_trend_1h
        
        # ML cache freshness (global)
        total_ml = len(intelligence.live_ml)
        fresh_ml = sum(1 for ml in intelligence.live_ml.values() if not ml.is_stale())
        self.state.ml_total_cached = total_ml
        self.state.ml_fresh_pct = (fresh_ml / total_ml * 100) if total_ml > 0 else 0.0
        
        # ML/indicators for focus symbol
        focus = self._focus_symbol or self.scanner.get_focus_symbol()
        if focus:
            ml = intelligence.get_live_ml(focus)
            ind = intelligence.get_live_indicators(focus)
            if ml:
                self.state.ml_score = ml.raw_score
                self.state.ml_confidence = ml.confidence
            if ind and ind.is_ready:
                self.state.is_choppy = ind.is_choppy
                self.state.rsi = ind.rsi_14
        
        # Candle store stats
        self.state.candles_persisted = candle_store.candles_written
        
        # Kill switch
        self.state.kill_switch = ds.should_stop
        if self.state.kill_switch:
            self.state.kill_reason = f"Daily loss limit (${settings.daily_max_loss_usd})"
    
    async def _display_loop(self):
        """Update display periodically."""
        from rich.live import Live
        from core.logging_utils import suppress_console_logging
        
        await asyncio.sleep(2)
        
        # Suppress console logging while TUI is active (logs still go to file)
        suppress_console_logging(True)
        
        try:
            with Live(
                self.dashboard.render_full(),
                console=self.dashboard.console,
                refresh_per_second=2,
                screen=True
            ) as live:
                while self._running:
                    try:
                        live.update(self.dashboard.render_full())
                        await asyncio.sleep(0.5)
                    except Exception:
                        await asyncio.sleep(1)
        finally:
            # Restore console logging when TUI exits
            suppress_console_logging(False)


def main():
    """Main entry point with command line mode switching."""
    import sys
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='CoinTrader V2 - Multi-strategy trading bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_v2.py                    # Use TRADING_MODE from .env
  python run_v2.py --paper           # Force paper trading mode
  python run_v2.py --live            # Force live trading mode  
  python run_v2.py --mode=live       # Explicit mode setting
        """
    )
    
    parser.add_argument('--mode', choices=['paper', 'live'],
                       help='Override trading mode (paper or live)')
    parser.add_argument('--paper', action='store_true',
                       help='Shortcut for --mode=paper')
    parser.add_argument('--live', action='store_true',
                       help='Shortcut for --mode=live')
    parser.add_argument('--validate', action='store_true',
                       help='Run validation checks before starting')
    
    args = parser.parse_args()
    
    # Handle mode override
    if args.paper and args.live:
        print("❌ Cannot specify both --paper and --live")
        sys.exit(1)
    
    original_mode = os.environ.get('TRADING_MODE', 'paper')
    
    if args.paper:
        os.environ['TRADING_MODE'] = 'paper'
        print("🔄 Override: Using PAPER trading mode")
    elif args.live:
        os.environ['TRADING_MODE'] = 'live'
        print("🔄 Override: Using LIVE trading mode")
    elif args.mode:
        os.environ['TRADING_MODE'] = args.mode
        print(f"🔄 Override: Using {args.mode.upper()} trading mode")
    else:
        print(f"📋 Using {original_mode.upper()} mode from environment")
    
    # Reload configuration if mode was overridden
    if args.paper or args.live or args.mode:
        from importlib import reload
        import core.config
        reload(core.config)
    
    # Import settings (after any reload)
    from core.config import settings
    
    print(f"🎯 Trading Mode: {settings.trading_mode}")
    print(f"🔑 API Keys: {'Configured' if settings.coinbase_api_key else 'Missing'}")
    
    # Run validation if requested
    if args.validate:
        print("\n🧪 Running pre-start validation...")
        from tests.integration.data_sync_validator import DataSynchronizationValidator
        import asyncio as validation_asyncio
        
        async def run_validation():
            validator = DataSynchronizationValidator()
            success = await validator.run_complete_validation()
            return success
        
        validation_success = validation_asyncio.run(run_validation())
        if not validation_success:
            print("❌ Validation failed - check issues before starting")
            if input("Continue anyway? (y/N): ").lower() != 'y':
                sys.exit(1)
    
    bot = TradingBotV2()
    shutdown_requested = False
    
    def handle_interrupt(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            logger.error("[BOT] Force exit!")
            sys.exit(1)
        shutdown_requested = True
        logger.info("[BOT] Ctrl+C received - shutting down gracefully...")
        logger.info("[BOT] Press Ctrl+C again to force exit")
        bot._running = False
    
    sig.signal(sig.SIGINT, handle_interrupt)
    sig.signal(sig.SIGTERM, handle_interrupt)
    
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("[BOT] Exiting...")
    except Exception as e:
        logger.exception("[BOT] Error: %s", e)
    finally:
        logger.info("[BOT] Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
