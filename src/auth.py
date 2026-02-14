"""Авторизация: несколько пользователей, у каждого — только свои группы."""

import json
import os
from pathlib import Path
from typing import Any

import bcrypt

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.config import config
from src.logger import get_logger

log = get_logger("auth")
COOKIE_NAME = "wa_session"
MAX_AGE_DAYS = 30


def _users_path() -> Path:
    path = getattr(config, "AUTH_USERS_FILE", None) or os.environ.get("AUTH_USERS_FILE", "")
    if path:
        return Path(path)
    return Path(__file__).resolve().parent.parent / "data" / "users.json"


def _secret() -> str:
    secret = getattr(config, "APP_SECRET_KEY", None) or os.environ.get("APP_SECRET_KEY", "")
    if not secret and getattr(config, "AUTH_ENABLED", False):
        raise ValueError("AUTH_ENABLED=1 requires APP_SECRET_KEY in .env")
    return secret or "dev-secret-change-in-production"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt="wa-teachers-session")


def _load_users() -> dict[str, Any]:
    path = _users_path()
    if not path.exists():
        return {"users": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Не удалось загрузить users.json: %s", e)
        return {"users": {}}


def _save_users(data: dict[str, Any]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def verify_user(username: str, password: str) -> bool:
    """Проверка логина и пароля."""
    data = _load_users()
    users = data.get("users") or {}
    user = users.get(username)
    if not user or not isinstance(user, dict):
        return False
    phash = user.get("password_hash")
    if not phash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], phash.encode("utf-8"))
    except Exception:
        return False


def get_user_chat_ids(username: str) -> list[str] | None:
    """Список chat_id, доступных пользователю. None или [] = показывать все (выбор «мои группы»)."""
    data = _load_users()
    users = data.get("users") or {}
    user = users.get(username)
    if not user or not isinstance(user, dict):
        return None
    cids = user.get("chat_ids")
    if cids is None:
        return None
    return cids if isinstance(cids, list) else []


def set_user_chat_ids(username: str, chat_ids: list[str]) -> None:
    """Сохранить «мои группы» для пользователя."""
    data = _load_users()
    users = data.get("users") or {}
    if username not in users:
        raise HTTPException(404, "User not found")
    users[username]["chat_ids"] = list(chat_ids)
    data["users"] = users
    _save_users(data)


def create_session_cookie(username: str) -> str:
    """Подписать и вернуть значение cookie сессии."""
    return _serializer().dumps(username)


def read_session_cookie(value: str) -> str | None:
    """Прочитать username из cookie. None если невалидно."""
    try:
        return _serializer().loads(value, max_age=MAX_AGE_DAYS * 86400)
    except BadSignature:
        return None


def auth_enabled() -> bool:
    return getattr(config, "AUTH_ENABLED", False) or os.environ.get("AUTH_ENABLED", "").lower() in ("1", "true", "yes")


class CurrentUser:
    def __init__(self, username: str, chat_ids: list[str] | None):
        self.username = username
        self.chat_ids = chat_ids  # None или [] = видит все (режим выбора), иначе только эти

    def can_access_chat(self, chat_id: str) -> bool:
        if self.chat_ids is None or len(self.chat_ids) == 0:
            return True
        return chat_id in self.chat_ids

    def filter_chats(self, chats: list[dict]) -> list[dict]:
        if self.chat_ids is None or len(self.chat_ids) == 0:
            return chats
        allowed = set(self.chat_ids)
        return [c for c in chats if c.get("id") in allowed]


def get_current_user(request: Request) -> CurrentUser | None:
    """Текущий пользователь из cookie. Если auth выключен — возвращаем «гостя» с доступом ко всему."""
    if not auth_enabled():
        return CurrentUser("", None)
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    username = read_session_cookie(cookie)
    if not username:
        return None
    chat_ids = get_user_chat_ids(username)
    return CurrentUser(username, chat_ids)


def require_user(request: Request) -> CurrentUser:
    """Зависимость: вернуть текущего пользователя или 401."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(401, "Login required")
    return user


def login_response(username: str, response: Response) -> dict:
    """Установить cookie и вернуть JSON."""
    value = create_session_cookie(username)
    response.set_cookie(key=COOKIE_NAME, value=value, max_age=MAX_AGE_DAYS * 86400, httponly=True, samesite="lax")
    return {"ok": True, "username": username}


def logout_response(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")
