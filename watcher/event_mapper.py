from __future__ import annotations

import json
from typing import Any

from app.models import NormalizedEvent
from app.sanitize import (
    extract_artifact_refs,
    extract_ids,
    extract_node_step,
    extract_safe_summary,
    extract_token_usage,
    infer_status_from_node,
    safe_payload_meta,
    truncate_text,
)
from app.settings import Settings


def parse_sse_data(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def map_sse_event(
    *,
    event_name: str,
    data: Any,
    sse_id: str | None,
    thread_id: str,
    run_id: str | None,
    settings: Settings,
) -> tuple[NormalizedEvent | None, str | None]:
    """Map raw SSE to normalized event. Returns (event, payload_meta)."""
    ids = extract_ids(data) if isinstance(data, (dict, str)) else {}
    rid = ids.get("run_id") or run_id
    tid = ids.get("thread_id") or thread_id
    if not rid:
        if event_name in ("end", "error") and run_id:
            rid = run_id
        else:
            return None, None

    source = event_name or "unknown"
    node, step = extract_node_step(data)
    inp, out, total, model = extract_token_usage(data)
    apath, rurl = extract_artifact_refs(data)
    payload_meta = safe_payload_meta(data, settings.store_payloads, settings.max_payload_chars)

    base = dict(
        run_id=rid,
        thread_id=tid or thread_id,
        session_id=ids.get("session_id") or tid,
        trace_id=ids.get("trace_id"),
        source_event=source,
        sse_id=sse_id,
        node=node,
        step=step,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=total,
        artifact_path=apath,
        result_url=rurl,
    )

    en = event_name.lower().strip() if event_name else ""

    if en == "metadata":
        return NormalizedEvent(event_type="run_started", status="running", **base), payload_meta

    if en == "end":
        return NormalizedEvent(event_type="run_finished", status="finished", **base), payload_meta

    if en == "error":
        err = extract_safe_summary(data, settings.max_text_field_chars) or "run error"
        if isinstance(data, dict) and "error" in data:
            err = truncate_text(str(data.get("error")), settings.max_text_field_chars) or err
        return (
            NormalizedEvent(event_type="run_failed", status="failed", error=err, **base),
            payload_meta,
        )

    if en in ("updates", "values", "messages", "messages-tuple"):
        st = infer_status_from_node(node, step)
        et = "tool_started" if st == "tool" else "subagent_started" if st == "subagent" else "node_started"
        if inp or out or total:
            return (
                NormalizedEvent(
                    event_type="token_usage",
                    status=st,
                    message=extract_safe_summary(data, settings.max_text_field_chars),
                    **base,
                ),
                payload_meta,
            )
        return NormalizedEvent(event_type=et, status=st, **base), payload_meta

    if en == "custom":
        summary = extract_safe_summary(data, settings.max_text_field_chars)
        text = (summary or "").lower()
        if "artifact" in text or apath or rurl:
            return (
                NormalizedEvent(event_type="artifact_created", status="running", message=summary, **base),
                payload_meta,
            )
        if "tool" in text:
            et = "tool_finished" if "finish" in text or "done" in text else "tool_started"
            return NormalizedEvent(event_type=et, status="tool", message=summary, **base), payload_meta
        if "subagent" in text:
            et = "subagent_finished" if "finish" in text else "subagent_started"
            return NormalizedEvent(event_type=et, status="subagent", message=summary, **base), payload_meta
        return (
            NormalizedEvent(event_type="run_progress", status="running", message=summary, **base),
            payload_meta,
        )

    if en in ("checkpoints", "tasks", "debug", "events"):
        return NormalizedEvent(event_type="run_progress", status="running", **base), payload_meta

    return (
        NormalizedEvent(
            event_type="unknown",
            status="running",
            message=extract_safe_summary(data, settings.max_text_field_chars),
            **base,
        ),
        payload_meta,
    )
