from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.deerflow_paths import resolve_user_id
from app.settings import Settings

logger = logging.getLogger(__name__)

SESSION_VERSION = 1


def session_file_path(settings: Settings) -> Path:
    return Path(settings.tracker_db_path).resolve().parent / "deerflow_session.json"


def _read_session_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read DeerFlow session file: %s", e)
        return None


def load_stored_auth(settings: Settings) -> tuple[str, str]:
    data = _read_session_file(session_file_path(settings))
    if not data:
        return "", ""
    cookie = str(data.get("cookie") or "").strip()
    bearer = str(data.get("bearer") or "").strip()
    return cookie, bearer


def save_stored_auth(
    settings: Settings,
    *,
    cookie: str | None = None,
    bearer: str | None = None,
    source: str,
) -> None:
    path = session_file_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_session_file(path) or {}
    payload = {
        "version": SESSION_VERSION,
        "cookie": cookie.strip() if cookie is not None else str(existing.get("cookie") or ""),
        "bearer": bearer.strip() if bearer is not None else str(existing.get("bearer") or ""),
        "source": source,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    from app.settings import get_settings

    get_settings.cache_clear()


def save_session_cookie(settings: Settings, cookie: str, *, source: str) -> None:
    save_stored_auth(settings, cookie=cookie, bearer="", source=source)


def clear_stored_session(settings: Settings) -> None:
    path = session_file_path(settings)
    if path.is_file():
        path.unlink()
    from app.settings import get_settings

    get_settings.cache_clear()


def effective_auth(settings: Settings) -> tuple[str, str]:
    """Return (cookie, bearer). Env vars override stored session file."""
    if settings.deerflow_bearer_token.strip():
        return "", settings.deerflow_bearer_token.strip()
    if settings.deerflow_auth_cookie.strip():
        return settings.deerflow_auth_cookie.strip(), ""
    return load_stored_auth(settings)


def cookie_header_from_jar(jar: httpx.Cookies) -> str:
    parts = [f"{name}={value}" for name, value in jar.items()]
    return "; ".join(parts)


async def login_with_password(
    settings: Settings,
    *,
    email: str,
    password: str,
) -> str:
    """Login via DeerFlow /api/v1/auth/login/local; return Cookie header value."""
    base = settings.deerflow_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.post(
            f"{base}/api/v1/auth/login/local",
            data={"username": email.strip(), "password": password},
        )
        if resp.status_code in (401, 403):
            raise PermissionError("Incorrect email or password")
        if resp.status_code >= 400:
            raise RuntimeError(f"DeerFlow login failed ({resp.status_code})")

        cookie = cookie_header_from_jar(client.cookies)
        if "access_token=" not in cookie:
            raise RuntimeError("DeerFlow login did not return access_token cookie")

        # Ensure csrf_token cookie exists (set on auth POST responses).
        if "csrf_token=" not in cookie:
            probe = await client.get(f"{base}/api/v1/auth/me")
            if probe.status_code in (401, 403):
                raise PermissionError("Session invalid after login")
            cookie = cookie_header_from_jar(client.cookies)

        return cookie


async def verify_deerflow_auth(settings: Settings) -> dict[str, Any]:
    """Probe DeerFlow API; never log secrets."""
    cookie, bearer = effective_auth(settings)
    if not cookie and not bearer:
        return {
            "ok": False,
            "source": "none",
            "message": "Not connected — log in below or set DEERFLOW_AUTH_COOKIE in .env",
        }

    headers = settings.deerflow_headers_for(cookie, bearer)
    base = settings.deerflow_base_url.rstrip("/")
    if settings.deerflow_auth_cookie.strip() or settings.deerflow_bearer_token.strip():
        source = "env"
    elif bearer and not cookie:
        source = "file_bearer"
    else:
        source = "file"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base}/api/threads/search",
            headers=headers,
            json={"limit": 1},
        )

    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "source": source,
            "message": "DeerFlow rejected credentials (expired or invalid)",
        }
    if resp.status_code >= 400:
        return {
            "ok": False,
            "source": source,
            "message": f"DeerFlow returned HTTP {resp.status_code}",
        }

    user_id = resolve_user_id(
        env_user_id=settings.deerflow_user_id,
        auth_cookie=cookie,
        bearer_token=bearer,
    )
    return {
        "ok": True,
        "source": source,
        "message": "Connected to DeerFlow",
        "user_id": user_id,
    }


async def try_env_bootstrap_login(settings: Settings) -> bool:
    """Optional DEERFLOW_LOGIN_EMAIL + DEERFLOW_LOGIN_PASSWORD on startup."""
    email = settings.deerflow_login_email
    password = settings.deerflow_login_password
    if not email.strip() or not password:
        return False
    if effective_auth(settings)[0] or effective_auth(settings)[1]:
        return False
    try:
        cookie = await login_with_password(settings, email=email, password=password)
        save_session_cookie(settings, cookie, source="env_login")
        logger.info("DeerFlow session bootstrapped from env login")
        return True
    except Exception as e:
        logger.warning("DeerFlow env bootstrap login failed: %s", e)
        return False
