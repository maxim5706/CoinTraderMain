# CoinTrader - Burst-Coin Bull-Flag Bot

A Coinbase Advanced Trade API bot that:
1. **Finds burst coins** - detects volume/volatility spikes
2. **Avoids traps** - filters out triple tops and head & shoulders
3. **Waits for bull flags** - enters only on clean flag breakouts
4. **Tight risk** - quick feedback within the hour

## Quick Start

### 1. Install uv (if you don't have it)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Setup project
```bash
cd /Users/maximreilly/1.Code/CoinTrader
uv sync
```

### 3. Configure API keys
```bash
cp .env.example .env
# Edit .env with your Coinbase API credentials
```

### 4. Run in paper mode (always start here!)
```bash
uv run python run.py
```

## Config Flow (2025-12-30)

- `start_config_*`: immutable boot snapshot built once at startup.
- `running_config_*`: refreshed when runtime config changes via the dashboard.
- **2025-12-29**: `config_start`/`config_running` snapshots are redacted (API keys) and exposed via `/api/state` + `/api/config` for dashboard visibility.
- **2025-12-29**: `/api/config/refresh` reloads runtime config from disk; dashboard shows start vs running diffs.
- **2025-12-30**: `TradePlanner` converts `Intent` to `TradePlan` with explicit sizing precedence.
- Runtime updates: `ConfigManager` → `settings` → `RuntimeConfigStore.refresh()` → `OrderRouter.update_config()`.

## Project Structure

```
CoinTrader/
├── config.py           # Settings from .env
├── models.py           # Data models (Candle, Position, Signal)
├── ws_collector.py     # WebSocket candle collector
├── logic/strategies/   # Multi-strategy orchestrator + individual strats
├── order_router.py     # Order execution (paper + live)
├── dashboard.py        # Rich terminal display
└── run.py              # Main entry point
```

## Strategy Logic

### Burst Detection
- Volume spike: `vol_5m > 2.5× median(last 2h)`
- Range spike: `range_5m > 1.8× median(last 2h)`
- Price above VWAP(2h)

### Impulse Leg
- Last 15-45 min up ≥ +3%
- At least 3 green 5m candles
- Price above EMA(20)

### Bull Flag
- 20-50% retracement of impulse
- 10-40 min duration
- Declining volume during flag
- Price above EMA(50)

### Trap Avoidance
- Skip if triple top detected (3 highs within 0.5%)
- Skip if head & shoulders pattern

### Entry
- Breakout above flag high + 0.2% buffer
- Volume confirmation: `vol_1m > 1.8× avg_flag_vol`

### Risk Management
- Stop: below flag low or 1.1× ATR
- TP1: impulse high (partial)
- TP2: impulse high + 0.5× impulse range
- Time stop: 25-35 min
- Daily loss limit: -$25

## Safety Notes

⚠️ **ALWAYS START IN PAPER MODE**

- This is for learning and fun, not guaranteed profits
- Start with tiny size ($10 trades)
- Never risk more than you can afford to lose
- Watch the bot for at least a few hours before going live
