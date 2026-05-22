from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WatchStartRequest(BaseModel):
    thread_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    stream_mode: list[str] | None = None


class WatchStartResponse(BaseModel):
    run_id: str | None = None
    thread_id: str
    status: str
    dashboard_url: str


class RunFinishRequest(BaseModel):
    summary: str | None = None
    artifact_path: str | None = None
    result_url: str | None = None


class RunFailRequest(BaseModel):
    error: str = ""


class HealthResponse(BaseModel):
    status: str
    db: str
    deerflow_base_url: str
    runs_count: int = 0
    events_count: int = 0
    last_record_at: str | None = None
    last_run_event_at: str | None = None
    last_event_created_at: str | None = None


class NormalizedEvent(BaseModel):
    event_type: str
    run_id: str
    thread_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    source_event: str | None = None
    sse_id: str | None = None
    node: str | None = None
    step: str | None = None
    status: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    artifact_path: str | None = None
    result_url: str | None = None
    message: str | None = None
    error: str | None = None
    created_at: str | None = None  # event time; used as finished_at for terminal events
