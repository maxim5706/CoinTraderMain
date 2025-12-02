"""Convert JSONL logs to Parquet for analytics.

Run manually after a session or on a schedule:
    uv run python tools/session_rollup.py
    uv run python tools/session_rollup.py 2025-11-29
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")
LOGS_DIR = Path("logs")

LAYERS = ["raw", "candles_1m", "candles_5m", "burst", "signals", "trades"]


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def roll_layer(layer: str, date_str: str) -> bool:
    """Convert one layer's JSONL to Parquet."""
    src = LOGS_DIR / f"{layer}_{date_str}.jsonl"
    
    if not src.exists():
        print(f"[rollup] missing {src}, skipping")
        return False
    
    # Read JSONL
    rows = []
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    
    if not rows:
        print(f"[rollup] empty {src}")
        return False
    
    df = pd.DataFrame(rows)
    
    # Output path
    out_dir = DATA_DIR / layer
    ensure_dir(out_dir)
    out_path = out_dir / f"{layer}_{date_str}.parquet"
    
    df.to_parquet(out_path, index=False)
    print(f"[rollup] wrote {out_path} rows={len(df)}")
    return True


def main(date_str: str = None):
    """Roll up all layers for a given date."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    ensure_dir(DATA_DIR)
    
    success = 0
    for layer in LAYERS:
        if roll_layer(layer, date_str):
            success += 1
    
    print(f"[rollup] completed {success}/{len(LAYERS)} layers for {date_str}")


if __name__ == "__main__":
    # Accept optional date arg
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(date_arg)
