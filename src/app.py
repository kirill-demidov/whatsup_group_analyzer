"""FastAPI: webhook для моста, веб-приложение (подключение моста, чаты, анализ через Gemini)."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import fastapi
from fastapi import Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from pywa import WhatsApp

from src.auth import (
    CurrentUser,
    auth_enabled,
    get_current_user,
    get_user_chat_ids,
    login_google_response,
    login_response,
    logout_response,
    require_user,
    verify_user,
)
from src.google_auth import (
    exchange_code_for_userinfo,
    get_authorization_url,
    google_oauth_enabled,
)
from src.config import config
from src.logger import get_logger
from src.wa_handlers import register_handlers

logger = get_logger("app")
app = fastapi.FastAPI(title="WhatsApp Teachers → Google Sheets")

if config.WA_PHONE_ID and config.WA_TOKEN:
    logger.info("Инициализация WhatsApp webhook: phone_id=%s", config.WA_PHONE_ID)
    wa = WhatsApp(
        phone_id=config.WA_PHONE_ID,
        token=config.WA_TOKEN,
        server=app,
        callback_url=config.WA_CALLBACK_URL or "https://localhost",
        verify_token=config.WA_VERIFY_TOKEN or "teachers-verify-token",
        app_id=config.WA_APP_ID or None,
        app_secret=config.WA_APP_SECRET or None,
    )
    register_handlers(wa)
else:
    wa = None


def _bridge_fetch(path: str, method: str = "GET", data: dict | None = None, timeout: int = 15, username: str | None = None) -> tuple[int, dict]:
    """Запрос к мосту. Возвращает (status_code, json_body). timeout — секунды (для загрузки истории нужен большой, например 120)."""
    url = (config.BRIDGE_URL or "").rstrip("/") + path
    # Multi-tenant: добавляем ?user=<username>
    if username:
        sep = "&" if "?" in url else "?"
        url += f"{sep}user={quote(username, safe='')}"
    body = json.dumps(data).encode() if method == "POST" and data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            resp_body = json.loads(e.read().decode()) if e.fp else {}
        except Exception:
            resp_body = {"error": str(e)}
        return e.code, resp_body
    except Exception as e:
        logger.warning("Bridge fetch %s: %s", path, e)
        return 503, {"error": str(e)}


@app.get("/")
def root():
    return {"service": "whatsapp-teachers-to-sheets", "whatsapp_configured": wa is not None}


# Веб-приложение: раздача статики
_static_dir = Path(__file__).resolve().parent.parent / "static"


def _maybe_redirect_login(request: fastapi.Request):
    """Если включён auth и пользователь не залогинен — редирект на логин."""
    if not auth_enabled():
        return None
    user = get_current_user(request)
    if user is not None:
        return None
    return RedirectResponse(url="/app/login.html", status_code=302)


@app.get("/app")
@app.get("/app/")
def app_index(request: fastapi.Request):
    """Страница веб-приложения. При AUTH_ENABLED редирект на логин, если не залогинен."""
    redir = _maybe_redirect_login(request)
    if redir is not None:
        return redir
    index = _static_dir / "index.html"
    if not index.exists():
        raise fastapi.HTTPException(404, "Static files not found. Create static/index.html")
    return FileResponse(index)


@app.get("/app/{path:path}")
def app_static(path: str, request: fastapi.Request):
    """Статические файлы. login.html доступен без авторизации."""
    if auth_enabled() and path != "login.html" and not path.startswith("login"):
        user = get_current_user(request)
        if user is None:
            return RedirectResponse(url="/app/login.html", status_code=302)
    f = _static_dir / path
    if not f.is_file():
        return FileResponse(_static_dir / "index.html")  # SPA fallback
    return FileResponse(f)


# --- Авторизация: логин, логаут, текущий пользователь ---
class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/login")
async def api_login(response: Response, request: fastapi.Request):
    """Вход по логину и паролю. Устанавливает cookie сессии. Принимает JSON или form-data."""
    if not auth_enabled():
        return {"ok": True, "username": ""}
    body = {}
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json() or {}
        except Exception:
            body = {}
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            body = {"username": (form.get("username") or "").strip(), "password": form.get("password") or ""}
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        logger.warning("Login: empty username or password, body_keys=%s", list(body.keys()))
        raise fastapi.HTTPException(400, "Введите логин и пароль")
    if not verify_user(username, password):
        logger.warning("Login: invalid credentials for user %s", username)
        raise fastapi.HTTPException(401, "Неверный логин или пароль")
    # Multi-tenant: запустить сессию моста для этого пользователя (если ещё не запущена)
    try:
        _bridge_fetch("/api/session/start", method="POST", timeout=10, username=username)
    except Exception as e:
        logger.debug("Bridge session start on login: %s", e)
    return login_response(username, response)


@app.post("/api/logout")
def api_logout(response: Response):
    """Выход: удалить cookie сессии."""
    return logout_response(response)


@app.get("/api/auth/providers")
def api_auth_providers():
    """Available auth providers."""
    return {"google": google_oauth_enabled()}


@app.get("/api/me")
def api_me(request: fastapi.Request):
    """Текущий пользователь. 401 если не залогинен (при AUTH_ENABLED)."""
    if not auth_enabled():
        return {"username": "", "chat_ids": None}
    user = get_current_user(request)
    if user is None:
        raise fastapi.HTTPException(401, "Login required")
    return {"username": user.username, "chat_ids": get_user_chat_ids(user.username)}


# --- Google OAuth ---
_google_oauth_states: dict[str, bool] = {}  # state → True (простой CSRF protection)


@app.get("/api/auth/google")
def api_auth_google(request: fastapi.Request):
    """Redirect to Google OAuth consent screen."""
    if not google_oauth_enabled():
        raise fastapi.HTTPException(501, "Google OAuth not configured")
    url, state = get_authorization_url(request)
    _google_oauth_states[state] = True
    # Limit stored states to prevent memory leak
    if len(_google_oauth_states) > 100:
        keys = list(_google_oauth_states.keys())
        for k in keys[:50]:
            _google_oauth_states.pop(k, None)
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/auth/google/callback")
def api_auth_google_callback(request: fastapi.Request, code: str = "", state: str = "", error: str = ""):
    """Google OAuth callback: exchange code, create/login user, redirect to app."""
    if error:
        logger.warning("Google OAuth error: %s", error)
        return RedirectResponse(url="/app/login.html?error=google_denied", status_code=302)
    if not code or not state:
        raise fastapi.HTTPException(400, "Missing code or state")
    if state not in _google_oauth_states:
        raise fastapi.HTTPException(400, "Invalid state (CSRF)")
    _google_oauth_states.pop(state, None)

    try:
        userinfo = exchange_code_for_userinfo(code, request)
    except Exception as e:
        logger.exception("Google OAuth token exchange failed: %s", e)
        return RedirectResponse(url="/app/login.html?error=google_failed", status_code=302)

    email = userinfo.get("email")
    if not email:
        raise fastapi.HTTPException(400, "Google account has no email")
    name = userinfo.get("name") or email
    logger.info("Google OAuth login: %s (%s)", email, name)
    # Multi-tenant: запустить сессию моста для этого пользователя
    try:
        _bridge_fetch("/api/session/start", method="POST", timeout=10, username=email)
    except Exception as e:
        logger.debug("Bridge session start on Google login: %s", e)
    return login_google_response(email, name, None)


def _current_user(request: fastapi.Request) -> CurrentUser:
    """Зависимость: текущий пользователь или «гость» (все чаты), если auth выключен."""
    if not auth_enabled():
        return CurrentUser("", None)
    return require_user(request)


# API моста (прокси). При AUTH_ENABLED возвращаем только чаты пользователя.
@app.get("/api/bridge/status")
def bridge_status(request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user)):
    status, body = _bridge_fetch("/api/status", username=user.username)
    if status != 200:
        raise fastapi.HTTPException(status, body.get("error", "Bridge unavailable"))
    return body


@app.get("/api/bridge/qr-image")
def bridge_qr_image(request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user)):
    """QR как картинка с моста. 204 если QR нет."""
    qr_path = "/api/qr-image"
    if user.username:
        qr_path += f"?user={quote(user.username, safe='')}"
    url = (config.BRIDGE_URL or "").rstrip("/") + qr_path
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 204:
                return fastapi.Response(status_code=204)
            data = r.read()
            return fastapi.Response(content=data, media_type="image/png")
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return fastapi.Response(status_code=204)
        raise fastapi.HTTPException(e.code or 503, "Bridge error")
    except OSError as e:
        raise fastapi.HTTPException(503, str(e))


@app.get("/api/bridge/logs")
def bridge_logs(request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user), tail: int = 200):
    """Последние строки логов моста (для отдельной вкладки UI)."""
    status, body = _bridge_fetch(f"/api/logs?tail={min(max(1, tail), 500)}", timeout=5, username=user.username)
    if status != 200:
        return {"lines": [f"[Ошибка моста: {body.get('error', status)}]"]}
    return body


@app.get("/api/bridge/history-stats")
def bridge_history_stats(request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user)):
    """Статистика загрузки истории моста: чатов, сообщений по чатам. Для проверки, что история пришла."""
    status, body = _bridge_fetch("/api/history-stats", username=user.username)
    if status != 200:
        raise fastapi.HTTPException(status or 503, body.get("error", "Bridge unavailable"))
    return body


@app.get("/api/bridge/chats")
def bridge_chats(
    request: fastapi.Request,
    user: CurrentUser = fastapi.Depends(_current_user),
    type_filter: str | None = None,
    search: str | None = None,
):
    """Список чатов из моста. При AUTH_ENABLED — только чаты, доступные пользователю."""
    status, body = _bridge_fetch("/api/chats", username=user.username)
    if status != 200:
        raise fastapi.HTTPException(status, body.get("error", "Bridge unavailable"))
    chats = body.get("chats") or []
    chats = user.filter_chats(chats)
    if type_filter and type_filter in ("group", "direct", "channel"):
        chats = [c for c in chats if c.get("type") == type_filter]
    if search and search.strip():
        q = search.strip().lower()
        chats = [c for c in chats if q in (c.get("name") or "").lower()]
    chats.sort(key=lambda c: (-(c.get("lastActive") or 0), (c.get("name") or "").lower()))
    return {"chats": chats}


@app.get("/api/bridge/chat/{chat_id:path}/messages")
def bridge_chat_messages(
    chat_id: str,
    request: fastapi.Request,
    user: CurrentUser = fastapi.Depends(_current_user),
    limit: int = 500,
    sync: bool = False,
):
    if auth_enabled() and not user.can_access_chat(chat_id):
        raise fastapi.HTTPException(403, "Access denied to this chat")
    timeout = 240 if sync else (180 if limit > 1000 else 30)
    path = f"/api/chat/{quote(chat_id, safe='')}/messages?limit={limit}"
    if sync:
        path += "&sync=1"
    status, body = _bridge_fetch(path, timeout=timeout, username=user.username)
    if status != 200:
        raise fastapi.HTTPException(status, body.get("error", "Bridge unavailable"))
    return body


@app.post("/api/bridge/chat/{chat_id:path}/sync")
def bridge_chat_sync(
    chat_id: str,
    request: fastapi.Request,
    user: CurrentUser = fastapi.Depends(_current_user),
):
    if auth_enabled() and not user.can_access_chat(chat_id):
        raise fastapi.HTTPException(403, "Access denied to this chat")
    status, body = _bridge_fetch(
        f"/api/chat/{quote(chat_id, safe='')}/sync", method="POST", timeout=30, username=user.username
    )
    if status not in (200, 201):
        raise fastapi.HTTPException(status, body.get("error", "Bridge unavailable"))
    return body


@app.post("/api/bridge/logout")
def bridge_logout(request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user)):
    """Отключить текущий аккаунт WhatsApp в мосте; после этого мост покажет новый QR для другого аккаунта."""
    status, body = _bridge_fetch("/api/logout", method="POST", username=user.username)
    if status not in (200, 201):
        raise fastapi.HTTPException(status, body.get("error", "Bridge logout failed"))
    return body


class AnalyzePayload(BaseModel):
    chatIds: list[str] = []
    prompt: str = ""
    syncFirst: bool = False  # перед загрузкой запросить синхронизацию истории с телефоном
    messageLimit: int = 0  # 0 = авто (Gemini определит по промпту)
    lang: str = ""  # язык ответа: ru/en/he (пустой = язык промпта)


@app.post("/api/analyze")
def api_analyze(payload: AnalyzePayload, request: fastapi.Request, user: CurrentUser = fastapi.Depends(_current_user)):
    """Анализ выбранных чатов через Gemini. При AUTH_ENABLED — только чаты, доступные пользователю."""
    if not payload.prompt.strip():
        raise fastapi.HTTPException(400, "prompt is required")
    if not payload.chatIds:
        raise fastapi.HTTPException(400, "choose at least one chat")
    if auth_enabled():
        for cid in payload.chatIds:
            if not user.can_access_chat(cid):
                raise fastapi.HTTPException(403, f"Access denied to chat {cid[:20]}…")

    from datetime import datetime
    from src.gemini_client import analyze_with_prompt, estimate_message_limit
    from src.gcs_client import load_chat_messages

    # Определяем эффективный лимит сообщений
    if payload.messageLimit > 0:
        effective_limit = min(payload.messageLimit, 15000)
    else:
        effective_limit = estimate_message_limit(payload.prompt.strip())
    logger.info("Лимит сообщений: %s (запрошено: %s)", effective_limit, payload.messageLimit)
    # Запрашиваем у моста с запасом (x2), но не больше 15000
    fetch_limit = min(effective_limit * 2, 15000)

    parts = []
    all_timestamps: list[float] = []
    total_loaded = 0  # всего сообщений, попавших в контекст
    sync_suffix = "&sync=1" if payload.syncFirst else ""
    timeout = 240 if payload.syncFirst else 180
    for cid in payload.chatIds[:20]:  # не более 20 чатов
        messages = load_chat_messages(cid) if config.GCS_BUCKET else None
        if not messages:
            status, body = _bridge_fetch(
                f"/api/chat/{quote(cid, safe='')}/messages?limit={fetch_limit}{sync_suffix}",
                timeout=timeout,
                username=user.username,
            )
            if status != 200 or "messages" not in body:
                logger.warning("Не удалось загрузить чат %s: %s", cid[:30], body.get("error"))
                parts.append(f"[Чат {cid}]: не удалось загрузить сообщения\n")
                continue
            messages = body.get("messages", [])
        else:
            logger.info("Чат %s: загружено %s сообщений из GCS", cid[:24], len(messages))
        # Обрезаем до effective_limit последних сообщений
        if len(messages) > effective_limit:
            messages = messages[:effective_limit]
        parts.append(f"\n=== Чат id: {cid} ===\n")
        for m in reversed(messages):  # хронологический порядок
            ts = m.get("timestamp")
            if ts is not None and isinstance(ts, (int, float)) and float(ts) > 0:
                all_timestamps.append(float(ts))
            elif m.get("date"):
                try:
                    dt = datetime.fromisoformat(
                        str(m["date"]).replace("Z", "+00:00")[:19]
                    )
                    all_timestamps.append(dt.timestamp())
                except (ValueError, TypeError):
                    pass
            date = m.get("date") or m.get("timestamp") or ""
            from_name = m.get("from_name") or m.get("from_id") or m.get("from") or "?"
            body_text = (m.get("body") or "").strip()
            if body_text:
                parts.append(f"[{date}] {from_name}: {body_text}\n")
                total_loaded += 1

    context_text = "\n".join(parts)
    if not context_text.strip():
        raise fastapi.HTTPException(400, "No messages in selected chats")
    if total_loaded == 0:
        # Мост вернул чаты, но без ни одного сообщения с текстом (или пустой список)
        return {
            "result": "По выбранным чатам не загружено ни одного сообщения.\n\n"
            "Возможные причины: история по этому чату ещё не подгружена в мост (при Baileys дождитесь окончания History Sync после подключения или откройте чат на телефоне); либо мост отдал пустой список. Попробуйте включить «Перед загрузкой запросить синхронизацию с телефоном» и нажать «Анализировать» снова через 10–20 секунд.",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "in_free_tier": True},
        }

    total_messages = total_loaded
    first_date: str | None = None
    last_date: str | None = None
    if all_timestamps:
        first_date = datetime.fromtimestamp(min(all_timestamps)).strftime("%Y-%m-%d %H:%M")
        last_date = datetime.fromtimestamp(max(all_timestamps)).strftime("%Y-%m-%d %H:%M")

    try:
        result, usage = analyze_with_prompt(
            payload.prompt.strip(),
            context_text,
            total_messages=total_messages,
            first_date=first_date,
            last_date=last_date,
            lang=payload.lang,
        )
        return {"result": result, "usage": usage}
    except Exception as e:
        logger.exception("Analyze error: %s", e)
        raise fastapi.HTTPException(500, str(e))


class BridgePayload(BaseModel):
    text: str
    chat_id: str | None = None
    from_name: str | None = None


@app.post("/webhook/bridge")
def webhook_bridge(payload: BridgePayload):
    """Приём сообщений от моста (POST с текстом и chat_id)."""
    from src.wa_handlers import process_message_text

    text = (payload.text or "").strip()
    chat_id = payload.chat_id or None
    if not text:
        raise fastapi.HTTPException(400, "Missing or empty 'text'")
    result = process_message_text(text, chat_id, payload.from_name)
    return result
