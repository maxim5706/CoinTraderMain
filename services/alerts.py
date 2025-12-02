"""
Alert system for trade notifications via Telegram.

Setup:
1. Create a Telegram bot via @BotFather
2. Get your chat_id by messaging @userinfobot
3. Set environment variables:
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
"""

import os
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from enum import Enum

from core.logging_utils import get_logger

logger = get_logger(__name__)

class AlertLevel(Enum):
    INFO = "ℹ️"
    SUCCESS = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    TRADE = "💰"


@dataclass
class AlertConfig:
    """Configuration for alerts."""
    enabled: bool = True
    telegram_token: str = ""
    telegram_chat_id: str = ""
    
    # Alert filters
    send_trades: bool = True      # Entry/exit notifications
    send_errors: bool = True      # Error alerts
    send_regime: bool = True      # BTC regime changes
    send_daily: bool = True       # Daily summary
    
    # Rate limiting
    min_interval_sec: float = 1.0  # Min time between alerts
    
    @classmethod
    def from_env(cls) -> "AlertConfig":
        """Load config from environment variables."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        
        return cls(
            enabled=bool(token and chat_id),
            telegram_token=token,
            telegram_chat_id=chat_id
        )


class AlertManager:
    """Manages sending alerts via Telegram."""
    
    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(self, config: Optional[AlertConfig] = None):
        self.config = config or AlertConfig.from_env()
        self._last_alert_time: Optional[datetime] = None
        self._client: Optional[httpx.AsyncClient] = None
        
        if self.config.enabled:
            logger.info("[ALERT] Telegram alerts enabled")
        else:
            logger.info("[ALERT] Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client
    
    async def close(self):
        """Close the client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    async def send(
        self, 
        message: str, 
        level: AlertLevel = AlertLevel.INFO,
        parse_mode: str = "HTML"
    ) -> bool:
        """
        Send an alert message.
        Returns True if sent successfully.
        """
        if not self.config.enabled:
            return False
        
        # Rate limiting
        now = datetime.now(timezone.utc)
        if self._last_alert_time:
            elapsed = (now - self._last_alert_time).total_seconds()
            if elapsed < self.config.min_interval_sec:
                await asyncio.sleep(self.config.min_interval_sec - elapsed)
        
        try:
            client = await self._get_client()
            url = self.TELEGRAM_API.format(token=self.config.telegram_token)
            
            # Format message with emoji
            full_message = f"{level.value} {message}"
            
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": full_message,
                "parse_mode": parse_mode
            }
            
            resp = await client.post(url, json=payload)
            self._last_alert_time = datetime.now(timezone.utc)
            
            if resp.status_code == 200:
                return True
            else:
                logger.warning("[ALERT] Telegram error: %s", resp.status_code)
                return False
                    
        except Exception as e:
            logger.error("[ALERT] Failed to send: %s", e, exc_info=True)
            return False
    
    # Convenience methods for specific alert types
    async def trade_entry(
        self, 
        symbol: str, 
        price: float, 
        size_usd: float,
        stop_price: float,
        score: int
    ):
        """Alert for new trade entry."""
        if not self.config.send_trades:
            return
        
        message = (
            f"<b>🟢 ENTRY: {symbol}</b>\n"
            f"Price: ${price:.4f}\n"
            f"Size: ${size_usd:.2f}\n"
            f"Stop: ${stop_price:.4f}\n"
            f"Score: {score}/100"
        )
        await self.send(message, AlertLevel.TRADE)
    
    async def trade_exit(
        self, 
        symbol: str, 
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        reason: str
    ):
        """Alert for trade exit."""
        if not self.config.send_trades:
            return
        
        emoji = "🟢" if pnl >= 0 else "🔴"
        message = (
            f"<b>{emoji} EXIT: {symbol}</b>\n"
            f"Entry: ${entry_price:.4f}\n"
            f"Exit: ${exit_price:.4f}\n"
            f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"Reason: {reason}"
        )
        await self.send(message, AlertLevel.TRADE)
    
    async def regime_change(self, old_regime: str, new_regime: str, btc_change: float):
        """Alert for BTC regime change."""
        if not self.config.send_regime:
            return
        
        emoji = "🟢" if "NORMAL" in new_regime else "🟡" if "CAUTION" in new_regime else "🔴"
        message = (
            f"<b>{emoji} REGIME CHANGE</b>\n"
            f"From: {old_regime}\n"
            f"To: {new_regime}\n"
            f"BTC 1h: {btc_change:+.2f}%"
        )
        await self.send(message, AlertLevel.WARNING)
    
    async def error(self, error_type: str, details: str):
        """Alert for errors."""
        if not self.config.send_errors:
            return
        
        message = (
            f"<b>ERROR: {error_type}</b>\n"
            f"{details}"
        )
        await self.send(message, AlertLevel.ERROR)
    
    async def daily_summary(
        self,
        trades: int,
        wins: int,
        total_pnl: float,
        positions: int,
        equity: float
    ):
        """Send daily trading summary."""
        if not self.config.send_daily:
            return
        
        win_rate = (wins / trades * 100) if trades > 0 else 0
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        
        message = (
            f"<b>📊 DAILY SUMMARY</b>\n"
            f"Trades: {trades} ({wins}W/{trades-wins}L)\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"PnL: {emoji} ${total_pnl:+.2f}\n"
            f"Open Positions: {positions}\n"
            f"Equity: ${equity:.2f}"
        )
        await self.send(message, AlertLevel.INFO)
    
    async def startup(self, positions: int, equity: float):
        """Send startup notification."""
        message = (
            f"<b>🚀 BOT STARTED</b>\n"
            f"Positions: {positions}\n"
            f"Equity: ${equity:.2f}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self.send(message, AlertLevel.SUCCESS)
    
    async def shutdown(self, positions: int, daily_pnl: float):
        """Send shutdown notification."""
        message = (
            f"<b>🛑 BOT STOPPED</b>\n"
            f"Open Positions: {positions}\n"
            f"Daily PnL: ${daily_pnl:+.2f}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self.send(message, AlertLevel.WARNING)


# Global instance (lazy initialized)
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Get or create the global alert manager."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


# Convenience async functions
async def alert_trade_entry(symbol: str, price: float, size_usd: float, stop_price: float, score: int):
    """Send trade entry alert."""
    await get_alert_manager().trade_entry(symbol, price, size_usd, stop_price, score)


async def alert_trade_exit(symbol: str, entry_price: float, exit_price: float, pnl: float, pnl_pct: float, reason: str):
    """Send trade exit alert."""
    await get_alert_manager().trade_exit(symbol, entry_price, exit_price, pnl, pnl_pct, reason)


async def alert_error(error_type: str, details: str):
    """Send error alert."""
    await get_alert_manager().error(error_type, details)
