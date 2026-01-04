# Coinbot Core Flow (Single Coin Example: BTC-USD)

This document describes the end-to-end operational flow of a coinbot using **one trading pair** as the unit of reasoning and execution.

---

## 0. Config & Runtime Control

**2025-12-29**: Normalize boot vs runtime config.
- `start_config_*` is immutable for the run (built once at startup).
- `running_config_*` is refreshed when runtime settings change.
- Runtime changes flow: `ConfigManager` → `settings` → `RuntimeConfigStore.refresh()` → `OrderRouter.update_config()`.
- **2025-12-29**: `config_start`/`config_running` snapshots are redacted (API keys) before shared-state/API exposure.
- **2025-12-29**: `/api/config/refresh` reloads runtime config from disk; dashboard renders start vs running diffs.
- **2025-12-30**: Execution boundary is `Intent` → `TradePlan`; sizing precedence lives in `PositionSizer`.

## 1. Symbol Admission

**Purpose:** Decide whether BTC-USD is allowed to participate in trading.

**Inputs**
- Exchange symbol metadata
- Liquidity, volume, spread
- Historical data availability
- Volatility constraints

**Process**
- Validate USD-quoted pair
- Run health scoring
- Assign tier (trusted / stable / unreliable)

**Output**
- BTC-USD marked as `ACTIVE` or `BLOCKED`

---

## 2. Market Data Ingestion

**Purpose:** Maintain a clean, continuous view of market reality.

**Inputs**
- Live candles (multi-timeframe)
- Trades / order book (optional)

**Process**
- Stream ingestion
- Gap detection
- Automatic historical backfill
- Time alignment

**Output**
- Persisted OHLCV series for BTC-USD

---

## 3. Feature Engineering

**Purpose:** Convert raw price data into structured signals.

**Inputs**
- OHLCV data (multiple timeframes)

**Process**
- Indicator computation (RSI, MA, ATR, etc.)
- Volatility and momentum metrics
- Regime-relevant features
- Feature normalization

**Output**
- Feature vector describing BTC-USD state at time _t_

---

## 4. Signal Generation (Decision Core)

**Purpose:** Determine directional bias and confidence.

### 4.1 Model Layer
- Consume feature vector
- Produce:
  - Direction probability
  - Confidence score
  - Expected return or edge

### 4.2 Rule / Filter Layer
- Reject signals on:
  - Regime mismatch
  - Excess volatility
  - Spread anomalies
- Enforce multi-timeframe agreement

**Output**
- Trade intent (LONG / SHORT / HOLD)

---

## 5. Risk Management

**Purpose:** Ensure survival and capital discipline.

**Inputs**
- Trade intent
- Portfolio state
- Account mode (paper / live)

**Process**
- Exposure checks
- Correlation analysis
- Max risk per trade
- Dynamic position sizing

**Output**
- Risk-approved trade plan
  - Direction
  - Size
  - Risk bounds

---

## 6. Strategy Promotion & Trust Control

**Purpose:** Decide whether the strategy is allowed to act.

**Process**
- Evaluate recent accuracy
- Compare performance vs baseline
- Promote / demote / disable strategy

**Output**
- Strategy authorization status

---

## 7. Order Execution

**Purpose:** Convert intent into market action.

**Process**
- Route to paper or live exchange
- Select order type
- Apply slippage and safety controls
- Submit order

**Output**
- Open position or execution failure

---

## 8. Position Management

**Purpose:** Manage risk and profit after entry.

**Process**
- Monitor price vs entry
- Apply:
  - Stop-loss
  - Take-profit
  - Trailing logic
  - Time-based exits
- Detect regime shifts

**Output**
- Closed position (intentional exit)

---

## 9. Post-Trade Feedback Loop

**Purpose:** Improve future decisions.

**Captured**
- Entry/exit quality
- Model confidence vs outcome
- Slippage
- Regime accuracy

**Used For**
- Strategy scoring
- Model retraining
- Promotion engine updates

**Output**
- Updated system state and historical intelligence

---

## Core Loop Summary

Market Data
↓
Features
↓
Signals
↓
Risk Approval
↓
Execution
↓
Position Management
↓
Feedback → (loop)



## Key Principle

> Each coin operates as an independent autonomous trader,  
> constrained by shared risk, capital, and statistical truth.
