"""Загрузка секретов из GCP Secret Manager (проект playground-332710)."""

import json
from typing import Any

from google.cloud import secretmanager

from src.logger import get_logger

log = get_logger("gcp_secrets")


def get_secret(project_id: str, secret_id: str, version: str = "latest") -> str:
    """Возвращает значение секрета как строку."""
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    log.debug("Запрос секрета: %s", name)
    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": name})
        data = response.payload.data.decode("utf-8")
        log.debug("Секрет %s получен, размер: %s байт", secret_id, len(data))
        return data
    except Exception as e:
        log.error("Ошибка доступа к секрету %s: %s", secret_id, e)
        raise


def get_secret_json(project_id: str, secret_id: str, version: str = "latest") -> Any:
    """Возвращает значение секрета как распарсенный JSON."""
    raw = get_secret(project_id, secret_id, version)
    return json.loads(raw)
