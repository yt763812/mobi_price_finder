
#!/bin/sh
set -eu

./run_local_bot_api.sh &
api_pid=$!

bot_pid=""

cleanup() {
    trap - EXIT INT TERM
    if [ -n "$bot_pid" ]; then
        kill "$bot_pid" 2>/dev/null || true
    fi
    kill "$api_pid" 2>/dev/null || true
    wait "$bot_pid" 2>/dev/null || true
    wait "$api_pid" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 143' INT TERM

echo "Waiting for local Telegram Bot API..."

ready=0
for i in $(seq 1 60); do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        echo "Telegram Bot API exited unexpectedly"
        exit 1
    fi

    if python -c '
import socket
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", 8081))
finally:
    s.close()
' 2>/dev/null; then
        ready=1
        break
    fi

    sleep 2
done

if [ "$ready" -ne 1 ]; then
    echo "Telegram Bot API startup timed out"
    exit 1
fi

echo "Local Telegram Bot API is listening"
echo "Starting Telegram bot..."

python bot.py &
bot_pid=$!

wait "$bot_pid"
