"""ntfy dedupe helper for terminal run statuses."""

from __future__ import annotations

from app.db import Database
from app.ntfy import notify_run_status
from app.settings import Settings, get_settings


async def notify_run_if_needed(
    db: Database,
    run_id: str,
    status: str,
    *,
    settings: Settings | None = None,
) -> None:
    if status not in ("finished", "failed", "stale"):
        return
    settings = settings or get_settings()
    if not settings.ntfy_enabled:
        return
    if await db.was_notified(run_id, status):
        return
    run = await db.get_run(run_id)
    if not run:
        return
    ok = await notify_run_status(
        settings,
        run_id=run_id,
        thread_id=run["thread_id"],
        status=status,
        last_event_at=run.get("last_event_at"),
        error=run.get("error"),
        duration_sec=run.get("duration_sec"),
    )
    await db.notification_sent(run_id, status, ok)
