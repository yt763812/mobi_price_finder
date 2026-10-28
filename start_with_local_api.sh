#!/bin/sh
set -eu
./run_local_bot_api.sh &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM
# API server is started before the bot; bot retries start on temporary startup failures.
sleep 3
exec python bot.py
