from __future__ import annotations

import logging
from datetime import timezone
from typing import Any

from app.db import Database, utc_now
from app.deerflow_credentials import effective_auth
from app.deerflow_paths import agent_name_to_folder, resolve_user_id
from app.models import NormalizedEvent
from app.constants import ACTIVE_STATUSES
from app.sanitize import (
    format_thread_value_counts,
    progress_counts_from_mapping,
    progress_counts_snapshot,
    truncate_text,
)
from app.agent_text import sync_agent_text_from_state
from watcher.deerflow_client import DeerflowClient

logger = logging.getLogger(__name__)

TERMINAL_OK = frozenset({"success", "completed", "finished", "done", "complete"})
TERMINAL_FAIL = frozenset({
    "error",
    "failed",
    "cancelled",
    "canceled",
    "timeout",
    "interrupted",  # DeerFlow: forced stop / user cancel / safety limit
})


def _map_run_status(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).lower()
    if s in TERMINAL_OK:
        return "finished"
    if s in TERMINAL_FAIL:
        return "failed"
    if s in ("running", "pending", "queued", "in_progress"):
        return "running"
    return None


def _format_progress_message(progress: dict[str, Any], *, step_s: str | None, model: Any) -> str:
    bits: list[str] = []
    if step_s is not None:
        bits.append(f"step {step_s}")
    if model:
        bits.append(str(model))
    if progress.get("mode"):
        bits.append(f"mode {progress['mode']}")
    counts = format_thread_value_counts(progress_counts_from_mapping(progress))
    if counts:
        bits.append(counts)
    return " · ".join(bits)


class PollClient:
    def __init__(self, settings: Any, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.client = DeerflowClient(settings)

    async def backstop(self, thread_id: str, run_id: str) -> None:
        detail = await self.client.get_run(thread_id, run_id)
        if detail:
            await self._apply_run_dict(thread_id, run_id, detail)
            return
        runs = await self.client.list_runs(thread_id)
        for r in runs:
            rid = str(r.get("run_id") or r.get("id") or "")
            if rid == run_id:
                await self._apply_run_dict(thread_id, run_id, r)
                return

    async def _apply_run_dict(self, thread_id: str, run_id: str, data: dict) -> None:
        status = _map_run_status(data.get("status"))
        err = truncate_text(str(data.get("error") or ""), 500) if data.get("error") else None
        if status == "finished":
            ev = NormalizedEvent(
                event_type="run_finished",
                run_id=run_id,
                thread_id=thread_id,
                status="finished",
            )
            await self.db.apply_normalized_event(ev)
        elif status == "failed":
            ev = NormalizedEvent(
                event_type="run_failed",
                run_id=run_id,
                thread_id=thread_id,
                status="failed",
                error=err or "poll detected failure",
            )
            await self.db.apply_normalized_event(ev)

    def _parse_duration(self, data: dict) -> int | None:
        from datetime import datetime

        for a, b in (
            (data.get("created_at"), data.get("updated_at")),
            (data.get("started_at"), data.get("finished_at")),
        ):
            if not a or not b:
                continue
            try:
                sa = str(a).replace("Z", "+00:00")
                sb = str(b).replace("Z", "+00:00")
                if "+" not in sa[10:]:
                    sa += "+00:00"
                if "+" not in sb[10:]:
                    sb += "+00:00"
                t0 = datetime.fromisoformat(sa)
                t1 = datetime.fromisoformat(sb)
                return max(0, int((t1 - t0).total_seconds()))
            except ValueError:
                continue
        return None

    async def _snapshot_run_tokens_from_state(self, thread_id: str, run_id: str) -> None:
        """Freeze state message token sum on this run at completion (not whole thread)."""
        usage = await self.client.get_state_token_usage(thread_id)
        if not usage.get("total_tokens"):
            return
        run = await self.db.get_run(run_id)
        if not run:
            return
        await self.db.upsert_run(
            run_id=run_id,
            thread_id=thread_id,
            status=run["status"],
            total_tokens=usage["total_tokens"],
            input_tokens=usage.get("total_input_tokens"),
            output_tokens=usage.get("total_output_tokens"),
            touch_event=False,
        )

    async def poll_thread(self, thread_id: str) -> list[str]:
        """Passive mode: sync runs from DeerFlow API without SSE."""
        updated: list[str] = []
        self.client.begin_state_cache()
        try:
            return await self._poll_thread_cached(thread_id, updated)
        finally:
            self.client.end_state_cache()

    async def _poll_thread_cached(self, thread_id: str, updated: list[str]) -> list[str]:
        runs = await self.client.list_runs(thread_id)
        apath, rurl = await self.client.get_state_refs(thread_id)
        progress = await self.client.get_thread_progress(thread_id)
        state_for_agent: dict[str, Any] | None = None
        if self.settings.store_thinking_enabled or self.settings.store_final_answer_enabled:
            state_for_agent = await self.client.get_thread_state(thread_id)
        agent_name = progress.get("agent_name")
        agent_folder = agent_name_to_folder(str(agent_name) if agent_name else None)
        cookie, bearer = effective_auth(self.settings)
        user_id = resolve_user_id(
            env_user_id=self.settings.deerflow_user_id,
            auth_cookie=cookie,
            bearer_token=bearer,
        )

        for r in runs:
            run_id = str(r.get("run_id") or r.get("id") or "")
            if not run_id:
                continue
            status = _map_run_status(r.get("status")) or "running"
            raw_status = str(r.get("status") or "")
            existing = await self.db.get_run(run_id)
            old_status = existing["status"] if existing else None
            deerflow_created = r.get("created_at")
            assistant = str(r.get("assistant_id") or "")

            finished_at = None
            duration_sec = None
            if status in ("finished", "failed"):
                raw_end = r.get("updated_at") or r.get("finished_at")
                if raw_end:
                    try:
                        from datetime import datetime

                        s = str(raw_end).replace("Z", "+00:00")
                        if "+" not in s[10:]:
                            s += "+00:00"
                        finished_at = (
                            datetime.fromisoformat(s)
                            .astimezone(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                        )
                    except ValueError:
                        finished_at = utc_now()
                else:
                    finished_at = utc_now()
                duration_sec = self._parse_duration(r)
                if existing and existing.get("duration_sec"):
                    duration_sec = duration_sec or existing["duration_sec"]

            model = progress.get("model_name")
            step = progress.get("step")
            step_s = str(step) if step is not None else None
            counts = progress_counts_from_mapping(progress)
            snapshot = progress_counts_snapshot(counts)

            await self.db.upsert_run(
                run_id=run_id,
                thread_id=thread_id,
                status=status,
                artifact_path=apath,
                result_url=rurl,
                last_event_type="poll_sync" if old_status == status else f"status_{status}",
                finished_at=finished_at,
                duration_sec=duration_sec,
                assistant_id=assistant or None,
                deerflow_user_id=user_id,
                agent_folder=agent_folder,
                model=str(model) if model else None,
                current_step=step_s,
                progress_snapshot=snapshot,
                touch_event=True,
            )

            if old_status is None:
                parts = ["poll"]
                if assistant:
                    parts.append(f"api={assistant}")
                if agent_name:
                    parts.append(str(agent_name))
                if deerflow_created:
                    parts.append(f"created={str(deerflow_created)[:19]}")
                count_line = format_thread_value_counts(counts)
                if count_line:
                    parts.append(count_line)
                await self.db.insert_event(
                    NormalizedEvent(
                        event_type="run_started",
                        run_id=run_id,
                        thread_id=thread_id,
                        status=status,
                        message=" · ".join(parts),
                        model=str(model) if model else None,
                        step=step_s,
                    )
                )
            elif old_status != status:
                err = truncate_text(str(r.get("error") or ""), 500)
                if not err and status == "failed":
                    detail = await self.client.get_run(thread_id, run_id)
                    if detail:
                        err = truncate_text(str(detail.get("error") or ""), 500)
                msg = f"DeerFlow {raw_status} → {status}"
                if duration_sec is not None:
                    msg += f" · {duration_sec}s"
                if status == "finished":
                    await self.db.apply_normalized_event(
                        NormalizedEvent(
                            event_type="run_finished",
                            run_id=run_id,
                            thread_id=thread_id,
                            status="finished",
                            message=msg,
                            model=str(model) if model else None,
                        )
                    )
                elif status == "failed":
                    default_err = (
                        "DeerFlow run interrupted (forced stop / cancel)"
                        if raw_status == "interrupted"
                        else "DeerFlow reported run failed"
                    )
                    await self.db.apply_normalized_event(
                        NormalizedEvent(
                            event_type="run_failed",
                            run_id=run_id,
                            thread_id=thread_id,
                            status="failed",
                            error=err or default_err,
                            message=msg,
                        )
                    )
            elif status in ("running", "tool", "subagent"):
                prev_model = existing.get("model") if existing else None
                prev_step = existing.get("current_step") if existing else None
                prev_snapshot = (existing or {}).get("progress_snapshot") or ""
                msg = _format_progress_message(progress, step_s=step_s, model=model)
                if msg and (
                    model != prev_model
                    or step_s != prev_step
                    or snapshot != prev_snapshot
                ):
                    await self.db.insert_event(
                        NormalizedEvent(
                            event_type="run_progress",
                            run_id=run_id,
                            thread_id=thread_id,
                            status=status,
                            message=msg,
                            model=str(model) if model else None,
                            step=step_s,
                        )
                    )

            if status in ("finished", "failed"):
                had = int(existing.get("total_tokens") or 0) if existing else 0
                if old_status != status or not had:
                    try:
                        await self._snapshot_run_tokens_from_state(thread_id, run_id)
                    except Exception as e:
                        logger.debug(
                            "token snapshot run_id=%s thread_id=%s: %s",
                            run_id,
                            thread_id,
                            e,
                        )

            if state_for_agent is not None:
                await sync_agent_text_from_state(
                    self.db,
                    self.settings,
                    run_id=run_id,
                    thread_id=thread_id,
                    state=state_for_agent,
                    terminal=status in ("finished", "failed"),
                )

            updated.append(run_id)

        try:
            usage = await self.client.get_thread_token_usage(thread_id, prefer_state=True)
            live_total = int(usage.get("state_tokens") or usage.get("total_tokens") or 0)
            if live_total:
                for run_id in updated:
                    run = await self.db.get_run(run_id)
                    if not run or run["status"] not in ACTIVE_STATUSES:
                        continue
                    await self.db.upsert_run(
                        run_id=run_id,
                        thread_id=thread_id,
                        status=run["status"],
                        total_tokens=live_total,
                        input_tokens=usage.get("total_input_tokens"),
                        output_tokens=usage.get("total_output_tokens"),
                        touch_event=False,
                    )
        except Exception as e:
            logger.debug("thread token usage thread_id=%s: %s", thread_id, e)

        return updated
