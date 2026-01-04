# CoinTrader Boot Process & Architecture

> **Version:** 1.0 | **Created:** 2025-12-21

---

## Current Architecture (PROBLEMATIC)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     CURRENT: MIXED RESPONSIBILITY                            │
│                                                                              │
│   TWO WAYS TO START BOT (confusing):                                        │
│                                                                              │
│   1. PM2 starts COIN-BOT directly                                           │
│      └── Bot writes bot_state.json                                          │
│      └── Dashboard reads bot_state.json                                     │
│      └── Dashboard shows "Running" if state is fresh ✓                      │
│                                                                              │
│   2. Dashboard spawns bot as subprocess (legacy)                            │
│      └── Dashboard tracks _bot_process                                      │
│      └── Direct Python object access                                        │
│      └── Conflicts with PM2 management ✗                                    │
│                                                                              │
│   PROBLEMS:                                                                  │
│   • Dashboard has subprocess spawning code that conflicts with PM2          │
│   • Two different ways to check "is bot running"                            │
│   • Start/Stop buttons try to spawn subprocess, not control PM2             │
│   • Port 8080 conflicts (both try to bind)                                  │
│   • StateWriter timing issues                                               │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Ideal Architecture (RECOMMENDED)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     RECOMMENDED: CLEAN SEPARATION                            │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                         PM2 (Process Manager)                        │   │
│   │                                                                      │   │
│   │   • Single source of truth for process lifecycle                    │   │
│   │   • Auto-restart on crash                                           │   │
│   │   • Log aggregation                                                 │   │
│   │   • Startup on boot (pm2 startup)                                   │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│              ┌───────────────┴───────────────┐                              │
│              │                               │                              │
│              ▼                               ▼                              │
│   ┌─────────────────────┐       ┌─────────────────────┐                    │
│   │   COIN (Dashboard)  │       │  COIN-BOT (Trading) │                    │
│   │   Port: 8080        │       │  No port binding    │                    │
│   ├─────────────────────┤       ├─────────────────────┤                    │
│   │ RESPONSIBILITIES:   │       │ RESPONSIBILITIES:   │                    │
│   │ • Serve web UI      │       │ • WebSocket data    │                    │
│   │ • Read bot_state    │       │ • Strategy eval     │                    │
│   │ • Display positions │       │ • Order execution   │                    │
│   │ • Send commands     │       │ • Write bot_state   │                    │
│   │   via control.json  │       │ • Read control.json │                    │
│   │                     │       │                     │                    │
│   │ NEVER:              │       │ NEVER:              │                    │
│   │ • Spawn bot process │       │ • Bind to port      │                    │
│   │ • Execute trades    │       │ • Serve web pages   │                    │
│   └──────────┬──────────┘       └──────────┬──────────┘                    │
│              │                               │                              │
│              │      ┌───────────────┐       │                              │
│              │      │  Shared Files │       │                              │
│              └─────►│               │◄──────┘                              │
│                     │ bot_state.json│ (bot writes, dash reads)             │
│                     │ control.json  │ (dash writes, bot reads)             │
│                     │ positions.json│ (bot owns)                           │
│                     └───────────────┘                                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Ideal Boot Sequence

### Phase 1: System Boot (PM2)

```
1. System starts
2. PM2 daemon starts (via systemd)
3. PM2 reads ecosystem.config.js
4. PM2 starts COIN (dashboard) first
5. PM2 starts COIN-BOT (trading) second
6. Both processes run independently
```

### Phase 2: Dashboard Boot (COIN)

```
COIN Process Startup:
├── 1. Load config (read-only)
├── 2. Initialize FastAPI server
├── 3. Bind to port 8080
├── 4. Start state polling loop
│   └── Every 500ms: read bot_state.json
├── 5. Serve web UI
└── 6. Ready to accept connections

Dashboard State Machine:
┌─────────┐     bot_state.json      ┌─────────┐
│ LOADING │ ───── fresh ─────────► │ RUNNING │
└─────────┘                         └─────────┘
     │                                   │
     │ no file / stale                   │ state goes stale (>5s)
     ▼                                   ▼
┌─────────┐                         ┌─────────┐
│ OFFLINE │ ◄────────────────────── │  STALE  │
└─────────┘                         └─────────┘
```

### Phase 3: Bot Boot (COIN-BOT)

```
COIN-BOT Process Startup:
├── 1. Load config
├── 2. Validate API keys (preflight)
├── 3. Initialize components
│   ├── State object
│   ├── Event bus
│   ├── Scanner
│   └── OrderRouter
├── 4. Connect to exchange
│   ├── Sync positions from Coinbase
│   ├── Sync portfolio value
│   └── Mark holdings in registry
├── 5. Start StateWriter (NOW positions are synced)
├── 6. Initial backfill
│   ├── Fetch 1m candles
│   ├── Fetch 5m candles
│   ├── Fetch 1H candles (for trends)
│   └── Fetch 1D candles (for trends)
├── 7. Start WebSocket connection
├── 8. Start Clock B (5s polling)
├── 9. Start Clock C (30min slow loop)
└── 10. Begin trading loop

Bot State Machine:
┌──────────┐   config OK   ┌──────────┐   API OK   ┌──────────┐
│  INIT    │ ───────────► │ PREFLIGHT │ ─────────► │ SYNCING  │
└──────────┘               └──────────┘            └──────────┘
                                │                       │
                                │ API fail              │ sync complete
                                ▼                       ▼
                           ┌──────────┐           ┌──────────┐
                           │  ERROR   │           │ BACKFILL │
                           └──────────┘           └──────────┘
                                                       │
                                                       │ backfill done
                                                       ▼
                                                  ┌──────────┐
                                                  │ RUNNING  │
                                                  └──────────┘
                                                       │
                                    kill_switch=true   │   shutdown signal
                                                       ▼
                                                  ┌──────────┐
                                                  │ STOPPED  │
                                                  └──────────┘
```

---

## Communication Protocol

### bot_state.json (Bot → Dashboard)

Written by COIN-BOT every 500ms:
```json
{
  "ts": "2025-12-21T20:00:00Z",
  "mode": "live",
  "status": "running",        // NEW: explicit status field
  "phase": "trading",         // NEW: init/preflight/syncing/backfill/trading
  "positions": [...],
  "portfolio_value": 639.0,
  "ws_ok": true,
  "error": null               // NEW: last error if any
}
```

### control.json (Dashboard → Bot)

Written by COIN (dashboard) when user clicks buttons:
```json
{
  "command": null,            // "stop" | "kill" | "pause" | null
  "kill_switch": false,
  "updated_at": "2025-12-21T20:00:00Z"
}
```

Bot reads control.json every 5 seconds and responds to commands.

---

## Error Handling & Recovery

### Dashboard Errors

| Error | Detection | Recovery |
|-------|-----------|----------|
| bot_state.json missing | File not found | Show "Bot Offline" status |
| bot_state.json stale | ts > 5s old | Show "Bot Stale" warning |
| bot_state.json corrupt | JSON parse error | Retry with backoff, show error |
| PM2 not running | Can't read PM2 status | Show "PM2 Offline" |

### Bot Errors

| Error | Detection | Recovery |
|-------|-----------|----------|
| API key invalid | Preflight fails | Write error to state, exit |
| Exchange unreachable | Connection timeout | Retry with backoff, circuit breaker |
| Position sync fails | Exception in sync | Log error, continue with local state |
| WebSocket disconnect | is_connected=false | Auto-reconnect with backoff |
| Rate limited (429) | HTTP status | Backoff, reduce request rate |
| Out of memory | Process crash | PM2 auto-restart |

---

## Debugging Checklist

### Bot Not Starting
```bash
# Check PM2 status
pm2 list

# Check bot logs
pm2 logs COIN-BOT --lines 100

# Check for port conflicts
lsof -i :8080

# Check bot_state.json
cat data/bot_state.json | python3 -m json.tool
```

### Dashboard Shows Wrong Status
```bash
# Check if state file is fresh
cat data/bot_state.json | python3 -c "
import json, sys
from datetime import datetime, timezone
d = json.load(sys.stdin)
ts = datetime.fromisoformat(d['ts'])
age = (datetime.now(timezone.utc) - ts).total_seconds()
print(f'State age: {age:.1f}s (fresh if <5s)')
print(f'Mode: {d.get(\"mode\")}')
print(f'Positions: {len(d.get(\"positions\", []))}')
"

# Check web server logs
pm2 logs COIN --lines 50
```

### Positions Not Showing
```bash
# Check positions in state file
cat data/bot_state.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in d.get('positions', []):
    print(f'{p[\"symbol\"]}: \${p.get(\"size_usd\", 0):.2f}')
"

# Compare with exchange
cat data/live_positions.json | python3 -m json.tool
```

---

## Refactoring TODO

### High Priority

1. **Remove subprocess spawning from web_server.py**
   - Delete `start_bot()` function that spawns subprocess
   - Dashboard should ONLY read state, never spawn processes
   - Start/Stop buttons should write to control.json

2. **Add explicit status/phase to bot_state.json**
   - Bot writes its current phase: init/preflight/syncing/backfill/trading
   - Dashboard displays phase clearly

3. **Remove port binding from COIN-BOT**
   - Bot should not try to start web server
   - Only COIN (dashboard) binds to port 8080

### Medium Priority

4. **Add control.json protocol**
   - Dashboard writes commands
   - Bot reads and executes
   - Clean separation of concerns

5. **Add health endpoint to dashboard**
   - `/api/health` returns dashboard status
   - `/api/bot/health` proxies bot health from state file

### Low Priority

6. **Add PM2 status API**
   - Dashboard can query PM2 for process status
   - More accurate than just checking state file freshness

---

## Quick Reference

### Start Everything
```bash
pm2 start ecosystem.config.js
```

### Restart Bot Only
```bash
pm2 restart COIN-BOT
```

### View Logs
```bash
pm2 logs              # All processes
pm2 logs COIN-BOT     # Bot only
pm2 logs COIN         # Dashboard only
```

### Check Status
```bash
pm2 list
pm2 show COIN-BOT
```

### Enable Boot Startup
```bash
pm2 save
pm2 startup
# Run the command it outputs with sudo
```

---

*Boot Process Guide v1.0 | Created: 2025-12-21*
