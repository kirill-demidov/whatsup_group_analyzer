import json
import re
from typing import Any

import google.generativeai as genai

from src.config import config
from src.logger import get_logger

logger = get_logger("gemini")

if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)


def _normalize_phone(s: str) -> str:
    """Нормализация израильских телефонов: 054-xxx → +972-54-xxx."""
    s = (s or "").strip()
    if not s:
        return "—"
    digits = re.sub(r"\D", "", s)
    if not digits:
        return s
    if digits.startswith("972") and len(digits) >= 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+972" + digits[1:]
    if len(digits) >= 9:
        return digits
    return s


def _normalize_email(s: str) -> str:
    """Нормализация email: lowercase, trim."""
    s = (s or "").strip().lower()
    if "@" in s and "." in s:
        return s
    return s or "—"


def _normalize_contact(value: str) -> str:
    """Телефон или email: нормализуем по типу."""
    value = (value or "").strip()
    if not value:
        return "—"
    if "@" in value:
        return _normalize_email(value)
    return _normalize_phone(value)


def extract_teacher_phones(message_text: str) -> list[dict[str, Any]]:
    """Извлекает из текста сообщения упоминания учителей и их телефонов."""
    if not config.GEMINI_API_KEY:
        logger.error("Вызов extract_teacher_phones без GEMINI_API_KEY")
        raise ValueError("GEMINI_API_KEY не задан")

    text = (message_text or "")[:4000]
    logger.info("Запрос к Gemini: длина текста=%s символов", len(text))
    logger.debug("Текст (первые 200 символов): %s", text[:200].replace("\n", " "))

    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = """You are a parser for a school parents' WhatsApp group chat (in Hebrew).
Parents share teachers'/staff phone numbers. The format can be EITHER "name + phone" OR "phone + name" (e.g. "אלון 054-6427786" or "054-6427786 אלון" or "נדב 050-6555025").

Extract ALL teacher/staff contacts: any line or phrase that contains a Hebrew name and an Israeli mobile number (05X-XXXXXXX or 054-xxx, 050-xxx, +972-xx-xxx). Count both "Name 054-..." and "054-... Name" as valid.

Teacher names are in Hebrew. If the message does NOT contain any name+phone (in either order) — return an empty array [].

Reply with ONLY a valid JSON array of objects, no markdown, no explanation. Format:
{"teacherName": "שם המורה", "phone": "phone as written", "role": "תפקיד - e.g. מורה למוזיקה, מורה לעברית, חונך. Use Hebrew; if unknown use empty string."}

Message text:
---
"""
    prompt += text
    prompt += "\n---"

    try:
        response = model.generate_content(prompt)
        raw = (response.text or "").strip()
        logger.debug("Ответ Gemini: длина=%s, превью=%s", len(raw), raw[:150].replace("\n", " "))
        cleaned = re.sub(r"```json?\s*|\s*```", "", raw).strip()
        parsed = json.loads(cleaned)
        items = parsed if isinstance(parsed, list) else [parsed]
        result = []
        for x in items:
            if not x:
                continue
            name = (x.get("teacherName") or "").strip() or "—"
            phone_raw = (x.get("phone") or "").strip()
            role = (x.get("role") or "").strip() or "—"
            if name or phone_raw:
                result.append({
                    "teacherName": name,
                    "phone": _normalize_contact(phone_raw),
                    "role": role,
                    "source": (message_text or "")[:200],
                })
        logger.info("Gemini извлёк контактов: %s — %s", len(result), [r["teacherName"] for r in result])
        return result
    except json.JSONDecodeError as e:
        logger.warning("Gemini вернул невалидный JSON: %s", e)
        return []
    except Exception as e:
        logger.warning("Ошибка разбора ответа Gemini: %s", e, exc_info=True)
        return []


def classify_phone_with_context(
    context_messages: list[dict[str, Any]],
    phone_raw: str,
    posted_by: str,
    message_date: str,
) -> dict[str, Any] | None:
    """
    По контексту (5–10 сообщений) определяет, относится ли номер к учителю/сотруднику.
    context_messages: список {"from_name", "body", "date"} в порядке хронологии.
    Возвращает None если не учитель, иначе {"teacherName", "phone", "role", "postedBy", "messageDate", "source"}.
    """
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не задан")

    lines = []
    for m in context_messages:
        name = (m.get("from_name") or "—").strip()
        body = (m.get("body") or "").strip()
        date = m.get("date") or m.get("timestamp") or ""
        if isinstance(date, (int, float)):
            from datetime import datetime
            date = datetime.fromtimestamp(date).strftime("%Y-%m-%d %H:%M") if date else ""
        lines.append(f"[{date}] {name}: {body}")
    context_text = "\n".join(lines)[:6000]

    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""Chat in a school parents' WhatsApp group (Hebrew). One message contains the phone number: {phone_raw}
The person who posted this number is "{posted_by}", message date: {message_date}.

Based on the conversation context (messages before and after), is this phone number a TEACHER or SCHOOL STAFF contact (e.g. music teacher, homeroom teacher, counselor)?
- If the conversation is about sharing a teacher's number, or someone asked "who is responsible for X" and then this number was given — answer yes.
- If it's clearly a parent's number, a business, or unrelated — answer no.

Reply with ONLY a valid JSON object, no markdown:
{{"isTeacher": true or false, "teacherName": "שם המורה in Hebrew or empty if not teacher", "role": "תפקיד e.g. מורה למוזיקה, or empty"}}

Conversation:
---
{context_text}
---
"""
    try:
        response = model.generate_content(prompt)
        raw = (response.text or "").strip()
        cleaned = re.sub(r"```json?\s*|\s*```", "", raw).strip()
        data = json.loads(cleaned)
        if not data.get("isTeacher"):
            return None
        name = (data.get("teacherName") or "").strip() or "—"
        role = (data.get("role") or "").strip() or "—"
        return {
            "teacherName": name,
            "phone": _normalize_contact(phone_raw),
            "role": role,
            "postedBy": posted_by,
            "messageDate": message_date,
            "source": (context_messages[0].get("body") or "")[:200] if context_messages else "",
        }
    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning("classify_phone_with_context: %s", e)
        return None


# Gemini 2.0 Flash: бесплатный тир без лимита по токенам (лимит по RPM). Платный: $0.10/1M input, $0.40/1M output.
GEMINI_2_FLASH_INPUT_PER_1M = 0.10
GEMINI_2_FLASH_OUTPUT_PER_1M = 0.40


def _usage_from_response(response: Any) -> dict[str, Any]:
    """Из ответа Gemini извлекает usage и считает стоимость (платный тариф). Возвращает только примитивы для JSON."""
    u = getattr(response, "usage_metadata", None)
    if not u:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "in_free_tier": True}
    in_tok = int(getattr(u, "prompt_token_count", 0) or 0)
    out_tok = int(getattr(u, "candidates_token_count", 0) or 0)
    total = int(getattr(u, "total_token_count", 0) or (in_tok + out_tok))
    cost = (in_tok / 1_000_000 * GEMINI_2_FLASH_INPUT_PER_1M) + (out_tok / 1_000_000 * GEMINI_2_FLASH_OUTPUT_PER_1M)
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total,
        "cost_usd": round(cost, 6),
        "in_free_tier": True,
    }


def estimate_message_limit(user_prompt: str) -> int:
    """По промпту определяет сколько сообщений нужно. ~50 input-токенов к Gemini Flash."""
    if not config.GEMINI_API_KEY:
        return 200
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = (
        f'How many recent chat messages are needed to answer: "{user_prompt}"\n'
        "Reply with ONLY a number. Guidelines: last message→5, today→100, "
        "summary→200, search→500, full/all→15000\nNumber:"
    )
    try:
        response = model.generate_content(prompt)
        n = int(re.sub(r"[^\d]", "", response.text.strip()) or "200")
        return max(5, min(n, 15000))
    except Exception as e:
        logger.warning("estimate_message_limit fallback: %s", e)
        return 200


# Максимум символов контекста для анализа (Gemini 2.0 Flash — до 1M токенов, ~200k символов ок)
ANALYZE_CONTEXT_MAX_CHARS = 200_000


_LANG_NAMES = {"ru": "Russian", "en": "English", "he": "Hebrew"}


def analyze_with_prompt(
    user_prompt: str,
    context_text: str,
    *,
    total_messages: int | None = None,
    first_date: str | None = None,
    last_date: str | None = None,
    lang: str = "",
) -> tuple[str, dict[str, Any]]:
    """
    Анализ произвольного текста по промпту пользователя (для веб-приложения).
    context_text — история чатов (хронологически: старые → новые). В промпт берём последние
    ANALYZE_CONTEXT_MAX_CHARS символов. Если переданы total_messages/first_date/last_date —
    они добавляются в промпт отдельно (не обрезаются) для ответов про количество и даты.
    """
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не задан")
    raw = (context_text or "").strip()
    # Последние N символов = самые новые сообщения (включая сегодня)
    text = raw[-ANALYZE_CONTEXT_MAX_CHARS:] if len(raw) > ANALYZE_CONTEXT_MAX_CHARS else raw
    model = genai.GenerativeModel("gemini-2.0-flash")
    meta_parts = []
    if total_messages is not None:
        meta_parts.append(f"В загруженной истории выбранных чатов сообщений: {total_messages}. На вопрос «сколько сообщений в группе» отвечай именно этим числом.")
    if first_date and last_date:
        meta_parts.append(f"Первая дата сообщения: {first_date}. Последняя дата: {last_date}.")
    meta_block = "\n".join(meta_parts)
    meta_instruction = (
        f"ВАЖНО — данные по загрузке (учитывай в ответах):\n{meta_block}\n\n"
        if meta_block
        else ""
    )
    prompt = f"""Ниже — фрагмент истории чата (хронологически до конца).
{meta_instruction}{user_prompt}

Текст для анализа (чаты/сообщения):
---
{text}
---
Ответ (на {_LANG_NAMES.get(lang, '') or 'том же языке, что и промпт'}):"""
    try:
        response = model.generate_content(prompt)
        result_text = (response.text or "").strip()
        usage = _usage_from_response(response)
        return result_text, usage
    except Exception as e:
        logger.warning("analyze_with_prompt: %s", e)
        raise
