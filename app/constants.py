"""Shared status sets and UI labels."""

from __future__ import annotations

ACTIVE_STATUSES = frozenset({"queued", "running", "tool", "subagent"})
TERMINAL_STATUSES = frozenset({"finished", "failed", "stale"})

TIMELINE_LABELS: dict[str, str] = {
    "run_started": "started",
    "run_progress": "progress",
    "run_finished": "finished",
    "run_failed": "failed",
    "run_stale": "stale",
    "node_started": "node",
    "tool_started": "tool",
    "tool_finished": "tool",
    "subagent_started": "subagent",
    "subagent_finished": "subagent",
    "token_usage": "token",
    "artifact_created": "artifact",
    "unknown": "event",
}
