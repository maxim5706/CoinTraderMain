#!/bin/bash
# CoinTrader Setup Script

set -e

echo "🚀 Setting up CoinTrader..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "⚠️  Please restart your terminal and run this script again"
    exit 0
fi

# Sync dependencies
echo "📦 Installing dependencies..."
uv sync

# Create .env from example if not exists
if [ ! -f .env ]; then
    echo "📝 Creating .env file..."
    cp .env.example .env
    echo "⚠️  Please edit .env with your Coinbase API credentials"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Coinbase API key and secret"
echo "     Get them from: https://www.coinbase.com/settings/api"
echo ""
echo "  2. Run the bot in paper mode:"
echo "     uv run python run.py"
echo ""
echo "  3. Watch the dashboard for signals and trades!"
echo ""
