# Data Synchronization Requirements

## 🔄 **Complete Data Flow Map**

```
Market Data → Strategies → Signals → OrderRouter → Execution → Positions → PnL → Portfolio → Display
     ↓            ↓          ↓          ↓           ↓           ↓        ↓         ↓         ↓
   Price      Strategy    Signal    Position    Order       Position  PnL      Portfolio  State
   Updates    Evaluation  Selection Validation  Execution   Updates   Calc     Snapshot   Update
```

## 📊 **Critical Synchronization Points**

### **1. Real-Time Data Sync (Every Tick)**

| **Component** | **Data** | **Source** | **Frequency** | **Validation** |
|---------------|----------|------------|---------------|----------------|
| **Price Data** | Current prices | Market API/Collector | ~100ms | Price reasonableness check |
| **Position PnL** | Unrealized PnL | Current price × position | Every price update | PnL calculation accuracy |
| **Strategy Signals** | Entry/exit signals | Strategy algorithms | Every price update | Signal validity |
| **Position Limits** | Available capacity | Position registry | Before each trade | Limit compliance |

### **2. Periodic Data Sync (Every 30 seconds)**

| **Component** | **Data** | **Source** | **Frequency** | **Critical Check** |
|---------------|----------|------------|---------------|-------------------|
| **Portfolio Snapshot** | Exchange positions | Live API | 30s | Position count match |
| **Balance Updates** | Cash + holdings | Exchange API | 30s | Balance reconciliation |
| **Position Sync** | Live vs tracked | Exchange comparison | 30s | No missing positions |
| **PnL Verification** | Calculated vs actual | Exchange PnL vs engine | 30s | <1% difference tolerance |

### **3. Trade Event Sync (Immediate)**

| **Event** | **Components Updated** | **Order** | **Validation** |
|-----------|----------------------|-----------|----------------|
| **Order Placed** | Position Registry → Persistence | 1. Registry update<br>2. File save | Order recorded correctly |
| **Fill Detected** | Position → PnL → Portfolio | 1. Position state<br>2. PnL recalc<br>3. Balance update | Fill amount matches |
| **Position Closed** | Registry → PnL → Strategy Stats | 1. Remove from registry<br>2. Final PnL calc<br>3. Strategy attribution | Trade complete correctly |

## ⚠️ **Race Condition Prevention**

### **Critical Sections (Must Be Atomic)**

1. **Position Updates**
   ```python
   # ATOMIC: Position registry modifications
   async with position_lock:
       position_registry.add_position(position)
       persistence.save_positions()
   ```

2. **PnL Calculations** 
   ```python
   # ATOMIC: PnL engine calculations
   async with pnl_lock:
       pnl = pnl_engine.calculate_trade_pnl(...)
       pnl_engine.track_strategy_pnl(strategy_id, pnl)
   ```

3. **Balance Updates**
   ```python
   # ATOMIC: Portfolio balance changes
   async with balance_lock:
       portfolio.debit_balance(amount)
       position_registry.add_position(position)
   ```

## 🧪 **Validation Order of Operations**

### **Step 1: Component Initialization Validation**
```
✅ Configuration loaded consistently across all components
✅ PnLEngine initialized with correct fee structure
✅ PositionRegistry initialized with correct limits
✅ Portfolio manager connected (live mode only)
```

### **Step 2: Data Flow Validation**
```
✅ Price data accessible and reasonable
✅ Position tracking add/remove/query works
✅ PnL calculations accurate for known test cases
✅ Strategy attribution tracking works
```

### **Step 3: Integration Validation**
```
✅ Position registry ↔ PnL engine consistency
✅ PnL engine ↔ Portfolio manager consistency  
✅ Strategy limits enforced correctly
✅ Dust handling consistent across components
```

### **Step 4: Concurrent Operations Validation**
```
✅ No race conditions in position updates
✅ No race conditions in PnL calculations
✅ No data corruption under concurrent access
✅ Performance acceptable under load
```

### **Step 5: End-to-End Pipeline Validation**
```
✅ Complete trade cycle: Signal → Order → Fill → Position → PnL
✅ Multi-strategy isolation and attribution
✅ Error handling and recovery
✅ Data persistence and reload
```

## 🎯 **Data Consistency Requirements**

### **Position Data**
- **Registry positions** == **Exchange positions** (live mode)
- **Active position count** <= **Max position limit**
- **Per-strategy counts** <= **Per-strategy limits**
- **Dust positions** tracked but not counted in limits

### **PnL Data**
- **Sum of strategy PnL** == **Total portfolio PnL**
- **Calculated unrealized PnL** matches **Exchange unrealized PnL** (±1%)
- **Fee calculations** consistent across all trades
- **PnL attribution** sums to total correctly

### **Portfolio Data**
- **Cash + Holdings** == **Total portfolio value** (±$0.10)
- **Position values** consistent with current prices
- **Balance changes** match executed trade amounts
- **Snapshot consistency** across multiple calls (<0.1% variance)

## 🔧 **Critical Configuration Sync**

All components must use **identical configuration values**:

```python
# MUST BE CONSISTENT ACROSS ALL COMPONENTS:
config.dust_threshold_usd     # Position registry dust handling
config.max_positions          # Position registry limits
config.max_positions_per_strategy  # Strategy orchestrator limits
config.maker_fee_pct         # PnL engine fee calculations
config.taker_fee_pct         # PnL engine fee calculations
config.min_position_usd      # Position registry minimum size
config.min_hold_seconds      # Position registry hold time
```

## ⚡ **Performance Requirements**

| **Operation** | **Max Latency** | **Throughput** |
|---------------|----------------|----------------|
| Price data update | <100ms | 100+ updates/sec |
| Position PnL calculation | <10ms | 1000+ calcs/sec |
| Position registry query | <1ms | 10000+ queries/sec |
| Portfolio snapshot | <1000ms | 1+ snapshots/sec |
| Strategy signal generation | <500ms | 10+ signals/sec |

## 🚨 **Error Detection Triggers**

### **Immediate Alerts**
- Position count mismatch (registry vs exchange)
- PnL calculation error >1%
- Balance discrepancy >$0.10
- Configuration mismatch between components
- Position limit violations

### **Warning Conditions**
- Portfolio snapshot latency >2s
- Price data staleness >5s
- Strategy signal latency >1s
- Memory usage growth
- Error rate increase

## 📈 **Monitoring Dashboard Requirements**

### **Real-Time Metrics**
- Position count (active/dust/total)
- PnL accuracy (calculated vs actual)
- Data sync latency
- Component health status
- Error rates and types

### **Historical Tracking**
- Strategy performance attribution
- Data consistency over time
- System performance metrics
- Error frequency and patterns
- Trade execution quality

## ✅ **Deployment Checklist**

Before deploying multi-strategy system:

1. **Run Data Sync Validator**
   ```bash
   python tests/integration/data_sync_validator.py
   ```

2. **Verify All Components Pass**
   - ✅ Configuration consistency
   - ✅ Position tracking accuracy
   - ✅ PnL calculation correctness
   - ✅ Portfolio synchronization
   - ✅ Race condition safety

3. **Test with Paper Mode First**
   - Run complete validation in paper mode
   - Verify multi-strategy isolation
   - Test error handling and recovery

4. **Live Mode Validation**
   - API connectivity and permissions
   - Small test trade validation
   - Real-time monitoring setup

5. **Production Monitoring**
   - Set up alerts for all error conditions
   - Monitor data consistency continuously
   - Regular validation runs (daily/weekly)

---

**This framework ensures bulletproof data synchronization for your multi-strategy trading platform! 🎯**
