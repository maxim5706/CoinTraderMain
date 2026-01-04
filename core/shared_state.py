"""
Shared state between bot process and dashboard via JSON file.
Bot writes state every 500ms, dashboard reads it.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading
import time

STATE_FILE = Path(__file__).parent.parent / "data" / "bot_state.json"
STATE_FILE.parent.mkdir(exist_ok=True)

_write_lock = threading.Lock()
_last_write_error_at: float = 0.0
_write_error_throttle_sec: float = 5.0


def _to_jsonable(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        try:
            return _to_jsonable(vars(value))
        except Exception:
            return str(value)
    return str(value)


def write_state(state) -> bool:
    """Write bot state to shared file (called by bot)."""
    try:
        with _write_lock:
            if isinstance(state, dict):
                snapshot = state
                if "ts" not in snapshot:
                    snapshot = dict(snapshot)
                    snapshot["ts"] = datetime.now(timezone.utc).isoformat()
            else:
                snapshot = _serialize_state(state)
            snapshot = _to_jsonable(snapshot)
            # Atomic write
            temp_file = STATE_FILE.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(snapshot, f)
            temp_file.replace(STATE_FILE)
        return True
    except Exception as e:
        global _last_write_error_at
        now = time.time()
        if now - _last_write_error_at >= _write_error_throttle_sec:
            _last_write_error_at = now
            print(f"[SHARED] Failed to write state: {e}")
        return False


def read_state() -> Optional[dict]:
    """Read bot state from shared file (called by dashboard)."""
    # Retry up to 5 times with increasing delays to handle race conditions
    max_retries = 5
    for attempt in range(max_retries):
        try:
            if not STATE_FILE.exists():
                return None
            with open(STATE_FILE, 'r') as f:
                content = f.read()
            if not content.strip():
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                return None
            data = json.loads(content)
            # Check if stale (> 5 seconds old)
            ts = data.get('ts')
            if ts:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                data['state_age'] = age
                data['state_fresh'] = age < 10
            return data
        except (json.JSONDecodeError, ValueError):
            # Race condition with writer - retry after pause (no logging to reduce spam)
            if attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    return None


# Command file for dashboard -> bot communication
COMMAND_FILE = Path(__file__).parent.parent / "data" / "bot_commands.json"


def write_command(command: str, data: dict = None) -> bool:
    """Write a command for the bot to pick up (dashboard -> bot)."""
    try:
        commands = []
        if COMMAND_FILE.exists():
            try:
                with open(COMMAND_FILE, 'r') as f:
                    commands = json.load(f)
            except:
                commands = []
        
        commands.append({
            'ts': datetime.now(timezone.utc).isoformat(),
            'command': command,
            'data': data or {}
        })
        
        # Keep only last 100 commands
        commands = commands[-100:]
        
        with open(COMMAND_FILE, 'w') as f:
            json.dump(commands, f)
        return True
    except Exception as e:
        print(f"[SHARED] Failed to write command: {e}")
        return False


def read_commands() -> list:
    """Read pending commands (called by bot)."""
    try:
        if not COMMAND_FILE.exists():
            return []
        with open(COMMAND_FILE, 'r') as f:
            return json.load(f)
    except:
        return []


def clear_commands():
    """Clear processed commands."""
    try:
        with open(COMMAND_FILE, 'w') as f:
            json.dump([], f)
    except:
        pass


def _serialize_focus_coin(state) -> dict:
    """Serialize focus coin with trend indicators from intelligence."""
    fc = state.focus_coin
    symbol = fc.symbol
    
    # Get trend indicators from intelligence if available
    trend_1m = 0.0
    trend_5m = fc.trend_5m
    trend_1h = 0.0
    trend_1d = 0.0
    acceleration = 0.0
    whale_bias = 0.0
    
    if symbol:
        try:
            from logic.intelligence import intelligence
            ind = intelligence.get_live_indicators(symbol)
            if ind:
                trend_1m = getattr(ind, 'price_change_1m', 0.0)
                trend_1h = getattr(ind, 'trend_1h', 0.0)
                trend_1d = getattr(ind, 'trend_1d', 0.0)
                acceleration = getattr(ind, 'acceleration_score', 0.0)
                whale_bias = getattr(ind, 'whale_bias', 0.0)
        except Exception:
            pass
    
    return {
        "symbol": symbol,
        "price": fc.price,
        "trend_5m": trend_5m,
        "trend_1m": trend_1m,
        "trend_1h": trend_1h,
        "trend_1d": trend_1d,
        "stage": fc.stage,
        "vol_spike": fc.vol_spike,
        "acceleration": acceleration,
        "whale_bias": whale_bias,
    }


def _serialize_state(state) -> dict:
    """Convert BotState to serializable dict."""
    
    # Build positions list
    positions = []
    for pos in getattr(state, 'positions', []):
        positions.append({
            "symbol": pos.symbol,
            "entry_price": pos.entry_price,
            "current_price": getattr(pos, 'current_price', pos.entry_price),
            "size_usd": pos.size_usd,
            "pnl_usd": getattr(pos, 'pnl_usd', getattr(pos, 'unrealized_pnl', 0.0)),
            "pnl_pct": getattr(pos, 'pnl_pct', getattr(pos, 'unrealized_pct', 0.0)),
            "stop_price": getattr(pos, 'stop_price', 0.0),
            "tp1_price": getattr(pos, 'tp1_price', 0.0),
            "tp2_price": getattr(pos, 'tp2_price', 0.0),
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
    
    # Build recent signals
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
            signals.append({
                "ts": sig[0].isoformat() if hasattr(sig[0], 'isoformat') else str(sig[0]),
                "symbol": sig[1],
                "strategy": sig[2],
                "score": sig[3],
                "spread_bps": sig[4],
                "taken": sig[5],
                "reason": sig[6],
            })

    # Build gate traces (last decision per symbol)
    gate_traces = []
    for trace in getattr(state, 'last_gate_trace_by_symbol', {}).values():
        gate_traces.append(trace)
    try:
        gate_traces = sorted(
            gate_traces,
            key=lambda x: x.get("ts", ""),
            reverse=True
        )[:50]
    except Exception:
        pass

    def _heartbeat_age(ts):
        if ts is None:
            return 999.0
        return (datetime.now(timezone.utc) - ts).total_seconds()
    
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": state.mode,
        "status": "running",  # Bot is running if StateWriter is active
        "phase": getattr(state, 'phase', 'trading'),  # init/preflight/syncing/backfill/trading
        "profile": getattr(state, 'profile', 'prod'),
        "config_start": getattr(state, 'config_start', {}),
        "config_running": getattr(state, 'config_running', {}),
        "config_last_refreshed": getattr(state, 'config_last_refreshed', None),
        
        # Portfolio
        "portfolio_value": state.portfolio_value,
        "cash_balance": state.cash_balance,
        "holdings_value": state.holdings_value,
        "daily_pnl": state.daily_pnl,
        "unrealized_pnl": getattr(state, 'unrealized_pnl', 0.0),
        
        # Portfolio history (1h/1d/5d changes)
        "portfolio_change_1h": getattr(state, 'portfolio_change_1h', None),
        "portfolio_change_1d": getattr(state, 'portfolio_change_1d', None),
        "portfolio_change_5d": getattr(state, 'portfolio_change_5d', None),
        "portfolio_ath": getattr(state, 'portfolio_ath', 0.0),
        
        # Health
        "ws_ok": state.ws_ok,
        "ws_last_age": state.ws_last_age,
        "api_ok": state.api_ok,
        "btc_regime": state.btc_regime,
        "btc_trend_1h": getattr(state, 'btc_trend_1h', 0.0),
        "rest_rate_degraded": getattr(state, "rest_rate_degraded", False),
        
        # Sector rotation
        "sectors": getattr(state, 'sector_summary', {}),
        
        # Universe
        "warm_symbols": state.warm_symbols,
        "cold_symbols": state.cold_symbols,
        "symbols_streaming": getattr(state.universe, 'symbols_streaming', 0),
        
        # Coverage summary (derived from warm/cold for dashboard display)
        "coverage_summary": {
            "ok": state.warm_symbols,
            "stale": state.cold_symbols,
            "missing": 0,
        },
        
        # Rejections
        "rejections": {
            "spread": state.rejections_spread,
            "warmth": state.rejections_warmth,
            "regime": state.rejections_regime,
            "score": state.rejections_score,
            "rr": state.rejections_rr,
            "limits": state.rejections_limits,
        },
        
        # Focus coin - include trend indicators from intelligence
        "focus_coin": _serialize_focus_coin(state),
        
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
        "gate_traces": gate_traces,
        
        # Kill switch
        "kill_switch": state.kill_switch,
        
        # Dust positions and exchange holdings (for reconciliation)
        "dust_positions": getattr(state, 'dust_positions', []),
        "exchange_holdings": getattr(state, 'exchange_holdings', {}),
        "max_positions": getattr(state, 'max_positions', 15),
        
        # Engine stats
        "engine": {
            "uptime_seconds": (datetime.now(timezone.utc) - state.startup_time).total_seconds() if state.startup_time else 0,
            "trades_today": state.trades_today,
            "wins_today": state.wins_today,
            "losses_today": state.losses_today,
            "realized_pnl_today": getattr(state, 'realized_pnl', 0.0),
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
        
        # Heartbeats
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


class StateWriter:
    """Background thread that writes state every 500ms."""
    
    def __init__(self, state):
        self.state = state
        self._running = False
        self._thread = None
    
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._running = False
    
    def _run(self):
        while self._running:
            write_state(self.state)
            time.sleep(0.5)
