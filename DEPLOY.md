# Развёртывание на сервере (159.223.0.234)

Сервис: бэкенд (FastAPI, порт 8080) + мост WhatsApp на Baileys (порт 3080, только localhost). После деплоя QR для подключения WhatsApp открывается в браузере: `http://159.223.0.234:8080/app/qr.html`.

## Требования на сервере

- **Python 3.11+** и **[uv](https://docs.astral.sh/uv/)** (или pip)
- **Node.js 18+** (для моста Baileys)
- Открытый **порт 8080** (для доступа к веб-интерфейсу и API)

## 1. Подготовка один раз (на сервере)

Подключись по SSH и установи зависимости (если ещё нет):

```bash
ssh root@159.223.0.234

# Ubuntu/Debian: Python 3.11, Node.js, uv
sudo apt update
sudo apt install -y python3.11 python3.11-venv nodejs npm
curl -LsSf https://astral.sh/uv/install.sh | sh
# или: npm install -g n && n 20
```

Создай каталог приложения (например `/opt/playground` или `~/playground`):

```bash
mkdir -p /opt/playground
```

## 2. Деплой на сервер

**Вариант A: с локальной машины (где настроен SSH к серверу)**

Из каталога проекта выполни (подставь пользователя, если не root):

```bash
./scripts/deploy.sh 159.223.0.234
# или: ./scripts/deploy.sh user@159.223.0.234 /opt/playground
```

Скрипт копирует проект через rsync (без `.env`, без `credentials.json`, без `.venv` и `node_modules`). Папку `bridge-baileys/auth_baileys` на сервере не трогает.

**Вариант B: всё делать на сервере (если деплой с машины недоступен)**

Подключись по SSH и выполни (если репозиторий в Git — подставь URL репо; иначе залей архив и распакуй в `/opt/playground`):

```bash
ssh root@159.223.0.234
sudo mkdir -p /opt/playground
cd /opt/playground
# Если есть git:
# git clone https://github.com/ТВОЙ_РЕПО/playground.git .   # или скопируй файлы иначе
# Затем:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync
cd bridge-baileys && npm install && cd ..
cp .env.example .env
nano .env   # заполни GEMINI_API_KEY, GOOGLE_SPREADSHEET_ID и т.д.
./run.sh
```

## 3. Конфигурация на сервере

На сервере создай `.env` и при необходимости `credentials.json`:

```bash
ssh root@159.223.0.234
cd /opt/playground

# Скопировать шаблон и отредактировать
cp .env.example .env
nano .env   # заполни GEMINI_API_KEY, GOOGLE_SPREADSHEET_ID, пути к credentials и т.д.

# Если используешь файл ключей Google — скопируй credentials.json на сервер (scp с локальной машины)
```

Важно: **BRIDGE_URL** на сервере оставь `http://localhost:3080` (мост и бэкенд на одной машине).

## 4. Запуск на сервере

**Вариант A: вручную (screen/tmux)**

```bash
ssh root@159.223.0.234
cd /opt/playground
uv sync
nohup ./run.sh >> logs/run.log 2>&1 &
# или: screen -S wa && ./run.sh
```

**Вариант B: systemd (рекомендуется)**

На сервере создай юнит (подставь путь и пользователя):

```bash
sudo nano /etc/systemd/system/wa-teachers.service
```

Содержимое (один сервис — мост + бэкенд через run.sh):

```ini
[Unit]
Description=WhatsApp Teachers (bridge + backend)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/playground
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash -c 'cd /opt/playground && ./run.sh'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable wa-teachers
sudo systemctl start wa-teachers
sudo systemctl status wa-teachers
```

Логи: `journalctl -u wa-teachers -f`

## 5. Первый запуск и QR

1. Открой в браузере: **http://159.223.0.234:8080/app/qr.html**
2. Если мост уже подключён — нажми «Отключить мост», подожди 15 сек, обнови страницу.
3. Отсканируй QR в WhatsApp (Настройки → Связанные устройства → Привязать устройство).
4. Дальше пользуйся **http://159.223.0.234:8080/app/** (чаты, анализ через Gemini).

## 6. Файрвол

Если порт 8080 закрыт снаружи, открой его:

```bash
# UFW (Ubuntu)
sudo ufw allow 8080/tcp
sudo ufw reload
```
</think>
Добавляю в DEPLOY два systemd-юнита и скрипт деплоя.
<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>
StrReplace