#!/bin/sh
set -eu
: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH}"
mkdir -p /data/bot-api
exec telegram-bot-api --local --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" \
  --http-ip-address=127.0.0.1 --http-port=8081 --dir=/data/bot-api
