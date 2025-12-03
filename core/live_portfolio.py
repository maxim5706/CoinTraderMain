"""Live portfolio manager backed by Coinbase client."""

from typing import Dict, Optional

from coinbase.rest import RESTClient

from core.logging_utils import get_logger
from core.mode_configs import LiveModeConfig
from core.portfolio import PortfolioSnapshot, portfolio_tracker
from core.trading_interfaces import IPortfolioManager

logger = get_logger(__name__)


class LivePortfolioManager(IPortfolioManager):
    """Retrieves balances and holdings from the exchange."""

    def __init__(self, config: LiveModeConfig):
        self.config = config
        self._client: Optional[RESTClient] = None
        self._exchange_holdings: Dict[str, float] = {}
        self._portfolio_snapshot: Optional[PortfolioSnapshot] = None
        self._portfolio_value: float = 0.0
        self._usd_balance: float = 0.0
        self._product_info: dict[str, dict] = {}
        self._init_live_client()
        self.update_portfolio_state()

    def _init_live_client(self) -> None:
        if not self.config.api_key or not self.config.api_secret:
            logger.warning("[ORDER] Live client not initialized (missing API keys)")
            self._client = None
            return
        try:
            self._client = RESTClient(api_key=self.config.api_key, api_secret=self.config.api_secret)
            logger.info("[ORDER] Live client initialized")
        except Exception as e:
            logger.error("[ORDER] Failed to init live client: %s", e, exc_info=True)
            self._client = None

    @property
    def client(self) -> Optional[RESTClient]:
        return self._client

    @property
    def exchange_holdings(self) -> Dict[str, float]:
        return self._exchange_holdings

    @property
    def portfolio_snapshot(self) -> Optional[PortfolioSnapshot]:
        return self._portfolio_snapshot

    def update_portfolio_state(self) -> None:
        """Refresh USD balance, holdings, and snapshot."""
        if not self._client:
            return
        try:
            accounts = self._client.get_accounts()
            usd_bal = 0.0
            usdc_bal = 0.0
            holdings_value = 0.0
            self._exchange_holdings = {}

            for acct in getattr(accounts, "accounts", []):
                currency = getattr(acct, "currency", "")
                bal = getattr(acct, "available_balance", {})
                value = float(bal.get("value", 0) if isinstance(bal, dict) else getattr(bal, "value", 0))

                if currency == "USD":
                    usd_bal = value
                elif currency == "USDC":
                    usdc_bal = value
                elif value > 0.0001:
                    symbol = f"{currency}-USD"
                    delisted = {"BOND-USD", "NU-USD", "CLV-USD", "SNX-USD"}
                    if symbol in delisted:
                        continue

                    try:
                        product = self._client.get_product(symbol)
                        price = float(getattr(product, "price", 0))
                        position_value = value * price
                        holdings_value += position_value
                        if position_value >= 1.0:
                            self._exchange_holdings[symbol] = position_value
                    except Exception:
                        continue

            self._usd_balance = usd_bal + usdc_bal
            self._portfolio_value = self._usd_balance + holdings_value

            if self._portfolio_value < 50:
                logger.warning("[ORDER] API balance low ($%s), using fallback", f"{self._portfolio_value:.2f}")
                self._portfolio_value = 500.0
                self._usd_balance = 450.0

            try:
                self._portfolio_snapshot = portfolio_tracker.get_snapshot()
            except Exception as pe:
                logger.warning("[ORDER] Portfolio snapshot failed: %s", pe, exc_info=True)
        except Exception as e:
            logger.error("[ORDER] Balance check failed: %s", e, exc_info=True)

    def get_available_balance(self) -> float:
        return self._usd_balance

    def get_total_portfolio_value(self) -> float:
        if self._portfolio_snapshot:
            return self._portfolio_snapshot.total_value
        return self._portfolio_value

    def has_exchange_holding(self, symbol: str) -> bool:
        return symbol in self._exchange_holdings

    def get_product_info(self, symbol: str) -> dict:
        if symbol in self._product_info:
            return self._product_info[symbol]

        if not self._client:
            return {"quote_min": 1.0, "base_min": 0.0001}

        try:
            product = self._client.get_product(symbol)
            info = {
                "quote_min": float(getattr(product, "quote_min_size", 1) or 1),
                "base_min": float(getattr(product, "base_min_size", 0.0001) or 0.0001),
                "base_increment": float(getattr(product, "base_increment", 0.0001) or 0.0001),
            }
            self._product_info[symbol] = info
            return info
        except Exception as e:
            logger.warning("[ORDER] Product info failed for %s: %s", symbol, e, exc_info=True)
            return {"quote_min": 1.0, "base_min": 0.0001}
