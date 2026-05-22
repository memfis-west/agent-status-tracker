from __future__ import annotations

import logging

from app.db import Database
from app.settings import Settings
from watcher.deerflow_client import DeerflowClient
from watcher.poll_client import PollClient

logger = logging.getLogger(__name__)


async def sync_deerflow_threads(
    db: Database,
    client: DeerflowClient,
    settings: Settings | None = None,
) -> dict[str, int | list[str]]:
    """
    1) Import runs from DeerFlow threads (UI-started chats) via poll.
    2) Prune tracker rows whose thread was deleted in DeerFlow (404).
    """
    if settings is None:
        from app.settings import get_settings

        settings = get_settings()
    limit = settings.deerflow_thread_search_limit
    poll = PollClient(settings, db)

    removed_threads: list[str] = []
    removed_runs = 0
    pending_removed = await db.delete_pending_runs()
    polled_threads: list[str] = []
    imported_runs = 0

    try:
        discovered = await client.search_threads(limit=limit)
    except PermissionError:
        raise
    except Exception as e:
        logger.warning("thread search failed: %s", e)
        discovered = []

    search_ids: set[str] = set()
    for t in discovered:
        tid = str(t.get("thread_id") or t.get("id") or "")
        if tid:
            search_ids.add(tid)

    # Poll threads visible in DeerFlow (includes active UI chats)
    for tid in search_ids:
        try:
            run_ids = await poll.poll_thread(tid)
            if run_ids:
                polled_threads.append(tid)
                imported_runs += len(run_ids)
        except Exception as e:
            logger.warning("poll thread_id=%s failed: %s", tid, e)

    # Prune DB threads that no longer exist in DeerFlow
    for thread_id in await db.list_thread_ids():
        if thread_id in search_ids:
            continue
        if not await client.thread_exists(thread_id):
            n = await db.delete_runs_for_thread(thread_id)
            if n:
                removed_threads.append(thread_id)
                removed_runs += n
            logger.info("pruned thread_id=%s runs=%s (gone in DeerFlow)", thread_id, n)

    spam_removed = await db.prune_poll_spam_events()

    return {
        "removed_runs": removed_runs,
        "removed_threads": removed_threads,
        "pending_removed": pending_removed,
        "polled_threads": polled_threads,
        "imported_runs": imported_runs,
        "discovered_threads": len(search_ids),
        "spam_events_removed": spam_removed,
    }
