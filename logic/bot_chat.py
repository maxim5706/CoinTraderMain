"""Built-in AI chat for trading bot.

Provides two capabilities:
A) Trading assistant - helps make decisions, explains reasoning
B) User chat - answer questions about bot status, positions, strategy
"""

import json
import requests
from datetime import datetime
from typing import Optional, Dict, Any

from core.logging_utils import get_logger

logger = get_logger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:1b"
TIMEOUT = 30

SYSTEM_PROMPT = """You are the AI assistant for CoinTrader, a crypto trading bot.

Your role:
1. Explain what the bot is doing and why
2. Answer questions about positions, performance, and strategy
3. Provide trading insights based on current data
4. Help the user understand market conditions

Be concise and direct. Focus on actionable information.
When discussing trades, always consider fees (1.2-1.8% round trip).
"""


def get_bot_context() -> str:
    """Get current bot state for context."""
    try:
        from core.shared_state import read_state
        state = read_state() or {}
        
        positions = state.get('positions', [])
        winning = sum(1 for p in positions if p.get('pnl_usd', 0) > 0)
        losing = len(positions) - winning
        total_pnl = sum(p.get('pnl_usd', 0) for p in positions)
        
        sorted_pos = sorted(positions, key=lambda x: x.get('pnl_usd', 0), reverse=True)
        top_3 = sorted_pos[:3] if sorted_pos else []
        bottom_3 = sorted_pos[-3:] if sorted_pos else []
        
        lines = [
            "CURRENT BOT STATE:",
            f"- Portfolio: ${state.get('portfolio_value', 0):.2f}",
            f"- Cash: ${state.get('cash_balance', 0):.2f}",
            f"- Positions: {len(positions)} ({winning} winning, {losing} losing)",
            f"- Unrealized PnL: ${total_pnl:.2f}",
            f"- Exposure: {state.get('engine', {}).get('exposure_pct', 0):.0f}%",
            "",
            "TOP PERFORMERS:",
        ]
        for p in top_3:
            lines.append(f"- {p.get('symbol','?')}: ${p.get('pnl_usd',0):.2f}")
        lines.append("")
        lines.append("WORST PERFORMERS:")
        for p in bottom_3:
            lines.append(f"- {p.get('symbol','?')}: ${p.get('pnl_usd',0):.2f}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting bot state: {e}"


def chat(user_message: str, include_context: bool = True) -> str:
    """Chat with the trading bot AI."""
    try:
        context = get_bot_context() if include_context else ""
        
        prompt = SYSTEM_PROMPT + "\n\n" + context + "\n\nUser: " + user_message + "\n\nAssistant:"
        
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": 256,
                }
            },
            timeout=TIMEOUT
        )
        
        if response.status_code == 200:
            result = response.json()
            return result.get("response", "No response from AI")
        else:
            return f"AI error: {response.status_code}"
            
    except requests.exceptions.ConnectionError:
        return "AI offline - Ollama not running"
    except requests.exceptions.Timeout:
        return "AI timeout - try a shorter question"
    except Exception as e:
        logger.error("Chat error: %s", e)
        return f"Chat error: {str(e)}"


def get_bot_thinking() -> str:
    """Get what the bot is currently thinking/doing."""
    try:
        from core.shared_state import read_state
        state = read_state() or {}
        
        phase = state.get('phase', 'unknown')
        focus = state.get('focus_coin', {})
        signals = state.get('signals', [])
        
        lines = [
            f"Phase: {phase}",
            f"Focus: {focus.get('symbol', 'none')}",
            f"Active signals: {len(signals)}",
        ]
        
        if signals:
            lines.append("Signals:")
            for s in signals[:3]:
                lines.append(f"  - {s.get('symbol')}: {s.get('type')} score={s.get('score',0)}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"
