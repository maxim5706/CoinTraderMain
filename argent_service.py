#!/usr/bin/env python3
"""
ARGENT - Autonomous AI Trading Assistant Service
=================================================
Standalone service that can control the trading bot, search the web,
take screenshots, and manage files.

Run with: pm2 start argent_service.py --name ARGENT --interpreter python3
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ============================================================================
# CONFIGURATION
# ============================================================================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:1b"
DATA_DIR = PROJECT_ROOT / "data" / "argent"
MEMORY_FILE = DATA_DIR / "memory.json"
KNOWLEDGE_FILE = DATA_DIR / "knowledge.json"
ACTIONS_LOG = DATA_DIR / "actions.json"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
LOGS_DIR = PROJECT_ROOT / "logs"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# RAG - RETRIEVAL & ANALYSIS
# ============================================================================

class SmartAnalyzer:
    """Analyze logs, data, and workspace to make smart decisions."""
    
    @staticmethod
    def get_recent_logs(lines: int = 50) -> str:
        """Get recent bot logs."""
        try:
            result = subprocess.run(
                ["pm2", "logs", "coin-back", "--lines", str(lines), "--nostream"],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout[-3000:] if result.stdout else "No logs available"
        except Exception as e:
            return f"Error getting logs: {e}"
    
    @staticmethod
    def get_trade_history() -> str:
        """Get recent trade decisions from bot."""
        try:
            stats_file = PROJECT_ROOT / "data" / "session_stats.json"
            if stats_file.exists():
                data = json.loads(stats_file.read_text())
                wins = data.get("wins", 0)
                losses = data.get("losses", 0)
                pnl = data.get("realized_pnl", 0)
                trades = data.get("trades", [])[-10:]
                
                lines = [f"Session: {wins}W/{losses}L, PnL: ${pnl:.2f}"]
                for t in trades:
                    lines.append(f"  - {t.get('symbol', '?')}: {t.get('exit_reason', '?')} ${t.get('pnl', 0):.2f}")
                return "\n".join(lines)
            return "No trade history found"
        except Exception as e:
            return f"Error: {e}"
    
    @staticmethod
    def get_rejections() -> str:
        """Analyze why trades are being rejected."""
        try:
            from core.shared_state import read_state
            state = read_state() or {}
            rej = state.get("rejections", {})
            if rej:
                lines = ["Trade rejections:"]
                for reason, count in sorted(rej.items(), key=lambda x: -x[1])[:5]:
                    lines.append(f"  - {reason}: {count:,}")
                return "\n".join(lines)
            return "No rejections data"
        except Exception as e:
            return f"Error: {e}"
    
    @staticmethod
    def get_positions_detail() -> str:
        """Get detailed position analysis."""
        try:
            from core.shared_state import read_state
            state = read_state() or {}
            positions = state.get("positions", [])
            
            if not positions:
                return "No open positions"
            
            # Sort by PnL
            sorted_pos = sorted(positions, key=lambda x: x.get("pnl_pct", 0), reverse=True)
            
            lines = [f"Positions ({len(positions)} total):"]
            
            # Winners
            winners = [p for p in sorted_pos if p.get("pnl_usd", 0) > 0]
            if winners:
                lines.append(f"\nWinners ({len(winners)}):")
                for p in winners[:5]:
                    sym = p.get("symbol", "?").replace("-USD", "")
                    pnl = p.get("pnl_usd", 0)
                    pct = p.get("pnl_pct", 0)
                    lines.append(f"  {sym}: +${pnl:.2f} ({pct:+.1f}%)")
            
            # Losers
            losers = [p for p in sorted_pos if p.get("pnl_usd", 0) < 0]
            if losers:
                lines.append(f"\nLosers ({len(losers)}):")
                for p in losers[:5]:
                    sym = p.get("symbol", "?").replace("-USD", "")
                    pnl = p.get("pnl_usd", 0)
                    pct = p.get("pnl_pct", 0)
                    stop = " [BEYOND STOP]" if pct < -8 else ""
                    lines.append(f"  {sym}: ${pnl:.2f} ({pct:.1f}%){stop}")
            
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"
    
    @staticmethod
    def search_codebase(query: str) -> str:
        """Search codebase with grep."""
        try:
            result = subprocess.run(
                ["grep", "-r", "-i", "-l", query, str(PROJECT_ROOT)],
                capture_output=True, text=True, timeout=10
            )
            files = result.stdout.strip().split("\n")[:10]
            if files and files[0]:
                return f"Found in: {', '.join([Path(f).name for f in files])}"
            return "No matches found"
        except Exception as e:
            return f"Error: {e}"
    
    @staticmethod
    def think(question: str) -> str:
        """Deep analysis mode - gather all context and reason."""
        context_parts = []
        
        # Gather all available data
        context_parts.append("=== POSITIONS ===")
        context_parts.append(SmartAnalyzer.get_positions_detail())
        
        context_parts.append("\n=== TRADE HISTORY ===")
        context_parts.append(SmartAnalyzer.get_trade_history())
        
        context_parts.append("\n=== REJECTIONS ===")
        context_parts.append(SmartAnalyzer.get_rejections())
        
        context_parts.append("\n=== RECENT LOGS ===")
        logs = SmartAnalyzer.get_recent_logs(30)
        # Filter to important lines
        important_lines = [l for l in logs.split("\n") if any(x in l.lower() for x in ["error", "close", "buy", "sell", "stop", "profit", "loss"])]
        context_parts.append("\n".join(important_lines[-15:]) if important_lines else "No significant log entries")
        
        full_context = "\n".join(context_parts)
        
        # Use AI to analyze
        prompt = f"""Analyze this trading data and answer the question.
Be specific - reference actual symbols, numbers, and data points.
Give actionable recommendations based on the data.

DATA:
{full_context}

QUESTION: {question}

ANALYSIS:"""
        
        try:
            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 500}
                },
                timeout=90
            )
            if response.status_code == 200:
                return response.json().get("response", "No analysis")
            return f"AI error: {response.status_code}"
        except Exception as e:
            return f"Analysis error: {e}"

app = FastAPI(title="Argent AI Service", version="2.0")

# Add CORS for dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# MEMORY SYSTEM (Persistent)
# ============================================================================

class ArgentMemory:
    """Enhanced persistent memory with knowledge base."""
    
    def __init__(self):
        self.short_term: List[Dict] = []  # Recent conversation
        self.decisions: List[Dict] = []    # Trading decisions
        self.knowledge: Dict[str, str] = {}  # Learned facts
        self.insights: List[str] = []      # Trading insights
        self.load()
    
    def load(self):
        if MEMORY_FILE.exists():
            try:
                data = json.loads(MEMORY_FILE.read_text())
                self.short_term = data.get("short_term", [])[-20:]
                self.decisions = data.get("decisions", [])[-100:]
                self.insights = data.get("insights", [])[-50:]
            except:
                pass
        if KNOWLEDGE_FILE.exists():
            try:
                self.knowledge = json.loads(KNOWLEDGE_FILE.read_text())
            except:
                pass
    
    def save(self):
        MEMORY_FILE.write_text(json.dumps({
            "short_term": self.short_term[-20:],
            "decisions": self.decisions[-100:],
            "insights": self.insights[-50:],
            "updated": datetime.now().isoformat()
        }, indent=2))
        KNOWLEDGE_FILE.write_text(json.dumps(self.knowledge, indent=2))
    
    def add_message(self, role: str, content: str):
        self.short_term.append({
            "role": role,
            "content": content,
            "time": datetime.now().isoformat()
        })
        self.save()
    
    def add_decision(self, action: str, reason: str, result: str):
        self.decisions.append({
            "time": datetime.now().isoformat(),
            "action": action,
            "reason": reason,
            "result": result
        })
        self.save()
    
    def learn(self, key: str, value: str):
        self.knowledge[key] = value
        self.save()
    
    def recall(self, query: str) -> str:
        """Search memory for relevant info."""
        results = []
        query_lower = query.lower()
        
        # Search knowledge
        for k, v in self.knowledge.items():
            if query_lower in k.lower() or query_lower in v.lower():
                results.append(f"Knowledge: {k} = {v}")
        
        # Search recent decisions
        for d in self.decisions[-10:]:
            if query_lower in d.get("action", "").lower():
                results.append(f"Decision: {d['action']} -> {d['result']}")
        
        return "\n".join(results) if results else "No relevant memories found."
    
    def get_context(self) -> str:
        lines = []
        if self.short_term:
            lines.append("Recent conversation:")
            for m in self.short_term[-5:]:
                lines.append(f"  {m['role']}: {m['content'][:100]}...")
        if self.decisions:
            lines.append(f"\nRecent decisions: {len(self.decisions)}")
            for d in self.decisions[-3:]:
                lines.append(f"  - {d['action']}: {d['result'][:50]}")
        return "\n".join(lines)


memory = ArgentMemory()


# ============================================================================
# BOT CONTROL
# ============================================================================

class BotControl:
    """Control the trading bot via PM2."""
    
    @staticmethod
    def status() -> Dict:
        try:
            result = subprocess.run(
                ["pm2", "jlist"],
                capture_output=True, text=True, timeout=10
            )
            processes = json.loads(result.stdout)
            bot = next((p for p in processes if p["name"] == "coin-back"), None)
            if bot:
                return {
                    "running": bot["pm2_env"]["status"] == "online",
                    "status": bot["pm2_env"]["status"],
                    "memory": bot["monit"]["memory"] // 1024 // 1024,
                    "uptime": bot["pm2_env"].get("pm_uptime", 0)
                }
            return {"running": False, "status": "not found"}
        except Exception as e:
            return {"running": False, "error": str(e)}
    
    @staticmethod
    def start() -> Dict:
        try:
            result = subprocess.run(
                ["pm2", "start", "coin-back"],
                capture_output=True, text=True, timeout=30
            )
            memory.add_decision("START BOT", "User request", result.stdout[:100])
            return {"success": True, "message": "Bot started"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def stop() -> Dict:
        try:
            result = subprocess.run(
                ["pm2", "stop", "coin-back"],
                capture_output=True, text=True, timeout=30
            )
            memory.add_decision("STOP BOT", "User request", result.stdout[:100])
            return {"success": True, "message": "Bot stopped"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def restart() -> Dict:
        try:
            result = subprocess.run(
                ["pm2", "restart", "coin-back"],
                capture_output=True, text=True, timeout=30
            )
            memory.add_decision("RESTART BOT", "User request", result.stdout[:100])
            return {"success": True, "message": "Bot restarted"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============================================================================
# WEB SEARCH
# ============================================================================

class WebSearch:
    """Search the web for trading info."""
    
    @staticmethod
    def search(query: str) -> str:
        """Search using DuckDuckGo (no API key needed)."""
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
                if results:
                    lines = [f"Search results for: {query}\n"]
                    for r in results:
                        lines.append(f"- {r['title']}: {r['body'][:150]}...")
                    return "\n".join(lines)
                return "No results found."
        except ImportError:
            return "Web search not available. Install: pip install duckduckgo-search"
        except Exception as e:
            return f"Search error: {e}"
    
    @staticmethod
    def get_crypto_price(symbol: str) -> str:
        """Get current price from CoinGecko."""
        try:
            # Map common symbols
            symbol_map = {
                "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
                "ADA": "cardano", "DOT": "polkadot", "LINK": "chainlink"
            }
            coin_id = symbol_map.get(symbol.upper(), symbol.lower())
            
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true",
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if coin_id in data:
                    price = data[coin_id]["usd"]
                    change = data[coin_id].get("usd_24h_change", 0)
                    return f"{symbol.upper()}: ${price:,.2f} ({change:+.1f}% 24h)"
            return f"Could not get price for {symbol}"
        except Exception as e:
            return f"Price error: {e}"


# ============================================================================
# FILE OPERATIONS
# ============================================================================

class FileOps:
    """Safe file operations within project."""
    
    ALLOWED_DIRS = [
        PROJECT_ROOT / "data",
        PROJECT_ROOT / "config",
        PROJECT_ROOT / "logs",
    ]
    
    @staticmethod
    def is_safe_path(path: Path) -> bool:
        """Check if path is within allowed directories."""
        path = path.resolve()
        return any(str(path).startswith(str(d)) for d in FileOps.ALLOWED_DIRS)
    
    @staticmethod
    def read_file(filepath: str) -> str:
        path = Path(filepath)
        if not path.is_absolute():
            path = PROJECT_ROOT / filepath
        
        if not FileOps.is_safe_path(path):
            return f"Access denied: {filepath}"
        
        if not path.exists():
            return f"File not found: {filepath}"
        
        try:
            return path.read_text()[:5000]  # Limit size
        except Exception as e:
            return f"Error reading file: {e}"
    
    @staticmethod
    def write_file(filepath: str, content: str) -> Dict:
        path = Path(filepath)
        if not path.is_absolute():
            path = PROJECT_ROOT / filepath
        
        if not FileOps.is_safe_path(path):
            return {"success": False, "error": "Access denied"}
        
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            memory.add_decision(f"WRITE {filepath}", "User request", "success")
            return {"success": True, "message": f"Written to {filepath}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def list_dir(dirpath: str) -> str:
        path = Path(dirpath)
        if not path.is_absolute():
            path = PROJECT_ROOT / dirpath
        
        if not path.exists():
            return f"Directory not found: {dirpath}"
        
        try:
            items = list(path.iterdir())[:50]
            lines = [f"Contents of {dirpath}:"]
            for item in items:
                size = item.stat().st_size if item.is_file() else "dir"
                lines.append(f"  {'[D]' if item.is_dir() else '[F]'} {item.name} ({size})")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing directory: {e}"


# ============================================================================
# SCREENSHOT (Playwright)
# ============================================================================

class Screenshots:
    """Take screenshots using Playwright."""
    
    @staticmethod
    async def capture(url: str, name: str = None) -> Dict:
        try:
            from playwright.async_api import async_playwright
            
            if not name:
                name = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            filepath = SCREENSHOTS_DIR / f"{name}.png"
            
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.goto(url, timeout=30000)
                await page.screenshot(path=str(filepath), full_page=True)
                await browser.close()
            
            memory.add_decision(f"SCREENSHOT {url}", "User request", str(filepath))
            return {"success": True, "path": str(filepath)}
        except ImportError:
            return {"success": False, "error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============================================================================
# PORTFOLIO DATA
# ============================================================================

def get_portfolio_context() -> str:
    """Get current portfolio state."""
    try:
        from core.shared_state import read_state
        state = read_state() or {}
        
        positions = state.get('positions', [])
        winners = [p for p in positions if p.get('pnl_usd', 0) > 0]
        losers = [p for p in positions if p.get('pnl_usd', 0) < 0]
        total_pnl = sum(p.get('pnl_usd', 0) for p in positions)
        
        lines = [
            f"Portfolio: ${state.get('portfolio_value', 0):.2f}",
            f"Cash: ${state.get('cash_balance', 0):.2f}",
            f"Positions: {len(positions)} ({len(winners)}W / {len(losers)}L)",
            f"Unrealized P&L: ${total_pnl:.2f}",
            f"Exposure: {state.get('engine', {}).get('exposure_pct', 0):.0f}%",
        ]
        
        # Alert for positions beyond stop
        beyond_stop = [p for p in losers if abs(p.get('pnl_pct', 0)) > 8]
        if beyond_stop:
            syms = [p.get('symbol', '?').replace('-USD', '') for p in beyond_stop]
            lines.append(f"ALERT: {', '.join(syms)} beyond stop!")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting portfolio: {e}"


# ============================================================================
# AI CHAT
# ============================================================================

SYSTEM_PROMPT = """You are Argent, a friendly AI assistant that helps with crypto trading.

Be natural and conversational - like talking to a knowledgeable friend, not a robot.
Keep responses short and casual. Don't list rules or give lectures.
If someone says "hi", just say hi back warmly.

You can help with:
- Checking portfolio and positions
- Starting/stopping the trading bot
- Looking up crypto prices
- Searching for market news
- Giving trading advice when asked

Only mention portfolio details if relevant to the conversation.
Don't dump information unless asked. Be chill."""


def process_command(message: str) -> Optional[str]:
    """Process direct commands."""
    msg = message.lower().strip()
    
    # Thinking/Analysis mode - deep analysis
    if any(x in msg for x in ["think", "analyze", "why", "what happened", "decisions", "explain"]):
        return SmartAnalyzer.think(message)
    
    # Quick data commands
    if msg in ["positions", "show positions", "my positions"]:
        return SmartAnalyzer.get_positions_detail()
    
    if msg in ["trades", "history", "trade history"]:
        return SmartAnalyzer.get_trade_history()
    
    if msg in ["logs", "show logs", "recent logs"]:
        return SmartAnalyzer.get_recent_logs(30)
    
    if msg in ["rejections", "why rejected", "rejected"]:
        return SmartAnalyzer.get_rejections()
    
    # Bot control
    if msg == "bot status":
        status = BotControl.status()
        return f"Bot: {status.get('status', 'unknown')}, Memory: {status.get('memory', 0)}MB"
    if msg == "start bot" or msg == "bot start":
        result = BotControl.start()
        return result.get("message", result.get("error"))
    if msg == "stop bot" or msg == "bot stop":
        result = BotControl.stop()
        return result.get("message", result.get("error"))
    if msg == "restart bot" or msg == "bot restart":
        result = BotControl.restart()
        return result.get("message", result.get("error"))
    
    # Web search
    if msg.startswith("search "):
        query = message[7:]
        return WebSearch.search(query)
    
    # Crypto price
    if msg.startswith("price "):
        symbol = message[6:].strip()
        return WebSearch.get_crypto_price(symbol)
    
    # File operations
    if msg.startswith("read "):
        filepath = message[5:].strip()
        return FileOps.read_file(filepath)
    
    if msg.startswith("list ") or msg.startswith("ls "):
        dirpath = message.split(" ", 1)[1].strip() if " " in message else "data"
        return FileOps.list_dir(dirpath)
    
    # Memory
    if msg.startswith("remember "):
        parts = message[9:].split(" ", 1)
        if len(parts) == 2:
            memory.learn(parts[0], parts[1])
            return f"Remembered: {parts[0]}"
        return "Usage: remember [key] [value]"
    
    if msg.startswith("recall "):
        query = message[7:]
        return memory.recall(query)
    
    return None


def chat(user_message: str) -> str:
    """Main chat function."""
    memory.add_message("user", user_message)
    
    # Try direct command first
    cmd_result = process_command(user_message)
    if cmd_result:
        memory.add_message("argent", cmd_result)
        return cmd_result
    
    # AI conversation
    try:
        context = get_portfolio_context()
        
        # Build conversation history for context
        history_lines = []
        for msg in memory.short_term[-8:]:  # Last 8 messages
            role = "User" if msg["role"] == "user" else "Argent"
            content = msg["content"][:200]  # Truncate long messages
            history_lines.append(f"{role}: {content}")
        
        conversation_history = "\n".join(history_lines) if history_lines else ""
        
        prompt = f"""{SYSTEM_PROMPT}

PORTFOLIO: {context}

CONVERSATION HISTORY:
{conversation_history}

User: {user_message}

Argent:"""
        
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.5, "num_predict": 300}
            },
            timeout=60
        )
        
        if response.status_code == 200:
            reply = response.json().get("response", "No response")
            memory.add_message("argent", reply)
            return reply
        return f"AI error: {response.status_code}"
        
    except Exception as e:
        return f"Error: {e}"


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.post("/chat")
async def api_chat(request: Request):
    data = await request.json()
    message = data.get("message", "")
    if not message:
        return {"error": "No message"}
    response = chat(message)
    return {"response": response}


@app.get("/status")
async def api_status():
    return {
        "argent": "online",
        "bot": BotControl.status(),
        "memory_size": len(memory.decisions)
    }


@app.post("/screenshot")
async def api_screenshot(request: Request):
    data = await request.json()
    url = data.get("url", "")
    name = data.get("name")
    result = await Screenshots.capture(url, name)
    return result


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("Starting Argent AI Service...")
    print(f"Data directory: {DATA_DIR}")
    print(f"Memory loaded: {len(memory.decisions)} decisions")
    
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
