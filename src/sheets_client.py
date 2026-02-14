import re
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from src.config import config
from src.logger import get_logger

logger = get_logger("sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_sheets_service = None

# Колонки: Учитель | Телефон/Почта | Роль | Кто написал | Дата сообщения | Источник
_HEADERS = ["Учитель", "Телефон/Почта", "Роль", "Кто написал", "Дата сообщения", "Источник"]
_RANGE = "A:F"


def _get_sheets():
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    # Сначала — JSON из GCP Secret Manager (main_SA)
    if getattr(config, "GOOGLE_CREDENTIALS_JSON", None):
        logger.info("Sheets: учётные данные из GCP (main_SA)")
        creds = Credentials.from_service_account_info(
            config.GOOGLE_CREDENTIALS_JSON, scopes=_SCOPES
        )
    else:
        path = config.GOOGLE_CREDENTIALS_PATH
        if not path.exists():
            logger.error("Файл учётных данных не найден: %s", path)
            raise FileNotFoundError(
                f"Файл учётных данных не найден: {path}. "
                "Положите JSON ключ сервисного аккаунта или настройте секрет main_SA в GCP."
            )
        logger.info("Sheets: учётные данные из файла %s", path)
        creds = Credentials.from_service_account_file(str(path), scopes=_SCOPES)
    _sheets_service = build("sheets", "v4", credentials=creds)
    logger.debug("Клиент Google Sheets инициализирован")
    return _sheets_service


def _ensure_headers(sheets, sid: str, sheet_name: str) -> None:
    """Записывает строку заголовков (6 колонок), если её нет или старый формат (5 колонок)."""
    range_name = f"{sheet_name}!A1:F1"
    res = sheets.values().get(spreadsheetId=sid, range=range_name).execute()
    rows = res.get("values") or []
    first_row = (rows[0] if rows else [])
    need_headers = not rows or not any(c for c in first_row)
    # Переход с 5 колонок на 6 (добавили «Кто написал», «Дата сообщения»)
    if not need_headers and len(first_row) == 5 and (first_row[0] or "").strip().lower() == "учитель":
        need_headers = True
    if need_headers:
        sheets.values().update(
            spreadsheetId=sid,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [_HEADERS]},
        ).execute()
        logger.info("Записаны заголовки таблицы: %s", _HEADERS)


def append_teacher_if_new(
    teacher_name: str,
    phone: str,
    source: str = "",
    role: str = "",
    posted_by: str = "",
    message_date: str = "",
) -> dict[str, Any]:
    """Добавляет запись об учителе в таблицу. Дедупликация по телефону/email.
    posted_by — имя того, кто написал сообщение с номером; message_date — дата сообщения.
    """
    sid = config.GOOGLE_SPREADSHEET_ID
    sheet_name = config.GOOGLE_SHEET_NAME
    if not sid:
        raise ValueError("GOOGLE_SPREADSHEET_ID не задан")

    sheets = _get_sheets().spreadsheets()
    range_name = f"{sheet_name}!{_RANGE}"
    _ensure_headers(sheets, sid, sheet_name)
    res = sheets.values().get(spreadsheetId=sid, range=range_name).execute()
    rows = res.get("values") or []
    data_rows = rows[1:] if len(rows) > 1 else []
    logger.debug("append_teacher: в таблице строк данных: %s", len(data_rows))

    # Дедупликация: по телефону (последние 10 цифр) или по email
    is_email = "@" in (phone or "")
    for row in data_rows:
        if len(row) < 2:
            continue
        row_contact = str(row[1]).strip()
        if is_email and "@" in row_contact and row_contact.lower() == (phone or "").lower():
            logger.info("Дубликат не добавлен: %s — %s (совпадение по почте)", teacher_name, phone)
            return {"added": False, "reason": "duplicate"}
        if not is_email:
            norm = re.sub(r"\D", "", phone)
            row_norm = re.sub(r"\D", "", row_contact)
            if norm and row_norm and row_norm[-10:] == norm[-10:]:
                logger.info("Дубликат не добавлен: %s — %s (совпадение по телефону)", teacher_name, phone)
                return {"added": False, "reason": "duplicate"}

    new_row = [
        teacher_name,
        phone,
        (role or "—")[:200],
        (posted_by or "—")[:200],
        (message_date or __import__("datetime").datetime.now().strftime("%d.%m.%Y %H:%M"))[:50],
        (source or "")[:500],
    ]
    sheets.values().append(
        spreadsheetId=sid,
        range=range_name,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [new_row]},
    ).execute()
    logger.info("Добавлен учитель в таблицу: %s — %s (кто написал: %s)", teacher_name, phone, posted_by or "—")
    return {"added": True}


def get_existing_teachers() -> list[dict[str, Any]]:
    """Возвращает все записи из листа (для отладки и MCP)."""
    sid = config.GOOGLE_SPREADSHEET_ID
    sheet_name = config.GOOGLE_SHEET_NAME
    if not sid:
        logger.warning("get_existing_teachers: GOOGLE_SPREADSHEET_ID не задан")
        return []

    logger.debug("get_existing_teachers: лист=%s", sheet_name)
    sheets = _get_sheets().spreadsheets()
    res = sheets.values().get(
        spreadsheetId=sid,
        range=f"{sheet_name}!{_RANGE}",
    ).execute()
    rows = res.get("values") or []
    # Пропускаем заголовок (первая строка)
    data_start = 1 if rows and _is_header_row(rows[0]) else 0
    out = []
    for row in rows[data_start:]:
        # Новый формат (6 колонок): Учитель, Телефон, Роль, Кто написал, Дата сообщения, Источник
        if len(row) >= 6:
            out.append({
                "teacherName": row[0],
                "phone": row[1],
                "role": row[2],
                "postedBy": row[3],
                "messageDate": row[4],
                "source": row[5],
            })
        else:
            # Старый формат (5 колонок): Учитель, Телефон, Роль, Источник, Дата
            out.append({
                "teacherName": row[0] if len(row) > 0 else "",
                "phone": row[1] if len(row) > 1 else "",
                "role": row[2] if len(row) > 2 else "",
                "postedBy": "",
                "messageDate": row[4] if len(row) > 4 else "",
                "source": row[3] if len(row) > 3 else "",
            })
    return out


def _is_header_row(row: list) -> bool:
    """Проверяет, похожа ли первая ячейка на заголовок (Учитель / Teacher и т.д.)."""
    if not row:
        return False
    first = (row[0] or "").strip().lower()
    return first in ("учитель", "teacher", "teachername", "name")
