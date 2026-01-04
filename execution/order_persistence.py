"""Order persistence for tracking all orders and comparing with exchange."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.logging_utils import get_logger

logger = get_logger(__name__)

# Order storage path
ORDERS_FILE = Path("data/live_orders.json")
ORDERS_BACKUP = Path("data/live_orders.json.bak")


def _atomic_write(path: Path, data: dict) -> bool:
    """Write data atomically: write to temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Backup existing file
    if path.exists():
        try:
            import shutil
            shutil.copy2(path, path.with_suffix(".json.bak"))
        except Exception as e:
            logger.warning("[ORDERS] Failed to create backup: %s", e)
    
    temp_fd = None
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=".orders_",
            suffix=".tmp"
        )
        with os.fdopen(temp_fd, "w") as f:
            temp_fd = None
            json.dump(data, f, indent=2, default=str)
        
        os.replace(temp_path, path)
        temp_path = None
        return True
    except Exception as e:
        logger.error("[ORDERS] Atomic write failed: %s", e)
        return False
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except Exception:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except Exception:
                pass


def save_orders(orders: Dict[str, dict], position_orders: Dict[str, dict]) -> bool:
    """
    Save all orders to disk for comparison with exchange.
    
    Args:
        orders: Dict of order_id -> order data
        position_orders: Dict of symbol -> position order IDs
    """
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "orders": orders,
        "position_orders": position_orders,
        "summary": {
            "total_orders": len(orders),
            "positions_with_stops": sum(
                1 for po in position_orders.values() 
                if po.get("stop_order_id")
            ),
        }
    }
    
    if _atomic_write(ORDERS_FILE, data):
        logger.debug("[ORDERS] Persisted %d orders, %d position mappings", 
                    len(orders), len(position_orders))
        return True
    return False


def load_orders() -> tuple[Dict[str, dict], Dict[str, dict]]:
    """
    Load orders from disk.
    
    Returns:
        Tuple of (orders dict, position_orders dict)
    """
    if not ORDERS_FILE.exists():
        return {}, {}
    
    try:
        with open(ORDERS_FILE, "r") as f:
            data = json.load(f)
        
        orders = data.get("orders", {})
        position_orders = data.get("position_orders", {})
        
        logger.info("[ORDERS] Loaded %d orders, %d position mappings from disk",
                   len(orders), len(position_orders))
        return orders, position_orders
    except Exception as e:
        logger.error("[ORDERS] Failed to load orders: %s", e)
        
        # Try backup
        if ORDERS_BACKUP.exists():
            try:
                with open(ORDERS_BACKUP, "r") as f:
                    data = json.load(f)
                logger.info("[ORDERS] Recovered from backup")
                return data.get("orders", {}), data.get("position_orders", {})
            except Exception:
                pass
        
        return {}, {}


def get_order_summary() -> dict:
    """Get summary of persisted orders for comparison."""
    if not ORDERS_FILE.exists():
        return {"error": "No orders file"}
    
    try:
        with open(ORDERS_FILE, "r") as f:
            data = json.load(f)
        
        orders = data.get("orders", {})
        position_orders = data.get("position_orders", {})
        
        # Count by status
        status_counts = {}
        for order in orders.values():
            status = order.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        
        # Count stops
        stops_count = sum(
            1 for po in position_orders.values() 
            if po.get("stop_order_id")
        )
        
        return {
            "last_updated": data.get("last_updated"),
            "total_orders": len(orders),
            "positions_tracked": len(position_orders),
            "positions_with_stops": stops_count,
            "by_status": status_counts,
        }
    except Exception as e:
        return {"error": str(e)}
