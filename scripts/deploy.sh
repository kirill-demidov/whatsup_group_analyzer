#!/usr/bin/env bash
# Копирует проект на сервер по SSH. Не перезаписывает .env и auth_baileys на сервере.
# Использование:
#   ./scripts/deploy.sh 159.223.0.234
#   ./scripts/deploy.sh -i ~/.ssh/do_key 159.223.0.234
#   ./scripts/deploy.sh user@159.223.0.234 /opt/playground

set -e
cd "$(dirname "$0")/.."

SSH_KEY=""
if [[ "${1:-}" == "-i" ]] && [[ -n "${2:-}" ]]; then
  SSH_KEY="$2"
  shift 2
fi
SERVER="${1:-159.223.0.234}"
REMOTE_DIR="${2:-/opt/playground}"
[[ -n "${SSH_KEY:-}" ]] && export RSYNC_RSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"

# Нормализуем SERVER: если передан только IP, подставляем root@
if [[ "$SERVER" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ "$SERVER" == *.* ]]; then
  if [[ "$SERVER" != *@* ]]; then
    SERVER="root@${SERVER}"
  fi
fi

echo "→ Деплой на $SERVER в $REMOTE_DIR"

# rsync: исключаем секреты, кэш, виртуальное окружение, node_modules
# На сервере не трогаем .env и bridge-baileys/auth_baileys (сессия WhatsApp)
rsync -avz --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='.env.*.local' \
  --exclude='credentials.json' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='bridge-baileys/node_modules' \
  --exclude='bridge-baileys/auth_baileys' \
  --exclude='logs' \
  --exclude='data/users.json' \
  . "${SERVER}:${REMOTE_DIR}/"

echo "→ Файлы скопированы."
echo ""
echo "Дальше на сервере:"
echo "  ssh $SERVER"
echo "  cd $REMOTE_DIR"
echo "  cp .env.example .env   # если ещё нет .env"
echo "  nano .env              # заполни переменные"
echo "  uv sync"
echo "  cd bridge-baileys && npm install"
echo "  ./run.sh   # или настрой systemd (см. DEPLOY.md)"
echo ""
HOST="${SERVER#*@}"
echo "QR для WhatsApp: http://${HOST}:8080/app/qr.html"
