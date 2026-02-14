"""Работа с GCS: сохранение и чтение экспорта истории чатов (wa-export/latest/)."""

import json
import re
from typing import Any

from src.config import config
from src.logger import get_logger

logger = get_logger("gcs")

_GCS_CLIENT: Any = None


def _get_client():
    """GCS Client с учётами из config (main_SA или файл)."""
    global _GCS_CLIENT
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    if not config.GCS_BUCKET:
        raise ValueError("GCS_BUCKET не задан в конфиге")
    from google.cloud import storage
    from google.oauth2 import service_account

    if config.GOOGLE_CREDENTIALS_JSON:
        creds = service_account.Credentials.from_service_account_info(
            config.GOOGLE_CREDENTIALS_JSON,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS_PATH),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    _GCS_CLIENT = storage.Client(credentials=creds)
    return _GCS_CLIENT


def chat_id_to_blob_name(chat_id: str) -> str:
    """Безопасное имя объекта: 120363...@g.us → 120363..._at_g_us.json"""
    safe = (chat_id or "").strip()
    safe = re.sub(r"@", "_at_", safe)
    safe = re.sub(r"[^\w\-]", "_", safe)
    return f"{safe}.json"


def load_chat_messages(chat_id: str) -> list[dict[str, Any]] | None:
    """
    Загружает экспорт чата из GCS. Путь: {GCS_EXPORT_PREFIX}/{chat_id_safe}.json.
    Возвращает список сообщений или None, если объекта нет.
    """
    if not config.GCS_BUCKET:
        return None
    try:
        from google.cloud.exceptions import NotFound

        client = _get_client()
        bucket = client.bucket(config.GCS_BUCKET)
        prefix = (config.GCS_EXPORT_PREFIX or "").strip().rstrip("/")
        blob_path = f"{prefix}/{chat_id_to_blob_name(chat_id)}" if prefix else chat_id_to_blob_name(chat_id)
        blob = bucket.blob(blob_path)
        try:
            data = json.loads(blob.download_as_string().decode())
        except NotFound:
            return None
        messages = data.get("messages")
        if messages is None:
            return None
        return list(messages)
    except Exception as e:
        logger.warning("GCS load %s: %s", chat_id[:30], e)
        return None


def save_chat_export(
    chat_id: str,
    chat_name: str,
    messages: list[dict[str, Any]],
) -> None:
    """
    Сохраняет экспорт чата в GCS. Формат: { chat_id, chat_name, exported_at, message_count, messages }.
    """
    if not config.GCS_BUCKET:
        raise ValueError("GCS_BUCKET не задан")
    from datetime import datetime, timezone

    client = _get_client()
    bucket = client.bucket(config.GCS_BUCKET)
    prefix = (config.GCS_EXPORT_PREFIX or "").strip().rstrip("/")
    blob_path = f"{prefix}/{chat_id_to_blob_name(chat_id)}" if prefix else chat_id_to_blob_name(chat_id)
    blob = bucket.blob(blob_path)
    payload = {
        "chat_id": chat_id,
        "chat_name": chat_name or "",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(messages),
        "messages": messages,
    }
    blob.upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=0),
        content_type="application/json",
    )
    logger.info("GCS сохранён %s: %s сообщений → %s", chat_id[:24], len(messages), blob_path)
