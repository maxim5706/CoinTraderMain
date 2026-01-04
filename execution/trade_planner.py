"""Trade planning: convert intents into executable trade plans."""

from dataclasses import dataclass
from typing import Optional

from core.helpers import GateReason
from core.models import Intent, TradePlan, SignalType
from execution.entry_gates import (
    EntryGateChecker,
    GateCheck,
    GateResult,
    PositionSizer,
    SizingResult,
    calculate_stops,
    validate_rr_ratio,
)


@dataclass
class PlanResult:
    """Plan result with rejection context."""
    plan: Optional[TradePlan]
    gate_result: GateResult
    entry_score: Optional[object] = None
    sizing: Optional[SizingResult] = None


class TradePlanner:
    """Risk/position planning for trade intents."""

    def __init__(
        self,
        positions: dict,
        position_registry,
        daily_stats,
        circuit_breaker,
        order_cooldown: dict,
        exchange_holdings: dict,
        cooldown_seconds: int,
        get_candle_buffer_func,
        exchange_sync,
        config,
        is_test: bool = False,
    ):
        self.positions = positions
        self.position_registry = position_registry
        self.exchange_sync = exchange_sync
        self.config = config
        self.is_test = is_test
        self._gate_checker = EntryGateChecker(
            positions=positions,
            position_registry=position_registry,
            daily_stats=daily_stats,
            circuit_breaker=circuit_breaker,
            order_cooldown=order_cooldown,
            exchange_holdings=exchange_holdings,
            cooldown_seconds=cooldown_seconds,
            get_candle_buffer_func=get_candle_buffer_func,
            is_test=is_test,
        )

    def plan_trade(self, intent: Intent, portfolio_value: float, get_price_func) -> PlanResult:
        """Run gates, sizing, and stop/TP planning."""
        gate_result, entry_score = self._gate_checker.check_all_gates(
            intent, skip_position_registry=True
        )
        trace = list(getattr(gate_result, "trace", []) or [])

        def record_gate(name: str, passed: bool, reason: str = "", details: Optional[dict] = None):
            trace.append(GateCheck(
                name=name,
                passed=passed,
                reason=reason,
                details=details or {},
            ))

        if not gate_result.passed:
            gate_result.trace = trace
            return PlanResult(None, gate_result, entry_score=entry_score)

        # Truth/sync validation belongs to risk planning.
        if not self.exchange_sync.validate_before_trade(intent.symbol, get_price_func):
            record_gate("sync_truth", False, "sync_failed", {"reason": "sync_failed"})
            return PlanResult(
                None,
                GateResult(False, "sync_failed", GateReason.TRUTH, {"reason": "sync_failed"}, trace=trace),
                entry_score=entry_score,
            )
        record_gate("sync_truth", True)

        sizer = PositionSizer(self.positions, self.config)
        pv = portfolio_value if portfolio_value > 0 else 500.0
        sizing = sizer.calculate_size(entry_score, intent, pv)

        if sizing.size_usd <= 0 or sizing.available_budget <= 0:
            record_gate("budget", False, "budget_exceeded", {
                "available": sizing.available_budget,
                "size_usd": sizing.size_usd,
            })
            return PlanResult(
                None,
                GateResult(False, "budget_exceeded", GateReason.BUDGET, {"available": sizing.available_budget}, trace=trace),
                entry_score=entry_score,
                sizing=sizing,
            )
        record_gate("budget", True, {
            "available": sizing.available_budget,
            "size_usd": sizing.size_usd,
        })

        if sizing.size_usd < sizing.min_order_usd:
            record_gate("min_position", False, "below_min_position", {
                "min_order_usd": sizing.min_order_usd,
                "size_usd": sizing.size_usd,
            })
            return PlanResult(
                None,
                GateResult(
                    False,
                    "below_min_position",
                    GateReason.LIMITS,
                    {"min_order_usd": sizing.min_order_usd, "size_usd": sizing.size_usd},
                    trace=trace,
                ),
                entry_score=entry_score,
                sizing=sizing,
            )
        record_gate("min_position", True, {
            "min_order_usd": sizing.min_order_usd,
            "size_usd": sizing.size_usd,
        })

        can_open, limit_reason = self.position_registry.can_open_position(
            intent.strategy_id or "default",
            sizing.size_usd,
        )
        if not can_open:
            record_gate("registry_limits", False, limit_reason, {
                "reason": limit_reason,
                "size_usd": sizing.size_usd,
            })
            return PlanResult(
                None,
                GateResult(False, limit_reason, GateReason.LIMITS, {"reason": limit_reason}, trace=trace),
                entry_score=entry_score,
                sizing=sizing,
            )
        record_gate("registry_limits", True, {"size_usd": sizing.size_usd})

        is_fast = intent.type == SignalType.FAST_BREAKOUT
        stop_price, tp1_price, tp2_price, time_stop_min = calculate_stops(
            intent.price, is_fast, self.config, intent.symbol
        )

        valid_rr, rr_ratio, rr_reason = validate_rr_ratio(
            intent.price, stop_price, tp1_price, self.config.min_rr_ratio, self.is_test
        )
        if not valid_rr:
            record_gate("rr_ratio", False, rr_reason, {"rr_ratio": rr_ratio})
            return PlanResult(
                None,
                GateResult(False, rr_reason, GateReason.RR, {"rr_ratio": rr_ratio}, trace=trace),
                entry_score=entry_score,
                sizing=sizing,
            )
        record_gate("rr_ratio", True, {
            "rr_ratio": rr_ratio,
            "min_rr_ratio": self.config.min_rr_ratio,
        })

        plan = TradePlan(
            intent=intent,
            size_usd=sizing.size_usd,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            time_stop_min=time_stop_min,
            rr_ratio=rr_ratio,
            tier=sizing.tier,
            tier_code=getattr(sizing, "tier_code", "normal"),
            confluence=sizing.confluence,
            entry_score=float(getattr(entry_score, "total_score", 0.0) or 0.0),
            available_budget=sizing.available_budget,
            metadata={
                "session_mult": sizing.session_mult,
                "max_trade_usd": sizing.max_trade_usd,
                "min_order_usd": sizing.min_order_usd,
                "portfolio_value": pv,
                "exposure_usd": sizing.current_exposure,
            },
        )

        gate_result.trace = trace
        return PlanResult(plan, gate_result, entry_score=entry_score, sizing=sizing)
