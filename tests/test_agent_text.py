from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent_text import (
    extract_final_answer_from_sse,
    extract_final_answer_from_state,
    extract_last_thinking_from_normalized_event,
    extract_last_thinking_from_sse,
    extract_last_thinking_from_state,
    finalize_final_answer_from_state,
    sync_agent_text_from_sse,
    sync_agent_text_from_state,
)
from app.db import Database
from app.models import NormalizedEvent
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        store_agent_text=False,
        store_last_thinking=True,
        store_final_answer=False,
        max_agent_text_chars=500,
    )


@pytest.fixture
def settings_final_on() -> Settings:
    return Settings(
        store_agent_text=False,
        store_last_thinking=False,
        store_final_answer=True,
        max_agent_text_chars=4000,
    )


def test_long_ai_not_thinking():
    long_answer = "word " * 300
    assert extract_last_thinking_from_state(
        {"values": {"messages": [{"type": "ai", "content": long_answer}]}}
    ) is None


def test_reasoning_content_from_state():
    state = {
        "values": {
            "messages": [
                {"type": "human", "content": "secret prompt"},
                {
                    "type": "ai",
                    "content": "",
                    "additional_kwargs": {
                        "reasoning_content": "The user wants a read-only smoke audit of the skill."
                    },
                },
                {
                    "type": "ai",
                    "content": "Final summary for the user.",
                    "additional_kwargs": {"reasoning_content": "All tasks complete."},
                },
            ]
        }
    }
    thinking = extract_last_thinking_from_state(state)
    assert thinking is not None
    assert "read-only smoke audit" in thinking
    assert "All tasks complete" in thinking
    assert "secret prompt" not in thinking
    assert extract_final_answer_from_state(state) == "Final summary for the user."


def test_reasoning_from_sse_messages_tuple():
    chunk = {
        "type": "ai",
        "content": "This is the visible answer chunk.",
        "additional_kwargs": {"reasoning_content": "Less steps\nPlanning the audit."},
    }
    assert extract_last_thinking_from_sse("messages-tuple", chunk) == (
        "Less steps\nPlanning the audit."
    )
    assert extract_last_thinking_from_sse("messages", chunk) == (
        "Less steps\nPlanning the audit."
    )


def test_user_message_skipped():
    state = {
        "values": {
            "messages": [
                {"type": "human", "content": "secret user prompt here"},
                {"type": "ai", "content": "Update to-do list: step 2"},
            ]
        }
    }
    assert extract_last_thinking_from_state(state) == "Update to-do list: step 2"
    assert extract_final_answer_from_state(state) is None


def test_messages_tuple_chunk_no_final():
    chunk = {"type": "ai", "content": "Hello", "chunk": True}
    assert extract_final_answer_from_sse("messages-tuple", chunk) is None


def test_messages_tuple_complete_ai_final():
    msg = {"type": "ai", "content": "Full answer text here.", "complete": True}
    assert extract_final_answer_from_sse("messages-tuple", msg) == "Full answer text here."


def test_sse_thinking_from_custom():
    data = {"status": "running", "message": "Update to-do list for subagent"}
    assert extract_last_thinking_from_sse("custom", data) is not None


def test_sse_messages_tuple_not_thinking():
    assert extract_last_thinking_from_sse("messages-tuple", {"type": "ai", "content": "x"}) is None


def test_normalized_node_started_without_message():
    norm = NormalizedEvent(
        event_type="node_started",
        run_id="r1",
        thread_id="t1",
        source_event="updates",
        node="researcher",
        status="running",
    )
    assert extract_last_thinking_from_normalized_event(norm) == "Node started: researcher"


def test_normalized_tool_started_without_message():
    norm = NormalizedEvent(
        event_type="tool_started",
        run_id="r1",
        thread_id="t1",
        source_event="updates",
        node="write_todos",
        status="tool",
    )
    assert extract_last_thinking_from_normalized_event(norm) == "Tool started: write_todos"


def test_normalized_messages_source_skipped():
    norm = NormalizedEvent(
        event_type="node_started",
        run_id="r1",
        thread_id="t1",
        source_event="messages",
        node="coder",
        status="running",
    )
    assert extract_last_thinking_from_normalized_event(norm) is None


def test_normalized_messages_tuple_norm_skipped():
    norm = NormalizedEvent(
        event_type="node_started",
        run_id="r1",
        thread_id="t1",
        source_event="messages-tuple",
        message="chunk of assistant answer text",
    )
    assert extract_last_thinking_from_normalized_event(norm) is None


def test_normalized_long_assistant_message_rejected():
    long_answer = "Here is the full report.\n\n" + ("paragraph. " * 80)
    norm = NormalizedEvent(
        event_type="run_progress",
        run_id="r1",
        thread_id="t1",
        source_event="custom",
        message=long_answer,
    )
    assert extract_last_thinking_from_normalized_event(norm) is None


def test_normalized_updates_without_node_still_operational():
    norm = NormalizedEvent(
        event_type="node_started",
        run_id="r1",
        thread_id="t1",
        source_event="updates",
        status="running",
    )
    assert extract_last_thinking_from_normalized_event(norm) == "Node started"


def test_sse_updates_fallback_via_norm():
    norm = NormalizedEvent(
        event_type="node_started",
        run_id="r1",
        thread_id="t1",
        source_event="updates",
        node="planner",
        status="running",
    )
    assert extract_last_thinking_from_sse("updates", {"langgraph_node": "planner"}) is None
    assert extract_last_thinking_from_normalized_event(norm) == "Node started: planner"


def test_sensitive_rejected():
    assert (
        extract_last_thinking_from_sse(
            "custom", {"message": "access_token=abc123xyz"}
        )
        is None
    )


def test_final_answer_from_state_last_ai():
    state = {
        "values": {
            "messages": [
                {"type": "human", "content": "hi"},
                {"type": "ai", "content": "short"},
                {
                    "type": "ai",
                    "content": "This is the final substantive assistant reply for the user.",
                },
            ]
        }
    }
    assert (
        extract_final_answer_from_state(state)
        == "This is the final substantive assistant reply for the user."
    )


def test_migration_columns():
    async def _run() -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            schema = Path(__file__).resolve().parent.parent / "TRACKER_SCHEMA.sql"
            db = Database(db_path, schema)
            await db.connect()
            async with db._conn.execute("PRAGMA table_info(runs)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            await db.close()
            return cols

    cols = asyncio.run(_run())
    assert "last_thinking" in cols
    assert "last_thinking_at" in cols
    assert "final_answer" in cols
    assert "final_answer_at" in cols


def test_sync_skips_final_when_disabled(settings: Settings):
    async def _run() -> None:
        db = MagicMock()
        db.update_final_answer = AsyncMock()
        db.update_last_thinking = AsyncMock()
        state = {
            "values": {
                "messages": [{"type": "ai", "content": "Final answer for user."}]
            }
        }
        await sync_agent_text_from_state(
            db,
            settings,
            run_id="r1",
            thread_id="t1",
            state=state,
            terminal=True,
        )
        db.update_final_answer.assert_not_called()

    asyncio.run(_run())


def test_sync_writes_final_when_enabled(settings_final_on: Settings):
    async def _run() -> None:
        db = MagicMock()
        db.update_final_answer = AsyncMock()
        db.update_last_thinking = AsyncMock()
        state = {
            "values": {
                "messages": [{"type": "ai", "content": "Final answer for user."}]
            }
        }
        await sync_agent_text_from_state(
            db,
            settings_final_on,
            run_id="r1",
            thread_id="t1",
            state=state,
            terminal=True,
        )
        db.update_final_answer.assert_called_once()

    asyncio.run(_run())


def test_truncate_on_save(settings: Settings):
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            schema = Path(__file__).resolve().parent.parent / "TRACKER_SCHEMA.sql"
            db = Database(db_path, schema)
            await db.connect()
            await db.upsert_run(run_id="r1", thread_id="t1", status="running")
            long = "x" * 800
            await db.update_last_thinking(
                "r1", "t1", long, max_chars=settings.max_agent_text_chars
            )
            run = await db.get_run("r1")
            assert run
            assert len(run["last_thinking"] or "") <= settings.max_agent_text_chars
            await db.close()

    asyncio.run(_run())


def test_finalize_from_state(settings_final_on: Settings):
    async def _run() -> None:
        db = MagicMock()
        db.update_final_answer = AsyncMock()
        db.update_last_thinking = AsyncMock()
        client = MagicMock()
        client.get_thread_state = AsyncMock(
            return_value={
                "values": {
                    "messages": [{"type": "ai", "content": "Done from state."}]
                }
            }
        )
        await finalize_final_answer_from_state(
            db, settings_final_on, client, run_id="r1", thread_id="t1"
        )
        client.get_thread_state.assert_called_once_with("t1")
        db.update_final_answer.assert_called_once()

    asyncio.run(_run())


def test_sse_sync_no_raise(settings: Settings):
    async def _run() -> None:
        db = MagicMock()
        db.update_last_thinking = AsyncMock()
        db.update_final_answer = AsyncMock()
        await sync_agent_text_from_sse(
            db,
            settings,
            run_id="r1",
            thread_id="t1",
            event_name="updates",
            data={"langgraph_node": "researcher"},
            norm=NormalizedEvent(
                event_type="node_started",
                run_id="r1",
                thread_id="t1",
                source_event="updates",
                node="researcher",
                status="running",
            ),
        )
        db.update_last_thinking.assert_called()
        args = db.update_last_thinking.call_args[0]
        assert "Node started" in args[2]

    asyncio.run(_run())
