"""Lightweight preflight checks before starting the bot."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Callable

from dotenv import load_dotenv
from core.helpers import is_dust

load_dotenv()


def test_api_keys() -> tuple[bool, str]:
    """
    Test Coinbase API keys before starting the bot.
    
    Returns:
        (ok, message) tuple
    """
    key = os.getenv("COINBASE_API_KEY")
    secret = os.getenv("COINBASE_API_SECRET")
    
    if not key or not secret:
        return False, "Missing COINBASE_API_KEY or COINBASE_API_SECRET in .env"
    
    if key == "your_api_key_here" or secret == "your_api_secret_here":
        return False, "API keys not configured - still using placeholder values"
    
    try:
        from coinbase.rest import RESTClient
        
        client = RESTClient(api_key=key, api_secret=secret)
        accounts = client.get_accounts()
        
        if hasattr(accounts, 'accounts'):
            n = len(accounts.accounts)
        elif isinstance(accounts, dict):
            n = len(accounts.get("accounts", []))
        else:
            n = len(accounts) if accounts else 0
        
        # Try to get USD balance
        usd_balance = 0.0
        try:
            if hasattr(accounts, 'accounts'):
                for acc in accounts.accounts:
                    currency = getattr(acc, 'currency', None)
                    if currency == 'USD':
                        avail = getattr(acc, 'available_balance', None)
                        if avail:
                            usd_balance = float(getattr(avail, 'value', 0))
                        break
            elif isinstance(accounts, dict):
                for acc in accounts.get('accounts', []):
                    if acc.get('currency') == 'USD':
                        avail = acc.get('available_balance', {})
                        usd_balance = float(avail.get('value', 0))
                        break
        except Exception:
            pass  # Balance parsing failed, but auth worked
        
        if usd_balance > 0:
            return True, f"Authenticated. {n} accounts, ${usd_balance:.2f} USD available"
        else:
            return True, f"Authenticated. {n} accounts visible"
            
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            return False, "Auth failed: Invalid API key or secret"
        elif "403" in error_msg or "Forbidden" in error_msg:
            return False, "Auth failed: API key doesn't have required permissions"
        else:
            return False, f"Auth failed: {error_msg[:100]}"


def _test_write(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _ping_rest(rest_ping_func: Callable | None) -> tuple[bool, str]:
    if not rest_ping_func:
        return True, "skipped"
    try:
        rest_ping_func()
        return True, "ok"
    except Exception as e:
        return False, str(e)


def run_preflight(state, collector=None, router=None, rest_ping_func: Callable | None = None) -> Dict[str, Any]:
    """Return a dict of preflight checks with booleans and messages."""
    checks: Dict[str, Any] = {}

    # API status
    checks["api_ok"] = bool(getattr(state, "api_ok", False))

    # WebSocket
    ws_ok = False
    if collector and hasattr(collector, "is_connected"):
        ws_ok = bool(collector.is_connected)
    elif collector and hasattr(collector, "is_receiving"):
        ws_ok = bool(collector.is_receiving)
    else:
        ws_ok = bool(getattr(state, "ws_ok", False))
    checks["ws_ok"] = ws_ok

    # Snapshot age
    age = getattr(state, "portfolio_snapshot_age_s", 999)
    checks["sync_fresh"] = age < 15
    checks["sync_age_s"] = age

    # Warmth
    warm = getattr(state, "warm_symbols", 0)
    checks["warm_ready"] = warm > 0
    checks["warm_count"] = warm

    # Dust-free holdings
    dust_free = True
    try:
        holdings = getattr(router, "_exchange_holdings", {}) if router else {}
        for val in holdings.values():
            if is_dust(val):
                dust_free = False
                break
    except Exception:
        dust_free = True
    checks["dust_free"] = dust_free

    # Cooldowns
    cooldowns = getattr(router, "_order_cooldown", {}) if router else {}
    checks["cooldowns_active"] = len(cooldowns)

    # Filesystem writable
    checks["logs_writable"] = _test_write(Path("logs/.preflight_test"))
    checks["data_writable"] = _test_write(Path("data/.preflight_test"))

    # REST ping
    rest_ok, rest_reason = _ping_rest(rest_ping_func)
    checks["rest_ok"] = rest_ok
    checks["rest_reason"] = rest_reason

    # Clock sanity
    try:
        now = datetime.now(timezone.utc)
        checks["clock_ok"] = abs((datetime.utcnow().timestamp() - now.timestamp())) < 2
    except Exception:
        checks["clock_ok"] = True

    return checks
