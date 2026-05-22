from __future__ import annotations

import json
import re
from typing import Any

FORBIDDEN_KEYS = frozenset(
    {
        "input",
        "output",
        "prompt",
        "completion",
        "messages",
        "content",
        "raw",
        "api_key",
        "secret",
        "token",
    }
)

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "proxy-authorization",
    }
)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def truncate_text(text: str | None, max_chars: int = 500) -> str | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


def strip_forbidden(obj: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in FORBIDDEN_KEYS or any(f in kl for f in ("password", "secret", "api_key")):
                continue
            if kl in ("messages", "values", "checkpoint", "channel_values"):
                continue
            cleaned = strip_forbidden(v, depth + 1)
            if cleaned is not None:
                out[k] = cleaned
        return out
    if isinstance(obj, list):
        return [strip_forbidden(x, depth + 1) for x in obj[:20]]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str) and len(obj) > 200:
            return obj[:200] + "..."
        return obj
    return None


def safe_payload_meta(data: Any, store_payloads: bool, max_payload: int) -> str | None:
    if not store_payloads:
        return None
    cleaned = strip_forbidden(data)
    if cleaned is None:
        return None
    try:
        raw = json.dumps(cleaned, default=str)
    except (TypeError, ValueError):
        return None
    if len(raw) > max_payload:
        raw = raw[: max_payload - 3] + "..."
    return raw


def extract_safe_summary(data: Any, max_chars: int = 500) -> str | None:
    if isinstance(data, str):
        return truncate_text(data, max_chars)
    if isinstance(data, dict):
        for key in ("summary", "message", "title", "status", "name"):
            if key in data and isinstance(data[key], str):
                return truncate_text(data[key], max_chars)
        for key in ("artifact_path", "result_url", "path", "url"):
            if key in data and isinstance(data[key], str):
                return truncate_text(f"{key}={data[key]}", max_chars)
    return None


def extract_node_step(data: Any) -> tuple[str | None, str | None]:
    node = step = None
    if isinstance(data, dict):
        for k in ("node", "langgraph_node", "current_node"):
            if k in data and isinstance(data[k], str):
                node = data[k]
                break
        for k in ("step", "current_step"):
            if k in data and isinstance(data[k], str):
                step = data[k]
                break
        if not node:
            for v in data.values():
                if isinstance(v, dict):
                    n, s = extract_node_step(v)
                    node = node or n
                    step = step or s
    return node, step


def infer_status_from_node(node: str | None, step: str | None) -> str:
    text = f"{node or ''} {step or ''}".lower()
    if any(x in text for x in ("tool", "sandbox", "bash", "execute")):
        return "tool"
    if any(x in text for x in ("subagent", "task_tool", "delegate")):
        return "subagent"
    return "running"


def extract_token_usage(data: Any) -> tuple[int | None, int | None, int | None, str | None]:
    model = None
    inp = out = total = None

    def walk(obj: Any) -> None:
        nonlocal model, inp, out, total
        if isinstance(obj, dict):
            if "model" in obj and isinstance(obj["model"], str):
                model = obj["model"]
            if "model_name" in obj and isinstance(obj["model_name"], str):
                model = obj["model_name"]
            usage = obj.get("usage") or obj.get("token_usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens") or usage.get("prompt_tokens") or inp
                out = usage.get("output_tokens") or usage.get("completion_tokens") or out
                total = usage.get("total_tokens") or total
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj[:30]:
                walk(item)

    walk(data)
    if total is None and inp is not None and out is not None:
        total = int(inp) + int(out)
    return (
        int(inp) if inp is not None else None,
        int(out) if out is not None else None,
        int(total) if total is not None else None,
        model,
    )


def extract_ids(data: Any) -> dict[str, str | None]:
    ids: dict[str, str | None] = {
        "run_id": None,
        "thread_id": None,
        "session_id": None,
        "trace_id": None,
    }
    if isinstance(data, dict):
        for key in ids:
            if key in data and data[key]:
                ids[key] = str(data[key])
        meta = data.get("metadata")
        if isinstance(meta, dict):
            for key in ids:
                if not ids[key] and key in meta and meta[key]:
                    ids[key] = str(meta[key])
    return ids


def extract_artifact_refs(data: Any) -> tuple[str | None, str | None]:
    path = url = None
    if isinstance(data, dict):
        for k, v in data.items():
            kl = k.lower()
            if isinstance(v, str):
                if "artifact" in kl or kl in ("path", "file_path"):
                    path = v if not path else path
                if kl in ("result_url", "url") and ("http" in v or v.startswith("/")):
                    url = v
        for v in data.values():
            if isinstance(v, (dict, list)):
                p, u = extract_artifact_refs(v)
                path = path or p
                url = url or u
    return path, url


_TODO_DONE = frozenset({"completed", "done", "cancelled", "canceled"})


def extract_thread_value_counts(values: Any) -> dict[str, int | None]:
    """Count messages/artifacts/todos from thread state values (no content)."""
    if not isinstance(values, dict):
        return {"messages": None, "artifacts": None, "todos_done": None, "todos_total": None}

    messages = values.get("messages")
    msg_n = len(messages) if isinstance(messages, list) else None

    artifacts = values.get("artifacts")
    art_n: int | None = None
    if isinstance(artifacts, list):
        art_n = len(artifacts)
    elif isinstance(artifacts, dict):
        art_n = len(artifacts)

    todos = values.get("todos")
    todos_done = todos_total = None
    if isinstance(todos, list):
        todos_total = len(todos)
        todos_done = sum(
            1
            for item in todos
            if isinstance(item, dict) and str(item.get("status") or "").lower() in _TODO_DONE
        )

    return {
        "messages": msg_n,
        "artifacts": art_n,
        "todos_done": todos_done,
        "todos_total": todos_total,
    }


def format_thread_value_counts(counts: dict[str, int | None]) -> str:
    parts: list[str] = []
    if counts.get("messages") is not None:
        parts.append(f"msgs {counts['messages']}")
    if counts.get("artifacts") is not None:
        parts.append(f"artifacts {counts['artifacts']}")
    td, tt = counts.get("todos_done"), counts.get("todos_total")
    if tt is not None:
        parts.append(f"todos {td or 0}/{tt}")
    return " · ".join(parts)


def progress_counts_snapshot(counts: dict[str, int | None]) -> str:
    return "|".join(
        str(counts.get(k) if counts.get(k) is not None else "")
        for k in ("messages", "artifacts", "todos_done", "todos_total")
    )


def sum_tokens_from_messages(messages: Any) -> dict[str, int]:
    """Sum usage_metadata from LangGraph messages (no message content)."""
    inp = out = 0
    if not isinstance(messages, list):
        return {"total_tokens": 0, "total_input_tokens": 0, "total_output_tokens": 0}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        um = msg.get("usage_metadata")
        if not isinstance(um, dict):
            continue
        inp += int(um.get("input_tokens") or um.get("prompt_tokens") or 0)
        out += int(um.get("output_tokens") or um.get("completion_tokens") or 0)
    total = inp + out
    return {
        "total_tokens": total,
        "total_input_tokens": inp,
        "total_output_tokens": out,
    }


def progress_counts_from_mapping(data: dict[str, Any]) -> dict[str, int | None]:
    """Extract count fields from get_thread_progress() or similar dicts."""
    return {
        "messages": data.get("messages"),
        "artifacts": data.get("artifacts"),
        "todos_done": data.get("todos_done"),
        "todos_total": data.get("todos_total"),
    }


def parse_progress_counts_snapshot(snapshot: str | None) -> dict[str, int | None]:
    if not snapshot:
        return {}
    parts = snapshot.split("|")
    if len(parts) != 4:
        return {}
    out: dict[str, int | None] = {}
    keys = ("messages", "artifacts", "todos_done", "todos_total")
    for key, raw in zip(keys, parts, strict=True):
        out[key] = int(raw) if raw != "" else None
    return out
