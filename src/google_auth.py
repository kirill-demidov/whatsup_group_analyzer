"""Google OAuth 2.0 flow: authorization URL, callback, token exchange, userinfo."""

import secrets
from urllib.parse import urlencode

import httpx

from src.config import config
from src.logger import get_logger

log = get_logger("google_auth")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
SCOPES = "openid email profile"


def google_oauth_enabled() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def build_redirect_uri(request) -> str:
    if config.GOOGLE_REDIRECT_URI:
        return config.GOOGLE_REDIRECT_URI
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{scheme}://{host}/api/auth/google/callback"


def get_authorization_url(request) -> tuple[str, str]:
    """Returns (authorization_url, state)."""
    state = secrets.token_urlsafe(32)
    redirect_uri = build_redirect_uri(request)
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return url, state


def exchange_code_for_userinfo(code: str, request) -> dict:
    """Exchange authorization code for tokens, then fetch userinfo. Returns dict with email, name, picture."""
    redirect_uri = build_redirect_uri(request)
    token_data = {
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=10) as client:
        token_resp = client.post(GOOGLE_TOKEN_URL, data=token_data)
        token_resp.raise_for_status()
        tokens = token_resp.json()

        access_token = tokens["access_token"]
        userinfo_resp = client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_resp.raise_for_status()
        return userinfo_resp.json()
