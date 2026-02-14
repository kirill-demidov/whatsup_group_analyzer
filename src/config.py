import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.logger import get_logger

log = get_logger("config")
# Всегда грузим .env из корня проекта (playground/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)
log.debug("Загружен .env: %s", _env_path)

# Проект GCP и секреты (playground-332710, пользователь kirkademidov@gmail.com)
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "playground-332710").strip()
GCP_SECRET_GEMINI = "gemini_api"
GCP_SECRET_SA = "main_SA"


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _path(key: str, default: str) -> Path:
    return Path(_str(key) or default)


def _load_gcp_secrets() -> tuple[str, dict[str, Any] | None]:
    """Загружает gemini_api и main_SA из GCP Secret Manager. Возвращает (gemini_key, sa_info_dict или None)."""
    try:
        from src.gcp_secrets import get_secret, get_secret_json

        log.info("Загрузка секретов из GCP: проект=%s, секреты=%s, %s", GCP_PROJECT_ID, GCP_SECRET_GEMINI, GCP_SECRET_SA)
        gemini_key = get_secret(GCP_PROJECT_ID, GCP_SECRET_GEMINI).strip()
        log.debug("Секрет %s загружен, длина ключа: %s", GCP_SECRET_GEMINI, len(gemini_key))
        sa_raw = get_secret(GCP_PROJECT_ID, GCP_SECRET_SA)
        sa_info = json.loads(sa_raw) if sa_raw else None
        if sa_info:
            log.debug("Секрет %s загружен, ключи: %s", GCP_SECRET_SA, list(sa_info.keys())[:6])
        return gemini_key, sa_info
    except Exception as e:
        log.warning("Ошибка загрузки GCP-секретов: %s", e, exc_info=True)
        return "", None


class Config:
    # Gemini: из .env или из GCP Secret Manager (gemini_api)
    GEMINI_API_KEY: str = _str("GEMINI_API_KEY")

    # Google Sheets: путь к файлу или JSON из GCP (main_SA)
    GOOGLE_CREDENTIALS_PATH: Path = _path("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    GOOGLE_CREDENTIALS_JSON: dict[str, Any] | None = None  # из секрета main_SA
    GOOGLE_SPREADSHEET_ID: str = _str("GOOGLE_SPREADSHEET_ID")
    GOOGLE_SHEET_NAME: str = _str("GOOGLE_SHEET_NAME") or "Учителя"

    # WhatsApp Cloud API (PyWa)
    WA_PHONE_ID: str = _str("WA_PHONE_ID")
    WA_TOKEN: str = _str("WA_TOKEN")
    WA_CALLBACK_URL: str = _str("WA_CALLBACK_URL")
    WA_VERIFY_TOKEN: str = _str("WA_VERIFY_TOKEN")
    WA_APP_ID: str = _str("WA_APP_ID")
    WA_APP_SECRET: str = _str("WA_APP_SECRET")
    # Фильтр по группе: только обрабатывать сообщения из этой группы (ID вида 120363...@g.us).
    WA_GROUP_ID: str = _str("WA_GROUP_ID")

    # URL моста для веб-приложения (status, chats, messages)
    BRIDGE_URL: str = _str("BRIDGE_URL") or "http://localhost:3080"

    # GCS: экспорт истории чатов (один раз выгрузить все чаты — анализ потом берёт оттуда)
    GCS_BUCKET: str = _str("GCS_BUCKET")
    GCS_EXPORT_PREFIX: str = _str("GCS_EXPORT_PREFIX") or "wa-export/latest"

    # Мультипользовательский режим: логин, у каждого — только свои группы
    AUTH_ENABLED: bool = _str("AUTH_ENABLED").lower() in ("1", "true", "yes")
    APP_SECRET_KEY: str = _str("APP_SECRET_KEY")  # секрет для подписи сессий (обязателен при AUTH_ENABLED)
    AUTH_USERS_FILE: str = _str("AUTH_USERS_FILE")  # путь к data/users.json (логины и chat_ids по пользователю)


config = Config()

# Подтягиваем секреты из GCP (playground-332710): gemini_api, main_SA
if GCP_PROJECT_ID:
    try:
        _gemini, _sa = _load_gcp_secrets()
        if _gemini and not config.GEMINI_API_KEY:
            config.GEMINI_API_KEY = _gemini
            log.info("GEMINI_API_KEY установлен из GCP (секрет %s)", GCP_SECRET_GEMINI)
        elif config.GEMINI_API_KEY:
            log.debug("GEMINI_API_KEY взят из .env")
        else:
            log.warning("GEMINI_API_KEY не задан ни в .env, ни в GCP")
        if _sa:
            config.GOOGLE_CREDENTIALS_JSON = _sa
            log.info("Учётные данные Google взяты из GCP (секрет %s)", GCP_SECRET_SA)
        else:
            log.debug("Учётные данные Google: из файла %s", config.GOOGLE_CREDENTIALS_PATH)
    except Exception as e:
        log.error("GCP Secret Manager недоступен: %s. Используйте .env и credentials.json.", e, exc_info=True)
else:
    log.debug("GCP_PROJECT_ID не задан, секреты из .env / файлов")

log.info(
    "Конфиг: GEMINI=%s, SHEET_ID=%s, SA_source=%s, WA_GROUP_ID=%s",
    "ok" if config.GEMINI_API_KEY else "—",
    (config.GOOGLE_SPREADSHEET_ID[:16] + "…") if config.GOOGLE_SPREADSHEET_ID else "—",
    "GCP" if config.GOOGLE_CREDENTIALS_JSON else "file",
    (config.WA_GROUP_ID[:20] + "…") if config.WA_GROUP_ID else "(не задан)",
)
