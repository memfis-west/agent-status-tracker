from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tracker_host: str = "0.0.0.0"
    tracker_port: int = 8090
    tracker_db_path: str = "/data/status.db"

    deerflow_base_url: str = "http://127.0.0.1:2026"
    deerflow_data_base: str = "/home/memfis/apps/deer-flow/backend/.deer-flow"
    deerflow_user_id: str = ""
    deerflow_auth_cookie: str = ""
    deerflow_bearer_token: str = ""
    # Optional: auto-login on startup if no cookie in .env or session file
    deerflow_login_email: str = ""
    deerflow_login_password: str = ""

    stale_after_seconds: int = 600
    stale_check_interval_seconds: int = 60
    deerflow_thread_search_limit: int = 50

    tracker_stream_modes: str = "updates,custom,messages-tuple"

    store_payloads: bool = False
    max_text_field_chars: int = 500
    max_payload_chars: int = 4000

    ntfy_server: str = ""
    ntfy_topic: str = ""

    basic_auth_user: str = ""
    basic_auth_password: str = ""

    @property
    def schema_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "TRACKER_SCHEMA.sql"

    @property
    def stream_modes_list(self) -> list[str]:
        modes = [m.strip() for m in self.tracker_stream_modes.split(",") if m.strip()]
        return [m for m in modes if m != "tools"]

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_server.strip() and self.ntfy_topic.strip())

    @property
    def basic_auth_enabled(self) -> bool:
        return bool(self.basic_auth_user.strip() and self.basic_auth_password.strip())

    def _csrf_from_cookie(self, cookie: str) -> str | None:
        if not cookie:
            return None
        m = re.search(r"csrf_token=([^;]+)", cookie)
        return m.group(1).strip() if m else None

    def deerflow_headers_for(
        self,
        cookie: str,
        bearer: str,
        *,
        include_csrf: bool = True,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        elif cookie:
            headers["Cookie"] = cookie
            if include_csrf:
                csrf = self._csrf_from_cookie(cookie)
                if csrf:
                    headers["X-CSRF-Token"] = csrf
        return headers

    def deerflow_headers(self, *, include_csrf: bool = True) -> dict[str, str]:
        from app.deerflow_credentials import effective_auth

        cookie, bearer = effective_auth(self)
        return self.deerflow_headers_for(cookie, bearer, include_csrf=include_csrf)


@lru_cache
def get_settings() -> Settings:
    return Settings()
