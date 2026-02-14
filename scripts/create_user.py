#!/usr/bin/env python3
"""Создать или обновить пользователя в data/users.json (для AUTH_ENABLED).
Использование: uv run python scripts/create_user.py USERNAME PASSWORD
"""

import json
import sys
from pathlib import Path

# add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import _load_users, _save_users, hash_password

def main():
    if len(sys.argv) < 3:
        print("Usage: uv run python scripts/create_user.py USERNAME PASSWORD")
        sys.exit(1)
    username = sys.argv[1].strip()
    password = sys.argv[2]
    if not username:
        print("Username required")
        sys.exit(1)
    data = _load_users()
    users = data.get("users") or {}
    users[username] = {
        "password_hash": hash_password(password),
        "chat_ids": users.get(username, {}).get("chat_ids", [])
    }
    data["users"] = users
    _save_users(data)
    print(f"User {username!r} created/updated. chat_ids: {users[username].get('chat_ids', [])}")

if __name__ == "__main__":
    main()
