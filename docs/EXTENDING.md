# Extending CoinTrader

> Best practices for adding features and maintaining the codebase.
> 
> **Version:** 1.0 | **Created:** 2025-12-21

---

## Architecture Principles

### 1. Separation of Concerns

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FEATURE EXTENSION POINTS                             │
│                                                                              │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│   │  STRATEGIES │  │   SIGNALS   │  │    GATES    │  │  EXECUTORS  │       │
│   │  (Entry)    │  │  (Events)   │  │   (Risk)    │  │  (Actions)  │       │
│   ├─────────────┤  ├─────────────┤  ├─────────────┤  ├─────────────┤       │
│   │ Add new     │  │ Subscribe   │  │ Add custom  │  │ New exchange│       │
│   │ pattern     │  │ to events   │  │ risk rules  │  │ integrations│       │
│   │ detectors   │  │ via bus     │  │ in gates    │  │             │       │
│   └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2. Key Invariants (Never Break These)

| Rule | Why | How to Verify |
|------|-----|---------------|
| **Every order passes risk gates** | Prevents runaway losses | `OrderRouter.open_position()` always calls gate checks |
| **State persists atomically** | Survives crashes | Temp file → fsync → rename pattern |
| **No duplicate positions** | Prevents double exposure | `has_position()` check + exchange sync |
| **Single writer per resource** | Avoids race conditions | Only COIN-BOT writes `bot_state.json` |

---

## Config Flow (2025-12-30)

- Boot snapshot: `start_config_live()` / `start_config_paper()` is immutable for the run.
- Runtime changes: `ConfigManager` applies updates to `settings` and persists to `data/runtime_config.json`.
- Refresh path: `RuntimeConfigStore.refresh()` rebuilds `running_config`, then `OrderRouter.update_config()` propagates to executors/portfolio/registry.
- **2025-12-29**: `config_start`/`config_running` snapshots are redacted (API keys) before exposure in shared state and HTTP endpoints.
- **2025-12-29**: `/api/config/refresh` forces a disk reload; dashboard shows start vs running diffs.
- **2025-12-30**: Execution boundary is `Intent` → `TradePlan` (`TradePlanner`); sizing precedence centralized in `PositionSizer`.
- If you add a new runtime-tunable setting, update `RuntimeConfig`, `PARAM_SETTINGS_MAP`, and `PARAM_VALIDATORS`.

---

## Adding a New Strategy

### Step 1: Create Strategy File

```python
# logic/strategies/my_strategy.py
from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection

class MyStrategy(BaseStrategy):
    """
    My custom pattern detector.
    
    Entry criteria:
    - [describe your entry logic]
    
    Exit criteria:
    - Stop: [describe stop logic]
    - TP1: [describe TP1 logic]
    """
    
    strategy_id = "my_strategy"
    name = "My Strategy"
    
    def analyze(self, symbol: str, buffer, intelligence) -> StrategySignal | None:
        candles = buffer.get_candles(symbol, count=50)
        if len(candles) < 50:
            return None
        
        # Your pattern detection logic here
        if self._detect_pattern(candles):
            entry = candles[-1].close
            stop = entry * 0.98  # 2% stop
            tp1 = entry * 1.03   # 3% target
            
            return StrategySignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                direction=SignalDirection.LONG,
                edge_score_base=75,
                entry_price=entry,
                stop_price=stop,
                tp1_price=tp1,
                tp2_price=None,
                rr_ratio=(tp1 - entry) / (entry - stop),
                context={"pattern": "my_pattern"}
            )
        return None
    
    def _detect_pattern(self, candles) -> bool:
        # Implement pattern detection
        return False
```

### Step 2: Register Strategy

Add to `logic/strategies/__init__.py`:

```python
from .my_strategy import MyStrategy

STRATEGIES = [
    # ... existing strategies
    MyStrategy(),
]
```

### Step 3: Test Strategy

```bash
# Run with paper mode first
python run_v2.py --mode paper
```

---

## Adding a New Risk Gate

### Step 1: Add Gate to EntryGateChecker

```python
# execution/entry_gates.py

def check_all_gates(self, signal, price, spread_bps, ...) -> tuple[bool, str]:
    # ... existing gates ...
    
    # Gate N+1: Your custom gate
    if not self._check_my_custom_rule(signal, price):
        return False, "my_custom_rule"
    
    return True, ""

def _check_my_custom_rule(self, signal, price) -> bool:
    """
    Custom risk rule.
    Returns True if allowed, False to reject.
    """
    # Your logic here
    return True
```

### Step 2: Add Gate Reason

```python
# core/helpers/reasons.py

GateReason = Literal[
    # ... existing reasons
    "my_custom_rule",
]
```

---

## Adding a New Data Source

### For Real-Time Data

```python
# datafeeds/collectors/my_collector.py

class MyDataCollector:
    """Custom data collector."""
    
    def __init__(self, on_data_callback):
        self.on_data = on_data_callback
    
    async def start(self):
        """Start collecting data."""
        while True:
            data = await self._fetch_data()
            self.on_data(data)
            await asyncio.sleep(1)
    
    async def stop(self):
        """Stop collector."""
        pass
```

### For Historical Data

```python
# datafeeds/my_fetcher.py

class MyFetcher:
    """Fetch historical data from custom source."""
    
    def fetch_candles(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        # Fetch and return candles
        pass
```

---

## Adding Dashboard Features

### Step 1: Add State Field

```python
# core/state.py

@dataclass
class BotState:
    # ... existing fields
    my_new_metric: float = 0.0
```

### Step 2: Update StateWriter Serialization

```python
# core/shared_state.py - _serialize_state()

def _serialize_state(state) -> dict:
    return {
        # ... existing fields
        "my_new_metric": getattr(state, 'my_new_metric', 0.0),
    }
```

### Step 3: Update Frontend

```javascript
// ui/web/static/js/dashboard.js (or React component)

function updateMyMetric(state) {
    document.getElementById('my-metric').textContent = state.my_new_metric;
}
```

---

## Adding a New Exchange

### Step 1: Implement IExecutor Interface

```python
# execution/kraken_executor.py (example)

from core.trading_interfaces import IExecutor

class KrakenExecutor(IExecutor):
    """Kraken exchange executor."""
    
    async def open_position(self, symbol, size_usd, price, stop_price, tp1_price, tp2_price):
        # Implement Kraken order placement
        pass
    
    async def close_position(self, position, price, reason):
        # Implement Kraken sell order
        pass
    
    def can_execute_order(self, size_usd, symbol=None):
        # Check if order can be executed
        return True, ""
```

### Step 2: Add to TradingFactory

```python
# execution/trading_factory.py

def create_executor(self) -> IExecutor:
    if self.exchange == "kraken":
        return KrakenExecutor(self.config)
    elif self.mode == TradingMode.LIVE:
        return LiveExecutor(...)
    else:
        return PaperExecutor(...)
```

---

## Testing Guidelines

### Unit Tests

```python
# tests/test_my_strategy.py

def test_my_strategy_detects_pattern():
    strategy = MyStrategy()
    buffer = create_mock_buffer_with_pattern()
    
    signal = strategy.analyze("BTC-USD", buffer, mock_intelligence)
    
    assert signal is not None
    assert signal.direction == SignalDirection.LONG
```

### Integration Tests

```python
# tests/test_integration.py

async def test_order_flow():
    """Test signal → gate check → order placement."""
    router = create_test_router()
    signal = create_test_signal()
    
    result = await router.open_position(signal)
    
    assert result is not None or router.last_rejection_reason != ""
```

---

## Deployment Checklist

### Before Deploying New Code

- [ ] Run all tests: `pytest tests/`
- [ ] Test in paper mode first: `python run_v2.py --mode paper`
- [ ] Check for breaking changes in state serialization
- [ ] Update ARCHITECTURE.md if architecture changed
- [ ] Verify PM2 config if new processes needed

### Deploying

```bash
# Pull latest code
git pull

# Restart bot (preserves positions)
pm2 restart COIN-BOT

# Restart web server
pm2 restart COIN

# Check logs
pm2 logs --lines 50
```

---

## Common Patterns

### Accessing Current Price

```python
price = self._get_price(symbol)  # From TradingBotV2
# or
price = self.router.get_current_price(symbol)  # From OrderRouter context
```

### Accessing Positions

```python
# Current positions
positions = self.router.positions  # dict[str, Position]

# Check if holding
has_position = symbol in self.router.positions
```

### Publishing Events

```python
from core.events import MarketEventBus, OrderEvent

self.events.publish(OrderEvent(
    event_type="OPEN",
    symbol=symbol,
    side="BUY",
    # ... other fields
))
```

### Subscribing to Events

```python
def my_handler(event: OrderEvent):
    print(f"Order event: {event.symbol}")

self.events.subscribe("order", my_handler)
```

---

## File Ownership

| File Type | Owner Process | Other Access |
|-----------|---------------|--------------|
| `bot_state.json` | COIN-BOT (write) | COIN (read) |
| `live_positions.json` | COIN-BOT | None |
| `live_cooldowns.json` | COIN-BOT | None |
| `candles_1m/*.json` | COIN-BOT | None |
| `control.json` | COIN (write) | COIN-BOT (read) |

---

## Debugging Tips

### Enable Verbose Logging

```python
# In run_v2.py or any module
import logging
logging.getLogger("execution.order_router").setLevel(logging.DEBUG)
```

### Check Bot State

```bash
cat data/bot_state.json | python3 -m json.tool | head -50
```

### Watch Logs in Real-Time

```bash
pm2 logs COIN-BOT --lines 100
```

### Check Position Sync

```bash
cat data/live_positions.json | python3 -m json.tool
```

---

*Extending Guide v1.0 | Created: 2025-12-21*

---

## 2026-01-07: Coverage + Gate Traces + Paper Cash

- Added `/api/coverage` for per-symbol/per-timeframe data coverage (status, age, bars, reasons).
- Added `gate_traces` to `bot_state.json` to expose last entry-gate decision per symbol.
- Paper cash now persists in `data/paper_state.json` (set `PAPER_RESET_STATE=1` to reset).
