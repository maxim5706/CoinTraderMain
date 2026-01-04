"""Paper portfolio manager implementation."""

from pathlib import Path

from core.trading_interfaces import IPortfolioManager
from core.paper_state import load_paper_state, should_reset_paper_state


class PaperPortfolioManager(IPortfolioManager):
    """Tracks paper balances without exchange dependency."""

    def __init__(self, start_balance: float, state_path: Path | None = None, reset: bool | None = None):
        self._state_path = state_path or Path("data/paper_state.json")
        if reset is None:
            reset = should_reset_paper_state()
        self._state = load_paper_state(self._state_path, start_balance, reset=reset)
        self.balance = self._state.balance
        self.realized_pnl = self._state.realized_pnl
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
        self._persist()

    def credit(self, amount: float) -> None:
        self.balance += amount
        self._persist()

    def record_realized_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self._persist()

    def _persist(self) -> None:
        self._state.balance = self.balance
        self._state.realized_pnl = self.realized_pnl
        self._state.save(self._state_path)
