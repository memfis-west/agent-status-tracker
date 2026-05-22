from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.db import Database
from app.settings import Settings
from watcher.deerflow_client import DeerflowClient
from watcher.event_mapper import map_sse_event, parse_sse_data
from watcher.poll_client import PollClient

logger = logging.getLogger(__name__)


@dataclass
class SSEMessage:
    event: str | None = None
    data: str = ""
    id: str | None = None
    is_heartbeat: bool = False


def parse_sse_chunk(buffer: str) -> list[SSEMessage]:
    messages: list[SSEMessage] = []
    current_event: str | None = None
    current_id: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal current_event, current_id, data_lines
        if current_event is not None or data_lines or current_id:
            messages.append(
                SSEMessage(
                    event=current_event,
                    data="\n".join(data_lines),
                    id=current_id,
                )
            )
        current_event = None
        current_id = None
        data_lines = []

    for line in buffer.split("\n"):
        if line.startswith(":"):
            comment = line[1:].strip().lower()
            if "heartbeat" in comment:
                messages.append(SSEMessage(is_heartbeat=True))
            continue
        if line == "":
            flush()
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("id:"):
            current_id = line[3:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip() if line.startswith("data: ") else line[5:])

    if current_event or data_lines or current_id:
        flush()
    return messages


class SSEWatcher:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.client = DeerflowClient(settings)
        self.poll = PollClient(settings, db)

    async def watch_stream(
        self,
        thread_id: str,
        body: dict[str, Any],
        *,
        provisional_run_id: str | None = None,
    ) -> tuple[str | None, str]:
        paths = self.client.stream_paths(thread_id)
        headers = self.settings.deerflow_headers()
        headers["Accept"] = "text/event-stream"
        run_id: str | None = None

        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as http:
            resp: httpx.Response | None = None
            used_path = ""
            for path in paths:
                url = f"{self.client.base}{path}"
                try:
                    r = await http.send(
                        http.build_request("POST", url, headers=headers, json=body),
                        stream=True,
                    )
                except httpx.HTTPError as e:
                    logger.warning("SSE connect failed path=%s err=%s", path, type(e).__name__)
                    continue
                logger.info("SSE POST %s -> %s", path, r.status_code)
                if r.status_code in (404, 405):
                    await r.aclose()
                    continue
                if r.status_code in (401, 403):
                    await r.aclose()
                    raise PermissionError("DeerFlow SSE auth failed")
                resp = r
                used_path = path
                break

            if resp is None:
                raise httpx.HTTPError("No SSE endpoint available")

            try:
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer or buffer.endswith("\n\n"):
                        if "\n\n" not in buffer:
                            break
                        part, buffer = buffer.split("\n\n", 1)
                        for msg in parse_sse_chunk(part):
                            if msg.is_heartbeat:
                                if run_id:
                                    await self.db.upsert_run(
                                        run_id=run_id,
                                        thread_id=thread_id,
                                        status="running",
                                        touch_event=True,
                                    )
                                continue
                            data = parse_sse_data(msg.data)
                            norm, pmeta = map_sse_event(
                                event_name=msg.event or "",
                                data=data,
                                sse_id=msg.id,
                                thread_id=thread_id,
                                run_id=run_id,
                                settings=self.settings,
                            )
                            if norm:
                                if norm.run_id and not run_id:
                                    run_id = norm.run_id
                                    if provisional_run_id:
                                        await self.db.delete_run(provisional_run_id)
                                        provisional_run_id = None
                                await self.db.apply_normalized_event(norm, payload_meta=pmeta)
                if run_id:
                    run = await self.db.get_run(run_id)
                    if run and run["status"] in ("running", "tool", "subagent", "queued"):
                        await self.poll.backstop(thread_id, run_id)
            finally:
                await resp.aclose()

        if provisional_run_id:
            await self.db.delete_run(provisional_run_id)
        if run_id:
            await self.db.delete_pending_runs(thread_id)

        return run_id, thread_id
