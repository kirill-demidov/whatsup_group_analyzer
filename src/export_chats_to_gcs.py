"""
Одноразовый (или по расписанию) экспорт всех чатов из моста в GCS.
Запуск: мост должен быть запущен и подключён. uv run python -m src.export_chats_to_gcs

Переменные: GCS_BUCKET, BRIDGE_URL (по умолчанию http://localhost:3080).
Учётные данные для GCS — как у приложения (main_SA или credentials.json).
"""

import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from src.config import config
from src.gcs_client import save_chat_export
from src.logger import get_logger

logger = get_logger("export_gcs")

# Таймаут на один чат (сек): загрузка до 100k сообщений может занять 10–20 мин
EXPORT_CHAT_TIMEOUT = 600
# Пауза между чатами (сек), чтобы снизить нагрузку на WhatsApp
DELAY_BETWEEN_CHATS = 15
# Лимит сообщений на чат при экспорте (мост поддерживает до 100000)
EXPORT_MESSAGE_LIMIT = 100000
# Повторная попытка при таймауте/сбое (1 = один повтор)
EXPORT_RETRIES = 1


def bridge_fetch(path: str, timeout: int = 30) -> tuple[int, dict]:
    """Запрос к мосту (без зависимости от app)."""
    url = (config.BRIDGE_URL or "").rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode()) if e.fp else {}
        except Exception:
            body = {"error": str(e)}
        return e.code, body
    except Exception as e:
        logger.warning("Bridge %s: %s", path, e)
        return 503, {"error": str(e)}


def main() -> None:
    if not config.GCS_BUCKET:
        logger.error("Задайте GCS_BUCKET в .env или переменных окружения")
        sys.exit(1)

    status, body = bridge_fetch("/api/status", timeout=15)
    if status != 200 or not (body.get("connected")):
        logger.error("Мост не подключён. Запустите bridge и отсканируйте QR.")
        sys.exit(1)

    status, body = bridge_fetch("/api/chats", timeout=30)
    if status != 200:
        logger.error("Не удалось загрузить список чатов: %s", body.get("error"))
        sys.exit(1)

    chats = body.get("chats") or []
    logger.info("Чатов к экспорту: %s", len(chats))

    for i, chat in enumerate(chats):
        cid = chat.get("id") or ""
        name = chat.get("name") or cid
        if not cid:
            continue
        logger.info("[%s/%s] Загрузка чата: %s", i + 1, len(chats), name[:50])
        path = f"/api/chat/{quote(cid, safe='')}/messages?limit={EXPORT_MESSAGE_LIMIT}&sync=1"
        status, data = bridge_fetch(path, timeout=EXPORT_CHAT_TIMEOUT)
        attempt = 0
        while status != 200 and attempt < EXPORT_RETRIES:
            attempt += 1
            logger.warning("Чат %s: ошибка %s — %s, повтор через 5 сек…", name[:30], status, data.get("error"))
            time.sleep(5)
            status, data = bridge_fetch(path, timeout=EXPORT_CHAT_TIMEOUT)
        if status != 200:
            logger.warning("Чат %s: ошибка %s — %s (пропуск)", name[:30], status, data.get("error"))
            time.sleep(5)
            continue
        messages = data.get("messages") or []
        logger.info("[%s/%s] Получено сообщений: %s", i + 1, len(chats), len(messages))
        save_chat_export(cid, name, messages)
        if i < len(chats) - 1:
            logger.info("Пауза %s сек…", DELAY_BETWEEN_CHATS)
            time.sleep(DELAY_BETWEEN_CHATS)

    logger.info("Экспорт завершён.")


if __name__ == "__main__":
    main()
