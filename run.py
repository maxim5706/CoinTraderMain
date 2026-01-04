#!/usr/bin/env python3
"""
CoinTrader - Professional Crypto Trading Bot

Usage:
    python run.py                # Start web dashboard (UI+API) on default port
    python run.py -p 8080         # Start web dashboard on custom port
    python run.py --host 127.0.0.1  # Bind to localhost only
    python run.py --help       # Show all options

Examples:
    python run.py              # Start dashboard (then click Start in UI)
"""

import argparse

def main():
    parser = argparse.ArgumentParser(
        prog='cointrader',
        description='CoinTrader - Professional Crypto Trading Bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py              Start web dashboard (bot controlled from UI)
  python run.py -p 8080       Start web dashboard on custom port
"""
    )
    
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Web dashboard bind host (default: 0.0.0.0)')
    parser.add_argument('-p', '--port', type=int, default=8080,
                        help='Web dashboard port (default: 8080)')
    
    args = parser.parse_args()

    print(f"Web dashboard: http://localhost:{args.port}")

    # Dashboard is the boss: it controls the bot subprocess via API.
    from ui.web_server import run_server
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
