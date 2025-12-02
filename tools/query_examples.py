"""Example queries for analyzing trading data.

Run after session_rollup.py and db_views.py:
    uv run python tools/query_examples.py
"""

import duckdb
from pathlib import Path

DB_PATH = Path("data/cointrader.duckdb")


def main():
    if not DB_PATH.exists():
        print("Run session_rollup.py and db_views.py first!")
        return
    
    con = duckdb.connect(str(DB_PATH), read_only=True)
    
    print("\n" + "="*60)
    print("TRADING ANALYTICS")
    print("="*60)
    
    # 1. Signal type distribution
    print("\n📊 Signal Type Distribution:")
    try:
        result = con.sql("""
            SELECT 
                signal_type,
                COUNT(*) as count,
                ROUND(AVG(confidence) * 100, 1) as avg_confidence
            FROM signals
            GROUP BY signal_type
            ORDER BY count DESC
        """).df()
        print(result.to_string(index=False))
    except Exception as e:
        print(f"  (no data yet: {e})")
    
    # 2. Trade outcomes
    print("\n💰 Trade Outcomes:")
    try:
        result = con.sql("""
            SELECT 
                exit_reason,
                COUNT(*) as trades,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl), 2) as avg_pnl,
                ROUND(AVG(pnl_pct), 2) as avg_pnl_pct
            FROM trades
            GROUP BY exit_reason
            ORDER BY total_pnl DESC
        """).df()
        print(result.to_string(index=False))
    except Exception as e:
        print(f"  (no trades yet: {e})")
    
    # 3. Burst score vs outcome
    print("\n🔥 Burst Score Buckets vs PnL:")
    try:
        result = con.sql("""
            SELECT 
                CASE 
                    WHEN burst_score >= 3 THEN 'high (≥3)'
                    WHEN burst_score >= 2 THEN 'med (2-3)'
                    ELSE 'low (<2)'
                END as burst_bucket,
                COUNT(*) as signals,
                ROUND(AVG(pnl), 2) as avg_pnl
            FROM burst_quality
            WHERE pnl IS NOT NULL
            GROUP BY burst_bucket
            ORDER BY avg_pnl DESC
        """).df()
        print(result.to_string(index=False))
    except Exception as e:
        print(f"  (no data yet: {e})")
    
    # 4. Top symbols by volume
    print("\n📈 Top Symbols by Tick Count (last session):")
    try:
        result = con.sql("""
            SELECT 
                symbol,
                COUNT(*) as ticks,
                ROUND(MIN(price), 4) as low,
                ROUND(MAX(price), 4) as high,
                ROUND((MAX(price) - MIN(price)) / MIN(price) * 100, 2) as range_pct
            FROM raw
            WHERE type = 'tick'
            GROUP BY symbol
            ORDER BY ticks DESC
            LIMIT 10
        """).df()
        print(result.to_string(index=False))
    except Exception as e:
        print(f"  (no data yet: {e})")
    
    # 5. Spread analysis
    print("\n📏 Spread Analysis (where available):")
    try:
        result = con.sql("""
            SELECT 
                symbol,
                ROUND(AVG(spread_bps), 2) as avg_spread_bps,
                ROUND(MAX(spread_bps), 2) as max_spread_bps,
                COUNT(*) as samples
            FROM raw
            WHERE spread_bps IS NOT NULL
            GROUP BY symbol
            ORDER BY avg_spread_bps DESC
            LIMIT 10
        """).df()
        print(result.to_string(index=False))
    except Exception as e:
        print(f"  (no spread data: {e})")
    
    con.close()
    print("\n" + "="*60)


if __name__ == "__main__":
    main()
