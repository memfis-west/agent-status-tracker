-- Lightweight agent-status-tracker schema (SQLite)
-- Primary key: run_id. Grouping: thread_id (DeerFlow chat).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    session_id      TEXT,
    trace_id        TEXT,
    assistant_id    TEXT,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','tool','subagent','finished','failed','stale')),
    current_step    TEXT,
    current_node    TEXT,
    last_event_type TEXT,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    result_summary  TEXT,
    error           TEXT,
    artifact_path   TEXT,
    result_url      TEXT,
    started_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_event_at   TEXT NOT NULL,
    finished_at     TEXT,
    duration_sec    INTEGER,
    deerflow_user_id TEXT,
    agent_folder    TEXT,
    progress_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_last_event ON runs(last_event_at);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL,
    session_id      TEXT,
    event_type      TEXT NOT NULL,
    source_event    TEXT,
    sse_id          TEXT,
    node            TEXT,
    step            TEXT,
    status          TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    total_tokens    INTEGER,
    artifact_path   TEXT,
    result_url      TEXT,
    message         TEXT,
    error           TEXT,
    trace_id        TEXT,
    payload_meta    TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL DEFAULT 'ntfy',
    reason          TEXT NOT NULL,
    sent_at         TEXT,
    dedupe_key      TEXT UNIQUE,
    success         INTEGER NOT NULL DEFAULT 0
);
