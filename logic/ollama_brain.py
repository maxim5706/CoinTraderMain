"""
Ollama Brain - AI-powered trading intelligence.

Uses local Ollama LLM to:
- Evaluate trade signals with market context
- Analyze position health and suggest exits
- Provide market sentiment analysis
- Make entry/exit decisions with reasoning
"""

import asyncio
import json
import requests
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from core.logging_utils import get_logger

logger = get_logger(__name__)

OLLAMA_URL = "http://localhost:11434"
MODEL = "llama3.2:1b"  # Fast, local model

# Thread pool for sync requests
_executor = ThreadPoolExecutor(max_workers=2)


@dataclass
class BrainDecision:
    """AI decision with reasoning."""
    action: str  # "buy", "sell", "hold", "skip"
    confidence: float  # 0-100
    reasoning: str
    risk_level: str  # "low", "medium", "high"
    suggested_size_pct: float  # 0-100% of normal size


class OllamaBrain:
    """
    AI brain for trading decisions using local Ollama.
    
    Provides intelligent signal evaluation beyond pure technical analysis.
    """
    
    def __init__(self, model: str = MODEL, timeout: float = 30.0):
        self.model = model
        self.timeout = timeout
        self._available = None
        self._last_query_time = 0
    
    async def is_available(self) -> bool:
        """Check if Ollama is running."""
        if self._available is not None:
            return self._available
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                _executor,
                lambda: requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
            )
            self._available = resp.status_code == 200
            return self._available
        except Exception:
            self._available = False
            return False
    
    async def _query(self, prompt: str, system: str = "") -> Optional[str]:
        """Query Ollama with a prompt."""
        if not await self.is_available():
            return None
            
        try:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {
                    "temperature": 0.3,  # Low temp for consistent decisions
                    "num_predict": 200,  # Short responses
                }
            }
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                _executor,
                lambda: requests.post(
                    f"{OLLAMA_URL}/api/generate",
                    json=payload,
                    timeout=self.timeout
                )
            )
            if resp.status_code == 200:
                return resp.json().get("response", "")
        except Exception as e:
            logger.warning("[BRAIN] Query failed: %s", e)
        return None
    
    async def evaluate_signal(
        self,
        symbol: str,
        strategy: str,
        score: int,
        price: float,
        trend_1m: float,
        trend_5m: float,
        trend_1h: float,
        rsi: float,
        volume_spike: float,
        btc_trend: float,
        portfolio_value: float,
        available_cash: float,
    ) -> BrainDecision:
        """
        Evaluate a trading signal with AI reasoning.
        
        Returns decision with confidence and reasoning.
        """
        system = """You are a professional crypto trader making quick decisions.
Respond in JSON format only: {"action": "buy/skip", "confidence": 0-100, "reasoning": "brief reason", "risk": "low/medium/high", "size_pct": 50-150}
Be conservative. Only recommend "buy" for strong setups. Default to "skip" if uncertain."""

        prompt = f"""Evaluate this crypto trade signal:

Symbol: {symbol}
Strategy: {strategy}
Score: {score}/100
Price: ${price:.4f}
Trends: 1m={trend_1m:+.1f}%, 5m={trend_5m:+.1f}%, 1h={trend_1h:+.1f}%
RSI: {rsi:.0f}
Volume Spike: {volume_spike:.1f}x
BTC Trend: {btc_trend:+.1f}%
Portfolio: ${portfolio_value:.0f}
Available: ${available_cash:.0f}

Should I take this trade? Respond with JSON only."""

        response = await self._query(prompt, system)
        
        # Parse response
        if response:
            try:
                # Extract JSON from response
                start = response.find("{")
                end = response.rfind("}") + 1
                if start >= 0 and end > start:
                    data = json.loads(response[start:end])
                    return BrainDecision(
                        action=data.get("action", "skip").lower(),
                        confidence=float(data.get("confidence", 50)),
                        reasoning=data.get("reasoning", "No reasoning provided"),
                        risk_level=data.get("risk", "medium").lower(),
                        suggested_size_pct=float(data.get("size_pct", 100)),
                    )
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("[BRAIN] Parse error: %s", e)
        
        # Default: conservative skip
        return BrainDecision(
            action="skip",
            confidence=0,
            reasoning="AI unavailable or parse error",
            risk_level="high",
            suggested_size_pct=0,
        )
    
    async def analyze_position(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        pnl_pct: float,
        hold_minutes: int,
        trend_1h: float,
        rsi: float,
    ) -> BrainDecision:
        """Analyze whether to hold or exit a position."""
        system = """You are managing an open crypto position.
Respond in JSON: {"action": "hold/sell", "confidence": 0-100, "reasoning": "brief reason", "risk": "low/medium/high"}
Protect profits. Cut losers. Be decisive."""

        prompt = f"""Analyze this open position:

Symbol: {symbol}
Entry: ${entry_price:.4f}
Current: ${current_price:.4f}
P&L: {pnl_pct:+.1f}%
Hold Time: {hold_minutes} minutes
1H Trend: {trend_1h:+.1f}%
RSI: {rsi:.0f}

Should I hold or sell? JSON only."""

        response = await self._query(prompt, system)
        
        if response:
            try:
                start = response.find("{")
                end = response.rfind("}") + 1
                if start >= 0 and end > start:
                    data = json.loads(response[start:end])
                    return BrainDecision(
                        action=data.get("action", "hold").lower(),
                        confidence=float(data.get("confidence", 50)),
                        reasoning=data.get("reasoning", ""),
                        risk_level=data.get("risk", "medium").lower(),
                        suggested_size_pct=100,
                    )
            except (json.JSONDecodeError, ValueError):
                pass
        
        return BrainDecision(
            action="hold",
            confidence=50,
            reasoning="Default hold - AI unavailable",
            risk_level="medium",
            suggested_size_pct=100,
        )
    
    async def get_market_sentiment(
        self,
        btc_price: float,
        btc_change_24h: float,
        eth_change_24h: float,
        total_positions: int,
        unrealized_pnl: float,
    ) -> Dict[str, Any]:
        """Get overall market sentiment analysis."""
        system = """You are a crypto market analyst.
Respond in JSON: {"sentiment": "bullish/neutral/bearish", "risk_appetite": "high/medium/low", "recommendation": "brief advice"}"""

        prompt = f"""Market snapshot:

BTC: ${btc_price:.0f} ({btc_change_24h:+.1f}% 24h)
ETH: {eth_change_24h:+.1f}% 24h
Open Positions: {total_positions}
Unrealized P&L: ${unrealized_pnl:+.2f}

What's the market sentiment? JSON only."""

        response = await self._query(prompt, system)
        
        if response:
            try:
                start = response.find("{")
                end = response.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(response[start:end])
            except (json.JSONDecodeError, ValueError):
                pass
        
        return {
            "sentiment": "neutral",
            "risk_appetite": "medium",
            "recommendation": "Trade with caution"
        }
    
    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Global instance
brain = OllamaBrain()


async def evaluate_with_brain(
    symbol: str,
    strategy: str,
    score: int,
    indicators: Any,
    portfolio_value: float,
    available_cash: float,
) -> Optional[BrainDecision]:
    """
    Convenience function to evaluate a signal with the brain.
    
    Returns None if brain is unavailable.
    """
    if not await brain.is_available():
        return None
    
    return await brain.evaluate_signal(
        symbol=symbol,
        strategy=strategy,
        score=score,
        price=getattr(indicators, 'price', 0),
        trend_1m=getattr(indicators, 'price_change_1m', 0),
        trend_5m=getattr(indicators, 'price_change_5m', 0),
        trend_1h=getattr(indicators, 'trend_1h', 0),
        rsi=getattr(indicators, 'rsi_14', 50),
        volume_spike=getattr(indicators, 'vol_1m', 1),
        btc_trend=getattr(indicators, 'btc_trend', 0),
        portfolio_value=portfolio_value,
        available_cash=available_cash,
    )
