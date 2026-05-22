from __future__ import annotations

import base64
import json
import re
from pathlib import Path


def agent_name_to_folder(agent_name: str | None) -> str | None:
    """DeerFlow uses ``agent_name.lower()`` for per-user agent directories."""
    if not agent_name or not isinstance(agent_name, str):
        return None
    name = agent_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", name):
        return None
    return name.lower()


def user_id_from_access_token(token: str) -> str | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        sub = data.get("sub")
        return str(sub) if sub else None
    except (json.JSONDecodeError, ValueError):
        return None


def resolve_user_id(*, env_user_id: str, auth_cookie: str, bearer_token: str) -> str | None:
    if env_user_id.strip():
        return env_user_id.strip()
    if bearer_token.strip():
        uid = user_id_from_access_token(bearer_token.strip())
        if uid:
            return uid
    cookie = auth_cookie.strip()
    if cookie:
        m = re.search(r"access_token=([^;]+)", cookie)
        if m:
            return user_id_from_access_token(m.group(1))
    return None


def build_host_paths(
    *,
    data_base: str,
    user_id: str,
    thread_id: str,
    agent_folder: str | None,
) -> dict[str, str | None]:
    base = Path(data_base.rstrip("/"))
    user_root = base / "users" / user_id
    thread_dir = user_root / "threads" / thread_id
    paths: dict[str, str | None] = {
        "thread_dir": str(thread_dir),
        "user_data": str(thread_dir / "user-data"),
        "soul_md": None,
        "agent_dir": None,
    }
    if agent_folder:
        agent_dir = user_root / "agents" / agent_folder
        paths["agent_dir"] = str(agent_dir)
        paths["soul_md"] = str(agent_dir / "SOUL.md")
    return paths
