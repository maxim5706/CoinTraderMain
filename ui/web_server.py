"""
FastAPI WebSocket server for real-time bot state streaming.
Runs alongside the trading bot to provide web dashboard access.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from core.state import BotState

app = FastAPI(title="CoinTrader Dashboard API")

# Shared state reference (set by bot on startup)
_bot_state: Optional[BotState] = None
_connected_clients: set[WebSocket] = set()


def set_bot_state(state: BotState):
    """Called by bot to share its state with the web server."""
    global _bot_state
    _bot_state = state


def _heartbeat_age(ts: Optional[datetime]) -> float:
    """Get age in seconds of a heartbeat timestamp."""
    if ts is None:
        return 999.0
    return (datetime.now(timezone.utc) - ts).total_seconds()


def get_state_snapshot() -> dict:
    """Get serializable snapshot of bot state."""
    if _bot_state is None:
        return {"error": "Bot not connected", "ts": datetime.now(timezone.utc).isoformat()}
    
    state = _bot_state
    
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
    
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": state.mode,
        "profile": getattr(state, 'profile', 'prod'),
        
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
        
        # Focus coin
        "focus_coin": {
            "symbol": state.focus_coin.symbol,
            "price": state.focus_coin.price,
            "trend_5m": state.focus_coin.trend_5m,
            "stage": state.focus_coin.stage,
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
    }


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


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "bot_connected": _bot_state is not None,
        "clients": len(_connected_clients),
    }


@app.post("/api/kill")
async def toggle_kill_switch():
    """Toggle the kill switch."""
    if _bot_state is None:
        return {"error": "Bot not connected", "kill_switch": False}
    
    _bot_state.kill_switch = not _bot_state.kill_switch
    _bot_state.kill_reason = "web_dashboard" if _bot_state.kill_switch else ""
    
    return {
        "success": True,
        "kill_switch": _bot_state.kill_switch,
        "message": "Kill switch ENABLED" if _bot_state.kill_switch else "Kill switch DISABLED"
    }


@app.get("/api/kill")
async def get_kill_switch():
    """Get kill switch status."""
    if _bot_state is None:
        return {"kill_switch": False, "reason": ""}
    return {
        "kill_switch": _bot_state.kill_switch,
        "reason": _bot_state.kill_reason
    }


# Serve static files
import os
_web_dir = os.path.join(os.path.dirname(__file__), "web")

@app.get("/")
async def serve_index():
    """Serve the dashboard."""
    return FileResponse(os.path.join(_web_dir, "index.html"))


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (blocking)."""
    uvicorn.run(app, host=host, port=port, log_level="warning")


async def run_server_async(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server as async task."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
