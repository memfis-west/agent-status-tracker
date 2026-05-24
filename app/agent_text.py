"""Opt-in extraction of last thinking and final answer (no raw history)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.db import Database, utc_now
from app.models import NormalizedEvent
from app.sanitize import extract_safe_summary, truncate_text
from app.settings import Settings

logger = logging.getLogger(__name__)

HUMAN_TYPES = frozenset({"human", "user"})
AI_TYPES = frozenset({"ai", "assistant"})
OPERATIONAL_MARKERS = re.compile(
    r"\b(thinking|todo|tool|progress|step|node|update|subagent|artifact|middleware|"
    r"started|finished|cancelled|canceled|interrupted)\b",
    re.IGNORECASE,
)
SENSITIVE_PATTERNS = re.compile(
    r"(access_token|csrf_token|api[_-]?key|bearer\s+|password\s*=|-----BEGIN)",
    re.IGNORECASE,
)
LONG_TEXT_WITHOUT_MARKER_LIMIT = 1000

_STREAM_CHUNK_EVENTS = frozenset({"messages-tuple", "messages"})
_SKIP_THINKING_SOURCE = frozenset({"messages", "messages-tuple", "end", "metadata"})
_ACCEPT_EVENT_TYPE = re.compile(
    r"(node|tool|subagent|run|progress|artifact|started|finished|running|error)",
    re.IGNORECASE,
)
_MAX_OPERATIONAL_THINKING = 300


def _role(msg: dict[str, Any]) -> str:
    return str(msg.get("type") or msg.get("role") or "").lower()


def _is_human(msg: dict[str, Any]) -> bool:
    return _role(msg) in HUMAN_TYPES


def _is_ai(msg: dict[str, Any]) -> bool:
    return _role(msg) in AI_TYPES


def _message_content(msg: dict[str, Any]) -> str | None:
    """Visible assistant reply text (excludes reasoning/thinking blocks)."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str) and block.strip():
                parts.append(block.strip())
            elif isinstance(block, dict):
                btype = str(block.get("type") or "").lower()
                if btype in ("thinking", "reasoning"):
                    continue
                if btype == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"].strip())
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"].strip())
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def _extract_reasoning_content(msg: dict[str, Any]) -> str | None:
    """DeerFlow UI thinking: additional_kwargs.reasoning_content or thinking blocks."""
    if not isinstance(msg, dict) or not _is_ai(msg):
        return None
    extra = msg.get("additional_kwargs")
    if isinstance(extra, dict):
        rc = extra.get("reasoning_content")
        if isinstance(rc, str):
            cleaned = _reject_text(rc.strip())
            if cleaned and len(cleaned) >= 8:
                return cleaned
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").lower() != "thinking":
                continue
            text = block.get("thinking") or block.get("text")
            if isinstance(text, str):
                cleaned = _reject_text(text.strip())
                if cleaned and len(cleaned) >= 8:
                    return cleaned
    return None


def _join_reasoning_parts(parts: list[str], max_chars: int) -> str | None:
    if not parts:
        return None
    deduped: list[str] = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        if deduped and deduped[-1] == s:
            continue
        deduped.append(s)
    joined = "\n\n".join(deduped)
    if len(joined) <= max_chars:
        return joined
    return "…\n\n" + joined[-(max_chars - 10) :]


def _reject_text(text: str | None) -> str | None:
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    if SENSITIVE_PATTERNS.search(s):
        return None
    if s.startswith("{") and len(s) > 120:
        return None
    return s


def _has_operational_markers(text: str) -> bool:
    return bool(OPERATIONAL_MARKERS.search(text))


def _looks_like_thinking(text: str) -> bool:
    s = _reject_text(text)
    if not s:
        return False
    if len(s) > LONG_TEXT_WITHOUT_MARKER_LIMIT and not _has_operational_markers(s):
        return False
    if _has_operational_markers(s):
        return True
    if len(s) <= 400 and not s.startswith("#"):
        return True
    return False


def _looks_like_final_answer(text: str) -> bool:
    s = _reject_text(text)
    if not s or len(s) < 2:
        return False
    if _has_operational_markers(s) and len(s) < 200:
        return False
    return True


def _truncate_agent_text(text: str, settings: Settings) -> str:
    return truncate_text(text, settings.max_agent_text_chars) or ""


def _cap_operational(text: str) -> str | None:
    return truncate_text(text.strip(), _MAX_OPERATIONAL_THINKING)


def _is_operational_status_message(text: str) -> bool:
    s = _reject_text(text)
    if not s or len(s) > _MAX_OPERATIONAL_THINKING:
        return False
    if _looks_like_final_answer(s):
        return False
    if _has_operational_markers(s):
        return True
    return len(s) <= 120 and not s.startswith("#")


def _build_operational_line(norm: NormalizedEvent) -> str | None:
    et = (norm.event_type or "").lower()
    node = (norm.node or "").strip() or None
    step = norm.step
    status = (norm.status or "").strip() or None

    if et == "run_started":
        return None
    if et == "run_finished":
        return _cap_operational("Run finished")
    if et == "run_failed":
        err = _reject_text(norm.error)
        if err:
            return _cap_operational(f"Run failed: {err[:120]}")
        return _cap_operational("Run failed")
    if et == "token_usage":
        return None

    label: str | None = None
    if et == "node_started":
        label = "Node started"
    elif et == "tool_started":
        label = "Tool started"
    elif et == "tool_finished":
        label = "Tool finished"
    elif et == "subagent_started":
        label = "Subagent running"
    elif et == "subagent_finished":
        label = "Subagent finished"
    elif et == "run_progress":
        label = "Run progress"
    elif et == "artifact_created":
        label = "Artifact created"
    elif _ACCEPT_EVENT_TYPE.search(et):
        label = et.replace("_", " ").strip().title()

    if not label:
        return None

    target = node
    if not target and step is not None and str(step).strip():
        target = f"step {step}"
    if not target and status in ("tool", "subagent"):
        target = status
    if not target and status and status not in ("running", "queued"):
        target = status

    if target:
        return _cap_operational(f"{label}: {target}")
    if et in ("node_started", "tool_started", "subagent_started", "run_progress"):
        return _cap_operational(label)
    if not (node or step is not None or status or norm.message):
        return None
    return _cap_operational(label)


def extract_last_thinking_from_normalized_event(norm: NormalizedEvent | None) -> str | None:
    """Operational status from sanitized normalized fields only (no raw SSE payload)."""
    if norm is None:
        return None
    src = (norm.source_event or "").lower().strip()
    if src in _SKIP_THINKING_SOURCE or src in _STREAM_CHUNK_EVENTS:
        return None

    et = (norm.event_type or "").lower()
    if not _ACCEPT_EVENT_TYPE.search(et):
        allowed_src = ("updates", "values", "tasks", "events", "debug", "checkpoints", "custom")
        if src not in allowed_src:
            return None

    msg = _reject_text(norm.message)
    if msg:
        if _is_operational_status_message(msg):
            return _cap_operational(msg)
        has_meta = bool(
            norm.node
            or norm.step is not None
            or norm.status in ("tool", "subagent", "failed", "finished")
        )
        if not has_meta:
            return None

    return _build_operational_line(norm)


def extract_last_thinking_from_sse(event_name: str, data: Any) -> str | None:
    en = (event_name or "").lower().strip()
    if en in _STREAM_CHUNK_EVENTS:
        if isinstance(data, dict):
            reasoning = _extract_reasoning_content(data)
            if reasoning:
                return reasoning
        return None
    if en in ("end", "error", "metadata"):
        return None
    summary = extract_safe_summary(data, 4000)
    if not summary or not _looks_like_thinking(summary):
        return None
    return summary


def extract_final_answer_from_sse(event_name: str, data: Any) -> str | None:
    """SSE stream chunks are unreliable; only explicit complete AI payloads."""
    en = (event_name or "").lower().strip()
    if not isinstance(data, dict):
        return None
    if en in _STREAM_CHUNK_EVENTS:
        if data.get("chunk") is not None:
            return None
        if not data.get("complete") and not data.get("finished"):
            return None
        role = str(data.get("type") or data.get("role") or "").lower()
        if role not in AI_TYPES:
            return None
        text = _message_content(data)
        if not text or not _looks_like_final_answer(text):
            return None
        if not data.get("complete") and not data.get("finished") and len(text) < 80:
            return None
        return text
    if en in ("values", "updates"):
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            return extract_final_answer_from_state({"values": {"messages": messages}})
    return None


def extract_last_thinking_from_state(
    state: dict[str, Any] | None,
    *,
    max_chars: int = 4000,
) -> str | None:
    if not state:
        return None
    values = state.get("values")
    messages = values.get("messages") if isinstance(values, dict) else None
    if not isinstance(messages, list):
        return None
    reasoning_parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or _is_human(msg) or not _is_ai(msg):
            continue
        rc = _extract_reasoning_content(msg)
        if rc:
            reasoning_parts.append(rc)
    if reasoning_parts:
        return _join_reasoning_parts(reasoning_parts, max_chars)
    for msg in reversed(messages):
        if not isinstance(msg, dict) or _is_human(msg):
            continue
        text = _message_content(msg)
        if text and _looks_like_thinking(text) and not _looks_like_final_answer(text):
            return text
    return None


def extract_final_answer_from_state(state: dict[str, Any] | None) -> str | None:
    """Primary reliable source: last assistant/AI message in thread state."""
    if not state:
        return None
    values = state.get("values")
    messages = values.get("messages") if isinstance(values, dict) else None
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or _is_human(msg) or not _is_ai(msg):
            continue
        text = _message_content(msg)
        if text and _looks_like_final_answer(text):
            return text
    return None


async def _save_thinking(
    db: Database,
    settings: Settings,
    *,
    run_id: str,
    thread_id: str,
    text: str | None,
    merge: bool = False,
) -> None:
    if not text or not settings.store_thinking_enabled:
        return
    if merge:
        run = await db.get_run(run_id)
        prev = (run or {}).get("last_thinking") or ""
        if text.strip() and text.strip() not in prev:
            text = f"{prev}\n\n{text}".strip() if prev else text
    cleaned = _truncate_agent_text(text, settings)
    if not cleaned:
        return
    await db.update_last_thinking(
        run_id, thread_id, cleaned, at=utc_now(), max_chars=settings.max_agent_text_chars
    )


async def _save_final(
    db: Database,
    settings: Settings,
    *,
    run_id: str,
    thread_id: str,
    text: str | None,
) -> None:
    if not text or not settings.store_final_answer_enabled:
        return
    cleaned = _truncate_agent_text(text, settings)
    if not cleaned:
        return
    await db.update_final_answer(
        run_id, thread_id, cleaned, at=utc_now(), max_chars=settings.max_agent_text_chars
    )


async def sync_agent_text_from_sse(
    db: Database,
    settings: Settings,
    *,
    run_id: str,
    thread_id: str,
    event_name: str,
    data: Any,
    norm: NormalizedEvent | None = None,
    norm_message: str | None = None,
) -> None:
    if not run_id or not thread_id:
        return
    if not settings.store_thinking_enabled and not settings.store_final_answer_enabled:
        return
    try:
        en = (event_name or "").lower().strip()
        thinking = extract_last_thinking_from_sse(event_name, data)
        merge = bool(thinking and en in _STREAM_CHUNK_EVENTS)
        msg = norm_message if norm_message is not None else (norm.message if norm else None)
        if not thinking and msg and _looks_like_thinking(msg):
            thinking = msg
        if not thinking and norm:
            thinking = extract_last_thinking_from_normalized_event(norm)
        await _save_thinking(
            db,
            settings,
            run_id=run_id,
            thread_id=thread_id,
            text=thinking,
            merge=merge,
        )
        if settings.store_final_answer_enabled:
            answer = extract_final_answer_from_sse(event_name, data)
            if answer:
                await _save_final(
                    db, settings, run_id=run_id, thread_id=thread_id, text=answer
                )
    except Exception as e:
        logger.debug("agent_text sse run_id=%s: %s", run_id, e)


async def finalize_final_answer_from_state(
    db: Database,
    settings: Settings,
    client: Any,
    *,
    run_id: str,
    thread_id: str,
) -> None:
    if not settings.store_final_answer_enabled and not settings.store_thinking_enabled:
        return
    try:
        state = await client.get_thread_state(thread_id)
        if settings.store_final_answer_enabled:
            answer = extract_final_answer_from_state(state)
            await _save_final(
                db, settings, run_id=run_id, thread_id=thread_id, text=answer
            )
        if settings.store_thinking_enabled:
            thinking = extract_last_thinking_from_state(
                state, max_chars=settings.max_agent_text_chars
            )
            await _save_thinking(
                db, settings, run_id=run_id, thread_id=thread_id, text=thinking
            )
    except Exception as e:
        logger.debug("finalize agent_text run_id=%s: %s", run_id, e)


async def sync_agent_text_from_state(
    db: Database,
    settings: Settings,
    *,
    run_id: str,
    thread_id: str,
    state: dict[str, Any] | None,
    terminal: bool = False,
) -> None:
    if not run_id or not thread_id:
        return
    if not settings.store_thinking_enabled and not (
        terminal and settings.store_final_answer_enabled
    ):
        return
    try:
        if settings.store_thinking_enabled:
            thinking = extract_last_thinking_from_state(
                state, max_chars=settings.max_agent_text_chars
            )
            await _save_thinking(
                db, settings, run_id=run_id, thread_id=thread_id, text=thinking
            )
        if terminal and settings.store_final_answer_enabled:
            answer = extract_final_answer_from_state(state)
            await _save_final(
                db, settings, run_id=run_id, thread_id=thread_id, text=answer
            )
    except Exception as e:
        logger.debug("agent_text state run_id=%s: %s", run_id, e)
