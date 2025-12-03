"""Create DuckDB views for modeling and analytics.

Run after session_rollup.py:
    uv run python tools/db_views.py

Then query:
    import duckdb
    con = duckdb.connect("data/cointrader.duckdb")
    con.sql("SELECT * FROM signal_outcomes LIMIT 20").df()
"""

import duckdb
from pathlib import Path

DB_PATH = Path("data/cointrader.duckdb")
DATA_DIR = Path("data")


def main():
    """Create/update all DuckDB views."""
    
    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    con = duckdb.connect(str(DB_PATH))
    
    # Load parquet globs as views (lazy, no data copy)
    views = {
        "raw": "raw/raw_*.parquet",
        "candles_1m": "candles_1m/candles_1m_*.parquet",
        "candles_5m": "candles_5m/candles_5m_*.parquet",
        "burst": "burst/burst_*.parquet",
        "signals": "signals/signals_*.parquet",
        "trades": "trades/trades_*.parquet",
    }
    
    for view_name, glob_pattern in views.items():
        full_path = DATA_DIR / glob_pattern
        try:
            con.execute(f"""
                CREATE OR REPLACE VIEW {view_name} AS
                SELECT * FROM parquet_scan('{full_path}');
            """)
            print(f"[duckdb] created view: {view_name}")
        except Exception as e:
            print(f"[duckdb] skipped {view_name}: {e}")
    
    # Modeling-ready join: signal → next trade outcome
    try:
        con.execute("""
            CREATE OR REPLACE VIEW signal_outcomes AS
            SELECT
                s.ts AS signal_ts,
                s.symbol,
                s.signal_type,
                s.confidence,
                s.price AS signal_price,
                s.reason,
                t.ts AS trade_ts,
                t.exit_price,
                t.pnl,
                t.pnl_pct,
                t.exit_reason,
                t.hold_minutes
            FROM signals s
            LEFT JOIN trades t
                ON s.symbol = t.symbol
                AND t.ts >= s.ts
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY s.ts, s.symbol 
                ORDER BY t.ts
            ) = 1
        """)
        print("[duckdb] created view: signal_outcomes")
    except Exception as e:
        print(f"[duckdb] skipped signal_outcomes: {e}")
    
    # Burst quality analysis view
    try:
        con.execute("""
            CREATE OR REPLACE VIEW burst_quality AS
            SELECT
                b.ts,
                b.symbol,
                b.burst_score,
                b.vol_spike,
                b.range_spike,
                b.trend_15m,
                b.rank,
                s.signal_type,
                t.pnl,
                t.exit_reason
            FROM burst b
            LEFT JOIN signals s
                ON b.symbol = s.symbol
                AND s.ts >= b.ts
                AND s.ts <= b.ts + INTERVAL 5 MINUTE
            LEFT JOIN trades t
                ON s.symbol = t.symbol
                AND t.ts >= s.ts
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY b.ts, b.symbol 
                ORDER BY s.ts, t.ts
            ) = 1
        """)
        print("[duckdb] created view: burst_quality")
    except Exception as e:
        print(f"[duckdb] skipped burst_quality: {e}")
    
    # Quick stats
    try:
        row_counts = {}
        for view in views.keys():
            try:
                result = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()
                row_counts[view] = result[0]
            except Exception:
                row_counts[view] = 0
        
        print("\n[duckdb] Row counts:")
        for view, count in row_counts.items():
            print(f"  {view}: {count:,}")
    except Exception:
        pass
    
    con.close()
    print(f"\n[duckdb] Database saved: {DB_PATH}")


if __name__ == "__main__":
    main()
