"""
FastAPI WebSocket server for real-time bot state streaming.
Runs alongside the trading bot to provide web dashboard access.

Full Control Dashboard API - Complete bot control from web interface.
"""

import asyncio
import subprocess
import sys
import os
import threading
from datetime import datetime, timezone
from typing import Optional
from collections import deque
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from core.state import BotState
from core.bot_controller import get_controller, BotController
from core.config_manager import get_config_manager, ConfigManager
from core.mode_config import sanitize_config_snapshot
from core.strategy_registry import get_strategy_registry, StrategyRegistry
from execution.position_controller import get_position_controller, PositionController

app = FastAPI(title="CoinTrader Dashboard API", version="2.0")
app.mount(
    "/js",
    StaticFiles(directory=Path(__file__).parent / "web" / "js"),
    name="js",
)

# Shared state reference (set by bot on startup)
_bot_state: Optional[BotState] = None
_controller: BotController = get_controller()
_config_manager: ConfigManager = get_config_manager()
_strategy_registry: StrategyRegistry = get_strategy_registry()
_position_controller: PositionController = get_position_controller()
_connected_clients: set[WebSocket] = set()

# Live logs buffer
_live_logs: deque = deque(maxlen=500)

# Bot process management
_bot_process: Optional[subprocess.Popen] = None
_bot_thread: Optional[threading.Thread] = None
_bot_running: bool = False
_bot_mode: str = "paper"
_project_root = Path(__file__).parent.parent

_PM2_MANAGED = str(os.getenv("COINTRADER_MANAGED_BY_PM2", "")).lower() in {"1", "true", "yes", "on"}
_PM2_MESSAGE = "Managed by PM2. Use: pm2 restart coin-back"
_RESTART_INSTRUCTIONS = {
    "bot": "pm2 restart coin-back",
    "dashboard": "pm2 restart coin-front",
}

# Scanner/Universe reference (set by bot)
_scanner = None
_order_router = None


def set_bot_state(state: BotState):
    """Called by bot to share its state with the web server."""
    global _bot_state
    _bot_state = state


def set_bot_components(order_router=None, scanner=None, executor=None, get_price_func=None):
    """Called by bot to share components for position control."""
    global _order_router, _scanner
    _order_router = order_router
    _scanner = scanner
    
    # Initialize position controller with components
    if order_router and get_price_func:
        _position_controller.initialize(
            order_router=order_router,
            executor=executor,
            get_price_func=get_price_func,
            state=_bot_state
        )


def add_log(level: str, message: str):
    """Add a log entry to the live logs buffer."""
    _live_logs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    })


def _pm2_block_response(status_code: int = 409):
    """Standardized response when PM2 manages lifecycle."""
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": _PM2_MESSAGE,
            "managed_by_pm2": True,
        },
    )


def _compute_health(snapshot: dict) -> dict:
    """Derive health summary from ts + heartbeats + rest flags."""
    ts = snapshot.get("ts")
    try:
        ts_dt = datetime.fromisoformat(ts) if ts else None
    except Exception:
        ts_dt = None
    state_age = (
        (datetime.now(timezone.utc) - ts_dt).total_seconds()
        if ts_dt and ts_dt.tzinfo
        else 999.0
    )

    hb = snapshot.get("heartbeats", {}) or {}
    ws_age = hb.get("ws", 999)
    scanner_age = hb.get("scanner", 999)
    router_age = hb.get("order_router", 999)
    data_age = hb.get("candles_1m", 999)
    rest_degraded = snapshot.get("rest_rate_degraded") or False

    reasons: list[str] = []
    status = "OK"

    # Stale overrides everything
    if state_age > 15:
        status = "STALE"
        reasons.append("state_stale")
    else:
        # Check individual components
        if ws_age >= 8:
            reasons.append("ws_heartbeat_old")
        if scanner_age >= 30:
            reasons.append("scanner_heartbeat_old")
        if router_age >= 30:
            reasons.append("order_router_heartbeat_old")
        if data_age >= 180:
            reasons.append("data_heartbeat_old")
        if rest_degraded:
            reasons.append("rest_throttling")

        if reasons:
            status = "DEGRADED"

    return {
        "status": status,
        "reasons": reasons,
        "state_age_s": state_age,
        "last_update_ts": ts,
    }


# =============================================================================
# BOT PROCESS MANAGEMENT
# =============================================================================

def _read_bot_output(process: subprocess.Popen):
    """Read bot output and add to logs."""
    global _bot_running
    try:
        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            line = line.strip()
            if line:
                # Determine log level from content
                level = "INFO"
                if "ERROR" in line or "error" in line.lower():
                    level = "ERROR"
                elif "WARNING" in line or "warn" in line.lower():
                    level = "WARNING"
                elif "TRADE" in line or "ORDER" in line:
                    level = "TRADE"
                elif "POSITION" in line:
                    level = "POSITION"
                add_log(level, line)
    except Exception as e:
        add_log("ERROR", f"Log reader error: {e}")
    finally:
        _bot_running = False
        add_log("INFO", "Bot process ended")


def start_bot_process(mode: str = "paper") -> dict:
    """Start the bot as a subprocess (dashboard controls everything)."""
    if _PM2_MANAGED:
        return {"success": False, "error": _PM2_MESSAGE, "managed_by_pm2": True}

    global _bot_process, _bot_thread, _bot_running, _bot_mode
    
    if _bot_running and _bot_process and _bot_process.poll() is None:
        return {"success": False, "error": f"Bot already running (PID: {_bot_process.pid})"}
    
    try:
        _bot_mode = mode
        _controller.set_mode(mode)
        
        # Build command
        python_path = sys.executable
        script_path = _project_root / "run_v2.py"
        
        if not script_path.exists():
            return {"success": False, "error": f"Bot script not found: {script_path}"}
        
        cmd = [python_path, str(script_path), "--mode", mode]
        
        add_log("INFO", f"Starting bot in {mode.upper()} mode...")
        
        # Start process
        _bot_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(_project_root),
            env={**os.environ, "PYTHONUNBUFFERED": "1", "COINTRADER_DASHBOARD_RUNNING": "1"}
        )
        
        _bot_running = True
        
        # Start log reader thread
        _bot_thread = threading.Thread(target=_read_bot_output, args=(_bot_process,), daemon=True)
        _bot_thread.start()
        
        # Update controller status
        _controller.set_status("running")
        
        add_log("INFO", f"Bot started in {mode.upper()} mode (PID: {_bot_process.pid})")
        
        return {
            "success": True,
            "pid": _bot_process.pid,
            "mode": mode,
            "message": f"Bot started in {mode.upper()} mode"
        }
        
    except Exception as e:
        add_log("ERROR", f"Failed to start bot: {e}")
        return {"success": False, "error": str(e)}


def stop_bot_process() -> dict:
    """Stop the bot subprocess."""
    if _PM2_MANAGED:
        return {"success": False, "error": _PM2_MESSAGE, "managed_by_pm2": True}

    global _bot_process, _bot_running
    
    if not _bot_process or _bot_process.poll() is not None:
        _bot_running = False
        return {"success": True, "message": "Bot not running"}
    
    try:
        add_log("INFO", "Stopping bot...")
        
        # Send SIGTERM for graceful shutdown
        _bot_process.terminate()
        
        # Wait up to 5 seconds for graceful shutdown
        try:
            _bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            add_log("WARNING", "Bot not responding, force killing...")
            _bot_process.kill()
            _bot_process.wait(timeout=2)
        
        _bot_running = False
        _controller.set_status("stopped")
        
        add_log("INFO", "Bot stopped")
        
        return {"success": True, "message": "Bot stopped"}
        
    except Exception as e:
        add_log("ERROR", f"Failed to stop bot: {e}")
        return {"success": False, "error": str(e)}


def get_bot_status() -> dict:
    """Get current bot process status."""
    global _bot_process, _bot_running
    
    # Check if bot is running as subprocess (started by web server)
    if _bot_process and _bot_process.poll() is None:
        return {
            "running": True,
            "pid": _bot_process.pid,
            "mode": _bot_mode,
            "status": "running"
        }
    
    # Check if bot is running via PM2 (separate process writing to bot_state.json)
    from core.shared_state import read_state
    shared = read_state()
    if shared:
        # Check if state is fresh (< 10 seconds old) - more lenient than 5s
        ts = shared.get('ts')
        is_fresh = False
        if ts:
            try:
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                is_fresh = age < 10  # Consider fresh if < 10 seconds
            except:
                pass
        
        # Also check explicit status field
        if is_fresh or shared.get('status') == 'running':
            return {
                "running": True,
                "pid": None,
                "mode": shared.get('mode', _bot_mode),
                "status": "running"
            }
    
    _bot_running = False
    return {
        "running": False,
        "pid": None,
        "mode": _bot_mode,
        "status": "stopped"
    }


def _heartbeat_age(ts: Optional[datetime]) -> float:
    """Get age in seconds of a heartbeat timestamp."""
    if ts is None:
        return 999.0
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _get_edge_indicator(symbol: str, attr: str) -> float:
    """Get edge indicator from intelligence cache."""
    if not symbol:
        return 0.0
    try:
        from logic.intelligence import intelligence
        ind = intelligence.get_live_indicators(symbol)
        if ind:
            return getattr(ind, attr, 0.0)
    except Exception:
        pass
    return 0.0


def _get_config_snapshots_from_state(state: BotState) -> dict:
    """Extract config snapshots from BotState."""
    last_refreshed = getattr(state, "config_last_refreshed", None)
    if isinstance(last_refreshed, datetime):
        last_refreshed = last_refreshed.isoformat()
    return {
        "config_start": sanitize_config_snapshot(getattr(state, "config_start", {})),
        "config_running": sanitize_config_snapshot(getattr(state, "config_running", {})),
        "config_last_refreshed": last_refreshed,
    }


def _get_fallback_config_snapshots() -> dict:
    """Best-effort config snapshots when bot state is unavailable."""
    try:
        from core.mode_config import ConfigurationManager
        from core.mode_configs import TradingMode

        mode_value = _bot_mode if _bot_mode in ("paper", "live") else "paper"
        config = ConfigurationManager.get_config_for_mode(TradingMode(mode_value))
        snapshot = sanitize_config_snapshot(config)
        return {
            "config_start": snapshot,
            "config_running": snapshot,
            "config_last_refreshed": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return {"config_start": {}, "config_running": {}, "config_last_refreshed": None}


def get_state_snapshot() -> dict:
    """Get serializable snapshot of bot state."""
    bot_status = get_bot_status()

    # Prefer the shared state file when it's fresh.
    # This is the authoritative path when the bot runs under PM2 (separate process writing bot_state.json).
    if bot_status["running"]:
        from core.shared_state import read_state
        shared = read_state()
        if shared and shared.get('state_fresh', False):
            shared['bot_running'] = True
            shared['bot_pid'] = bot_status['pid']
            shared['bot_status'] = 'running'
            if "capabilities" not in shared:
                shared["capabilities"] = {
                    "process_control": not _PM2_MANAGED,
                    "restart_instructions": _RESTART_INSTRUCTIONS,
                }
            if "health" not in shared:
                shared["health"] = _compute_health(shared)
            shared["state_age_s"] = shared.get("state_age_s", shared["health"].get("state_age_s"))
            return shared
    
    # If bot state not connected and no shared state, return basic status
    if _bot_state is None:
        config_snapshots = _get_fallback_config_snapshots()
        base = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": _bot_mode,
            "profile": "prod",
            "bot_running": bot_status["running"],
            "bot_pid": bot_status["pid"],
            "bot_status": bot_status["status"],
            "config_start": config_snapshots.get("config_start", {}),
            "config_running": config_snapshots.get("config_running", {}),
            "config_last_refreshed": config_snapshots.get("config_last_refreshed"),
            
            # These will be populated from live portfolio fetch on frontend
            "portfolio_value": 0,
            "cash_balance": 0,
            "holdings_value": 0,
            "daily_pnl": 0,
            "unrealized_pnl": 0,
            
            # Connection status
            "ws_ok": bot_status["running"],
            "ws_last_age": 0 if bot_status["running"] else 999,
            "api_ok": True,
            "btc_regime": "normal",
            
            # Universe
            "warm_symbols": 0,
            "cold_symbols": 0,
            "symbols_streaming": 0,
            
            # Empty lists
            "positions": [],
            "burst_leaderboard": [],
            "recent_signals": [],
            "rejections": {"spread": 0, "warmth": 0, "regime": 0, "score": 0, "rr": 0, "limits": 0},
            
            # Focus coin placeholder
            "focus_coin": {"symbol": "--", "price": 0, "trend_5m": 0, "stage": "--", "vol_spike": 0},
            "current_signal": {"symbol": "--", "strategy": "--", "score": 0, "entry_price": 0, "stop_price": 0, "tp1_price": 0},
            
            # Kill switch
            "kill_switch": False,
            
            # Engine stats
            "engine": {
                "uptime_seconds": 0,
                "trades_today": 0,
                "wins_today": 0,
                "losses_today": 0,
                "realized_pnl_today": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "biggest_win": 0,
                "biggest_loss": 0,
                "max_drawdown": 0,
                "bot_budget_usd": 0,
                "bot_exposure_usd": 0,
                "bot_available_usd": 0,
                "exposure_pct": 0,
                "ticks_5s": 0,
                "candles_5s": 0,
                "events_5s": 0,
                "rest_requests": 0,
                "rest_429s": 0,
                "candles_persisted": 0,
                "ml_fresh_pct": 0,
                "vol_regime": "normal",
            },
            
            # Heartbeats
            "heartbeats": {"ws": 999, "candles_1m": 999, "candles_5m": 999, "features": 999, "ml": 999, "scanner": 999, "order_router": 999},
            
            # Control state
            "control": {
                "command": _controller.command,
                "mode": _bot_mode,
                "status": bot_status["status"],
                "is_running": bot_status["running"],
            },
            "rest_rate_degraded": False,
        }
        base["health"] = _compute_health(base)
        base["state_age_s"] = base["health"]["state_age_s"]
        base["capabilities"] = {
            "process_control": not _PM2_MANAGED,
            "restart_instructions": _RESTART_INSTRUCTIONS,
        }
        return base
    
    state = _bot_state
    config_snapshots = _get_config_snapshots_from_state(state)
    
    # Build positions list
    positions = []
    for pos in getattr(state, 'positions_display', []):
        positions.append({
            "symbol": pos.symbol,
            "entry_price": pos.entry_price,
            "current_price": getattr(pos, 'current_price', pos.entry_price),
            "size_usd": pos.size_usd,
            "pnl_usd": getattr(pos, 'pnl_usd', getattr(pos, 'unrealized_pnl', 0.0)),
            "pnl_pct": getattr(pos, 'pnl_pct', getattr(pos, 'unrealized_pct', 0.0)),
            "stop_price": getattr(pos, 'stop_price', 0.0),
            "tp1_price": getattr(pos, 'tp1_price', 0.0),
            "hold_minutes": getattr(pos, 'hold_minutes', getattr(pos, 'age_min', 0)),
        })
    
    # Build burst leaderboard
    burst = []
    for b in getattr(state, 'burst_leaderboard', []):
        burst.append({
            "rank": b.rank,
            "symbol": b.symbol,
            "price": b.price,
            "burst_score": b.burst_score,
            "vol_spike": b.vol_spike,
            "trend_5m": b.trend_5m,
            "tier": b.tier,
        })
    
    # Build recent signals - handle both dict and tuple formats
    signals = []
    for sig in list(getattr(state, 'recent_signals', []))[:15]:
        if isinstance(sig, dict):
            signals.append({
                "ts": sig.get("ts", ""),
                "symbol": sig.get("symbol", ""),
                "strategy": sig.get("strategy", ""),
                "score": sig.get("score", 0),
                "spread_bps": sig.get("spread_bps", 0.0),
                "taken": sig.get("taken", False),
                "reason": sig.get("reason", ""),
            })
        elif isinstance(sig, (tuple, list)) and len(sig) >= 7:
            # Tuple format: (ts, symbol, strategy, score, spread_bps, taken, reason)
            signals.append({
                "ts": sig[0].isoformat() if hasattr(sig[0], 'isoformat') else str(sig[0]),
                "symbol": sig[1],
                "strategy": sig[2],
                "score": sig[3],
                "spread_bps": sig[4],
                "taken": sig[5],
                "reason": sig[6],
            })
    
    base = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": state.mode,
        "profile": getattr(state, 'profile', 'prod'),
        "config_start": config_snapshots.get("config_start", {}),
        "config_running": config_snapshots.get("config_running", {}),
        "config_last_refreshed": config_snapshots.get("config_last_refreshed"),
        
        # Portfolio
        "portfolio_value": state.portfolio_value,
        "cash_balance": state.cash_balance,
        "holdings_value": state.holdings_value,
        "daily_pnl": state.daily_pnl,
        "unrealized_pnl": getattr(state, 'unrealized_pnl', 0.0),
        
        # Health
        "ws_ok": state.ws_ok,
        "ws_last_age": state.ws_last_age,
        "api_ok": state.api_ok,
        "btc_regime": state.btc_regime,
        
        # Universe
        "warm_symbols": state.warm_symbols,
        "cold_symbols": state.cold_symbols,
        "symbols_streaming": getattr(state.universe, 'symbols_streaming', 0),
        
        # Rejections
        "rejections": {
            "spread": state.rejections_spread,
            "warmth": state.rejections_warmth,
            "regime": state.rejections_regime,
            "score": state.rejections_score,
            "rr": state.rejections_rr,
            "limits": state.rejections_limits,
        },
        
        # Focus coin with edge indicators and trends
        "focus_coin": {
            "symbol": state.focus_coin.symbol,
            "price": state.focus_coin.price,
            "trend_5m": state.focus_coin.trend_5m,
            "stage": state.focus_coin.stage,
            "vol_spike": state.focus_coin.vol_spike,
            "acceleration": _get_edge_indicator(state.focus_coin.symbol, 'acceleration_score'),
            "whale_bias": _get_edge_indicator(state.focus_coin.symbol, 'whale_bias'),
            "whale_activity": _get_edge_indicator(state.focus_coin.symbol, 'whale_activity'),
            # Trend indicators
            "trend_1m": _get_edge_indicator(state.focus_coin.symbol, 'price_change_1m'),
            "trend_15m": _get_edge_indicator(state.focus_coin.symbol, 'trend_15m'),
            "trend_1h": _get_edge_indicator(state.focus_coin.symbol, 'trend_1h'),
            "trend_1d": _get_edge_indicator(state.focus_coin.symbol, 'trend_1d'),
        },
        
        # Current signal
        "current_signal": {
            "symbol": state.current_signal.symbol,
            "strategy": state.current_signal.action,
            "score": int(state.current_signal.confidence),
            "entry_price": state.current_signal.entry_price,
            "stop_price": state.current_signal.stop_price,
            "tp1_price": state.current_signal.tp1_price,
        },
        
        # Lists
        "positions": positions,
        "burst_leaderboard": burst,
        "recent_signals": signals,
        
        # Kill switch
        "kill_switch": state.kill_switch,
        
        # Engine stats
        "engine": {
            "uptime_seconds": (datetime.now(timezone.utc) - state.startup_time).total_seconds() if state.startup_time else 0,
            "trades_today": state.trades_today,
            "wins_today": state.wins_today,
            "losses_today": state.losses_today,
            "realized_pnl_today": getattr(state, 'realized_pnl_today', 0.0),
            "win_rate": state.win_rate * 100,
            "profit_factor": state.profit_factor,
            "avg_win": state.avg_win,
            "avg_loss": state.avg_loss,
            "biggest_win": state.biggest_win,
            "biggest_loss": state.biggest_loss,
            "max_drawdown": state.max_drawdown,
            "bot_budget_usd": state.bot_budget_usd,
            "bot_exposure_usd": state.bot_exposure_usd,
            "bot_available_usd": state.bot_available_usd,
            "exposure_pct": state.exposure_pct,
            "ticks_5s": state.ticks_last_5s,
            "candles_5s": state.candles_last_5s,
            "events_5s": state.events_last_5s,
            "rest_requests": state.rest_requests,
            "rest_429s": state.rest_429s,
            "candles_persisted": state.candles_persisted,
            "ml_fresh_pct": state.ml_fresh_pct,
            "vol_regime": state.vol_regime,
        },
        
        # Heartbeats (component health)
        "heartbeats": {
            "ws": _heartbeat_age(state.heartbeat_ws),
            "candles_1m": _heartbeat_age(state.heartbeat_candles_1m),
            "candles_5m": _heartbeat_age(state.heartbeat_candles_5m),
            "features": _heartbeat_age(state.heartbeat_features),
            "ml": _heartbeat_age(state.heartbeat_ml),
            "scanner": _heartbeat_age(state.heartbeat_scanner),
            "order_router": _heartbeat_age(state.heartbeat_order_router),
        },
        
        # Predictive plays (MTF analysis)
        "predictive_plays": [
            {"symbol": p[0], "confidence": p[1], "direction": p[2]}
            for p in getattr(state, 'predictive_plays', [])
        ],
        
        # Control state (dashboard as source of truth)
        "control": {
            "command": _controller.command,
            "mode": _controller.mode,
            "status": _controller.status,
            "is_running": _controller.is_running,
        },
        "rest_rate_degraded": getattr(state, "rest_rate_degraded", False),
    }

    base["health"] = _compute_health(base)
    base["state_age_s"] = base["health"]["state_age_s"]
    base["capabilities"] = {
        "process_control": not _PM2_MANAGED,
        "restart_instructions": _RESTART_INSTRUCTIONS,
    }
    return base


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time state updates."""
    await websocket.accept()
    _connected_clients.add(websocket)
    
    try:
        # Send initial state
        await websocket.send_json(get_state_snapshot())
        
        # Send updates every 500ms
        while True:
            await asyncio.sleep(0.5)
            try:
                await websocket.send_json(get_state_snapshot())
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        _connected_clients.discard(websocket)


@app.get("/api/state")
async def get_state():
    """REST endpoint for current state (polling fallback)."""
    return get_state_snapshot()


@app.get("/api/coverage")
async def get_coverage():
    """Return per-symbol data coverage snapshot."""
    from core.coverage import build_coverage_snapshot
    from core.candle_store import candle_store
    from core.shared_state import read_state

    state = _bot_state
    if state is None:
        state = read_state()

    return build_coverage_snapshot(
        state=state,
        scanner=_scanner,
        store=candle_store,
    )


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    from core.shared_state import read_state
    shared = read_state()
    bot_running = shared is not None and shared.get('state_fresh', False)
    return {
        "status": "ok",
        "bot_connected": bot_running or _bot_state is not None,
        "clients": len(_connected_clients),
    }


@app.get("/api/session")
async def get_session_stats():
    """Get session statistics and chart data."""
    try:
        from core.session_stats import get_session
        session = get_session()
        return {
            "session_start": session.session_start,
            "trades": session.trades,
            "wins": session.wins,
            "losses": session.losses,
            "win_rate": round(session.win_rate, 1),
            "total_pnl": round(session.total_pnl, 2),
            "biggest_win": round(session.biggest_win, 2),
            "biggest_loss": round(session.biggest_loss, 2),
            "start_balance": round(session.start_balance, 2),
            "current_balance": round(session.current_balance, 2),
            "session_return": round(session.session_return, 2),
            "max_drawdown": round(session.max_drawdown, 2),
            "chart_data": session.get_chart_data(),
            "hourly_data": session.hourly_data[-12:],  # Last 12 hours
        }
    except Exception as e:
        return {"error": str(e), "trades": 0, "wins": 0, "losses": 0}


@app.get("/api/portfolio-history")
async def get_portfolio_history():
    """Get portfolio balance history for charting."""
    try:
        from core.portfolio_history import get_history
        history = get_history()
        return {
            "snapshots": history.snapshots[-288:],  # Last 24 hours (5 min intervals)
            "all_time_high": history.all_time_high,
            "all_time_low": history.all_time_low if history.all_time_low != float('inf') else 0,
        }
    except Exception as e:
        return {"error": str(e), "snapshots": []}


@app.post("/api/chat")
async def chat_with_bot(request: Request):
    """Chat with the trading bot AI."""
    try:
        data = await request.json()
        message = data.get("message", "")
        if not message:
            return {"error": "No message provided", "response": ""}
        
        from logic.argent import chat, get_bot_thinking
        
        # Special commands
        if message.lower() in ("thinking", "status", "what are you doing"):
            thinking = get_bot_thinking()
            return {"response": thinking, "type": "status"}
        
        response = chat(message)
        return {"response": response, "type": "chat"}
    except Exception as e:
        return {"error": str(e), "response": f"Error: {e}"}


@app.post("/api/positions/{currency}/close")
async def close_position(currency: str):
    """Close a single position by currency."""
    try:
        symbol = f"{currency}-USD" if not currency.endswith("-USD") else currency
        
        # Use position controller (works with shared state file)
        result = await _position_controller.close_position(symbol, "web_dashboard")
        return result
    except Exception as e:
        return {"error": str(e), "success": False}


@app.post("/api/positions/close-losers")
async def close_losing_positions():
    """Close losing positions, prioritizing lowest fees (smallest positions) first."""
    try:
        # Read positions from shared state file
        from core.shared_state import read_state
        shared = read_state()
        
        if not shared:
            return {"error": "Bot not running", "success": False}
        
        positions = shared.get('positions', [])
        # Filter losers and sort by size (smallest = lowest fees first)
        losers = [p for p in positions if p.get('pnl_usd', 0) < 0]
        losers.sort(key=lambda p: p.get('size_usd', 0))  # Smallest first = lowest fees
        loser_symbols = [p['symbol'] for p in losers]
        
        if not loser_symbols:
            return {"success": True, "message": "No losing positions to close", "count": 0}
        
        # Close each losing position via position controller (smallest first)
        closed = 0
        for symbol in loser_symbols:
            try:
                result = await _position_controller.close_position(symbol, "close_losers_low_fee")
                if result.get('success'):
                    closed += 1
            except:
                pass
        
        return {
            "success": True,
            "message": f"Closed {closed}/{len(loser_symbols)} losing positions (lowest fees first)",
            "count": closed,
            "symbols": loser_symbols
        }
    except Exception as e:
        return {"error": str(e), "success": False}


@app.post("/api/kill")
async def toggle_kill_switch():
    """Toggle the kill switch via shared state file."""
    from core.shared_state import read_state, write_command
    
    shared = read_state()
    if not shared:
        return {"error": "Bot not running", "kill_switch": False}
    
    current = shared.get('kill_switch', False)
    new_state = not current
    
    # Write command to shared state for bot to pick up
    write_command('kill_switch', {'enabled': new_state, 'reason': 'web_dashboard' if new_state else ''})
    
    return {
        "success": True,
        "kill_switch": new_state,
        "message": "Kill switch ENABLED" if new_state else "Kill switch DISABLED"
    }


@app.get("/api/kill")
async def get_kill_switch():
    """Get kill switch status from shared state."""
    from core.shared_state import read_state
    shared = read_state()
    if not shared:
        return {"kill_switch": False, "reason": ""}
    return {
        "kill_switch": shared.get('kill_switch', False),
        "reason": shared.get('kill_reason', '')
    }


# === Bot Control API (dashboard as source of truth) ===

@app.get("/api/control")
async def get_control_state():
    """Get current control state (mode, status, command)."""
    state = _controller.get_state()
    bot_status = get_bot_status()
    
    # Use actual bot process status, not just controller state
    actual_status = "running" if bot_status["running"] else "stopped"
    actual_mode = bot_status["mode"] if bot_status["running"] else _bot_mode
    
    return {
        "command": state.command,
        "mode": actual_mode,
        "status": actual_status,
        "is_running": bot_status["running"],
        "pid": bot_status["pid"],
        "error": state.error,
        "command_at": state.command_at,
        "status_at": state.status_at,
        "started_at": state.started_at,
    }


@app.post("/api/control/mode")
async def set_mode(mode: str = Query(default=None, description="Trading mode: paper or live")):
    """
    Set trading mode (paper/live).
    
    If bot is running, restarts it in new mode.
    """
    if _PM2_MANAGED:
        return _pm2_block_response()

    global _bot_mode
    try:
        if not mode:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Mode parameter required. Use ?mode=paper or ?mode=live"}
            )
        
        if mode not in ("paper", "live"):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Mode must be 'paper' or 'live'"}
            )
        
        _bot_mode = mode
        _controller.set_mode(mode)
        
        # If bot is running, restart in new mode
        bot_status = get_bot_status()
        if bot_status["running"]:
            stop_bot_process()
            await asyncio.sleep(1)
            result = start_bot_process(mode)
            return result
        
        return {"success": True, "mode": mode, "message": f"Mode set to {mode}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal error: {str(e)}"}
        )


@app.get("/api/bot/status")
async def api_get_bot_status():
    """Get real bot process status."""
    return get_bot_status()


@app.post("/api/control/command")
async def send_command(command: str = Query(default=None, description="Command: run, stop, restart, or pause")):
    """
    Send command to bot - actually starts/stops the bot process.
    
    Commands:
    - run: Start the bot process
    - stop: Stop the bot process
    - restart: Restart the bot process
    - pause: Pause trading (keep running)
    """
    if _PM2_MANAGED:
        return _pm2_block_response()

    try:
        if not command:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Command parameter required. Use ?command=run|stop|restart|pause"}
            )
        
        # Actually control the bot process
        if command == "run":
            result = start_bot_process(_bot_mode)
        elif command == "stop":
            result = stop_bot_process()
        elif command == "restart":
            stop_bot_process()
            await asyncio.sleep(1)
            result = start_bot_process(_bot_mode)
        elif command == "pause":
            # Pause just updates controller state
            result = _controller.send_command("pause")
        else:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Invalid command: {command}"}
            )
        
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal error: {str(e)}"}
        )


@app.post("/api/control/stop")
async def stop_bot():
    """Stop the bot process."""
    if _PM2_MANAGED:
        return _pm2_block_response()

    try:
        result = stop_bot_process()
        return result
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal error: {str(e)}"}
        )


@app.post("/api/control/restart")
async def restart_bot():
    """Restart the bot process."""
    if _PM2_MANAGED:
        return _pm2_block_response()

    try:
        stop_bot_process()
        await asyncio.sleep(1)
        result = start_bot_process(_bot_mode)
        return result
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Internal error: {str(e)}"}
        )


# =============================================================================
# POSITION MANAGEMENT API
# =============================================================================

@app.get("/api/positions")
async def get_positions():
    """Get all current positions with full details."""
    return {"positions": _position_controller.get_positions()}


@app.get("/api/position/{symbol}")
async def get_position(symbol: str):
    """Get a single position by symbol."""
    pos = _position_controller.get_position(symbol)
    if pos is None:
        return JSONResponse(status_code=404, content={"error": f"No position found for {symbol}"})
    return pos


@app.post("/api/position/{symbol}/close")
async def close_position(symbol: str, reason: str = Query(default="manual_close")):
    """Close a single position."""
    result = await _position_controller.close_position(symbol, reason)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("TRADE", f"Closed {symbol}: {reason}")
    return result


@app.post("/api/positions/close-all")
async def close_all_positions(reason: str = Query(default="manual_close_all")):
    """Close all positions (panic button)."""
    result = await _position_controller.close_all_positions(reason)
    add_log("TRADE", f"CLOSE ALL: {result.get('total', 0)} positions")
    return result


@app.post("/api/position/{symbol}/update-stop")
async def update_stop(symbol: str, stop_price: float = Query(..., description="New stop price")):
    """Update stop loss for a position."""
    result = await _position_controller.update_stop(symbol, stop_price)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("POSITION", f"Updated stop for {symbol}: ${stop_price:.4f}")
    return result


@app.post("/api/position/{symbol}/update-tp")
async def update_tp(
    symbol: str,
    tp1: float = Query(default=None, description="New TP1 price"),
    tp2: float = Query(default=None, description="New TP2 price")
):
    """Update take profit levels for a position."""
    result = await _position_controller.update_tp(symbol, tp1, tp2)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("POSITION", f"Updated TP for {symbol}")
    return result


@app.post("/api/position/{symbol}/lock-profits")
async def lock_profits(symbol: str):
    """Move stop to breakeven (lock profits)."""
    result = await _position_controller.lock_profits(symbol)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("POSITION", f"Locked profits for {symbol}")
    return result


@app.post("/api/position/{symbol}/trailing")
async def activate_trailing(symbol: str, trail_pct: float = Query(default=2.0, description="Trail percent")):
    """Activate trailing stop for a position."""
    result = await _position_controller.activate_trailing(symbol, trail_pct)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("POSITION", f"Activated trailing for {symbol} at {trail_pct}%")
    return result


# =============================================================================
# DUST & RECONCILIATION API
# =============================================================================

@app.get("/api/dust")
async def get_dust_positions():
    """Get dust positions (too small to sell on exchange)."""
    from core.shared_state import read_state
    shared = read_state()
    
    if not shared:
        return {"dust": [], "total_value": 0, "count": 0}
    
    dust = shared.get('dust_positions', [])
    total_value = sum(d.get('value_usd', 0) for d in dust)
    
    return {
        "dust": dust,
        "total_value": round(total_value, 2),
        "count": len(dust),
        "note": "Dust positions are below exchange minimum and cannot be sold via API"
    }


@app.get("/api/reconciliation")
async def get_reconciliation_status():
    """Get position reconciliation status (registry vs exchange)."""
    from core.shared_state import read_state
    shared = read_state()
    
    if not shared:
        return {"status": "offline", "registry_count": 0, "exchange_count": 0}
    
    positions = shared.get('positions', [])
    exchange_holdings = shared.get('exchange_holdings', {})
    dust = shared.get('dust_positions', [])
    
    registry_symbols = set(p.get('symbol') for p in positions)
    exchange_symbols = set(exchange_holdings.keys()) if isinstance(exchange_holdings, dict) else set()
    dust_symbols = set(d.get('symbol') for d in dust)
    
    # Find mismatches
    in_registry_not_exchange = registry_symbols - exchange_symbols - dust_symbols
    in_exchange_not_registry = exchange_symbols - registry_symbols - dust_symbols
    
    status = "synced" if not in_registry_not_exchange and not in_exchange_not_registry else "mismatch"
    
    return {
        "status": status,
        "registry_count": len(positions),
        "exchange_count": len(exchange_symbols),
        "dust_count": len(dust),
        "mismatches": {
            "in_registry_not_exchange": list(in_registry_not_exchange),
            "in_exchange_not_registry": list(in_exchange_not_registry),
        },
        "max_positions": shared.get('max_positions', 15),
        "can_open_new": len(positions) < shared.get('max_positions', 15),
    }


# =============================================================================
# CONFIGURATION MANAGEMENT API
# =============================================================================

class ConfigUpdate(BaseModel):
    """Model for config update request."""
    updates: dict


@app.get("/api/config")
async def get_config():
    """Get all runtime configuration parameters."""
    config = _config_manager.get_config()
    snapshots = {}
    try:
        from core.shared_state import read_state
        shared = read_state()
        if shared:
            snapshots = {
                "config_start": sanitize_config_snapshot(shared.get("config_start", {})),
                "config_running": sanitize_config_snapshot(shared.get("config_running", {})),
                "config_last_refreshed": shared.get("config_last_refreshed"),
            }
        elif _bot_state:
            snapshots = _get_config_snapshots_from_state(_bot_state)
    except Exception:
        snapshots = {}
    return {
        "config": config.to_dict(),
        "pause_new_entries": config.pause_new_entries,
        "snapshots": snapshots,
    }


@app.post("/api/config/update")
async def update_config(body: ConfigUpdate):
    """Update multiple configuration parameters."""
    result = _config_manager.update_params(body.updates, source="web")
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("CONFIG", f"Updated {len(body.updates)} params")
    return result


@app.post("/api/config/param")
async def update_single_param(
    param: str = Query(..., description="Parameter name"),
    value: float = Query(..., description="New value")
):
    """Update a single configuration parameter."""
    result = _config_manager.update_param(param, value, source="web")
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("CONFIG", f"Updated {param} = {value}")
    return result


@app.post("/api/config/pause-entries")
async def pause_entries(paused: bool = Query(..., description="Pause new entries")):
    """Pause or resume new entries."""
    result = _config_manager.set_pause_new_entries(paused, source="web")
    add_log("CONFIG", f"Entries {'PAUSED' if paused else 'RESUMED'}")
    return result


@app.post("/api/config/reset")
async def reset_config():
    """Reset configuration to defaults."""
    result = _config_manager.reset_to_defaults(source="web")
    add_log("CONFIG", "Reset to defaults")
    return result


@app.post("/api/config/refresh")
async def refresh_config():
    """Reload runtime configuration from disk (force)."""
    reloaded = _config_manager.reload_from_disk(force=True)
    add_log("CONFIG", "Reloaded from disk" if reloaded else "Refresh requested")
    return {
        "success": True,
        "reloaded": reloaded,
        "config": _config_manager.get_config().to_dict(),
    }


@app.get("/api/config/audit")
async def get_config_audit(limit: int = Query(default=50)):
    """Get configuration audit log."""
    return {"audit": _config_manager.get_audit_log(limit)}


# =============================================================================
# STRATEGY MANAGEMENT API
# =============================================================================

@app.get("/api/strategies")
async def get_strategies():
    """Get all strategies with their status and stats."""
    return _strategy_registry.to_dict()


@app.get("/api/strategy/{name}")
async def get_strategy(name: str):
    """Get a single strategy's config and stats."""
    strategy = _strategy_registry.get_strategy(name)
    if strategy is None:
        return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {name}"})
    return strategy.to_dict()


@app.post("/api/strategy/{name}/toggle")
async def toggle_strategy(name: str):
    """Toggle a strategy's enabled state."""
    result = _strategy_registry.toggle(name)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    add_log("STRATEGY", f"Strategy {name} {'enabled' if result.get('enabled') else 'disabled'}")
    return result


@app.post("/api/strategy/{name}/enable")
async def enable_strategy(name: str, enabled: bool = Query(...)):
    """Set a strategy's enabled state explicitly."""
    result = _strategy_registry.set_enabled(name, enabled)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/strategy/{name}/priority")
async def set_strategy_priority(name: str, priority: int = Query(..., ge=1, le=100)):
    """Update a strategy's priority (1-100)."""
    result = _strategy_registry.update_priority(name, priority)
    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/strategies/enable-all")
async def enable_all_strategies():
    """Enable all strategies."""
    result = _strategy_registry.enable_all()
    add_log("STRATEGY", "All strategies enabled")
    return result


@app.post("/api/strategies/disable-all")
async def disable_all_strategies():
    """Disable all strategies."""
    result = _strategy_registry.disable_all()
    add_log("STRATEGY", "All strategies disabled")
    return result


@app.get("/api/strategies/stats")
async def get_all_strategy_stats():
    """Get performance stats for all strategies."""
    return {"stats": _strategy_registry.get_all_stats()}


@app.post("/api/strategies/reset-stats")
async def reset_strategy_stats(name: str = Query(default=None)):
    """Reset stats for a strategy or all strategies."""
    result = _strategy_registry.reset_stats(name)
    return result


# =============================================================================
# UNIVERSE / SYMBOL MANAGEMENT API
# =============================================================================

# Symbol blacklist (runtime only)
_symbol_blacklist: set = set()


@app.get("/api/universe")
async def get_universe():
    """Get current universe status."""
    if _scanner is None:
        return {"error": "Scanner not initialized", "symbols": []}
    
    eligible = _scanner.get_eligible_symbols() if hasattr(_scanner, 'get_eligible_symbols') else []
    
    return {
        "eligible_count": len(eligible),
        "streaming_count": getattr(_bot_state.universe, 'symbols_streaming', 0) if _bot_state else 0,
        "warm_count": _bot_state.warm_symbols if _bot_state else 0,
        "cold_count": _bot_state.cold_symbols if _bot_state else 0,
        "blacklist": list(_symbol_blacklist),
        "symbols": eligible[:50],  # Top 50
    }


@app.post("/api/universe/refresh")
async def refresh_universe():
    """Force a universe refresh."""
    if _scanner is None:
        return JSONResponse(status_code=400, content={"error": "Scanner not initialized"})
    
    try:
        await _scanner.refresh_universe()
        add_log("UNIVERSE", "Universe refreshed")
        return {"success": True, "message": "Universe refreshed"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/universe/blacklist")
async def blacklist_symbol(symbol: str = Query(...), remove: bool = Query(default=False)):
    """Add or remove a symbol from the blacklist."""
    if not symbol.endswith("-USD"):
        symbol = f"{symbol}-USD"
    
    if remove:
        _symbol_blacklist.discard(symbol)
        add_log("UNIVERSE", f"Removed {symbol} from blacklist")
        return {"success": True, "action": "removed", "symbol": symbol}
    else:
        _symbol_blacklist.add(symbol)
        add_log("UNIVERSE", f"Added {symbol} to blacklist")
        return {"success": True, "action": "added", "symbol": symbol}


@app.get("/api/universe/blacklist")
async def get_blacklist():
    """Get current blacklist."""
    return {"blacklist": list(_symbol_blacklist)}


def is_symbol_blacklisted(symbol: str) -> bool:
    """Check if a symbol is blacklisted (called by bot)."""
    return symbol in _symbol_blacklist or symbol.replace("-USD", "") in _symbol_blacklist


# =============================================================================
# LOGS API
# =============================================================================

@app.get("/api/logs")
async def get_logs(limit: int = Query(default=100), level: str = Query(default=None)):
    """Get recent log entries."""
    logs = list(_live_logs)
    
    # Filter by level if specified
    if level:
        logs = [l for l in logs if l.get("level", "").upper() == level.upper()]
    
    return {"logs": logs[-limit:]}


@app.get("/api/logs/stream")
async def get_log_stream():
    """Get logs for real-time streaming (returns all since cleared)."""
    return {"logs": list(_live_logs)}


# =============================================================================
# ANALYTICS API
# =============================================================================

@app.get("/api/analytics/summary")
async def get_analytics_summary():
    """Get analytics summary."""
    if _bot_state is None:
        return {"error": "Bot not connected"}
    
    s = _bot_state
    
    return {
        "portfolio": {
            "value": s.portfolio_value,
            "cash": s.cash_balance,
            "holdings": s.holdings_value,
        },
        "performance": {
            "trades_today": s.trades_today,
            "wins": s.wins_today,
            "losses": s.losses_today,
            "win_rate": s.win_rate * 100,
            "profit_factor": s.profit_factor,
            "avg_win": s.avg_win,
            "avg_loss": s.avg_loss,
            "biggest_win": s.biggest_win,
            "biggest_loss": s.biggest_loss,
            "max_drawdown": s.max_drawdown,
            "daily_pnl": s.daily_pnl,
            "realized_pnl": getattr(s, 'realized_pnl', 0.0),
        },
        "exposure": {
            "budget_usd": s.bot_budget_usd,
            "exposure_usd": s.bot_exposure_usd,
            "available_usd": s.bot_available_usd,
            "exposure_pct": s.exposure_pct,
            "position_count": len(_position_controller.get_positions()),
        },
        "activity": {
            "ticks_5s": s.ticks_last_5s,
            "candles_5s": s.candles_last_5s,
            "rest_requests": s.rest_requests,
            "rest_429s": s.rest_429s,
        },
        "rejections": {
            "spread": s.rejections_spread,
            "warmth": s.rejections_warmth,
            "regime": s.rejections_regime,
            "score": s.rejections_score,
            "rr": s.rejections_rr,
            "limits": s.rejections_limits,
        },
    }


@app.get("/api/analytics/equity-history")
async def get_equity_history():
    """Get equity curve data (placeholder - needs historical tracking)."""
    # TODO: Implement equity history tracking
    return {"history": [], "message": "Equity history tracking not yet implemented"}


# =============================================================================
# AUDIT TRAIL API
# =============================================================================

@app.get("/api/audit/positions")
async def get_position_audit():
    """Get position action audit log."""
    return {"audit": _position_controller.get_audit_log()}


# =============================================================================
# LIVE PORTFOLIO API - Direct Coinbase Integration
# =============================================================================

_coinbase_client = None
_portfolio_uuid = None

def _get_coinbase_client():
    """Get or create Coinbase REST client."""
    global _coinbase_client
    if _coinbase_client is not None:
        return _coinbase_client
    
    try:
        from coinbase.rest import RESTClient
        from core.config import settings
        
        if not settings.coinbase_api_key or not settings.coinbase_api_secret:
            return None
        
        _coinbase_client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret
        )
        return _coinbase_client
    except Exception as e:
        print(f"[WEB] Failed to init Coinbase client: {e}")
        return None


def _get_portfolio_uuid():
    """Get the default portfolio UUID."""
    global _portfolio_uuid
    if _portfolio_uuid:
        return _portfolio_uuid
    
    client = _get_coinbase_client()
    if not client:
        return None
    
    try:
        portfolios = client.get_portfolios()
        for p in getattr(portfolios, 'portfolios', []):
            p_type = p.get('type') if isinstance(p, dict) else getattr(p, 'type', '')
            p_uuid = p.get('uuid') if isinstance(p, dict) else getattr(p, 'uuid', '')
            if p_type == 'DEFAULT' and p_uuid:
                _portfolio_uuid = p_uuid
                return _portfolio_uuid
        # Fallback to first portfolio
        if portfolios.portfolios:
            first = portfolios.portfolios[0]
            _portfolio_uuid = first.get('uuid') if isinstance(first, dict) else getattr(first, 'uuid', '')
            return _portfolio_uuid
    except Exception as e:
        print(f"[WEB] Failed to get portfolio UUID: {e}")
    return None


@app.get("/api/portfolio/live")
async def get_live_portfolio():
    """
    Fetch real portfolio data directly from Coinbase API.
    Returns balances, holdings with cost basis, entry price, and real PnL.
    """
    client = _get_coinbase_client()
    if not client:
        return JSONResponse(
            status_code=503,
            content={"error": "Coinbase client not initialized. Check API keys.", "connected": False}
        )
    
    try:
        portfolio_uuid = _get_portfolio_uuid()
        
        # Get portfolio breakdown for accurate balances and positions
        cash_balance = 0.0
        crypto_balance = 0.0
        total_balance = 0.0
        total_unrealized_pnl = 0.0
        holdings = []
        
        if portfolio_uuid:
            try:
                breakdown = client.get_portfolio_breakdown(portfolio_uuid)
                if breakdown and hasattr(breakdown, 'breakdown'):
                    pb = breakdown.breakdown
                    data = pb.to_dict() if hasattr(pb, 'to_dict') else pb
                    
                    # Get balances
                    balances = data.get('portfolio_balances', {})
                    cash_bal = balances.get('total_cash_equivalent_balance', {})
                    crypto_bal = balances.get('total_crypto_balance', {})
                    cash_balance = float(cash_bal.get('value', 0) if isinstance(cash_bal, dict) else 0)
                    crypto_balance = float(crypto_bal.get('value', 0) if isinstance(crypto_bal, dict) else 0)
                    total_balance = cash_balance + crypto_balance
                    
                    # Get spot positions with full details
                    spot_positions = data.get('spot_positions', [])
                    for pos in spot_positions:
                        asset = pos.get('asset', '')
                        value_usd = float(pos.get('total_balance_fiat', 0))
                        
                        # Skip cash and delisted coins
                        DELISTED = {'USD', 'USDC', 'CLV', 'NU', 'BOND', 'SNX', 'MANA', 'CGLD'}
                        if asset in DELISTED:
                            continue
                        if value_usd < 0.005:  # Only skip truly dust positions
                            continue
                        
                        # Get cost basis and entry price
                        cost_basis_data = pos.get('cost_basis', {})
                        cost_basis = float(cost_basis_data.get('value', 0) if isinstance(cost_basis_data, dict) else 0)
                        
                        entry_price_data = pos.get('average_entry_price', {})
                        entry_price = float(entry_price_data.get('value', 0) if isinstance(entry_price_data, dict) else 0)
                        
                        unrealized_pnl = float(pos.get('unrealized_pnl', 0))
                        total_unrealized_pnl += unrealized_pnl
                        
                        quantity = float(pos.get('total_balance_crypto', 0))
                        allocation = float(pos.get('allocation', 0)) * 100  # Convert to %
                        
                        # Calculate current price from value/quantity
                        current_price = value_usd / quantity if quantity > 0 else 0
                        
                        # Calculate PnL %
                        pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis > 0 else 0
                        
                        # Get 24h change
                        try:
                            product = client.get_product(f"{asset}-USD")
                            price_change_24h = float(getattr(product, "price_percentage_change_24h", 0))
                        except:
                            price_change_24h = 0.0
                        
                        # Get available to trade
                        available_fiat = float(pos.get('available_to_trade_fiat', 0))
                        available_crypto = float(pos.get('available_to_trade_crypto', 0))
                        
                        holdings.append({
                            "symbol": f"{asset}-USD",
                            "currency": asset,
                            "quantity": quantity,
                            "price": current_price,
                            "value_usd": value_usd,
                            "available_usd": available_fiat,
                            "available_qty": available_crypto,
                            "cost_basis": cost_basis,
                            "entry_price": entry_price,
                            "unrealized_pnl": unrealized_pnl,
                            "pnl_pct": pnl_pct,
                            "allocation": allocation,
                            "price_change_24h": price_change_24h,
                            "account_type": pos.get('account_type', 'WALLET'),
                            "is_staked": pos.get('account_type') == 'ACCOUNT_TYPE_STAKED_FUNDS',
                        })
                    
            except Exception as e:
                print(f"[WEB] Portfolio breakdown failed: {e}")
                import traceback
                traceback.print_exc()
        
        # Fallback to basic accounts if breakdown failed
        if not holdings:
            accounts = client.get_accounts()
            for acct in getattr(accounts, "accounts", []):
                currency = getattr(acct, "currency", "")
                bal = getattr(acct, "available_balance", {})
                value = float(bal.get("value", 0) if isinstance(bal, dict) else getattr(bal, "value", 0))
                
                if currency in ("USD", "USDC"):
                    if cash_balance == 0:
                        cash_balance += value
                    continue
                
                if value < 0.0001:
                    continue
                
                symbol = f"{currency}-USD"
                try:
                    product = client.get_product(symbol)
                    price = float(getattr(product, "price", 0))
                    price_change_24h = float(getattr(product, "price_percentage_change_24h", 0))
                except:
                    price = 0.0
                    price_change_24h = 0.0
                
                if price > 0:
                    position_value = value * price
                    if position_value >= 0.50:
                        holdings.append({
                            "symbol": symbol,
                            "currency": currency,
                            "quantity": value,
                            "price": price,
                            "value_usd": position_value,
                            "cost_basis": 0,
                            "entry_price": 0,
                            "unrealized_pnl": 0,
                            "pnl_pct": 0,
                            "allocation": 0,
                            "price_change_24h": price_change_24h,
                            "account_type": "WALLET",
                            "is_staked": False,
                        })
        
        # Sort by value descending
        holdings.sort(key=lambda x: x["value_usd"], reverse=True)
        
        # Calculate totals if not set
        if total_balance == 0:
            crypto_balance = sum(h["value_usd"] for h in holdings)
            total_balance = cash_balance + crypto_balance
        
        return {
            "connected": True,
            "portfolio_uuid": portfolio_uuid,
            "cash_balance": cash_balance,
            "crypto_balance": crypto_balance,
            "total_balance": total_balance,
            "total_unrealized_pnl": total_unrealized_pnl,
            "holdings_count": len(holdings),
            "holdings": holdings,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to fetch portfolio: {str(e)}", "connected": False}
        )


@app.get("/api/portfolio/price/{symbol}")
async def get_live_price(symbol: str):
    """Get live price for a symbol from Coinbase."""
    client = _get_coinbase_client()
    if not client:
        return JSONResponse(status_code=503, content={"error": "Coinbase client not initialized"})
    
    if not symbol.endswith("-USD"):
        symbol = f"{symbol}-USD"
    
    try:
        product = client.get_product(symbol)
        return {
            "symbol": symbol,
            "price": float(getattr(product, "price", 0)),
            "price_change_24h": float(getattr(product, "price_percentage_change_24h", 0)),
            "volume_24h": float(getattr(product, "volume_24h", 0)),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return JSONResponse(status_code=404, content={"error": f"Failed to get price for {symbol}: {e}"})


# =============================================================================
# AI-READABLE SNAPSHOT ENDPOINT
# =============================================================================

@app.get("/api/snapshot")
async def get_ai_snapshot():
    """
    AI-readable snapshot of entire system state.
    Designed to be copy-pasted to an AI for debugging.
    """
    from core.shared_state import read_state
    from core.config import settings
    
    shared = read_state() or {}
    positions = shared.get('positions', [])
    signals = shared.get('recent_signals', [])
    
    # Calculate invariants
    cash = shared.get('cash_balance', 0)
    invested = shared.get('holdings_value', 0)
    equity = shared.get('portfolio_value', 0)
    
    # Build snapshot
    snapshot = {
        "meta": {
            "app": "CoinTrader Terminal",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "environment": shared.get('profile', 'unknown'),
            "trade_mode": shared.get('mode', 'unknown').upper(),
            "service_state": shared.get('status', 'unknown'),
            "connection": {
                "ws_ok": shared.get('ws_ok', False),
                "streaming": shared.get('symbols_streaming', 0),
                "warm": shared.get('warm_symbols', 0),
            }
        },
        "portfolio": {
            "currency": "USD",
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "available": round(shared.get('available_budget', 0), 2),
            "pnl_today": round(shared.get('daily_pnl', 0), 2),
            "pnl_today_pct": round(shared.get('daily_pnl', 0) / equity * 100, 2) if equity > 0 else 0,
            "unrealized": round(shared.get('unrealized_pnl', 0), 2),
            "realized_today": round(shared.get('realized_today', 0), 2),
            "win_rate": {
                "pct": round(shared.get('engine', {}).get('win_rate', 0), 2),
                "wins": shared.get('engine', {}).get('wins_today', 0),
                "losses": shared.get('engine', {}).get('losses_today', 0),
            }
        },
        "risk": {
            "max_exposure_pct": settings.portfolio_max_exposure_pct * 100,
            "daily_loss_limit_usd": settings.daily_max_loss_usd,
            "max_positions": settings.max_positions,
            "entry_filters": {
                "min_score": settings.entry_score_min,
                "max_spread_bps": settings.spread_max_bps,
                "min_rr": settings.min_rr_ratio,
            },
            "stops": {
                "fixed_stop_pct": settings.fixed_stop_pct,
                "tp1_pct": settings.tp1_pct,
                "tp2_pct": settings.tp2_pct,
            }
        },
        "positions": {
            "count": len(positions),
            "max_allowed": settings.max_positions,
            "exposed_pct": round(invested / equity * 100, 1) if equity > 0 else 0,
            "regime": shared.get('btc_regime', 'normal'),
            "summary": {
                "green": len([p for p in positions if p.get('pnl_usd', 0) > 0]),
                "red": len([p for p in positions if p.get('pnl_usd', 0) <= 0]),
                "best": max(positions, key=lambda p: p.get('pnl_pct', 0))['symbol'].replace('-USD', '') if positions else None,
                "worst": min(positions, key=lambda p: p.get('pnl_pct', 0))['symbol'].replace('-USD', '') if positions else None,
            },
            "rows": [
                {
                    "symbol": p['symbol'].replace('-USD', ''),
                    "value_usd": round(p.get('size_usd', 0), 2),
                    "pnl_usd": round(p.get('pnl_usd', 0), 2),
                    "pnl_pct": round(p.get('pnl_pct', 0), 2),
                    "stop": round(p.get('stop_price', 0), 4),
                    "age_min": p.get('age_min', 0),
                }
                for p in sorted(positions, key=lambda x: x.get('pnl_pct', 0))[:10]  # Worst 10
            ]
        },
        "signals_recent": [
            {
                "time": s.get('ts', ''),
                "symbol": s.get('symbol', '').replace('-USD', ''),
                "strategy": s.get('strategy', ''),
                "score": s.get('score', 0),
                "spread_bps": round(s.get('spread_bps', 0), 1),
                "status": "TAKEN" if s.get('taken') else "BLOCKED",
                "reason": s.get('reason', s.get('block_reason', '')),
            }
            for s in signals[:10]
        ],
        "invariants": {
            "cash_plus_invested_equals_equity": abs((cash + invested) - equity) < 1.0,
            "positions_count_leq_max": len(positions) <= settings.max_positions,
            "exposure_leq_max": (invested / equity * 100 if equity > 0 else 0) <= settings.portfolio_max_exposure_pct * 100,
        },
        "bugs_detected": []
    }
    
    # Detect bugs automatically
    if not snapshot["invariants"]["positions_count_leq_max"]:
        snapshot["bugs_detected"].append(f"Position count ({len(positions)}) exceeds max ({settings.max_positions})")
    if not snapshot["invariants"]["exposure_leq_max"]:
        snapshot["bugs_detected"].append(f"Exposure exceeds max allowed")
    
    return snapshot


# Serve static files
import os
_web_dir = os.path.join(os.path.dirname(__file__), "web")

@app.get("/")
async def serve_index():
    """Serve the dashboard."""
    return FileResponse(os.path.join(_web_dir, "index.html"))


# =============================================================================
# ORDER HISTORY API - All orders from Coinbase
# =============================================================================

@app.get("/api/orders/history")
async def get_order_history(limit: int = 50):
    """Get order history from Coinbase (bot + manual orders)."""
    client = _get_coinbase_client()
    if not client:
        return {"connected": False, "orders": [], "error": "Not connected to Coinbase"}
    
    try:
        # Get all orders (filled, cancelled, etc.)
        orders_resp = client.list_orders(limit=limit, order_status=["FILLED", "CANCELLED", "EXPIRED"])
        
        orders = []
        for order in getattr(orders_resp, 'orders', []):
            order_dict = order.to_dict() if hasattr(order, 'to_dict') else order
            
            # Parse order data
            product_id = order_dict.get('product_id', '')
            side = order_dict.get('side', '')
            status = order_dict.get('status', '')
            
            # Get filled size and price
            filled_size = float(order_dict.get('filled_size', 0))
            avg_price = float(order_dict.get('average_filled_price', 0))
            total_value = filled_size * avg_price if avg_price else 0
            
            # Get order config for limit price
            order_config = order_dict.get('order_configuration', {})
            limit_price = 0
            if 'limit_limit_gtc' in order_config:
                limit_price = float(order_config['limit_limit_gtc'].get('limit_price', 0))
            elif 'limit_limit_gtd' in order_config:
                limit_price = float(order_config['limit_limit_gtd'].get('limit_price', 0))
            
            # Determine if bot order or manual
            # Bot orders use prefixes: ct_, stop_, CT_, bot_
            client_order_id = order_dict.get('client_order_id', '')
            is_bot_order = (
                client_order_id.startswith('ct_') or 
                client_order_id.startswith('stop_') or
                client_order_id.startswith('CT_') or 
                client_order_id.startswith('bot_')
            )
            
            orders.append({
                "order_id": order_dict.get('order_id', ''),
                "product_id": product_id,
                "symbol": product_id.replace('-USD', ''),
                "side": side,
                "status": status,
                "filled_size": filled_size,
                "avg_price": avg_price,
                "limit_price": limit_price,
                "total_value": total_value,
                "created_at": order_dict.get('created_time', ''),
                "completed_at": order_dict.get('last_fill_time', ''),
                "is_bot_order": is_bot_order,
                "source": "Bot" if is_bot_order else "Manual",
            })
        
        return {
            "connected": True,
            "orders": orders,
            "count": len(orders),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        
    except Exception as e:
        return {"connected": False, "orders": [], "error": str(e)}


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (blocking)."""
    uvicorn.run(app, host=host, port=port, log_level="warning")


async def run_server_async(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server as async task."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
