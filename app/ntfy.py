from __future__ import annotations

import logging

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)


async def send_ntfy(settings: Settings, *, title: str, message: str, tags: str = "") -> bool:
    if not settings.ntfy_enabled:
        return False
    base = settings.ntfy_server.rstrip("/")
    topic = settings.ntfy_topic.strip()
    url = f"{base}/{topic}"
    headers = {"Title": title[:200]}
    if tags:
        headers["Tags"] = tags
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, content=message[:4000], headers=headers)
            return r.status_code < 300
    except httpx.HTTPError as e:
        logger.warning("ntfy failed: %s", type(e).__name__)
        return False


async def notify_run_status(
    settings: Settings,
    *,
    run_id: str,
    thread_id: str,
    status: str,
    error: str | None = None,
    duration_sec: int | None = None,
    last_event_at: str | None = None,
) -> bool:
    if status == "finished":
        body = (
            f"DeerFlow run finished\nrun_id: {run_id}\nthread_id: {thread_id}\n"
            f"duration: {duration_sec or 'unknown'}s"
        )
        return await send_ntfy(settings, title="DeerFlow finished", message=body, tags="white_check_mark")
    if status == "failed":
        body = (
            f"DeerFlow run failed\nrun_id: {run_id}\nthread_id: {thread_id}\n"
            f"error: {error or 'unknown'}"
        )
        return await send_ntfy(settings, title="DeerFlow failed", message=body, tags="x")
    if status == "stale":
        body = (
            f"DeerFlow run stale\nrun_id: {run_id}\nthread_id: {thread_id}\n"
            f"last_event_at: {last_event_at or 'unknown'}"
        )
        return await send_ntfy(settings, title="DeerFlow stale", message=body, tags="warning")
    return False
