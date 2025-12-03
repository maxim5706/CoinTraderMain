# CoinTrader Refactoring Plan

**Created:** 2025-12-03  
**Status:** In Progress  
**Goal:** Clean, modular, maintainable codebase following Python best practices

---

## Current State Analysis

### Codebase Metrics
```
Total Lines:     25,046
Total Files:     108 Python files
Test Coverage:   18 test files (100 tests passing)
```

### Problem Areas

| Issue | Severity | Files Affected |
|-------|----------|----------------|
| Giant files (>1000 LOC) | High | order_router.py, run_v2.py, intelligence.py |
| Root script clutter | Medium | 7 scripts at project root |
| Dead forwarder files | Low | logic/live_features.py, apps/runner/run_v2.py |
| Inconsistent logging | Medium | Mixed get_logger() vs logging.getLogger() |
| Duplicate files | Low | health_check.py exists twice |

### File Size Analysis
```
execution/order_router.py    2,141 lines  ← SPLIT
run_v2.py                    1,855 lines  ← SPLIT
logic/intelligence.py        1,149 lines  ← SPLIT
features/live/live_features.py 912 lines  ← OK
apps/dashboard/dashboard_v2.py 864 lines  ← OK
```

---

## Phase 1: Safe Cleanup (No Breaking Changes)

### 1.1 Delete Dead Files
```bash
# Forwarder stubs that just redirect imports
rm logic/live_features.py           # 4 lines, forwards to features/live/
rm apps/runner/run_v2.py            # 6 lines, forwards to root run_v2.py
rm apps/runner/__init__.py          # Empty after removing run_v2.py
```

### 1.2 Move Root Scripts to tools/
```bash
# These are utilities, not entry points
mv analyze_portfolio.py tools/
mv start_live.py tools/
mv system_status.py tools/
mv view_live_portfolio.py tools/
rm health_check.py                  # Duplicate of tools/health_check.py
```

### 1.3 Clean Empty Directories
```bash
rmdir apps/runner/                  # Empty after cleanup
```

### 1.4 Update .gitignore
- Ensure __pycache__, .venv, logs/, data/ are ignored

---

## Phase 2: Unified Logging

### Current State
```python
# Inconsistent patterns found:
logger = get_logger(__name__)           # 12 occurrences (good)
logger = logging.getLogger(__name__)    # 2 occurrences (legacy)
self.logger = logging.getLogger(...)    # 2 occurrences (inconsistent)
```

### Target State
All modules use:
```python
from core.logging_utils import get_logger
logger = get_logger(__name__)
```

### Files to Update
- tests/integration/data_sync_validator.py
- tests/integration/live_validation.py
- Any file using `logging.getLogger()` directly

---

## Phase 3: Split Large Files

### 3.1 Split order_router.py (2,141 → ~4 files)

**Current Structure:**
```
order_router.py
├── DailyStats class (lines 46-130)
├── CircuitBreaker class (lines 132-186)
├── CooldownPersistence class (lines 190-236)
└── OrderRouter class (lines 238-2141)
    ├── Position sync methods
    ├── Balance/portfolio methods
    ├── Entry logic (open_position, _do_open_position)
    ├── Exit logic (check_exits, _close_partial, _close_full)
    └── Confidence updates
```

**Target Structure:**
```
trading/
├── __init__.py
├── router.py          # OrderRouter core (~800 lines)
│   └── OrderRouter class (entry/exit decisions)
├── executor.py        # Order execution (~300 lines)
│   └── Execute buys/sells, verify positions
├── positions.py       # Position management (~300 lines)
│   └── Position sync, registry integration
├── risk.py            # Risk management (~200 lines)
│   └── DailyStats, CircuitBreaker, CooldownPersistence
└── stops.py           # Stop order management
    └── Stop placement, trailing stops
```

### 3.2 Split run_v2.py (1,855 → ~4 files)

**Current Structure:**
```
run_v2.py
└── TradingBotV2 class
    ├── __init__, start, stop
    ├── Clock loops (A, B, C)
    ├── Event handlers (candle, tick, ws_connect)
    ├── Strategy analysis
    ├── State updates
    └── Display loop
```

**Target Structure:**
```
bot/
├── __init__.py
├── runner.py          # TradingBotV2 core (~500 lines)
│   └── __init__, start, stop, main orchestration
├── loops.py           # Clock loops (~400 lines)
│   └── _clock_a_loop, _clock_b_loop, _clock_c_loop
├── handlers.py        # Event handlers (~400 lines)
│   └── _on_candle, _on_tick, _on_ws_connect
├── analysis.py        # Strategy analysis (~300 lines)
│   └── _run_strategy_analysis, _build_features
└── state.py           # State management (from core/state.py)
```

### 3.3 Split intelligence.py (1,149 → ~3 files)

**Current Structure:**
```
intelligence.py
├── EntryScore dataclass
├── PositionLimits dataclass
└── IntelligenceLayer class
    ├── BTC trend / regime methods
    ├── Session / time methods
    ├── ML indicator methods
    ├── Scoring methods (score_entry)
    ├── Position limit methods
    └── Trade logging methods
```

**Target Structure:**
```
intelligence/
├── __init__.py
├── layer.py           # IntelligenceLayer core (~500 lines)
│   └── Main class, BTC trend, regime, session
├── scoring.py         # Entry scoring (~300 lines)
│   └── EntryScore, score_entry logic
├── limits.py          # Position limits (~200 lines)
│   └── PositionLimits, check_position_limits
└── analytics.py       # Trade analytics (~150 lines)
    └── Trade logging, daily stats
```

---

## Phase 4: Final Structure

```
cointrader/
├── main.py                    # Single entry point
│
├── config/                    # Configuration
│   ├── __init__.py           # exports: settings, Settings
│   └── settings.py           # All settings (from core/config.py)
│
├── models/                    # Data models (pure dataclasses)
│   ├── __init__.py           # exports: Position, Signal, Candle, Trade
│   ├── position.py
│   ├── signal.py
│   ├── candle.py
│   └── trade.py
│
├── trading/                   # Order execution (from execution/)
│   ├── __init__.py           # exports: OrderRouter
│   ├── router.py
│   ├── executor.py
│   ├── positions.py
│   ├── risk.py
│   └── stops.py
│
├── strategies/                # Trading strategies (from logic/strategies/)
│   ├── __init__.py           # Already well-organized ✅
│   ├── base.py
│   ├── orchestrator.py
│   └── [strategy files]
│
├── intelligence/              # Market analysis (from logic/)
│   ├── __init__.py
│   ├── layer.py
│   ├── scoring.py
│   └── limits.py
│
├── data/                      # Data feeds (from datafeeds/)
│   ├── __init__.py
│   ├── collector.py
│   ├── scanner.py
│   ├── tiers.py
│   └── backfill.py
│
├── bot/                       # Main bot (from run_v2.py)
│   ├── __init__.py
│   ├── runner.py
│   ├── loops.py
│   ├── handlers.py
│   └── state.py
│
├── dashboard/                 # Terminal UI (from apps/dashboard/)
│   ├── __init__.py
│   ├── display.py
│   └── panels.py
│
├── services/                  # Shared services
│   ├── __init__.py
│   ├── logging.py
│   ├── alerts.py
│   └── persistence.py
│
├── tools/                     # CLI utilities
│   ├── backtest.py
│   ├── analyze.py
│   ├── health.py
│   └── preflight.py
│
└── tests/                     # All tests
    ├── conftest.py
    ├── unit/
    └── integration/
```

---

## Execution Order

### Week 1: Phase 1 (Safe Cleanup)
- [x] Document plan (this file)
- [ ] Delete dead files
- [ ] Move root scripts to tools/
- [ ] Run tests to verify nothing broke

### Week 1: Phase 2 (Unified Logging)
- [ ] Update all files to use get_logger()
- [ ] Run tests

### Week 2: Phase 3 (Split Large Files)
- [ ] Split order_router.py → trading/
- [ ] Update all imports
- [ ] Run tests
- [ ] Split run_v2.py → bot/
- [ ] Update all imports
- [ ] Run tests
- [ ] Split intelligence.py → intelligence/
- [ ] Update all imports
- [ ] Run tests

### Week 2: Phase 4 (Final Cleanup)
- [ ] Rename directories to final structure
- [ ] Update all imports
- [ ] Final test run
- [ ] Update README.md

---

## Rollback Plan

Each phase will be committed separately:
```bash
git commit -m "phase1: cleanup dead files"
git commit -m "phase2: unified logging"
git commit -m "phase3a: split order_router"
git commit -m "phase3b: split run_v2"
git commit -m "phase3c: split intelligence"
git commit -m "phase4: final structure"
```

If any phase breaks tests, we can:
```bash
git revert HEAD  # Undo last commit
```

---

## Success Criteria

1. **All 100 tests still pass**
2. **No file > 800 lines** (except feature calculations)
3. **Unified logging** across all modules
4. **Clean imports** via `__init__.py` exports
5. **No dead code** or forwarder stubs
6. **Single entry point** at project root

---

## Notes

- Keep backward compatibility during transition
- Each phase is independently revertible
- Tests run after each change
- Document any API changes
