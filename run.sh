#!/usr/bin/env bash
# Запускает мост (WhatsApp) и бэкенд, при необходимости открывает веб-приложение.
# Остановка: Ctrl+C (мост и бэкенд завершатся).

set -e
cd "$(dirname "$0")"

BRIDGE_PID=""
cleanup() {
  if [[ -n "$BRIDGE_PID" ]] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo ""
    echo "→ Останавливаю мост (PID $BRIDGE_PID)..."
    kill "$BRIDGE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# 1) Запуск моста Baileys в фоне (нужен Node 20+, иначе мост упадёт)
BRIDGE_DIR="bridge-baileys"
# На сервере может быть fnm с Node 20; без него node из PATH (часто v19) не подходит для Baileys
if [[ -d "$HOME/.local/share/fnm" ]]; then
  export PATH="$HOME/.local/share/fnm/aliases/default/bin:$PATH"
  [[ -f "$HOME/.local/share/fnm/fnm" ]] && eval "$("$HOME/.local/share/fnm/fnm" env)" 2>/dev/null; fnm use 20 2>/dev/null || true
fi
if command -v node >/dev/null 2>&1 && [[ -d "$BRIDGE_DIR" ]]; then
  if [[ ! -d "$BRIDGE_DIR/node_modules" ]]; then
    echo "→ Установка зависимостей моста ($BRIDGE_DIR, npm install)..."
    (cd "$BRIDGE_DIR" && npm install --silent)
  fi
  echo "→ Запуск моста ($BRIDGE_DIR) на порту 3080... (node: $(node -v 2>/dev/null || echo '?'))"
  (
    cd "$BRIDGE_DIR"
    [[ -d "$HOME/.local/share/fnm" ]] && export PATH="$HOME/.local/share/fnm/aliases/default/bin:$PATH" && fnm use 20 2>/dev/null || true
    export BACKEND_URL="${BACKEND_URL:-http://localhost:8080}"
    export WEB_PORT="${WEB_PORT:-3080}"
    export GROUP_ID="${WA_GROUP_ID:-}"
    export BROWSER_TYPE="${BROWSER_TYPE:-}"
    node index.js
  ) &
  BRIDGE_PID=$!
  echo "  Мост PID: $BRIDGE_PID"
  if [[ -n "${BROWSER_TYPE:-}" ]]; then
    echo "  BROWSER_TYPE=$BROWSER_TYPE (тип устройства для истории)"
  fi
  sleep 3
else
  echo "→ Мост не запущен (нет node или папки $BRIDGE_DIR). Запусти вручную: cd $BRIDGE_DIR && npm install && BACKEND_URL=http://localhost:8080 WEB_PORT=3080 node index.js"
  echo "  Если история пустая: отвяжи устройство, затем BROWSER_TYPE=chrome ./run.sh"
fi

# 2) Открыть браузер через 5 сек (один раз)
(
  sleep 5
  if command -v open >/dev/null 2>&1; then
    open "http://localhost:8080/app/" 2>/dev/null || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:8080/app/" 2>/dev/null || true
  fi
) &

# 3) Бэкенд на переднем плане (логи здесь)
echo ""
echo "→ Запуск бэкенда на http://0.0.0.0:8080"
echo "  Веб-приложение: http://localhost:8080/app/"
echo "  Остановить всё: Ctrl+C"
echo ""

exec uv run uvicorn src.app:app --host 0.0.0.0 --port 8080
