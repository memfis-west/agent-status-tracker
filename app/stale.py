from __future__ import annotations

import asyncio
import logging

from app.db import Database
from app.notifications import notify_run_if_needed
from app.settings import Settings
from app.sync import sync_deerflow_threads
from watcher.deerflow_client import DeerflowClient

logger = logging.getLogger(__name__)


async def stale_loop(settings: Settings, db: Database, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            try:
                await sync_deerflow_threads(db, DeerflowClient(settings), settings)
            except PermissionError:
                pass
            stale_ids = await db.mark_stale_runs(settings.stale_after_seconds)
            for run_id in stale_ids:
                await notify_run_if_needed(db, run_id, "stale", settings=settings)
        except Exception as e:
            logger.exception("stale loop error: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.stale_check_interval_seconds)
        except asyncio.TimeoutError:
            pass
