"""API key preflight check - fail early if auth is broken."""

import os
from dotenv import load_dotenv

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
        
        # Lightweight authenticated call
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


def get_account_balance(currency: str = "USD") -> float:
    """Get account balance for a currency."""
    key = os.getenv("COINBASE_API_KEY")
    secret = os.getenv("COINBASE_API_SECRET")
    
    if not key or not secret:
        return 0.0
    
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(api_key=key, api_secret=secret)
        accounts = client.get_accounts()
        
        if hasattr(accounts, 'accounts'):
            for acc in accounts.accounts:
                if hasattr(acc, 'currency') and acc.currency == currency:
                    if hasattr(acc, 'available_balance'):
                        return float(acc.available_balance.value)
        return 0.0
    except Exception:
        return 0.0


if __name__ == "__main__":
    # Quick test
    ok, msg = test_api_keys()
    print(f"{'✅' if ok else '❌'} {msg}")
