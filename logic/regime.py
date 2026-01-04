"""Market regime detection and session awareness.

Handles BTC trend tracking, Fear & Greed integration, and trading session detection.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
import os

from core.config import settings
from core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class RegimeState:
    """Current market regime state."""
    regime: str = "normal"
    btc_trend_1h: float = 0.0
    btc_trend_15m: float = 0.0
    btc_price: float = 0.0
    last_update: Optional[datetime] = None
    fear_greed: Optional[int] = None
    fear_greed_class: str = ""
    fear_greed_updated: Optional[datetime] = None


class RegimeDetector:
    """Detects market regime based on BTC trend and sentiment."""
    
    BTC_DUMP_THRESHOLD = -1.5
    BTC_CRASH_THRESHOLD = -3.0
    
    def __init__(self):
        self._state = RegimeState()
    
    @property
    def regime(self) -> str:
        return self._state.regime
    
    @property
    def btc_trend_1h(self) -> float:
        return self._state.btc_trend_1h
    
    @property
    def btc_trend_15m(self) -> float:
        return self._state.btc_trend_15m
    
    @property
    def btc_price(self) -> float:
        return self._state.btc_price
    
    @property
    def is_safe_to_trade(self) -> bool:
        return self._state.regime == "normal"
    
    def update_btc_trend(self, trend_1h: float, trend_15m: float = 0.0, price: float = 0.0):
        """Update BTC trend and recalculate regime."""
        self._state.btc_trend_1h = trend_1h
        self._state.btc_trend_15m = trend_15m
        self._state.btc_price = price
        self._state.last_update = datetime.now(timezone.utc)
        
        if trend_1h <= self.BTC_CRASH_THRESHOLD:
            self._state.regime = "risk_off"
        elif trend_1h <= self.BTC_DUMP_THRESHOLD:
            self._state.regime = "caution"
        else:
            self._state.regime = "normal"
    
    def fetch_btc_trend(self) -> bool:
        """Fetch BTC trend from Coinbase."""
        try:
            from coinbase.rest import RESTClient
            
            client = RESTClient(
                api_key=os.getenv("COINBASE_API_KEY"),
                api_secret=os.getenv("COINBASE_API_SECRET")
            )
            
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=1)
            
            candles = client.get_public_candles(
                product_id="BTC-USD",
                start=str(int(start.timestamp())),
                end=str(int(end.timestamp())),
                granularity="FIVE_MINUTE"
            )
            
            candle_list = getattr(candles, 'candles', [])
            if len(candle_list) >= 2:
                candle_list = sorted(candle_list, key=lambda x: int(getattr(x, 'start', 0)))
                first_close = float(getattr(candle_list[0], 'close', 0))
                last_close = float(getattr(candle_list[-1], 'close', 0))
                
                if first_close > 0:
                    trend_1h = ((last_close / first_close) - 1) * 100
                    trend_15m = trend_1h / 4
                    if len(candle_list) >= 4:
                        mid_close = float(getattr(candle_list[-4], 'close', first_close))
                        trend_15m = ((last_close / mid_close) - 1) * 100
                    
                    self.update_btc_trend(trend_1h, trend_15m, last_close)
                    return True
            return False
        except Exception as e:
            logger.warning("[REGIME] Failed to fetch BTC trend: %s", e)
            return False
    
    def fetch_fear_greed(self) -> Optional[int]:
        """Fetch Fear & Greed index from alternative.me."""
        try:
            import urllib.request
            import json
            
            url = "https://api.alternative.me/fng/?limit=1"
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode())
                if data and "data" in data and len(data["data"]) > 0:
                    value = int(data["data"][0]["value"])
                    classification = data["data"][0]["value_classification"]
                    self._state.fear_greed = value
                    self._state.fear_greed_class = classification
                    self._state.fear_greed_updated = datetime.now(timezone.utc)
                    
                    if value >= 80 and self._state.regime == "normal":
                        self._state.regime = "caution"
                    
                    return value
            return None
        except Exception:
            return None
    
    def get_fear_greed(self) -> Optional[dict]:
        """Get cached Fear & Greed data."""
        if self._state.fear_greed is None:
            return None
        return {
            "value": self._state.fear_greed,
            "classification": self._state.fear_greed_class,
            "updated": self._state.fear_greed_updated,
        }
    
    def get_status_string(self) -> str:
        """Get human-readable regime status."""
        fg_str = f" F&G:{self._state.fear_greed}" if self._state.fear_greed else ""
        if self._state.regime == "risk_off":
            return f"RISK OFF (BTC {self._state.btc_trend_1h:+.1f}%{fg_str})"
        elif self._state.regime == "caution":
            return f"CAUTION (BTC {self._state.btc_trend_1h:+.1f}%{fg_str})"
        elif self._state.regime == "bullish":
            return f"BULLISH (BTC {self._state.btc_trend_1h:+.1f}%{fg_str})"
        return f"NORMAL (BTC {self._state.btc_trend_1h:+.1f}%{fg_str})"


class SessionDetector:
    """Detects trading session and provides size multipliers."""
    
    @staticmethod
    def get_session_info() -> dict:
        """Get current trading session info."""
        now = datetime.now(timezone.utc)
        hour = now.hour
        
        if 0 <= hour < 8:
            session, multiplier = "asia", 1.0
        elif 8 <= hour < 14:
            session, multiplier = "europe", 1.0
        elif 14 <= hour < 21:
            session, multiplier = "us", 1.0
        else:
            session, multiplier = "dead_zone", 0.6
        
        return {
            "session": session,
            "hour_utc": hour,
            "size_multiplier": multiplier,
            "is_active": multiplier >= 1.0,
        }
    
    @staticmethod
    def get_size_multiplier() -> float:
        """Get position size multiplier based on time of day."""
        return SessionDetector.get_session_info()["size_multiplier"]


regime_detector = RegimeDetector()
session_detector = SessionDetector()
