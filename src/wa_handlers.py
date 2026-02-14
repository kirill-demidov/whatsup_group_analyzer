"""Обработчик входящих сообщений WhatsApp: извлечение контактов учителей и запись в Google Таблицы."""

from pywa import WhatsApp, types, filters

from src.config import config
from src.gemini_client import extract_teacher_phones
from src.logger import get_logger
from src.sheets_client import append_teacher_if_new

logger = get_logger("wa_handlers")


def process_message_text(
    text: str,
    chat_id: str | None = None,
    from_name: str | None = None,
) -> dict:
    """
    Общая логика: разбор текста через Gemini и запись в таблицу.
    from_name — кто написал сообщение (для колонки «Кто написал»).
    """
    if not text or len(text.strip()) < 5:
        return {"processed": False, "reason": "text_too_short"}
    want = (config.WA_GROUP_ID or "").strip()
    got = (chat_id or "").strip()
    if want and got and want != got:
        logger.warning("Пропуск: chat_id не совпадает с WA_GROUP_ID (got=%s, want=%s)", got, want)
        return {"processed": False, "reason": "chat_not_target_group"}
    text = text.strip()
    logger.info("Обработка сообщения: len=%s, chat_id=%s, from_name=%s", len(text), chat_id, from_name or "—")
    try:
        from datetime import datetime
        message_date = datetime.now().strftime("%d.%m.%Y %H:%M")
        extracted = extract_teacher_phones(text)
        logger.info("Извлечено контактов: %s", len(extracted))
        added = 0
        for item in extracted:
            name = item.get("teacherName") or "—"
            phone = item.get("phone") or "—"
            role = item.get("role") or "—"
            source = item.get("source", "")[:200]
            result = append_teacher_if_new(
                name, phone, source, role=role,
                posted_by=from_name or "",
                message_date=message_date,
            )
            if result.get("added"):
                added += 1
                logger.info("Добавлен в таблицу: %s — %s (написал: %s)", name, phone, from_name or "—")
        return {"processed": True, "extracted": len(extracted), "added": added}
    except Exception as e:
        logger.exception("Ошибка обработки сообщения: %s", e)
        return {"processed": False, "reason": str(e)}


def _get_chat_id_from_message(msg: types.Message) -> str | None:
    """ID чата: для группы — из context.id в raw, иначе from_user.wa_id."""
    try:
        raw = getattr(msg, "raw", None)
        if raw and "entry" in raw:
            value = raw["entry"][0]["changes"][0]["value"]
            for m in value.get("messages", []):
                ctx = m.get("context", {})
                if ctx.get("id"):
                    return ctx["id"]
                return m.get("from")
    except (KeyError, IndexError, TypeError):
        pass
    if getattr(msg, "from_user", None):
        return getattr(msg.from_user, "wa_id", None)
    return None


def register_handlers(wa: WhatsApp) -> None:
    """Регистрирует обработчик сообщений на клиенте PyWa."""

    @wa.on_message(filters.text)
    def on_text(client: WhatsApp, msg: types.Message) -> None:
        chat_id = _get_chat_id_from_message(msg)
        if chat_id:
            logger.debug("Входящее сообщение chat_id=%s", chat_id)
        if config.WA_GROUP_ID and chat_id != config.WA_GROUP_ID:
            logger.debug("Сообщение не из целевой группы (ожидался %s), пропуск", config.WA_GROUP_ID)
            return
        text = (msg.text or "").strip()
        process_message_text(text, chat_id)
