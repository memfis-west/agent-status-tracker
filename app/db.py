from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import NormalizedEvent
from app.sanitize import truncate_text


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, db_path: str, schema_path: Path) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._init_schema()
        await self._migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _init_schema(self) -> None:
        assert self._conn
        sql = self.schema_path.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        await self._conn.commit()

    async def _migrate(self) -> None:
        assert self._conn
        migrations = [
            "ALTER TABLE runs ADD COLUMN current_step TEXT",
            "ALTER TABLE runs ADD COLUMN current_node TEXT",
            "ALTER TABLE runs ADD COLUMN last_event_type TEXT",
            "ALTER TABLE runs ADD COLUMN started_at TEXT",
            "ALTER TABLE runs ADD COLUMN duration_sec INTEGER",
            "ALTER TABLE events ADD COLUMN session_id TEXT",
            "ALTER TABLE events ADD COLUMN source_event TEXT",
            "ALTER TABLE events ADD COLUMN sse_id TEXT",
            "ALTER TABLE events ADD COLUMN message TEXT",
            "ALTER TABLE runs ADD COLUMN deerflow_user_id TEXT",
            "ALTER TABLE runs ADD COLUMN agent_folder TEXT",
            "ALTER TABLE runs ADD COLUMN progress_snapshot TEXT",
        ]
        for stmt in migrations:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError:
                pass
        await self._conn.commit()

    async def get_storage_stats(self) -> dict[str, Any]:
        """Row counts and latest timestamps for health UI."""
        assert self._conn
        stats: dict[str, Any] = {
            "runs_count": 0,
            "events_count": 0,
            "last_run_event_at": None,
            "last_event_at": None,
            "last_record_at": None,
        }
        async with self._conn.execute("SELECT COUNT(*) AS n FROM runs") as cur:
            row = await cur.fetchone()
            stats["runs_count"] = int(row["n"]) if row else 0
        async with self._conn.execute("SELECT COUNT(*) AS n FROM events") as cur:
            row = await cur.fetchone()
            stats["events_count"] = int(row["n"]) if row else 0
        async with self._conn.execute(
            "SELECT MAX(last_event_at) AS ts FROM runs"
        ) as cur:
            row = await cur.fetchone()
            stats["last_run_event_at"] = row["ts"] if row else None
        async with self._conn.execute(
            "SELECT MAX(created_at) AS ts FROM events"
        ) as cur:
            row = await cur.fetchone()
            stats["last_event_at"] = row["ts"] if row else None
        candidates = [stats["last_run_event_at"], stats["last_event_at"]]
        candidates = [c for c in candidates if c]
        stats["last_record_at"] = max(candidates) if candidates else None
        return stats

    async def ping(self) -> bool:
        assert self._conn
        async with self._conn.execute("SELECT 1") as cur:
            row = await cur.fetchone()
            return row is not None

    async def upsert_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        status: str,
        session_id: str | None = None,
        trace_id: str | None = None,
        current_step: str | None = None,
        current_node: str | None = None,
        last_event_type: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        result_summary: str | None = None,
        error: str | None = None,
        artifact_path: str | None = None,
        result_url: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_sec: int | None = None,
        assistant_id: str | None = None,
        deerflow_user_id: str | None = None,
        agent_folder: str | None = None,
        progress_snapshot: str | None = None,
        touch_event: bool = True,
    ) -> None:
        assert self._conn
        now = utc_now()
        async with self._conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)) as cur:
            exists = await cur.fetchone()

        last_event_at = now if touch_event else None

        if exists:
            sets = ["thread_id = ?", "status = ?", "updated_at = ?"]
            params: list[Any] = [thread_id, status, now]
            if session_id is not None:
                sets.append("session_id = ?")
                params.append(session_id)
            if trace_id is not None:
                sets.append("trace_id = ?")
                params.append(trace_id)
            if current_step is not None:
                sets.append("current_step = ?")
                params.append(current_step)
            if current_node is not None:
                sets.append("current_node = ?")
                params.append(current_node)
            if last_event_type is not None:
                sets.append("last_event_type = ?")
                params.append(last_event_type)
            if model is not None:
                sets.append("model = ?")
                params.append(model)
            if input_tokens is not None:
                sets.append("input_tokens = ?")
                params.append(input_tokens)
            if output_tokens is not None:
                sets.append("output_tokens = ?")
                params.append(output_tokens)
            if total_tokens is not None:
                sets.append("total_tokens = ?")
                params.append(total_tokens)
            if result_summary is not None:
                sets.append("result_summary = ?")
                params.append(truncate_text(result_summary, 500))
            if error is not None:
                sets.append("error = ?")
                params.append(truncate_text(error, 500))
            if artifact_path is not None:
                sets.append("artifact_path = ?")
                params.append(artifact_path)
            if result_url is not None:
                sets.append("result_url = ?")
                params.append(result_url)
            if started_at is not None:
                sets.append("started_at = ?")
                params.append(started_at)
            if finished_at is not None:
                sets.append("finished_at = ?")
                params.append(finished_at)
            if duration_sec is not None:
                sets.append("duration_sec = ?")
                params.append(duration_sec)
            if assistant_id is not None:
                sets.append("assistant_id = ?")
                params.append(assistant_id)
            if deerflow_user_id is not None:
                sets.append("deerflow_user_id = ?")
                params.append(deerflow_user_id)
            if agent_folder is not None:
                sets.append("agent_folder = ?")
                params.append(agent_folder)
            if progress_snapshot is not None:
                sets.append("progress_snapshot = ?")
                params.append(progress_snapshot)
            if last_event_at:
                sets.append("last_event_at = ?")
                params.append(last_event_at)
            params.append(run_id)
            await self._conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?", params)
        else:
            lea = last_event_at or now
            await self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, thread_id, session_id, trace_id, status,
                    current_step, current_node, last_event_type, model,
                    input_tokens, output_tokens, total_tokens,
                    result_summary, error, artifact_path, result_url,
                    started_at, created_at, updated_at, last_event_at,
                    finished_at, duration_sec, assistant_id, deerflow_user_id, agent_folder,
                    progress_snapshot
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    thread_id,
                    session_id or thread_id,
                    trace_id,
                    status,
                    current_step,
                    current_node,
                    last_event_type,
                    model,
                    input_tokens or 0,
                    output_tokens or 0,
                    total_tokens or 0,
                    truncate_text(result_summary, 500),
                    truncate_text(error, 500),
                    artifact_path,
                    result_url,
                    started_at or now,
                    now,
                    now,
                    lea,
                    finished_at,
                    duration_sec,
                    assistant_id,
                    deerflow_user_id,
                    agent_folder,
                    progress_snapshot,
                ),
            )
        await self._conn.commit()

    async def insert_event(self, ev: NormalizedEvent, payload_meta: str | None = None) -> None:
        assert self._conn
        now = ev.created_at or utc_now()
        await self._conn.execute(
            """
            INSERT INTO events (
                run_id, thread_id, session_id, event_type, source_event, sse_id,
                node, step, status, model, input_tokens, output_tokens, total_tokens,
                artifact_path, result_url, message, error, trace_id, payload_meta, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ev.run_id,
                ev.thread_id or "",
                ev.session_id,
                ev.event_type,
                ev.source_event,
                ev.sse_id,
                ev.node,
                ev.step,
                ev.status,
                ev.model,
                ev.input_tokens,
                ev.output_tokens,
                ev.total_tokens,
                ev.artifact_path,
                ev.result_url,
                truncate_text(ev.message, 500),
                truncate_text(ev.error, 500),
                ev.trace_id,
                payload_meta,
                now,
            ),
        )
        await self._conn.commit()

    async def apply_normalized_event(
        self,
        ev: NormalizedEvent,
        *,
        payload_meta: str | None = None,
        max_text: int = 500,
    ) -> None:
        if not ev.run_id or not ev.thread_id:
            return
        status = ev.status or "running"
        if ev.event_type == "run_finished":
            status = "finished"
        elif ev.event_type == "run_failed":
            status = "failed"
        elif ev.event_type in ("tool_started", "tool_finished"):
            status = "tool"
        elif ev.event_type in ("subagent_started", "subagent_finished"):
            status = "subagent"
        elif ev.event_type == "run_started":
            status = "running"

        finished_at = None
        duration_sec = None
        if status in ("finished", "failed"):
            finished_at = ev.created_at or utc_now()
            async with self._conn.execute(  # type: ignore[union-attr]
                "SELECT started_at, created_at FROM runs WHERE run_id = ?", (ev.run_id,)
            ) as cur:
                row = await cur.fetchone()
                start_raw = None
                if row:
                    start_raw = row["started_at"] or row["created_at"]
                duration_sec = self._duration_between(start_raw, finished_at)

        touch = ev.event_type != "heartbeat"
        await self.upsert_run(
            run_id=ev.run_id,
            thread_id=ev.thread_id,
            status=status,
            session_id=ev.session_id,
            trace_id=ev.trace_id,
            current_step=ev.step,
            current_node=ev.node,
            last_event_type=ev.event_type,
            model=ev.model,
            input_tokens=ev.input_tokens,
            output_tokens=ev.output_tokens,
            total_tokens=ev.total_tokens,
            result_summary=ev.message,
            error=ev.error,
            artifact_path=ev.artifact_path,
            result_url=ev.result_url,
            started_at=utc_now() if ev.event_type == "run_started" else None,
            finished_at=finished_at,
            duration_sec=duration_sec,
            touch_event=touch,
        )
        if ev.event_type != "heartbeat":
            await self.insert_event(ev, payload_meta=payload_meta)

    def _duration_between(self, start_raw: str | None, end_raw: str) -> int | None:
        if not start_raw:
            return None
        try:
            start = datetime.strptime(str(start_raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end = datetime.strptime(str(end_raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return max(0, int((end - start).total_seconds()))
        except ValueError:
            return None

    async def _terminal_end_at(self, run_id: str, fallback: str | None) -> str:
        assert self._conn
        async with self._conn.execute(
            """
            SELECT created_at FROM events
            WHERE run_id = ? AND event_type IN ('run_finished', 'run_failed')
            ORDER BY id DESC LIMIT 1
            """,
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        if row and row["created_at"]:
            return str(row["created_at"])
        return fallback or utc_now()

    async def backfill_terminal_fields(self, run_id: str) -> None:
        """Set finished_at/duration for failed/finished runs saved before this fix."""
        run = await self.get_run(run_id)
        if not run or run["status"] not in ("finished", "failed"):
            return
        if run.get("finished_at"):
            return
        end_at = await self._terminal_end_at(run_id, run.get("last_event_at"))
        start_raw = run.get("started_at") or run.get("created_at")
        duration = self._duration_between(start_raw, end_at)
        await self.upsert_run(
            run_id=run_id,
            thread_id=run["thread_id"],
            status=run["status"],
            finished_at=end_at,
            duration_sec=duration,
            touch_event=False,
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        assert self._conn
        async with self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_runs(
        self,
        *,
        status: str | None = None,
        thread_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        assert self._conn
        q = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if thread_id:
            q += " AND thread_id = ?"
            params.append(thread_id)
        q += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self._conn.execute(q, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def prune_poll_spam_events(self, run_id: str | None = None) -> int:
        """Remove redundant 'Passive poll sync' rows from older sync logic."""
        assert self._conn
        if run_id:
            q = "DELETE FROM events WHERE run_id = ? AND message = 'Passive poll sync'"
            params: tuple[Any, ...] = (run_id,)
        else:
            q = "DELETE FROM events WHERE message = 'Passive poll sync'"
            params = ()
        async with self._conn.execute(q, params) as cur:
            n = cur.rowcount
        await self._conn.commit()
        return n

    async def list_events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def mark_stale_runs(self, stale_after_seconds: int) -> list[str]:
        assert self._conn
        cutoff = datetime.now(timezone.utc).timestamp() - stale_after_seconds
        stale_ids: list[str] = []
        async with self._conn.execute(
            """
            SELECT run_id, thread_id, last_event_at FROM runs
            WHERE status IN ('queued','running','tool','subagent')
            """
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            try:
                ts = datetime.strptime(row["last_event_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                stale_ids.append(row["run_id"])
                await self.upsert_run(
                    run_id=row["run_id"],
                    thread_id=row["thread_id"],
                    status="stale",
                    last_event_type="run_stale",
                    touch_event=True,
                )
                ev = NormalizedEvent(
                    event_type="run_stale",
                    run_id=row["run_id"],
                    thread_id=row["thread_id"],
                    status="stale",
                    message="Run marked stale due to inactivity",
                )
                await self.insert_event(ev)
        await self._conn.commit()
        return stale_ids

    async def notification_sent(self, run_id: str, reason: str, success: bool) -> None:
        assert self._conn
        key = f"{run_id}:{reason}"
        now = utc_now()
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO notifications (run_id, channel, reason, sent_at, dedupe_key, success)
            VALUES (?, 'ntfy', ?, ?, ?, ?)
            """,
            (run_id, reason, now, key, 1 if success else 0),
        )
        await self._conn.commit()

    async def was_notified(self, run_id: str, reason: str) -> bool:
        assert self._conn
        key = f"{run_id}:{reason}"
        async with self._conn.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key = ? AND success = 1",
            (key,),
        ) as cur:
            return await cur.fetchone() is not None

    async def delete_run(self, run_id: str) -> bool:
        assert self._conn
        async with self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,)) as cur:
            deleted = cur.rowcount > 0
        await self._conn.commit()
        return deleted

    async def delete_runs_for_thread(self, thread_id: str) -> int:
        assert self._conn
        async with self._conn.execute("DELETE FROM runs WHERE thread_id = ?", (thread_id,)) as cur:
            n = cur.rowcount
        await self._conn.commit()
        return n

    async def delete_pending_runs(self, thread_id: str | None = None) -> int:
        assert self._conn
        if thread_id:
            async with self._conn.execute(
                "DELETE FROM runs WHERE run_id LIKE 'pending-%' AND thread_id = ?",
                (thread_id,),
            ) as cur:
                n = cur.rowcount
        else:
            async with self._conn.execute("DELETE FROM runs WHERE run_id LIKE 'pending-%'") as cur:
                n = cur.rowcount
        await self._conn.commit()
        return n

    async def list_thread_ids(self, limit: int = 200) -> list[str]:
        assert self._conn
        async with self._conn.execute(
            "SELECT DISTINCT thread_id FROM runs ORDER BY thread_id LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [str(r[0]) for r in rows if r[0]]
