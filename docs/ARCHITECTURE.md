# CoinTrader Architecture

> **Core Reference Document** - The source of truth for system design and dependencies.
> 
> **Version:** 1.0 | **Exchange:** Coinbase | **Reviewed:** 2025-12-17

---

## System Metaphor: The Car

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              THE VEHICLE                                     │
│                                                                              │
│   🔑 IGNITION (Config)         One key starts everything                    │
│   🚗 CHASSIS (Models)          The frame everything bolts to                │
│   ⚡ ELECTRICAL (State/Events)  Connects all systems                        │
│   💾 MEMORY (Persistence)       Remembers state across restarts             │
│   🛡️ SAFETY (Risk)              Prevents dangerous operations               │
│   🧠 BRAIN (Strategies)         Makes driving decisions                     │
│   👁️ SENSORS (Datafeeds)        Sees the road                               │
│   🦿 ACTUATORS (Execution)      Moves the wheels                            │
│   📊 DASHBOARD (UI)             Shows the driver what's happening           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Layer Dependency Order

> **Note:** These are *conceptual layers* for understanding the architecture.
> Runtime startup order differs: datafeeds start before strategy evaluation.

```
CONCEPTUAL LAYERS (grouped by responsibility):

┌─ FOUNDATION ────────────────────────────────────────────────────────────────┐
│  L0: Environment     .env, secrets                                          │
│  L1: Config          config.py, mode_configs.py, mode_config.py             │
│  L2: Models          Candle, Position, Signal, TradeResult                  │
│  L3: State/Events    BotState, MarketEventBus                               │
│  L4: Persistence     Atomic JSON, backup/recovery                           │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─ DATA LAYER ────────────────────────────────────────────────────────────────┐
│  L5: Datafeeds       WebSocket, REST polling, symbol universe               │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─ DECISIONING ───────────────────────────────────────────────────────────────┐
│  L6: Intelligence    Indicators, ML scoring, regime detection               │
│  L7: Strategies      9 pattern detectors, orchestrator                      │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─ EXECUTION ─────────────────────────────────────────────────────────────────┐
│  L8: Risk            Policy layer: gates, limits, circuit breaker           │
│  L9: Order Router    Central coordinator, invokes Risk before every order   │
│  L10: Executors      Paper/Live order placement                             │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─ PRESENTATION ──────────────────────────────────────────────────────────────┐
│  L11: UI             Dashboard, web interface                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Runtime Startup Order

```
1. Config          → Load .env, validate settings
2. Models          → Pure data structures (no I/O)
3. State/Events    → Initialize event bus
4. Persistence     → Load saved positions, recover from backup if needed
5. Datafeeds       → Connect WebSocket, start REST polling, backfill history
6. Intelligence    → Initialize indicators (uses datafeed output)
7. Risk            → Load daily stats, cooldowns (before any order placement)
8. Strategies      → Ready to analyze (may compute signals; Risk gates orders)
9. Order Router    → Wire up executor, portfolio, persistence (for order placement)
10. UI             → Start dashboard display loop

Note: Strategies may compute signals regardless of Risk state; Risk gates order
placement, not signal generation. This separation allows signal logging even
when trading is paused.
```

---

## Runtime Architecture (The Three Clocks)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        TradingBotV2 (run_v2.py)                              │
│                                                                              │
│         ┌─────────────────────────────────────────────────────────┐         │
│         │            ORCHESTRATION / COMPOSITION                   │         │
│         │                                                          │         │
│         │   TradingContainer ── TradingFactory ── TradingInterfaces │         │
│         │         │                    │                           │         │
│         │         ├── get_executor()   ├── create_executor()      │         │
│         │         ├── get_portfolio()  ├── create_portfolio()     │         │
│         │         ├── get_persistence()├── create_persistence()   │         │
│         │         └── get_stop_manager()└── create_stop_manager() │         │
│         │                                                          │         │
│         │   Mode: PAPER ──► Paper implementations                  │         │
│         │   Mode: LIVE  ──► Live implementations                   │         │
│         └─────────────────────────────────────────────────────────┘         │
│                                    │                                         │
│    ┌───────────────────────────────┼───────────────────────────────┐        │
│    │                               │                               │        │
│    ▼                               ▼                               ▼        │
│  ┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐     │
│  │    CLOCK A      │      │    CLOCK B      │      │    CLOCK C      │     │
│  │  EVENT-DRIVEN   │      │  POLLING LOOP   │      │   SLOW LOOP     │     │
│  ├─────────────────┤      ├─────────────────┤      ├─────────────────┤     │
│  │ WebSocket       │      │ Every 5 seconds │      │ Every 30 min    │     │
│  │ callbacks       │      │                 │      │                 │     │
│  │                 │      │ • Strategy eval │      │ • Universe      │     │
│  │ • _on_tick()    │      │ • Exit checks   │      │   refresh       │     │
│  │ • _on_candle()  │      │ • Portfolio sync│      │ • Tier reassign │     │
│  │                 │      │ • ML refresh    │      │ • Backfill      │     │
│  │ Triggers:       │      │                 │      │   queue         │     │
│  │ state updates,  │      │                 │      │                 │     │
│  │ candle buffer   │      │                 │      │                 │     │
│  └─────────────────┘      └─────────────────┘      └─────────────────┘     │
│                                                                              │
│  CONCURRENCY NOTE:                                                           │
│  Clock A is event-driven (callbacks from WebSocket library).                │
│  Clocks B and C are asyncio polling loops.                                  │
│  Shared state (positions, buffers) requires careful access patterns.        │
│                                                                              │
│  ACCESS PATTERN: Clock A is the sole writer for candle buffers (append-only);│
│  Clock B is the sole writer for positions. Buffers use append-only semantics;│
│  consumers read tail snapshots (copy last N candles into local list before   │
│  analysis). No explicit locks; single-threaded asyncio event loop.           │
│                                                                              │
│  APPEND-ONLY: Never mutate or delete historical candle entries in-place;    │
│  only append new candles and rotate by replacing entire buffer reference.   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DATA FLOW PIPELINE                                 │
│                                                                              │
│   MARKET DATA ─── FEATURES ─── SIGNALS ─── RISK ─── ORDERS ─── STATE       │
│       │              │            │          │         │          │         │
│       ▼              ▼            ▼          ▼         ▼          ▼         │
│   ┌────────┐    ┌────────┐   ┌────────┐  ┌────────┐ ┌────────┐ ┌────────┐  │
│   │Coinbase│    │intelli-│   │orches- │  │order_  │ │executor│ │persist-│  │
│   │WS/REST │    │gence   │   │trator  │  │router  │ │        │ │ence    │  │
│   │        │───►│edge_   │──►│+9 strat│─►│15+ gate│─►│paper/  │─►│state   │  │
│   │collect-│    │model   │   │egies   │  │checks  │ │live    │ │events  │  │
│   │ors     │    │        │   │        │  │        │ │        │ │        │  │
│   └────────┘    └────────┘   └────────┘  └────────┘ └────────┘ └────────┘  │
│       │              │            │          │         │          │         │
│       │              │            │          │         │          │         │
│   Candles,       Indicators,  StrategySignal  Gates:   OrderResult Position │
│   Ticks,         Scores,      edge_score     budget,  fill_price, PnL,     │
│   OHLCV          Regime       entry/stop/tp  spread,  qty         State    │
│                                              regime,                        │
│                                              cooldown                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Invariants (System Rules That Must Hold)

| # | Rule | Choke Point | Verified By |
|---|------|-------------|-------------|
| 1 | Every order passes risk gates | `OrderRouter.open_position()` calls 15+ gate checks before execution | Unit test + runtime logging |
| 2 | State persists on every trade | `persistence.save_positions()` called after open/close | Integration test |
| 3 | Positions survive restarts | Atomic writes: temp → fsync → rename; backup on overwrite | Recovery test |
| 4 | Daily loss limit stops trading | `DailyStats.should_stop` → sets kill_switch in BotState | Runtime assertion |
| 5 | Circuit breaker on API failures | `CircuitBreaker.record_failure()` after N consecutive failures | Logging |
| 6 | No duplicate positions per symbol | `OrderRouter.has_position()` check before open | Unit test |
| 7 | Graceful shutdown preserves positions | `TradingBotV2.stop()` calls `persistence.save_positions()` | Integration test |
| 8 | Strategy reads buffer via snapshot only | `CandleBuffer.get_candles()` returns copy; no strategy holds live buffer ref | Code review + unit test |

**Verification Definitions:**
- **Unit test**: Isolated test of single function/class
- **Integration test**: End-to-end test with real components
- **Recovery test**: Simulate crash/restart and verify state
- **Runtime assertion**: Code-level check that logs/throws on violation

---

## Cross-Cutting Concerns

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STATE / PERSISTENCE / EVENTS                              │
│                    (Touches Every Layer)                                     │
│                                                                              │
│   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐            │
│   │    state.py     │  │  persistence/   │  │    events.py    │            │
│   │                 │  │                 │  │                 │            │
│   │ BotState        │  │ base_persist*   │  │ MarketEventBus  │            │
│   │ FocusCoinState  │  │ paper_persist*  │  │ TickEvent       │            │
│   │ UniverseState   │  │ live_persist*   │  │ CandleEvent     │            │
│   │ PositionDisplay │  │ candle_store    │  │ OrderEvent      │            │
│   └────────┬────────┘  └────────┬────────┘  └────────┬────────┘            │
│            │                    │                    │                      │
│            └────────────────────┼────────────────────┘                      │
│                                 │                                           │
│                    All layers read/write through these                      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Module Responsibilities

### Core (The Foundation)

| Module | Responsibility | Depends On |
|--------|---------------|------------|
| `config.py` | Load settings from .env, validate | Environment |
| `mode_configs.py` | Paper vs Live config classes | config.py |
| `models/` | Data structures (Candle, Position, Signal) | Nothing |
| `state.py` | Runtime state for dashboard | models/ |
| `events.py` | Pub/sub event bus | models/ |
| `persistence/` | Atomic JSON save/load | models/, config |
| `trading_container.py` | Dependency injection | trading_factory |
| `trading_factory.py` | Creates mode-specific implementations | All components |
| `trading_interfaces.py` | Abstract contracts (IExecutor, etc.) | Nothing |

### Execution (The Actuators)

| Module | Responsibility | Depends On |
|--------|---------------|------------|
| `order_router.py` | Central trade coordinator | IExecutor, RiskManager (DailyStats, CircuitBreaker), Persistence, State/EventBus, Models, Config |
| `risk.py` | DailyStats, CircuitBreaker, Cooldowns | models, state, persistence, config |
| `paper_executor.py` | Simulated order execution | models, config |
| `live_executor.py` | Real Coinbase order execution | models, config |
| `paper_stops.py` | Simulated stop tracking | models |
| `live_stops.py` | Real exchange stop orders | config |

### Logic (The Brain)

| Module | Responsibility | Depends On |
|--------|---------------|------------|
| `intelligence.py` | Live indicators, ML scoring | models, datafeeds |
| `edge_model.py` | TrendAlignment, VolatilityRegime | models |
| `strategies/orchestrator.py` | Picks best signal per symbol | All strategies |
| `strategies/base.py` | BaseStrategy, StrategySignal | models |
| `strategies/*.py` | 9 pattern detectors | base, intelligence |

### Datafeeds (The Sensors)

| Module | Responsibility | Depends On |
|--------|---------------|------------|
| `collectors/` | WebSocket + REST candle collection | models |
| `universe/` | Symbol discovery, ranking, hotlist | config |
| `coinbase_fetcher.py` | REST API history fetcher | config |

---

## File Tree (Annotated)

```
CoinTrader/
├── run.py                    # Entry point (calls run_v2.main)
├── run_v2.py                 # TradingBotV2 - main orchestrator
├── run_headless.py           # Headless mode (no TUI)
├── .env                      # API keys, mode, secrets
│
├── core/                     # L0-L4: Foundation
│   ├── config.py             # L1: Settings loader (Pydantic)
│   ├── mode_config.py        # L1: Mode detection (which mode?)
│   ├── mode_configs.py       # L1: Mode definitions (Paper/Live classes)
│   ├── mode_paths.py         # L1: Mode-specific file paths
│   ├── profiles.py           # L1: Profile overrides
│   ├── models/               # L2: Data structures
│   │   ├── candle.py         #     Candle, CandleBuffer
│   │   ├── position.py       #     Position, PositionState, Side
│   │   ├── signal.py         #     Signal, SignalType
│   │   └── trade_result.py   #     TradeResult
│   ├── state.py              # L3: BotState for dashboard
│   ├── events.py             # L3: MarketEventBus
│   ├── helpers/              # L3: Utility functions
│   │   ├── gate_event.py     #     Gate rejection event creation
│   │   ├── portfolio.py      #     Portfolio helpers
│   │   ├── preflight.py      #     Startup validation
│   │   ├── reasons.py        #     GateReason enum
│   │   ├── rest_validation.py#     REST response validation
│   │   ├── validation.py     #     Candle validation
│   │   └── warmth.py         #     Buffer warmth checks
│   ├── base_persistence.py   # L4: Atomic writes base class ✅
│   ├── paper_persistence.py  # L4: Paper mode storage ✅
│   ├── live_persistence.py   # L4: Live mode storage ✅
│   ├── persistence.py        # L4: Facade + exchange sync
│   ├── candle_store.py       # L4: Disk-backed candle cache
│   ├── paper_portfolio.py    # L4: Paper portfolio tracking
│   ├── live_portfolio.py     # L4: Live portfolio from Coinbase
│   ├── portfolio.py          # L4: Portfolio tracker singleton
│   ├── pnl_engine.py         # L4: PnL calculations
│   ├── position_registry.py  # L4: Position limits enforcement
│   ├── trading_container.py  # DI container
│   ├── trading_factory.py    # Creates implementations
│   ├── trading_interfaces.py # Abstract contracts (IExecutor, etc.)
│   ├── alerts.py             # Telegram/Discord alerts
│   ├── logger.py             # JSONL trade logging
│   ├── logging_utils.py      # Logging configuration
│   └── signal_logger.py      # Signal event logging
│
├── datafeeds/                # L5: Sensors
│   ├── collectors/
│   │   ├── candle_collector.py  # WebSocket candle streaming
│   │   ├── rest_poller.py       # REST polling for Tier 2/3
│   │   └── dynamic_backfill.py  # Gap detection and backfill
│   ├── universe/
│   │   ├── symbol_scanner.py    # Symbol discovery/ranking
│   │   └── tiers.py             # Tier scheduler (T1/T2/T3)
│   ├── scanner_manager.py       # Symbol scanner lifecycle (moved from core/)
│   └── coinbase_fetcher.py      # REST history fetcher
│
├── logic/                    # L6-L7: Decisioning
│   ├── intelligence.py       # L6: Entry scoring, regime detection
│   ├── edge_model.py         # L6: TrendAlignment, VolatilityRegime
│   ├── live_features.py      # L6: Real-time feature extraction
│   ├── strategy.py           # L6: Legacy strategy module
│   └── strategies/           # L7: Pattern detectors
│       ├── orchestrator.py   #     Picks best signal per symbol
│       ├── base.py           #     BaseStrategy, StrategySignal
│       ├── burst_flag.py     #     Burst + flag pattern
│       ├── vwap_reclaim.py   #     VWAP reclaim
│       ├── momentum_1h.py    #     1H momentum
│       ├── bb_expansion.py   #     Bollinger band expansion
│       ├── daily_momentum.py #     Daily momentum
│       ├── range_breakout.py #     Range breakout
│       ├── relative_strength.py #  Relative strength
│       ├── rsi_momentum.py   #     RSI momentum
│       └── support_bounce.py #     Support bounce
│
├── execution/                # L8-L10: Execution
│   ├── risk.py               # L8: DailyStats, CircuitBreaker ✅
│   ├── order_router.py       # L9: Slim coordinator (~400 lines) ✅
│   ├── entry_gates.py        # L9: 21 gate checks + sizing ✅
│   ├── exit_manager.py       # L9: Exit logic (stops, TPs, thesis) ✅
│   ├── exchange_sync.py      # L9: Portfolio/position sync ✅
│   ├── signal_batch.py       # L9: Batch signal processing ✅
│   ├── rebalancer.py         # L9: Portfolio rebalancing ✅
│   ├── rejection_tracker.py  # L9: Gate rejection stats ✅
│   ├── order_manager.py      # L9: Order lifecycle tracking
│   ├── order_utils.py        # L9: Order helpers, rate limiter
│   ├── paper_executor.py     # L10: Simulated execution
│   ├── live_executor.py      # L10: Real Coinbase execution
│   ├── paper_stops.py        # L10: Simulated stops
│   └── live_stops.py         # L10: Exchange stop orders
│
├── ui/                       # L11: Presentation
│   ├── dashboard_v2.py       # TUI display (Rich)
│   ├── tui_live.py           # Live TUI components
│   ├── probe_monitor.py      # Health/probe monitoring (moved from core/)
│   └── web_server.py         # Web interface (FastAPI)
│
├── tests/                    # Test suite
│   ├── conftest.py           # Pytest fixtures
│   └── test_core.py          # Core module tests
│
├── tools/                    # Utility scripts (optional)
│
├── data/                     # Runtime data (gitignored)
│   ├── paper_positions.json
│   ├── live_positions.json
│   ├── cooldowns.json
│   └── candles/
│
├── logs/                     # JSONL logs (gitignored)
│
├── archive/                  # Deprecated code (gitignored)
│
└── docs/                     # Documentation
    ├── ARCHITECTURE.md       # This file
    └── GAMEPLAN.md           # Hardening roadmap
```

---

## Primary Control Loop

The canonical runtime entry point is `TradingBotV2.run()` in `run_v2.py`.

```
TradingBotV2 Responsibilities:

┌─────────────────────────────────────────────────────────────────────────────┐
│  start()                                                                     │
│  ├─ Preflight checks (API keys, mode validation)                            │
│  ├─ Initialize scanner, refresh universe                                    │
│  ├─ Initialize collector (WebSocket or Mock)                                │
│  ├─ Backfill initial candle history                                         │
│  ├─ Initialize OrderRouter with DI container                                │
│  ├─ Start Clock A (WebSocket task)                                          │
│  ├─ Start Clock B (5s polling loop)                                         │
│  ├─ Start Clock C (30min slow loop)                                         │
│  ├─ Start REST poller and backfill services                                 │
│  └─ await asyncio.gather() on all tasks                                     │
│                                                                              │
│  stop()                                                                      │
│  ├─ Set _running = False                                                    │
│  ├─ Save all open positions (do NOT liquidate)                              │
│  ├─ Stop collector (close WebSocket)                                        │
│  ├─ Stop REST poller and backfill                                           │
│  ├─ Flush candle store to disk                                              │
│  └─ Log shutdown complete                                                   │
│                                                                              │
│  Error Boundaries:                                                           │
│  • Each clock loop has try/except with logging                              │
│  • WebSocket has reconnect with exponential backoff                         │
│  • REST poller has rate limit degradation                                   │
│  • Order execution has circuit breaker                                      │
│                                                                              │
│  Restart Policy:                                                             │
│  • Positions persist across restarts (atomic JSON files)                    │
│  • Candle buffers rehydrate from disk cache                                 │
│  • Daily stats reset at UTC midnight                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Public API Contracts

Layer interfaces and their guarantees. Prevents accidental coupling and makes refactors safer.

### IDataFeed (Collectors)

```python
# Emits events via callbacks
on_tick(symbol: str, price: float, spread_bps: float) -> None
on_candle(symbol: str, candle: Candle) -> None

# Properties
is_connected: bool          # WebSocket connection state
is_receiving: bool          # Received data within last 30s
last_message_age: float     # Seconds since last message
```

### IIntelligence

```python
# Scoring and regime detection
get_edge_score(symbol: str) -> float                    # 0-100 composite score
get_regime() -> VolatilityRegime                        # LOW, NORMAL, HIGH, EXTREME
get_trend_alignment(symbol: str) -> TrendAlignment      # STRONG_UP, UP, NEUTRAL, DOWN, STRONG_DOWN
get_features(symbol: str) -> dict[str, float]           # All computed features for ML
```

### IStrategy (BaseStrategy)

```python
# Strategy evaluation
analyze(symbol: str, buffer: CandleBuffer, intelligence: Intelligence) -> StrategySignal | None
reset() -> None                                         # Clear internal state

# StrategySignal output
StrategySignal:
    symbol: str
    strategy_id: str
    direction: SignalDirection          # LONG, SHORT, FLAT
    edge_score_base: float              # 0-100
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float | None
    rr_ratio: float
    context: dict                       # Strategy-specific metadata
```

### IRiskManager (OrderRouter gates)

```python
# Risk evaluation (internal to OrderRouter)
_check_all_gates(signal: StrategySignal, price: float) -> tuple[bool, str]
    # Returns (allowed, reason)
    # reason is empty string if allowed, otherwise explains rejection
```

### IExecutor

```python
# Order execution (actual interface from trading_interfaces.py)
async open_position(
    symbol: str,
    size_usd: float,
    price: float,
    stop_price: float,
    tp1_price: float,
    tp2_price: float,
) -> Optional[Position]

async close_position(
    position: Position,
    price: float,
    reason: str,
) -> TradeResult

can_execute_order(size_usd: float, symbol: str | None = None) -> tuple[bool, str]
```

### IPortfolioManager

```python
get_available_balance() -> float
get_total_portfolio_value() -> float
update_portfolio_state() -> None
```

### IStopOrderManager

```python
place_stop_order(symbol: str, qty: float, stop_price: float) -> Optional[str]
update_stop_price(symbol: str, new_stop_price: float) -> bool
cancel_stop_order(symbol: str) -> bool
```

### IPositionPersistence

```python
save_positions(positions: dict[str, Position]) -> None  # Atomic write
load_positions() -> dict[str, Position]                 # With backup recovery
clear_position(symbol: str) -> None
```

---

## Event Taxonomy and Time Semantics

### Event Types (from core/events.py)

| Event | Source | Frequency | Fields |
|-------|--------|-----------|--------|
| `TickEvent` | WebSocket ticker | ~1-10/sec per symbol | symbol, price, spread_bps, source, ts |
| `CandleEvent` | Collector (on minute close) | 1/min per symbol | symbol, candle, tf, source, ts |
| `OrderEvent` | OrderRouter | On open/close/partial | event_type, symbol, side, mode, strategy_id, price, size_usd, size_qty, reason, pnl, pnl_pct, ts |

### Timestamp Semantics

- **Candle timestamp**: Represents **candle open time** (start of the minute)
- **Candle is complete**: When `timestamp < current_minute` (we've moved to next minute)
- **All timestamps**: UTC timezone, never local time

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| **Late/out-of-order ticks** | Ignored if older than current candle; logged at DEBUG |
| **WebSocket reconnect replay** | Dedupe by checking if candle already exists in buffer |
| **WS vs REST divergence** | REST is truth for historical; WS is truth for real-time price |
| **Gap in candle data** | Backfill service detects gaps and fetches missing candles via REST |

### Truth Sources

```
Real-time price      → WebSocket (lowest latency)
Historical candles   → REST API (authoritative)
Position state       → Local persistence (with exchange sync on restart)
Account balance      → Exchange API (fetched periodically)
```

---

## Failure Modes and Recovery Playbook

| Failure | Detection | Behavior | Recovery |
|---------|-----------|----------|----------|
| **WebSocket disconnect** | `is_connected=False` | Exponential backoff (1s→60s), max 10 attempts | Auto-reconnect; resubscribe all symbols |
| **WebSocket stale** | `last_message_age > 30s` | Log warning, trigger reconnect | Force close and reconnect |
| **REST rate limit (429)** | HTTP status code | Backoff 2^N seconds, degrade Tier 3 first | Token bucket refills; auto-resume |
| **Persistence write fail** | Exception on save | Log ERROR, retry once | If retry fails, positions stay in memory (risk: crash loses state) |
| **Partial fill** | `fill_qty < requested_qty` | Accept partial, adjust position size | Position reflects actual fill |
| **Order rejected** | `OrderResult.success=False` | Log reason, do not open position | Signal logged for analysis; cooldown may apply |
| **Exchange API error** | HTTP 5xx or timeout | Circuit breaker increments | After N failures, circuit opens (no new orders) |
| **Circuit breaker open** | `CircuitBreaker.is_open` | Reject all new orders | Auto-reset after cooldown period |
| **Daily loss limit hit** | `DailyStats.should_stop` | Set kill_switch=True | Manual reset or wait for UTC midnight |
| **Event loop blocked** | Heartbeat age check | Log warning if >5s between heartbeats | Indicates backpressure; reduce load |

### Restart Checklist

1. Load positions from persistence (with backup fallback)
2. Sync with exchange to reconcile any fills during downtime
3. Reload daily stats (or reset if new UTC day)
4. Clear stale cooldowns (expired entries)
5. Backfill candle gaps before enabling strategy evaluation

---

## State Model: Single Source of Truth

### Position Lifecycle

```
SIGNAL_GENERATED → OPENING → OPEN → [PARTIAL_CLOSE] → CLOSING → CLOSED
       │              │        │           │             │         │
       │              │        │           │             │         └─ Removed from active
       │              │        │           │             └─ Exit order placed
       │              │        │           └─ TP1 hit, partial exit
       │              │        └─ Entry order filled
       │              └─ Entry order placed
       └─ Strategy emits signal, gates pass
```

### Authoritative State Sources

| State | Authority | Sync Frequency |
|-------|-----------|----------------|
| **Open positions** | `OrderRouter.positions` dict | Real-time (in-memory) |
| **Persisted positions** | `data/{mode}_positions.json` | After every open/close |
| **Exchange positions** | Coinbase API | On startup + every 5 min |
| **Candle buffers** | `CandleBuffer` (in-memory) | Real-time from WebSocket |
| **Cached candles** | `CandleStore` (disk) | Flushed periodically |

### Reconciliation Rules

1. **On startup**: Load from persistence → sync with exchange → prune dust positions
2. **During runtime**: Local state is authoritative; exchange sync detects drift
3. **On conflict**: Exchange state wins for position existence; local state wins for metadata
4. **Recently closed guard**: 5-minute grace period prevents sync from re-adding closed positions

---

## Risk Gate Inventory

The OrderRouter performs **21 gate checks** in `_do_open_position()` before any order placement:

### Position Limits (5 gates)
1. **Max concurrent positions**: `len(positions) >= settings.max_positions`
2. **No duplicate positions**: `has_position(symbol)` check
3. **Symbol exposure limit**: Max $15 per symbol to prevent stacking
4. **Position registry limits**: `position_registry.can_open_position()`
5. **Exchange holdings check**: Skip if already holding on exchange (untracked)

### Financial Limits (3 gates)
6. **Daily loss limit**: `daily_stats.should_stop` halts all trading
7. **Budget available**: `size_usd > available_budget` (exposure % of portfolio)
8. **Trading halted**: `intelligence.is_trading_halted()` for external kill switch

### Market Conditions (2 gates)
9. **Spread filter**: `spread_bps > settings.spread_max_bps` (liquidity gate)
10. **Spread-adjusted score**: High spread requires higher entry score

### Timing Controls (3 gates)
11. **Symbol cooldown**: `symbol in _order_cooldown` with configurable duration
12. **Circuit breaker**: `_circuit_breaker.can_trade()` after API failures
13. **Warmup check**: `is_warm(symbol, buffer)` ensures sufficient candle history

### Signal Quality (5 gates)
14. **Signal type check**: Must be `FLAG_BREAKOUT` or `FAST_BREAKOUT`
15. **Entry score check**: `entry_score.should_enter` from intelligence layer
16. **Valid stop price**: `risk_per_share > 0` (stop below entry for long)
17. **R:R ratio check**: `rr_ratio >= config.min_rr_ratio`
18. **Stablecoin filter**: Skip USDT, USDC, DAI, etc.

### Operational (3 gates)
19. **Pre-trade validation**: `_validate_before_trade()` syncs with exchange
20. **Executor check**: `executor.can_execute_order(size_usd, symbol)`
21. **Whitelist gate**: Optional `settings.use_whitelist` for curated symbols

### Gate Categories (for rejection logging)
```python
GateReason = Literal[
    "limits",      # Position/exposure limits
    "spread",      # Liquidity/spread issues  
    "warmth",      # Insufficient candle history
    "score",       # Entry score too low
    "regime",      # Volatility regime filter
    "risk",        # Daily loss / trading halted
    "rr",          # R:R ratio check
    "truth",       # Exchange sync failed
    "circuit_breaker",
    "whitelist",
]
```

---

## Hardening Status

| Layer | Component | Status |
|-------|-----------|--------|
| L0 | Environment | ✅ Loaded via python-dotenv |
| L1 | Config | ✅ Hardened (Pydantic validators, range checks, R:R validation) |
| L2 | Models | ✅ Hardened (Position/Candle validation, `__repr__` for debugging) |
| L3 | State/Events | ✅ Hardened (handler error logging, duplicate fields fixed) |
| L4 | Persistence | ✅ Hardened (atomic writes, backup, recovery) |
| L5 | Datafeeds | ✅ Hardened (reconnect logic, rate limits, invalid data handling) |
| L6 | Intelligence | 🔲 Pending review |
| L7 | Strategies | ✅ Hardened (signal validation, `validate()` method) |
| L8 | Risk | ✅ Hardened (edge cases, atomic cooldowns) |
| L9 | Order Router | ✅ Hardened (safe removals, div-by-zero guards) |
| L10 | Executors | ✅ Coinbase-specific; implements IExecutor interface |
| L11 | UI | 🔲 Pending review |

**Exchange Abstraction:** Executors implement `IExecutor` interface. Coinbase is the only concrete live implementation in v1.0.

---

*Architecture v1.0 | Reviewed: 2025-12-17*
