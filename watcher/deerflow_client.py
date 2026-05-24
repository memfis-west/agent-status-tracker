from __future__ import annotations

import logging
from typing import Any

import httpx

from app.sanitize import extract_thread_value_counts, sum_tokens_from_messages
from app.settings import Settings

logger = logging.getLogger(__name__)


class DeerflowClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.deerflow_base_url.rstrip("/")
        self._state_cache: dict[str, dict[str, Any] | None] | None = None

    def begin_state_cache(self) -> None:
        self._state_cache = {}

    def end_state_cache(self) -> None:
        self._state_cache = None

    def _headers(self) -> dict[str, str]:
        return self.settings.deerflow_headers()

    async def _request(
        self,
        method: str,
        paths: list[str],
        *,
        json_body: dict | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        last_resp: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for path in paths:
                url = f"{self.base}{path}"
                try:
                    resp = await client.request(method, url, headers=self._headers(), json=json_body)
                except httpx.HTTPError as e:
                    logger.warning("DeerFlow request failed path=%s error=%s", path, type(e).__name__)
                    continue
                logger.info("DeerFlow %s %s -> %s", method, path, resp.status_code)
                if resp.status_code not in (404, 405):
                    return resp
                last_resp = resp
        if last_resp is not None:
            return last_resp
        raise httpx.HTTPError("All DeerFlow endpoint candidates failed")

    async def create_thread(self) -> str:
        resp = await self._request(
            "POST",
            ["/api/langgraph/threads", "/api/threads"],
            json_body={},
        )
        if resp.status_code in (401, 403):
            raise PermissionError("DeerFlow authentication required (401/403)")
        resp.raise_for_status()
        data = resp.json()
        tid = data.get("thread_id") or data.get("id")
        if not tid:
            raise ValueError("No thread_id in create thread response")
        return str(tid)

    async def list_runs(self, thread_id: str) -> list[dict[str, Any]]:
        resp = await self._request(
            "GET",
            [
                f"/api/langgraph/threads/{thread_id}/runs",
                f"/api/threads/{thread_id}/runs",
            ],
        )
        if resp.status_code in (401, 403):
            raise PermissionError("DeerFlow authentication required")
        if resp.status_code in (404, 405):
            return []
        if resp.status_code >= 400:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "runs" in data:
            return data["runs"]
        return []

    async def get_run(self, thread_id: str, run_id: str) -> dict[str, Any] | None:
        resp = await self._request(
            "GET",
            [
                f"/api/langgraph/threads/{thread_id}/runs/{run_id}",
                f"/api/threads/{thread_id}/runs/{run_id}",
            ],
        )
        if resp.status_code in (404, 405):
            return None
        if resp.status_code >= 400:
            return None
        return resp.json()

    async def get_state_refs(self, thread_id: str) -> tuple[str | None, str | None]:
        from app.sanitize import extract_artifact_refs

        data = await self._get_thread_state(thread_id)
        if not data:
            return None, None
        return extract_artifact_refs(data)

    async def search_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        resp = await self._request(
            "POST",
            ["/api/threads/search", "/api/langgraph/threads/search"],
            json_body={"limit": limit},
        )
        if resp.status_code in (401, 403):
            raise PermissionError("DeerFlow authentication required")
        if resp.status_code >= 400:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "threads" in data:
            return data["threads"]
        return []

    async def get_thread_state(self, thread_id: str) -> dict[str, Any] | None:
        return await self._get_thread_state(thread_id)

    async def _get_thread_state(self, thread_id: str) -> dict[str, Any] | None:
        if self._state_cache is not None and thread_id in self._state_cache:
            return self._state_cache[thread_id]
        resp = await self._request(
            "GET",
            [
                f"/api/langgraph/threads/{thread_id}/state",
                f"/api/threads/{thread_id}/state",
            ],
        )
        if resp.status_code >= 400:
            data = None
        else:
            raw = resp.json()
            data = raw if isinstance(raw, dict) else None
        if self._state_cache is not None:
            self._state_cache[thread_id] = data
        return data

    async def get_thread_progress(self, thread_id: str) -> dict[str, Any]:
        """Safe thread-level signals for timeline (no prompts)."""
        data = await self._get_thread_state(thread_id)
        if not data:
            return {}
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        title = None
        values = data.get("values")
        counts = extract_thread_value_counts(values)
        if isinstance(values, dict) and values.get("title"):
            title = str(values["title"])[:120]
        return {
            "agent_name": meta.get("agent_name"),
            "model_name": meta.get("model_name"),
            "step": meta.get("step"),
            "mode": meta.get("mode"),
            "thread_title": title,
            **counts,
        }

    async def thread_exists(self, thread_id: str) -> bool:
        return (await self._get_thread_state(thread_id)) is not None

    async def get_state_token_usage(self, thread_id: str) -> dict[str, int]:
        """Sum usage_metadata from thread state messages (live, same as active dashboard)."""
        state = await self._get_thread_state(thread_id)
        if not state:
            return {"total_tokens": 0, "total_input_tokens": 0, "total_output_tokens": 0}
        messages = (state.get("values") or {}).get("messages")
        return sum_tokens_from_messages(messages)

    async def get_thread_token_usage(
        self,
        thread_id: str,
        *,
        prefer_state: bool = False,
    ) -> dict[str, int | str]:
        """Thread token totals: API aggregate + state message sum (live)."""
        resp = await self._request(
            "GET",
            [
                f"/api/langgraph/threads/{thread_id}/token-usage",
                f"/api/threads/{thread_id}/token-usage",
            ],
        )
        if resp.status_code in (401, 403):
            raise PermissionError("DeerFlow authentication required")
        api_total = 0
        api_in = api_out = 0
        if resp.status_code < 400:
            data = resp.json()
            if isinstance(data, dict):
                api_total = int(data.get("total_tokens") or 0)
                api_in = int(data.get("total_input_tokens") or 0)
                api_out = int(data.get("total_output_tokens") or 0)

        state_usage = await self.get_state_token_usage(thread_id)
        state_total = int(state_usage.get("total_tokens") or 0)
        state_in = int(state_usage.get("total_input_tokens") or 0)
        state_out = int(state_usage.get("total_output_tokens") or 0)

        base: dict[str, int | str] = {
            "api_tokens": api_total,
            "state_tokens": state_total,
        }

        if prefer_state and state_total > 0:
            return {
                **base,
                "total_tokens": state_total,
                "total_input_tokens": state_in,
                "total_output_tokens": state_out,
                "source": "state",
            }
        if state_total > 0 and api_total == 0:
            return {
                **base,
                "total_tokens": state_total,
                "total_input_tokens": state_in,
                "total_output_tokens": state_out,
                "source": "state",
            }
        if api_total > 0:
            return {
                **base,
                "total_tokens": api_total,
                "total_input_tokens": api_in,
                "total_output_tokens": api_out,
                "source": "api",
            }
        if state_total > 0:
            return {
                **base,
                "total_tokens": state_total,
                "total_input_tokens": state_in,
                "total_output_tokens": state_out,
                "source": "state",
            }

        return {
            **base,
            "total_tokens": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "source": "none",
        }

    def stream_paths(self, thread_id: str) -> list[str]:
        return [
            f"/api/langgraph/threads/{thread_id}/runs/stream",
            f"/api/threads/{thread_id}/runs/stream",
        ]
