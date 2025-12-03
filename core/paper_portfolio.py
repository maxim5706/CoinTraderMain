"""Paper portfolio manager implementation."""

from core.trading_interfaces import IPortfolioManager


class PaperPortfolioManager(IPortfolioManager):
    """Tracks paper balances without exchange dependency."""

    def __init__(self, start_balance: float):
        self.balance = start_balance
        self.positions_value = 0.0

    def get_available_balance(self) -> float:
        return self.balance

    def get_total_portfolio_value(self) -> float:
        return self.balance + self.positions_value

    def update_portfolio_state(self) -> None:
        # Paper mode keeps simple in-memory totals; executor adjusts balance.
        return

    def debit(self, amount: float) -> None:
        self.balance -= amount
        if self.balance < 0:
            self.balance = 0.0

    def credit(self, amount: float) -> None:
        self.balance += amount
