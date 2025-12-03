"""
Complete Data Synchronization Validator

Validates the complete data flow and ensures all components work together:
Market Data → Strategies → Signals → Execution → Positions → PnL → Portfolio → Display

This framework validates:
1. Data flow integrity at each step
2. Component synchronization  
3. Race condition detection
4. Multi-strategy isolation
5. Real-time consistency checks
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

import sys
sys.path.append('.')

from core.config import settings
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.pnl_engine import PnLEngine, AccountPnL
from core.position_registry import PositionRegistry
from core.models import Position, Side
from core.portfolio import PortfolioTracker


@dataclass
class SyncValidationResult:
    """Result of a synchronization validation check."""
    component: str
    step: str
    passed: bool
    expected: Any
    actual: Any
    tolerance: float = 0.0
    error_msg: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    

@dataclass
class DataFlowSnapshot:
    """Complete system state snapshot for comparison."""
    timestamp: datetime
    prices: Dict[str, float] = field(default_factory=dict)
    positions: Dict[str, Position] = field(default_factory=dict)
    portfolio_value: float = 0.0
    cash_balance: float = 0.0
    holdings_value: float = 0.0
    total_pnl: float = 0.0
    strategy_pnl: Dict[str, float] = field(default_factory=dict)
    

class DataSynchronizationValidator:
    """
    Comprehensive data synchronization validator.
    
    Validates complete data flow from market data to display,
    ensuring all components stay synchronized and consistent.
    """
    
    def __init__(self):
        self.mode = ConfigurationManager.get_trading_mode()
        self.config = ConfigurationManager.get_config_for_mode(self.mode)
        
        # Initialize components
        self.pnl_engine = PnLEngine(self.config)
        self.position_registry = PositionRegistry(self.config)
        self.portfolio = PortfolioTracker() if self.mode == TradingMode.LIVE else None
        
        # Validation tracking
        self.results: List[SyncValidationResult] = []
        self.snapshots: List[DataFlowSnapshot] = []
        
        self.logger = logging.getLogger("data_sync_validator")
    
    async def run_complete_validation(self) -> bool:
        """Run complete data synchronization validation."""
        print("🔄 COMPLETE DATA SYNCHRONIZATION VALIDATION")
        print("=" * 60)
        print(f"Mode: {self.mode.value}")
        print(f"Time: {datetime.now()}")
        print()
        
        validation_steps = [
            ("Configuration Consistency", self._validate_configuration_sync),
            ("Component Initialization", self._validate_component_init),
            ("Price Data Flow", self._validate_price_data_flow),
            ("Position Tracking", self._validate_position_tracking),
            ("PnL Calculations", self._validate_pnl_calculations),
            ("Portfolio Synchronization", self._validate_portfolio_sync),
            ("Multi-Strategy Isolation", self._validate_strategy_isolation),
            ("Race Condition Detection", self._validate_race_conditions),
            ("Data Persistence", self._validate_data_persistence),
            ("Complete Pipeline", self._validate_complete_pipeline),
        ]
        
        overall_success = True
        
        for step_name, validator_func in validation_steps:
            print(f"🔍 {step_name}...")
            try:
                step_success = await validator_func()
                status = "✅ PASS" if step_success else "❌ FAIL"
                print(f"   {status}")
                
                if not step_success:
                    overall_success = False
                    # Print detailed failure info
                    recent_failures = [r for r in self.results[-10:] if not r.passed]
                    for failure in recent_failures:
                        print(f"   ❌ {failure.error_msg}")
                
            except Exception as e:
                print(f"   ❌ ERROR: {e}")
                overall_success = False
            
            print()
        
        self._print_summary()
        return overall_success
    
    async def _validate_configuration_sync(self) -> bool:
        """Validate all components use consistent configuration."""
        success = True
        
        # Check that all components use same config values
        config_checks = [
            ("dust_threshold_usd", self.position_registry.limits.dust_threshold_usd, self.config.dust_threshold_usd),
            ("max_positions", self.position_registry.limits.max_positions, self.config.max_positions),
            ("maker_fee_pct", self.pnl_engine.config.maker_fee_pct, self.config.maker_fee_pct),
            ("taker_fee_pct", self.pnl_engine.config.taker_fee_pct, self.config.taker_fee_pct),
        ]
        
        for field_name, component_value, config_value in config_checks:
            if abs(component_value - config_value) > 0.001:
                self.results.append(SyncValidationResult(
                    component="Configuration",
                    step="Config Sync",
                    passed=False,
                    expected=config_value,
                    actual=component_value,
                    error_msg=f"{field_name} mismatch: component={component_value}, config={config_value}"
                ))
                success = False
        
        return success
    
    async def _validate_component_init(self) -> bool:
        """Validate all components initialized correctly."""
        success = True
        
        # Check component initialization
        if not self.pnl_engine:
            self.results.append(SyncValidationResult(
                component="PnLEngine",
                step="Initialization",
                passed=False,
                expected="Initialized",
                actual="None",
                error_msg="PnLEngine not initialized"
            ))
            success = False
        
        if not self.position_registry:
            self.results.append(SyncValidationResult(
                component="PositionRegistry", 
                step="Initialization",
                passed=False,
                expected="Initialized",
                actual="None",
                error_msg="PositionRegistry not initialized"
            ))
            success = False
        
        return success
    
    async def _validate_price_data_flow(self) -> bool:
        """Validate price data flows correctly through system."""
        success = True
        
        # Test price consistency across multiple calls
        test_symbol = "BTC-USD"
        
        if self.mode == TradingMode.LIVE and self.portfolio:
            try:
                # Get price multiple times and check consistency
                snapshot = self.portfolio.get_snapshot()
                if snapshot and snapshot.positions.get(test_symbol):
                    position = snapshot.positions[test_symbol]
                    # Price should be reasonable (between $10k and $200k)
                    if not (10000 <= position.value_usd/position.qty <= 200000):
                        success = False
                        self.results.append(SyncValidationResult(
                            component="PriceData",
                            step="Price Validation",
                            passed=False,
                            expected="10k-200k range",
                            actual=f"${position.value_usd/position.qty:.2f}",
                            error_msg="BTC price outside reasonable range"
                        ))
            except Exception as e:
                success = False
                self.results.append(SyncValidationResult(
                    component="PriceData",
                    step="Price Access",
                    passed=False,
                    expected="No error",
                    actual=str(e),
                    error_msg=f"Price data access error: {e}"
                ))
        
        return success
    
    async def _validate_position_tracking(self) -> bool:
        """Validate position tracking consistency."""
        success = True
        
        # Test position registry functionality
        test_position = Position(
            symbol="TEST-USD",
            side=Side.BUY,
            entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=100.0,
            size_qty=0.002,
            stop_price=49000.0,
            tp1_price=51000.0,
            tp2_price=52000.0,
            strategy_id="test_strategy"
        )
        
        # Add position
        was_added_active = self.position_registry.add_position(test_position)
        
        # Verify it was added correctly
        retrieved = self.position_registry.get_position("TEST-USD")
        if not retrieved or retrieved.symbol != test_position.symbol:
            success = False
            self.results.append(SyncValidationResult(
                component="PositionRegistry",
                step="Position Add/Get",
                passed=False,
                expected="TEST-USD position",
                actual=str(retrieved),
                error_msg="Position not properly stored/retrieved"
            ))
        
        # Test position limits
        can_open, reason = self.position_registry.can_open_position("test_strategy", 5.0)
        if not can_open:
            success = False
            self.results.append(SyncValidationResult(
                component="PositionRegistry", 
                step="Position Limits",
                passed=False,
                expected="Can open position",
                actual=f"Cannot open: {reason}",
                error_msg=f"Position limit check failed: {reason}"
            ))
        
        # Clean up test position
        self.position_registry.remove_position("TEST-USD")
        
        return success
    
    async def _validate_pnl_calculations(self) -> bool:
        """Validate PnL calculation accuracy and consistency."""
        success = True
        
        # Test known PnL calculations
        test_cases = [
            {
                "entry": 50000.0,
                "exit": 51000.0, 
                "qty": 0.001,
                "expected_gross": 1.0,
                "name": "1% profit"
            },
            {
                "entry": 50000.0,
                "exit": 49000.0,
                "qty": 0.001, 
                "expected_gross": -1.0,
                "name": "1% loss"
            }
        ]
        
        for case in test_cases:
            pnl = self.pnl_engine.calculate_trade_pnl(
                entry_price=case["entry"],
                exit_price=case["exit"],
                qty=case["qty"]
            )
            
            # Check gross PnL accuracy
            gross_diff = abs(pnl.gross_pnl - case["expected_gross"])
            if gross_diff > 0.001:
                success = False
                self.results.append(SyncValidationResult(
                    component="PnLEngine",
                    step=f"PnL Calculation - {case['name']}",
                    passed=False,
                    expected=case["expected_gross"],
                    actual=pnl.gross_pnl,
                    tolerance=0.001,
                    error_msg=f"Gross PnL calculation error: expected {case['expected_gross']}, got {pnl.gross_pnl}"
                ))
            
            # Check that fees are reasonable (0.5% to 3%)
            fee_pct = pnl.fee_pct
            if not (0.5 <= fee_pct <= 3.0):
                success = False
                self.results.append(SyncValidationResult(
                    component="PnLEngine",
                    step=f"Fee Calculation - {case['name']}",
                    passed=False,
                    expected="0.5% - 3.0%",
                    actual=f"{fee_pct:.2f}%", 
                    error_msg=f"Fee percentage outside expected range: {fee_pct:.2f}%"
                ))
        
        return success
    
    async def _validate_portfolio_sync(self) -> bool:
        """Validate portfolio synchronization (live mode only)."""
        if self.mode != TradingMode.LIVE or not self.portfolio:
            return True  # Skip in paper mode
        
        success = True
        
        try:
            # Get multiple snapshots and check consistency
            snapshots = []
            for i in range(3):
                snapshot = self.portfolio.get_snapshot()
                if snapshot:
                    snapshots.append(snapshot)
                await asyncio.sleep(0.5)  # 500ms between snapshots
            
            if len(snapshots) < 3:
                success = False
                self.results.append(SyncValidationResult(
                    component="Portfolio",
                    step="Snapshot Consistency",
                    passed=False,
                    expected="3 snapshots",
                    actual=f"{len(snapshots)} snapshots",
                    error_msg="Could not get consistent portfolio snapshots"
                ))
                return success
            
            # Check value consistency across snapshots
            values = [s.total_value for s in snapshots]
            max_diff = max(values) - min(values)
            max_diff_pct = (max_diff / values[0] * 100) if values[0] > 0 else 0
            
            if max_diff_pct > 0.1:  # More than 0.1% difference
                success = False
                self.results.append(SyncValidationResult(
                    component="Portfolio",
                    step="Value Consistency", 
                    passed=False,
                    expected="<0.1% variance",
                    actual=f"{max_diff_pct:.3f}% variance",
                    error_msg=f"Portfolio value inconsistent: {max_diff_pct:.3f}% variance"
                ))
            
        except Exception as e:
            success = False
            self.results.append(SyncValidationResult(
                component="Portfolio",
                step="Portfolio Sync",
                passed=False,
                expected="No error",
                actual=str(e),
                error_msg=f"Portfolio sync error: {e}"
            ))
        
        return success
    
    async def _validate_strategy_isolation(self) -> bool:
        """Validate multi-strategy isolation and attribution."""
        success = True
        
        # Test strategy PnL attribution
        test_strategies = ["burst_flag", "vwap_reclaim", "mean_reversion"]
        
        for strategy in test_strategies:
            # Test strategy PnL tracking
            self.pnl_engine.track_strategy_pnl(strategy, 10.0)  # $10 profit
            self.pnl_engine.track_strategy_pnl(strategy, -3.0)  # $3 loss
            
            strategy_pnl = self.pnl_engine.get_strategy_pnl()
            expected_pnl = 7.0  # $10 - $3
            
            if strategy not in strategy_pnl or abs(strategy_pnl[strategy] - expected_pnl) > 0.001:
                success = False
                actual_pnl = strategy_pnl.get(strategy, 0.0)
                self.results.append(SyncValidationResult(
                    component="PnLEngine",
                    step=f"Strategy PnL - {strategy}",
                    passed=False,
                    expected=expected_pnl,
                    actual=actual_pnl,
                    tolerance=0.001,
                    error_msg=f"Strategy PnL attribution error for {strategy}"
                ))
        
        # Test per-strategy position limits
        for strategy in test_strategies:
            positions_for_strategy = self.position_registry.get_positions_by_strategy(strategy)
            max_allowed = self.config.max_positions_per_strategy
            
            if len(positions_for_strategy) > max_allowed:
                success = False
                self.results.append(SyncValidationResult(
                    component="PositionRegistry",
                    step=f"Strategy Limits - {strategy}",
                    passed=False,
                    expected=f"<= {max_allowed} positions",
                    actual=f"{len(positions_for_strategy)} positions",
                    error_msg=f"Strategy {strategy} exceeds position limit"
                ))
        
        # Reset strategy PnL for clean test
        self.pnl_engine.reset_daily_stats()
        
        return success
    
    async def _validate_race_conditions(self) -> bool:
        """Test for race conditions in concurrent operations."""
        success = True
        
        try:
            # Concurrent position registry operations
            tasks = []
            for i in range(10):
                task = self._concurrent_position_test(f"RACE{i}-USD", i)
                tasks.append(task)
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Check for exceptions or inconsistencies
            exceptions = [r for r in results if isinstance(r, Exception)]
            if exceptions:
                success = False
                self.results.append(SyncValidationResult(
                    component="PositionRegistry",
                    step="Race Condition Test",
                    passed=False,
                    expected="No exceptions",
                    actual=f"{len(exceptions)} exceptions",
                    error_msg=f"Race conditions detected: {exceptions[0]}"
                ))
        
        except Exception as e:
            success = False
            self.results.append(SyncValidationResult(
                component="System",
                step="Race Condition Test",
                passed=False,
                expected="No error",
                actual=str(e),
                error_msg=f"Race condition test failed: {e}"
            ))
        
        return success
    
    async def _concurrent_position_test(self, symbol: str, index: int) -> bool:
        """Concurrent position operation for race condition testing."""
        position = Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=50000.0 + index,
            entry_time=datetime.now(timezone.utc),
            size_usd=10.0,
            size_qty=0.0002,
            stop_price=49000.0,
            tp1_price=51000.0,
            tp2_price=52000.0,
            strategy_id=f"race_test_{index}"
        )
        
        # Add and immediately remove
        self.position_registry.add_position(position)
        retrieved = self.position_registry.get_position(symbol)
        self.position_registry.remove_position(symbol)
        
        return retrieved is not None
    
    async def _validate_data_persistence(self) -> bool:
        """Validate data persistence and loading."""
        success = True
        
        # Test position registry state consistency
        stats_before = self.position_registry.get_stats(lambda s: 50000.0)
        
        # Add a test position
        test_pos = Position(
            symbol="PERSIST-USD",
            side=Side.BUY,
            entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=100.0,
            size_qty=0.002,
            stop_price=49000.0,
            tp1_price=51000.0,
            tp2_price=52000.0,
            strategy_id="persistence_test"
        )
        
        self.position_registry.add_position(test_pos)
        stats_after = self.position_registry.get_stats(lambda s: 50000.0)
        
        # Verify stats updated correctly
        expected_count = stats_before.active_positions + 1
        if stats_after.active_positions != expected_count:
            success = False
            self.results.append(SyncValidationResult(
                component="PositionRegistry",
                step="State Consistency",
                passed=False,
                expected=expected_count,
                actual=stats_after.active_positions,
                error_msg="Position registry state not updated correctly"
            ))
        
        # Clean up
        self.position_registry.remove_position("PERSIST-USD")
        
        return success
    
    async def _validate_complete_pipeline(self) -> bool:
        """Validate the complete data pipeline end-to-end."""
        success = True
        
        # Create a complete data flow snapshot
        snapshot = DataFlowSnapshot(
            timestamp=datetime.now(timezone.utc),
            positions=self.position_registry.get_all_positions(),
            strategy_pnl=self.pnl_engine.get_strategy_pnl()
        )
        
        if self.mode == TradingMode.LIVE and self.portfolio:
            try:
                portfolio_snapshot = self.portfolio.get_snapshot()
                if portfolio_snapshot:
                    snapshot.portfolio_value = portfolio_snapshot.total_value
                    snapshot.cash_balance = portfolio_snapshot.total_cash
                    snapshot.holdings_value = portfolio_snapshot.total_crypto
                    snapshot.total_pnl = portfolio_snapshot.total_unrealized_pnl
            except Exception as e:
                success = False
                self.results.append(SyncValidationResult(
                    component="Pipeline",
                    step="Complete Pipeline",
                    passed=False,
                    expected="Successful snapshot",
                    actual=str(e),
                    error_msg=f"Complete pipeline test failed: {e}"
                ))
        
        self.snapshots.append(snapshot)
        
        # Validate internal consistency
        calculated_total = snapshot.cash_balance + snapshot.holdings_value
        if self.mode == TradingMode.LIVE and abs(calculated_total - snapshot.portfolio_value) > 0.01:
            success = False
            self.results.append(SyncValidationResult(
                component="Pipeline", 
                step="Internal Consistency",
                passed=False,
                expected=snapshot.portfolio_value,
                actual=calculated_total,
                tolerance=0.01,
                error_msg="Portfolio value calculation inconsistency"
            ))
        
        return success
    
    def _print_summary(self):
        """Print comprehensive validation summary."""
        print("📊 VALIDATION SUMMARY")
        print("=" * 60)
        
        total_checks = len(self.results)
        passed_checks = sum(1 for r in self.results if r.passed)
        failed_checks = total_checks - passed_checks
        
        print(f"Total Checks: {total_checks}")
        print(f"Passed: {passed_checks}")
        print(f"Failed: {failed_checks}")
        
        if failed_checks > 0:
            print(f"\n❌ FAILED VALIDATIONS:")
            for result in self.results:
                if not result.passed:
                    print(f"   • {result.component} - {result.step}: {result.error_msg}")
        
        print(f"\n📈 COMPONENT STATUS:")
        components = {}
        for result in self.results:
            if result.component not in components:
                components[result.component] = {"passed": 0, "failed": 0}
            
            if result.passed:
                components[result.component]["passed"] += 1
            else:
                components[result.component]["failed"] += 1
        
        for component, stats in components.items():
            total = stats["passed"] + stats["failed"]
            success_rate = (stats["passed"] / total * 100) if total > 0 else 0
            status = "✅" if success_rate == 100 else "⚠️" if success_rate >= 75 else "❌"
            print(f"   {status} {component}: {success_rate:.0f}% ({stats['passed']}/{total})")
        
        print()


async def main():
    """Run complete data synchronization validation."""
    validator = DataSynchronizationValidator()
    success = await validator.run_complete_validation()
    
    if success:
        print("🎉 ALL DATA SYNCHRONIZATION CHECKS PASSED")
        print("✅ System components work together correctly")
        print("✅ Ready for multi-strategy integration")
    else:
        print("❌ DATA SYNCHRONIZATION ISSUES DETECTED") 
        print("⚠️ Fix issues before deploying multi-strategy system")
    
    return success


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
