"""
ARGENT - Autonomous Trading AI Assistant
=========================================
"Argent" means money in French. This AI manages your portfolio when you're away.

Capabilities:
- Real-time portfolio analysis
- Aggressive but calculated trading decisions
- Position management (close losers, lock profits)
- Market insight integration
- Learning from past decisions
- Safe autonomous actions within defined limits

Permissions (Safe):
- READ: All portfolio data, positions, market feeds
- ANALYZE: Calculate P&L, risk metrics, opportunities
- RECOMMEND: Suggest specific actions with reasoning
- EXECUTE: Close positions, adjust stops (with limits)

Limits (Safety):
- Max single trade: $50
- Max daily loss: $25
- Cannot increase position sizes autonomously
- All actions logged for review
"""

import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from core.logging_utils import get_logger

logger = get_logger(__name__)

# Configuration
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:1b"
MEMORY_FILE = Path("data/argent_memory.json")
ACTIONS_LOG = Path("data/argent_actions.json")

# Safety limits
MAX_SINGLE_CLOSE_USD = 50.0
MAX_DAILY_LOSS_USD = 25.0
MAX_ACTIONS_PER_HOUR = 10


class ArgentConfig:
    """Config adjustments Argent can make."""
    
    @staticmethod
    def get_current_config() -> Dict:
        """Get current bot config values."""
        try:
            from core.config_manager import get_config_manager
            mgr = get_config_manager()
            return {
                "stop_pct": mgr.get("fixed_stop_pct", 0.05) * 100,
                "tp1_pct": mgr.get("tp1_pct", 0.08) * 100,
                "tp2_pct": mgr.get("tp2_pct", 0.15) * 100,
                "entry_score_min": mgr.get("entry_score_min", 35),
                "max_exposure": mgr.get("portfolio_max_exposure_pct", 0.8) * 100,
                "position_base_pct": mgr.get("position_base_pct", 0.03) * 100,
            }
        except:
            return {}
    
    @staticmethod
    def adjust(setting: str, value: float) -> Dict:
        """Adjust a bot config setting."""
        try:
            from core.config_manager import get_config_manager
            mgr = get_config_manager()
            
            # Map friendly names to config keys
            mapping = {
                "stop": ("fixed_stop_pct", 0.01),  # Convert % to decimal
                "tp1": ("tp1_pct", 0.01),
                "tp2": ("tp2_pct", 0.01),
                "entry_score": ("entry_score_min", 1),
                "exposure": ("portfolio_max_exposure_pct", 0.01),
                "position_size": ("position_base_pct", 0.01),
            }
            
            if setting not in mapping:
                return {"success": False, "error": f"Unknown setting: {setting}"}
            
            key, multiplier = mapping[setting]
            new_value = value * multiplier
            
            # Safety limits
            limits = {
                "fixed_stop_pct": (0.02, 0.15),
                "tp1_pct": (0.03, 0.20),
                "tp2_pct": (0.05, 0.30),
                "entry_score_min": (20, 80),
                "portfolio_max_exposure_pct": (0.3, 0.9),
                "position_base_pct": (0.01, 0.10),
            }
            
            min_val, max_val = limits.get(key, (0, 100))
            if new_value < min_val or new_value > max_val:
                return {"success": False, "error": f"{setting} must be between {min_val/multiplier} and {max_val/multiplier}"}
            
            mgr.update_param(key, new_value, source="argent")
            return {"success": True, "message": f"Set {setting} to {value}", "key": key, "value": new_value}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def analyze_profitability() -> str:
        """Analyze current settings for profitability."""
        try:
            from core.shared_state import read_state
            state = read_state() or {}
            
            # Get trade history
            positions = state.get('positions', [])
            winners = [p for p in positions if p.get('pnl_usd', 0) > 0]
            losers = [p for p in positions if p.get('pnl_usd', 0) < 0]
            
            win_rate = len(winners) / len(positions) * 100 if positions else 0
            avg_win = sum(p.get('pnl_pct', 0) for p in winners) / len(winners) if winners else 0
            avg_loss = sum(p.get('pnl_pct', 0) for p in losers) / len(losers) if losers else 0
            
            cfg = ArgentConfig.get_current_config()
            
            lines = [
                f"Win Rate: {win_rate:.0f}% ({len(winners)}W / {len(losers)}L)",
                f"Avg Win: {avg_win:+.1f}%",
                f"Avg Loss: {avg_loss:.1f}%",
                "",
                "Current Settings:",
                f"  Stop: {cfg.get('stop_pct', 5):.0f}%",
                f"  TP1: {cfg.get('tp1_pct', 8):.0f}%",
                f"  Entry Score Min: {cfg.get('entry_score_min', 35)}",
                f"  Max Exposure: {cfg.get('max_exposure', 80):.0f}%",
            ]
            
            # Suggestions
            if win_rate < 40:
                lines.append("\nSuggestion: Win rate low. Consider raising entry_score to be more selective.")
            if avg_loss < -8:
                lines.append("Suggestion: Avg loss too high. Consider tightening stops.")
            if win_rate > 60 and avg_win < 3:
                lines.append("Suggestion: Good win rate but small gains. Consider wider TP targets.")
            
            return "\n".join(lines)
        except Exception as e:
            return f"Error analyzing: {e}"


class ArgentMemory:
    """Persistent memory for learning and context."""
    
    def __init__(self):
        self.decisions: List[Dict] = []
        self.insights: List[str] = []
        self.performance: Dict = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        self.load()
    
    def load(self):
        if MEMORY_FILE.exists():
            try:
                data = json.loads(MEMORY_FILE.read_text())
                self.decisions = data.get("decisions", [])[-50:]  # Keep last 50
                self.insights = data.get("insights", [])[-20:]
                self.performance = data.get("performance", self.performance)
            except:
                pass
    
    def save(self):
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(json.dumps({
            "decisions": self.decisions[-50:],
            "insights": self.insights[-20:],
            "performance": self.performance,
            "last_updated": datetime.now().isoformat()
        }, indent=2))
    
    def add_decision(self, action: str, reason: str, result: str):
        self.decisions.append({
            "time": datetime.now().isoformat(),
            "action": action,
            "reason": reason,
            "result": result
        })
        self.save()
    
    def add_insight(self, insight: str):
        self.insights.append(f"[{datetime.now().strftime('%m/%d %H:%M')}] {insight}")
        self.save()
    
    def get_context(self) -> str:
        if not self.decisions:
            return "No previous decisions recorded."
        
        recent = self.decisions[-5:]
        lines = ["RECENT DECISIONS:"]
        for d in recent:
            lines.append(f"- {d['action']}: {d['result']}")
        
        lines.append(f"\nPERFORMANCE: {self.performance['wins']}W/{self.performance['losses']}L, Total P&L: ${self.performance['total_pnl']:.2f}")
        return "\n".join(lines)


class ArgentActions:
    """Safe action execution with limits."""
    
    def __init__(self):
        self.actions_today: List[Dict] = []
        self.load_actions()
    
    def load_actions(self):
        if ACTIONS_LOG.exists():
            try:
                data = json.loads(ACTIONS_LOG.read_text())
                today = datetime.now().date().isoformat()
                self.actions_today = [a for a in data.get("actions", []) 
                                      if a.get("date") == today]
            except:
                self.actions_today = []
    
    def log_action(self, action_type: str, details: Dict):
        action = {
            "date": datetime.now().date().isoformat(),
            "time": datetime.now().isoformat(),
            "type": action_type,
            **details
        }
        self.actions_today.append(action)
        
        # Save to file
        ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if ACTIONS_LOG.exists():
            try:
                existing = json.loads(ACTIONS_LOG.read_text()).get("actions", [])
            except:
                pass
        existing.append(action)
        ACTIONS_LOG.write_text(json.dumps({"actions": existing[-100:]}, indent=2))
    
    def can_execute(self) -> Tuple[bool, str]:
        if len(self.actions_today) >= MAX_ACTIONS_PER_HOUR:
            return False, f"Rate limit: {MAX_ACTIONS_PER_HOUR} actions/hour"
        
        # Check daily loss limit
        total_loss = sum(a.get("pnl", 0) for a in self.actions_today if a.get("pnl", 0) < 0)
        if abs(total_loss) >= MAX_DAILY_LOSS_USD:
            return False, f"Daily loss limit reached: ${abs(total_loss):.2f}"
        
        return True, "OK"
    
    def close_position(self, symbol: str, reason: str) -> Dict:
        """Queue a position close via the bot state."""
        can, msg = self.can_execute()
        if not can:
            return {"success": False, "error": msg}
        
        try:
            # Queue the close directly via shared state
            from core.shared_state import read_state, write_state
            state = read_state() or {}
            pending = state.get('pending_closes', [])
            if symbol not in pending:
                pending.append(symbol)
                state['pending_closes'] = pending
                write_state(state)
            
            self.log_action("close_position", {
                "symbol": symbol,
                "reason": reason,
                "queued": True
            })
            
            return {"success": True, "message": f"Queued {symbol} for close", "reason": reason}
        except Exception as e:
            return {"success": False, "error": str(e)}


# Global instances
_memory = ArgentMemory()
_actions = ArgentActions()


SYSTEM_PROMPT = """You are Argent, a helpful trading assistant. Your name means "money" in French.

STYLE:
- Concise and clear - short responses, no rambling
- Humble - you're here to help, not show off
- Smart - think before acting, explain your reasoning briefly
- Patient - don't rush to close positions, wait for good reasons

WHEN TO CLOSE POSITIONS:
- ONLY close if loss is beyond the stop (>8% for large caps, >6% for mid, >3% for micro)
- NEVER close positions that are barely down (-1% or less)
- Small fluctuations are normal - don't panic

WHEN TO RECOMMEND:
- Be conservative with recommendations
- Only suggest actions when there's a clear reason
- Ask for confirmation before executing trades

RESPONSE FORMAT:
- Keep responses under 100 words unless asked for details
- Use bullet points for lists
- Be friendly and professional

DO NOT:
- Close positions that are only slightly negative
- Spam multiple actions at once
- Be arrogant or lecture the user
- Recommend trades without clear reasoning

You have access to real portfolio data. Use it wisely. Help your owner make good decisions."""


def get_full_context() -> str:
    """Get comprehensive context for Argent."""
    lines = []
    
    # Portfolio data
    try:
        from core.shared_state import read_state
        state = read_state() or {}
        
        positions = state.get('positions', [])
        sorted_pos = sorted(positions, key=lambda x: x.get('pnl_pct', 0), reverse=True)
        
        winners = [p for p in sorted_pos if p.get('pnl_usd', 0) > 0]
        losers = [p for p in sorted_pos if p.get('pnl_usd', 0) < 0]
        beyond_stop = [p for p in losers if abs(p.get('pnl_pct', 0)) > 8]
        
        total_pnl = sum(p.get('pnl_usd', 0) for p in positions)
        
        lines.extend([
            "=== PORTFOLIO STATUS ===",
            f"Total Value: ${state.get('portfolio_value', 0):.2f}",
            f"Cash: ${state.get('cash_balance', 0):.2f}",
            f"Exposure: {state.get('engine', {}).get('exposure_pct', 0):.0f}%",
            f"Positions: {len(positions)} ({len(winners)} green, {len(losers)} red)",
            f"Net P&L: ${total_pnl:.2f}",
            ""
        ])
        
        # Alert on positions beyond stop
        if beyond_stop:
            lines.append("!!! ALERT: POSITIONS BEYOND STOP !!!")
            for p in beyond_stop:
                sym = p.get('symbol', '?').replace('-USD', '')
                pct = p.get('pnl_pct', 0)
                val = p.get('size_usd', 0)
                lines.append(f"  {sym}: ${val:.2f} at {pct:.1f}% (SHOULD CLOSE)")
            lines.append("")
        
        # Top performers
        lines.append("TOP 3 WINNERS:")
        for p in winners[:3]:
            sym = p.get('symbol', '?').replace('-USD', '')
            pnl = p.get('pnl_usd', 0)
            pct = p.get('pnl_pct', 0)
            lines.append(f"  {sym}: +${pnl:.2f} ({pct:+.1f}%)")
        
        lines.append("\nTOP 3 LOSERS:")
        for p in losers[:3]:
            sym = p.get('symbol', '?').replace('-USD', '')
            pnl = p.get('pnl_usd', 0)
            pct = p.get('pnl_pct', 0)
            lines.append(f"  {sym}: ${pnl:.2f} ({pct:.1f}%)")
        
        # Bot status
        lines.extend([
            "",
            "=== BOT STATUS ===",
            f"Phase: {state.get('phase', 'unknown')}",
            f"BTC Regime: {state.get('btc_regime', 'normal')}",
            f"Signals: {len(state.get('signals', []))}",
        ])
        
        # Rejections insight
        rej = state.get('rejections', {})
        if rej.get('score', 0) > 1000:
            lines.append(f"Note: {rej.get('score', 0)} signals rejected (low score)")
        
    except Exception as e:
        lines.append(f"Error getting portfolio: {e}")
    
    # Memory context
    lines.append("")
    lines.append(_memory.get_context())
    
    return "\n".join(lines)


def chat(user_message: str) -> str:
    """Main chat interface with Argent."""
    msg_lower = user_message.lower()
    
    # Profitability analysis
    if any(x in msg_lower for x in ["profitability", "analyze settings", "tune", "optimize"]):
        return ArgentConfig.analyze_profitability()
    
    # Config adjustments: "set stop to 6" or "adjust tp1 to 10"
    if any(x in msg_lower for x in ["set ", "adjust "]):
        import re
        # Parse "set X to Y" or "adjust X to Y"
        match = re.search(r'(?:set|adjust)\s+(\w+)\s+(?:to\s+)?(\d+(?:\.\d+)?)', msg_lower)
        if match:
            setting = match.group(1)
            value = float(match.group(2))
            result = ArgentConfig.adjust(setting, value)
            if result.get("success"):
                _memory.add_decision(f"CONFIG {setting}={value}", "User request", "success")
                return f"Done. {result.get('message')}"
            else:
                return f"Could not adjust: {result.get('error')}"
    
    # Show current config
    if "config" in msg_lower or "settings" in msg_lower:
        cfg = ArgentConfig.get_current_config()
        return f"""Current Settings:
- Stop Loss: {cfg.get('stop_pct', 5):.0f}%
- Take Profit 1: {cfg.get('tp1_pct', 8):.0f}%
- Take Profit 2: {cfg.get('tp2_pct', 15):.0f}%
- Entry Score Min: {cfg.get('entry_score_min', 35)}
- Max Exposure: {cfg.get('max_exposure', 80):.0f}%
- Position Size: {cfg.get('position_base_pct', 3):.0f}%

Say "set stop to 6" or "adjust tp1 to 10" to change settings."""
    
    # Close position command
    if "close" in msg_lower:
        # Extract symbol from message
        symbols = ["ADA", "DOT", "SOL", "BTC", "ETH", "MKR", "LINK", "AVAX", "ATOM", 
                   "UNI", "AAVE", "BCH", "RENDER", "IMX", "SKY", "CRV", "STX", "ETC",
                   "ALGO", "FIL", "HNT", "PAXG", "CBETH", "QNT", "WELL", "SQD", "ORCA",
                   "CRO", "FLR", "SUPER", "GRT"]
        found_symbol = None
        for sym in symbols:
            if sym.lower() in msg_lower:
                found_symbol = sym
                break
        
        if found_symbol:
            symbol = f"{found_symbol}-USD"
            result = _actions.close_position(symbol, f"User requested close")
            _memory.add_decision(f"CLOSE {found_symbol}", "User request", str(result))
            if result.get("success"):
                return f"Done. Queued {found_symbol} for close."
            else:
                return f"Could not close {found_symbol}: {result.get('error', 'unknown error')}"
    
    # AI conversation for everything else
    try:
        context = get_full_context()
        
        prompt = SYSTEM_PROMPT + "\n\n" + context + "\n\nUser: " + user_message + "\n\nArgent:"
        
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.5, "num_predict": 300}
            },
            timeout=45
        )
        
        if response.status_code == 200:
            return response.json().get("response", "No response")
        return f"AI error: {response.status_code}"
        
    except requests.exceptions.ConnectionError:
        return "Argent offline - Ollama not running"
    except Exception as e:
        logger.error("Argent error: %s", e)
        return f"Error: {e}"


def get_bot_thinking() -> str:
    """Quick status check."""
    try:
        from core.shared_state import read_state
        state = read_state() or {}
        positions = state.get('positions', [])
        winners = sum(1 for p in positions if p.get('pnl_usd', 0) > 0)
        losers = len(positions) - winners
        total_pnl = sum(p.get('pnl_usd', 0) for p in positions)
        
        beyond_stop = [p for p in positions if p.get('pnl_pct', 0) < -8]
        
        lines = [
            f"Portfolio: ${state.get('portfolio_value', 0):.2f}",
            f"Positions: {len(positions)} ({winners}W / {losers}L)",
            f"Net P&L: ${total_pnl:.2f}",
            f"Phase: {state.get('phase', 'unknown')}",
        ]
        
        if beyond_stop:
            syms = [p.get('symbol', '?').replace('-USD', '') for p in beyond_stop]
            lines.append(f"ALERT: {', '.join(syms)} beyond stop!")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def execute_recommendation(action: str, symbol: str, reason: str) -> Dict:
    """Execute a trading action."""
    if action.upper() == "CLOSE":
        return _actions.close_position(symbol, reason)
    return {"success": False, "error": f"Unknown action: {action}"}
