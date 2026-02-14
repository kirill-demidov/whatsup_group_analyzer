"""MCP-сервер: инструменты для просмотра/добавления учителей и разбора текста через Gemini."""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from src.config import config
from src.gemini_client import extract_teacher_phones
from src.logger import get_logger
from src.sheets_client import append_teacher_if_new, get_existing_teachers

logger = get_logger("mcp_server")

BRIDGE_URL = (config.BRIDGE_URL or "http://localhost:3080").rstrip("/")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")

mcp = FastMCP(
    "whatsapp-teachers",
    json_response=True,
)


@mcp.tool()
def list_teachers() -> list[dict]:
    """Возвращает список всех учителей из Google Таблицы (Учитель, Телефон, Источник, Дата)."""
    logger.info("MCP tool вызван: list_teachers")
    try:
        out = get_existing_teachers()
        logger.info("list_teachers: возвращено записей %s", len(out))
        return out
    except Exception as e:
        logger.exception("list_teachers ошибка: %s", e)
        return [{"error": str(e)}]


@mcp.tool()
def add_teacher(teacher_name: str, phone: str, role: str = "", source: str = "") -> dict:
    """Добавляет учителя в Google Таблицу. role — кто/что за учитель (например: מורה למוזיקה). Дедупликация по телефону/почте."""
    logger.info("MCP tool вызван: add_teacher name=%s phone=%s role=%s", teacher_name, phone, role or "")
    try:
        result = append_teacher_if_new(
            teacher_name.strip(), phone.strip(), source.strip()[:500], role=(role or "").strip()[:200]
        )
        logger.info("add_teacher результат: %s", result)
        return result
    except Exception as e:
        logger.exception("add_teacher ошибка: %s", e)
        return {"error": str(e)}


def _bridge_get(path: str, timeout: int = 30) -> dict:
    """GET к мосту Baileys. Возвращает JSON или пустой dict при ошибке."""
    try:
        req = urllib.request.Request(BRIDGE_URL + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()) if e.fp else {}
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def _backend_post_webhook(text: str, chat_id: str | None, from_name: str | None) -> dict:
    """POST сообщения в бэкенд webhook. Возвращает ответ process_message_text."""
    try:
        data = json.dumps({"text": text, "chat_id": chat_id, "from_name": from_name or ""}).encode()
        req = urllib.request.Request(
            BACKEND_URL + "/webhook/bridge",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def scan_chat_history(group_id: str = "", limit: int = 500) -> dict:
    """Сканирует историю WhatsApp-группы через мост Baileys: загружает сообщения и отправляет в бэкенд (Gemini + таблица).
    group_id — ID группы (120363...@g.us). Если пусто — берётся WA_GROUP_ID из конфига.
    limit — макс. сообщений (по умолчанию 500). Мост и бэкенд должны быть запущены."""
    gid = (group_id or config.WA_GROUP_ID or "").strip()
    if not gid:
        return {"error": "Укажи group_id или задай WA_GROUP_ID в .env. Узнать ID: list_whatsapp_chats(groups_only=True)"}
    lim = max(1, min(limit or 500, 10000))
    path = "/api/chat/" + quote(gid, safe="") + f"/messages?limit={lim}"
    body = _bridge_get(path, timeout=120)
    if "error" in body:
        return body
    messages = body.get("messages") or []
    processed = 0
    added = 0
    for m in messages:
        text = (m.get("body") or "").strip()
        if len(text) < 5:
            continue
        result = _backend_post_webhook(
            text,
            gid,
            m.get("from_name") or m.get("from") or "",
        )
        if result.get("processed"):
            processed += 1
            added += result.get("added", 0)
    return {"messages_found": len(messages), "processed": processed, "added_to_sheet": added}


@mcp.tool()
def list_whatsapp_chats(groups_only: bool = True) -> dict:
    """Список WhatsApp-чатов с их ID (через мост Baileys). groups_only=True — только группы. Мост должен быть запущен."""
    body = _bridge_get("/api/chats", timeout=15)
    if "error" in body:
        return body
    chats = body.get("chats") or []
    if groups_only:
        chats = [c for c in chats if c.get("isGroup")]
    return {"chats": [{"id": c.get("id"), "name": c.get("name"), "messageCount": c.get("messageCount", 0)} for c in chats]}


@mcp.tool()
def parse_message_for_teachers(message_text: str) -> list[dict]:
    """Извлекает из текста сообщения упоминания учителей и телефонов с помощью Gemini. Не записывает в таблицу."""
    logger.info("MCP tool вызван: parse_message_for_teachers text_len=%s", len(message_text or ""))
    if not config.GEMINI_API_KEY:
        logger.warning("parse_message_for_teachers: GEMINI_API_KEY не задан")
        return [{"error": "GEMINI_API_KEY не задан"}]
    try:
        out = extract_teacher_phones(message_text or "")
        logger.info("parse_message_for_teachers: извлечено контактов %s", len(out))
        return out
    except Exception as e:
        logger.exception("parse_message_for_teachers ошибка: %s", e)
        return [{"error": str(e)}]


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
