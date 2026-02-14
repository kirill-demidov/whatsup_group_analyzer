#!/usr/bin/env bash
# Перезапуск приложения на сервере. Запускать на сервере: ./scripts/restart_server.sh
set -e
cd "$(dirname "$0")/.."
export PATH="/root/.local/bin:$PATH"
pkill -f "uvicorn src.app" 2>/dev/null || true
pkill -f "node index.js" 2>/dev/null || true
sleep 2
nohup ./run.sh >> logs/run.log 2>&1 &
echo "Started. Waiting 5s..."
sleep 5
curl -s -o /dev/null -w "Backend: %{http_code}\n" http://127.0.0.1:8080/ || true
