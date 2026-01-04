# Config Ideology

This document defines config ownership, sizing precedence, and usage inventory.

## Boundaries (2025-12-30)

- Strategy emits **Intent** only (symbol/type/score/confidence/reason). Adapter remains for legacy `StrategySignal` → `Signal`.
- Risk/Position planning emits **TradePlan** (final size, stops/TPs, time-stop, rr).
- OrderRouter executes **TradePlan** without mutating size or risk decisions.
- Portfolio/Ledger updates only from fills and reconciliation.

## Sizing Precedence (2025-12-30)

Default sizing order:
1) Tier USD by score (whale/strong/normal/scout)
2) Clamp to `max_trade_usd`
3) Clamp to remaining exposure (`portfolio_max_exposure_pct`)
4) Enforce min order (`position_min_usd` / config `min_position_usd`)

Percent-based guardrails (`position_min_pct`/`position_max_pct`) apply before max-trade clamp to prevent tiny or oversized orders.

## Inventory

Columns: key name, module usage, domain owner, duplication risk.

### Strategy

| key | modules | owner | dup risk | notes |
| --- | --- | --- | --- | --- |
| `watch_coins` | (no direct settings usage found) | Strategy | no | Used via settings.coins property. |
| `vol_spike_threshold` | core/config_manager.py, core/mode_config.py, logic/strategy.py, tests/test_config_runtime_flow.py | Strategy | yes |  |
| `range_spike_threshold` | core/mode_config.py, logic/strategy.py | Strategy | yes |  |
| `impulse_min_pct` | logic/strategy.py | Strategy | no |  |
| `flag_retrace_min` | logic/strategy.py | Strategy | no |  |
| `flag_retrace_max` | logic/strategy.py | Strategy | no |  |
| `flag_vol_decay` | logic/strategy.py | Strategy | no |  |
| `breakout_buffer_pct` | logic/strategy.py | Strategy | no |  |
| `breakout_vol_mult` | logic/strategy.py | Strategy | no |  |
| `triple_top_tolerance_pct` | logic/strategy.py | Strategy | no |  |
| `hs_shoulder_tolerance_pct` | logic/strategy.py | Strategy | no |  |
| `fast_mode_enabled` | core/config_manager.py, core/mode_config.py, logic/strategy.py | Strategy | yes |  |
| `fast_confidence_min` | core/config_manager.py, logic/strategy.py | Strategy | yes |  |
| `ml_min_confidence` | core/mode_config.py, logic/scoring.py | Strategy | yes |  |
| `ml_boost_scale` | logic/scoring.py | Strategy | no |  |
| `ml_boost_min` | logic/scoring.py | Strategy | no |  |
| `ml_boost_max` | logic/scoring.py | Strategy | no |  |
| `base_score_strict_cutoff` | logic/scoring.py | Strategy | no |  |

### Risk/Sizing

| key | modules | owner | dup risk | notes |
| --- | --- | --- | --- | --- |
| `portfolio_max_exposure_pct` | core/config_manager.py, core/mode_config.py, execution/entry_gates.py, execution/rebalancer.py, run_v2.py, ui/web_server.py | Risk/Sizing | yes |  |
| `position_base_pct` | core/config_manager.py | Risk/Sizing | no |  |
| `position_min_pct` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `position_max_pct` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `max_trade_usd` | core/mode_config.py, execution/entry_gates.py, tests/test_runtime_config_store.py | Risk/Sizing | yes |  |
| `daily_max_loss_usd` | core/config_manager.py, core/mode_config.py, execution/risk.py, run_headless.py, run_v2.py, ui/web_server.py | Risk/Sizing | yes |  |
| `max_positions` | core/mode_config.py, core/position_registry.py, execution/signal_batch.py, run_headless.py, ui/web_server.py | Risk/Sizing | yes |  |
| `whale_trade_usd` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `whale_score_min` | execution/entry_gates.py | Risk/Sizing | no |  |
| `whale_confluence_min` | execution/entry_gates.py | Risk/Sizing | no |  |
| `strong_trade_usd` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `strong_score_min` | execution/entry_gates.py | Risk/Sizing | no |  |
| `normal_trade_usd` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `scout_trade_usd` | core/config_manager.py, execution/entry_gates.py | Risk/Sizing | yes |  |
| `scout_score_min` | execution/entry_gates.py | Risk/Sizing | no |  |
| `whale_max_positions` | execution/entry_gates.py | Risk/Sizing | no |  |
| `strong_max_positions` | execution/entry_gates.py | Risk/Sizing | no |  |
| `scout_max_positions` | execution/entry_gates.py | Risk/Sizing | no |  |
| `fixed_stop_pct` | core/config_manager.py, core/mode_config.py, core/persistence.py, execution/exchange_sync.py, logic/strategies/daily_momentum.py, logic/strategy.py, run_v2.py, tests/test_core.py, ui/web_server.py | Risk/Sizing | yes |  |
| `tp1_pct` | core/config_manager.py, core/mode_config.py, core/persistence.py, execution/exchange_sync.py, logic/strategies/daily_momentum.py, logic/strategy.py, run_v2.py, tests/test_core.py, ui/web_server.py | Risk/Sizing | yes |  |
| `tp2_pct` | core/config_manager.py, core/mode_config.py, core/persistence.py, execution/exchange_sync.py, logic/strategies/daily_momentum.py, logic/strategy.py, run_v2.py, tests/test_core.py, ui/web_server.py | Risk/Sizing | yes |  |
| `tp1_partial_pct` | core/config_manager.py, core/mode_config.py, execution/exit_manager.py | Risk/Sizing | yes |  |
| `stop_atr_mult` | logic/strategy.py | Risk/Sizing | no |  |
| `tp2_impulse_mult` | logic/strategy.py | Risk/Sizing | no |  |
| `max_hold_minutes` | core/config_manager.py, core/mode_config.py, core/persistence.py, execution/entry_gates.py, execution/exchange_sync.py, run_v2.py | Risk/Sizing | yes |  |
| `time_stop_enabled` | core/mode_config.py, execution/exit_manager.py | Risk/Sizing | yes |  |
| `min_rr_ratio` | core/config_manager.py, core/mode_config.py, execution/trade_planner.py, logic/strategy.py, ui/web_server.py | Risk/Sizing | yes |  |
| `trail_be_trigger_pct` | core/mode_config.py, execution/exit_manager.py | Risk/Sizing | yes |  |
| `trail_start_pct` | core/mode_config.py, execution/exit_manager.py | Risk/Sizing | yes |  |
| `trail_lock_pct` | core/mode_config.py, execution/exit_manager.py | Risk/Sizing | yes |  |
| `fast_spread_max_bps` | core/config_manager.py, logic/strategy.py | Risk/Sizing | yes |  |
| `fast_stop_pct` | core/config_manager.py, execution/entry_gates.py, run_v2.py | Risk/Sizing | yes |  |
| `fast_tp1_pct` | core/config_manager.py, execution/entry_gates.py, run_v2.py | Risk/Sizing | yes |  |
| `fast_tp2_pct` | execution/entry_gates.py, run_v2.py | Risk/Sizing | yes |  |
| `fast_time_stop_min` | execution/entry_gates.py, run_v2.py | Risk/Sizing | yes |  |
| `entry_score_min` | core/config_manager.py, execution/entry_gates.py, logic/scoring.py, run_headless.py, ui/web_server.py | Risk/Sizing | yes |  |
| `thesis_trend_flip_5m` | execution/exit_manager.py | Risk/Sizing | no |  |
| `thesis_trend_flip_15m` | (no direct settings usage found) | Risk/Sizing | no | Defined but not referenced outside Settings. |
| `thesis_vwap_distance` | execution/exit_manager.py | Risk/Sizing | no |  |
| `spread_max_bps` | core/config_manager.py, execution/entry_gates.py, ui/web_server.py | Risk/Sizing | yes |  |

### Execution

| key | modules | owner | dup risk | notes |
| --- | --- | --- | --- | --- |
| `coinbase_api_key` | core/mode_config.py, core/portfolio.py, execution/exchange_sync.py, run_v2.py, ui/web_server.py | Execution | yes |  |
| `coinbase_api_secret` | core/mode_config.py, core/portfolio.py, execution/exchange_sync.py, ui/web_server.py | Execution | yes |  |
| `trading_mode` | core/config.py, core/mode_config.py, run_v2.py | Execution | yes |  |
| `profile` | core/config.py, execution/order_router.py, logic/limits.py, run_v2.py | Execution | yes |  |
| `taker_fee_pct` | core/mode_config.py | Execution | no | Used via mode config (config.taker_fee_pct) in executors/exit manager. |
| `maker_fee_pct` | core/mode_config.py | Execution | no | Used via mode config (config.maker_fee_pct) in executors/exit manager. |
| `use_limit_orders` | core/mode_config.py | Execution | no |  |
| `limit_buffer_pct` | core/mode_config.py | Execution | no |  |
| `stop_health_check_interval` | execution/exit_manager.py | Execution | no |  |

### Portfolio/Reconciliation

| key | modules | owner | dup risk | notes |
| --- | --- | --- | --- | --- |
| `paper_start_balance_usd` | core/mode_config.py, run_v2.py | Portfolio/Reconciliation | yes |  |
| `position_min_usd` | core/helpers/portfolio.py, core/mode_config.py, core/persistence.py, core/position_registry.py, execution/entry_gates.py, execution/exchange_sync.py | Portfolio/Reconciliation | yes |  |
| `position_dust_usd` | core/mode_config.py, core/position_registry.py, execution/exchange_sync.py | Portfolio/Reconciliation | yes |  |
| `position_qty_drift_tolerance` | execution/exchange_sync.py | Portfolio/Reconciliation | no |  |
| `position_verify_tolerance` | execution/exchange_sync.py | Portfolio/Reconciliation | no |  |

### Safety

| key | modules | owner | dup risk | notes |
| --- | --- | --- | --- | --- |
| `symbol_whitelist` | execution/entry_gates.py | Safety | no |  |
| `use_whitelist` | core/mode_config.py, execution/entry_gates.py | Safety | yes |  |
| `min_24h_volume_usd` | (no direct settings usage found) | Safety | no | Defined but not referenced outside Settings. |
| `order_cooldown_seconds` | execution/order_router.py, execution/risk.py | Safety | yes |  |
| `order_cooldown_min_seconds` | execution/entry_gates.py | Safety | no |  |
| `circuit_breaker_max_failures` | execution/risk.py | Safety | no |  |
| `circuit_breaker_reset_seconds` | execution/risk.py | Safety | no |  |
| `ignored_symbols` | (no direct settings usage found) | Safety | no | Used via settings.ignored_symbol_set property. |

## Migration Notes

- `max_positions` is a **hard cap** enforced by `PositionRegistry`.
- `position_min_usd` (settings) and `min_position_usd` (mode config) are kept in sync via `ConfigurationManager` base config.
- Snapshot redaction preserves API secret masking while keeping dashboard diff stable.
