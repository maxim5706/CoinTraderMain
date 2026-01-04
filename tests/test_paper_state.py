from pathlib import Path

from core.mode_configs import PaperModeConfig, TradingMode
from core.paper_portfolio import PaperPortfolioManager
from core.trading_container import TradingContainer


def test_paper_cash_persists(tmp_path):
    state_path = tmp_path / "paper_state.json"
    manager = PaperPortfolioManager(1000.0, state_path=state_path, reset=True)
    manager.debit(200.0)
    manager.credit(50.0)
    manager.record_realized_pnl(10.0)

    reloaded = PaperPortfolioManager(1000.0, state_path=state_path)
    assert reloaded.balance == manager.balance
    assert reloaded.realized_pnl == manager.realized_pnl


def test_paper_mode_skips_live_endpoints(monkeypatch):
    import coinbase.rest

    def _fail(*_args, **_kwargs):
        raise AssertionError("RESTClient should not be initialized in paper mode")

    monkeypatch.setattr(coinbase.rest, "RESTClient", _fail)
    config = PaperModeConfig()
    container = TradingContainer(TradingMode.PAPER, config)
    container.get_executor()
    container.get_portfolio_manager()
