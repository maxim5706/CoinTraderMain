#!/bin/bash
# Auto-restart script for CoinTrader bot
# Usage: ./start.sh [--daemon]

cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PIDFILE=".bot.pid"
LOGFILE="logs/bot.log"

start_bot() {
    if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
        echo -e "${YELLOW}Bot already running (PID: $(cat $PIDFILE))${NC}"
        return 1
    fi
    
    mkdir -p logs
    
    echo -e "${GREEN}Starting CoinTrader...${NC}"
    
    if [ "$1" == "--daemon" ]; then
        # Run in background with auto-restart
        nohup bash -c '
            while true; do
                echo "[$(date)] Bot starting..."
                .venv/bin/python run_v2.py 2>&1 | tee -a logs/bot.log
                EXIT_CODE=$?
                echo "[$(date)] Bot exited with code $EXIT_CODE"
                if [ $EXIT_CODE -eq 0 ]; then
                    echo "[$(date)] Clean exit, not restarting"
                    break
                fi
                echo "[$(date)] Restarting in 10 seconds..."
                sleep 10
            done
        ' > /dev/null 2>&1 &
        echo $! > "$PIDFILE"
        echo -e "${GREEN}Bot started in daemon mode (PID: $(cat $PIDFILE))${NC}"
        echo -e "Logs: tail -f $LOGFILE"
    else
        # Run in foreground
        .venv/bin/python run_v2.py
    fi
}

stop_bot() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo -e "${YELLOW}Stopping bot (PID: $PID)...${NC}"
            kill "$PID"
            rm -f "$PIDFILE"
            echo -e "${GREEN}Bot stopped${NC}"
        else
            echo -e "${RED}Bot not running (stale PID file)${NC}"
            rm -f "$PIDFILE"
        fi
    else
        echo -e "${RED}Bot not running (no PID file)${NC}"
    fi
}

status_bot() {
    if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
        echo -e "${GREEN}Bot is running (PID: $(cat $PIDFILE))${NC}"
        # Show last 5 lines of log
        if [ -f "$LOGFILE" ]; then
            echo ""
            echo "Recent log:"
            tail -5 "$LOGFILE"
        fi
    else
        echo -e "${RED}Bot is not running${NC}"
    fi
}

case "$1" in
    start)
        start_bot "$2"
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 2
        start_bot "$2"
        ;;
    status)
        status_bot
        ;;
    --daemon)
        start_bot "--daemon"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status} [--daemon]"
        echo ""
        echo "  start          Start in foreground"
        echo "  start --daemon Start in background with auto-restart"
        echo "  stop           Stop the bot"
        echo "  restart        Restart the bot"
        echo "  status         Check if running"
        echo ""
        # Default: start in foreground
        start_bot
        ;;
esac
