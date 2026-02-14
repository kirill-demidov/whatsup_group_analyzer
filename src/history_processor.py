"""
Обработка истории чата: поиск любых номеров телефонов, контекст 5–10 сообщений,
определение через Gemini — учитель ли это; запись в таблицу с полями «кто написал» и «дата сообщения».
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.gemini_client import classify_phone_with_context
from src.logger import get_logger
from src.sheets_client import append_teacher_if_new

logger = get_logger("history_processor")

# Израильский мобильный: 054-xxx-xxxx, 050 xxx xx xx, 0546427786
PHONE_RE = re.compile(
    r"05[0-9][\s\-]*\d{3}[\s\-]*\d{4}|"
    r"\+972[\s\-]*5[0-9][\s\-]*\d{3}[\s\-]*\d{4}",
    re.IGNORECASE,
)

CONTEXT_BEFORE = 3
CONTEXT_AFTER = 10


def _extract_phones_from_text(text: str) -> list[str]:
    """Извлекает все израильские номера из текста (сырые вхождения)."""
    if not (text or "").strip():
        return []
    seen = set()
    out = []
    for m in PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 9:
            key = digits[-10:] if len(digits) >= 10 else digits
            if key not in seen:
                seen.add(key)
                out.append(raw)
    return out


def _format_message_date(ts: int | float | str | None) -> str:
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        except (OSError, ValueError):
            return str(ts)
    return str(ts)[:50]


def process_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    messages: список {"timestamp", "date", "from", "from_name", "body"} (как в group-history.json).
    Находит сообщения с телефонами, для каждого берёт контекст (несколько до + 5–10 после),
    спрашивает Gemini — учитель ли это. Возвращает список записей для таблицы.
    """
    results = []
    n = len(messages)

    for i, msg in enumerate(messages):
        body = (msg.get("body") or "").strip()
        if len(body) < 3:
            continue
        phones = _extract_phones_from_text(body)
        if not phones:
            continue

        start = max(0, i - CONTEXT_BEFORE)
        end = min(n, i + CONTEXT_AFTER + 1)
        window = []
        for j in range(start, end):
            m = messages[j]
            ts = m.get("timestamp")
            window.append({
                "from_name": m.get("from_name") or "—",
                "body": (m.get("body") or "").strip(),
                "date": m.get("date") or (datetime.fromtimestamp(ts).isoformat() if ts else ""),
                "timestamp": ts,
            })

        posted_by = (msg.get("from_name") or "—").strip()
        message_date = _format_message_date(msg.get("timestamp") or msg.get("date"))

        for phone_raw in phones:
            try:
                record = classify_phone_with_context(
                    window,
                    phone_raw,
                    posted_by=posted_by,
                    message_date=message_date,
                )
                if record:
                    record["source"] = body[:200]
                    results.append(record)
                    logger.info("Найден учитель по контексту: %s — %s (написал: %s)", record["teacherName"], record["phone"], posted_by)
            except Exception as e:
                logger.warning("Ошибка классификации номера %s: %s", phone_raw[:15], e)

    return results


def process_history_file(path: str | Path) -> list[dict[str, Any]]:
    """Загружает JSON с историей (как bridge/group-history.json) и возвращает список записей учителей."""
    import json
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raw = [raw]
    return process_history(raw)


def process_history_file_to_sheet(path: str | Path) -> dict[str, Any]:
    """
    Обрабатывает файл истории и записывает найденных учителей в таблицу.
    Возвращает {"processed": N, "added": M, "skipped_duplicates": K}.
    """
    records = process_history_file(path)
    added = 0
    skipped = 0
    for r in records:
        result = append_teacher_if_new(
            teacher_name=r["teacherName"],
            phone=r["phone"],
            source=r.get("source", ""),
            role=r.get("role", ""),
            posted_by=r.get("postedBy", ""),
            message_date=r.get("messageDate", ""),
        )
        if result.get("added"):
            added += 1
        else:
            skipped += 1
    return {"processed": len(records), "added": added, "skipped_duplicates": skipped}


if __name__ == "__main__":
    import sys
    from pathlib import Path
    # По умолчанию — group-history.json в корне проекта (JSON в формате: messages с body, from, from_name, date)
    project_root = Path(__file__).resolve().parent.parent
    default_path = project_root / "group-history.json"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    print("Обработка истории:", path)
    result = process_history_file_to_sheet(path)
    print("Результат:", result)
