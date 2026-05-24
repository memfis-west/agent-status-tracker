# agent-status-tracker

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)

**Lightweight operational monitor for [DeerFlow](https://github.com/bytedance/deer-flow) agent runs** — dashboard on port **8090**, SQLite storage, no Langfuse.

Repository: [github.com/memfis-west/agent-status-tracker](https://github.com/memfis-west/agent-status-tracker)

## What it does

| Area | Description |
|------|-------------|
| **Statuses** | `queued`, `running`, `tool`, `subagent`, `finished`, `failed`, `stale` |
| **UI** | Web dashboard + run detail (Jinja2, no React build) |
| **Watch (SSE)** | Start a run and stream status from DeerFlow |
| **Poll** | Sync runs started in DeerFlow UI (no browser SSE) |
| **Tokens** | Active: live from thread state · Finished: per-run snapshot at completion |
| **Agent output** | Opt-in `last_thinking` + `final_answer` on run detail (see below) |
| **Alerts** | Optional [ntfy](https://ntfy.sh) on finish / fail / stale |
| **Privacy** | No full chat history in DB; agent text fields are sanitized and truncated |

## What it does not do

- Langfuse-style traces, prompt archive, token streams
- Per-message token breakdown (thread-level / run snapshot only)
- Auto-delete when you remove a chat in DeerFlow (use **Sync**)
- Store raw SSE payloads or user prompts by default

## Quick start (Docker)

```bash
git clone git@github.com:memfis-west/agent-status-tracker.git
cd agent-status-tracker
cp .env.example .env
# Edit .env — DeerFlow auth (see below)
docker compose up --build -d
curl -s http://127.0.0.1:8090/api/health | jq .
```

Open **http://127.0.0.1:8090/dashboard** (and **/settings/auth** for DeerFlow login).

Uses **`network_mode: host`** on Linux so the container can reach DeerFlow at `127.0.0.1:2026`.

## Quick start (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data
export TRACKER_DB_PATH=./data/status.db
python -m uvicorn app.main:app --host 0.0.0.0 --port 8090
```

## DeerFlow authentication

| Method | When |
|--------|------|
| **Web UI** | `http://127.0.0.1:8090/settings/auth` — email/password, session in `data/deerflow_session.json` (chmod 600) |
| **Env bootstrap** | `DEERFLOW_LOGIN_EMAIL` + `DEERFLOW_LOGIN_PASSWORD` on container start |
| **Env override** | `DEERFLOW_AUTH_COOKIE` or `DEERFLOW_BEARER_TOKEN` in `.env` |
| **Tracker Basic auth** | `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` — recommended on LAN |

Never commit `.env` or session files.

## Dashboard

| Section | Meaning |
|---------|---------|
| **active** | Runs still in progress |
| **stale** | No tracker events for `STALE_AFTER_SECONDS` (default 10 min) |
| **failed** / **finished** | Terminal runs (latest 30 each) |

Columns: **started** · **updated** or **finished** · status · run · thread · duration · tokens · model.

- **Tokens `· live`** — sum from LangGraph `state.messages` while run is active.
- **Finished tokens** — snapshot when that run completed (not duplicated thread total on every row).

Background **Sync** (or every ~60s in stale loop) imports UI chats and removes runs for deleted DeerFlow threads.

## Run detail: Agent output

On **`/runs/{run_id}`**, card **Agent output** shows two collapsible blocks:

| Block | Source | Default UI |
|-------|--------|------------|
| **Last thinking** | DeerFlow `reasoning_content` on AI messages (same as UI “Thinking”), plus live SSE status fallback (`Node started`, `Tool started`, …) | Collapsed |
| **Final answer** | Last visible AI reply from `state.values.messages` on terminal poll/finish | Expanded |

Long paths and prose wrap inside the block (`overflow-wrap: anywhere`). No raw `messages` array or `STORE_PAYLOADS` content is stored.

## Configuration

Copy [`.env.example`](.env.example). Main variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEERFLOW_BASE_URL` | `http://127.0.0.1:2026` | DeerFlow API |
| `DEERFLOW_DATA_BASE` | — | Host paths on run detail (mount ro in Docker) |
| `STALE_AFTER_SECONDS` | `600` | Mark run stale after silence |
| `STALE_CHECK_INTERVAL_SECONDS` | `60` | Background sync + stale check |
| `DEERFLOW_THREAD_SEARCH_LIMIT` | `50` | Threads per sync |
| `STORE_LAST_THINKING` | `true` | Save sanitized thinking / reasoning text |
| `STORE_FINAL_ANSWER` | `false` | Save final assistant reply at run completion |
| `STORE_AGENT_TEXT` | `false` | Enables both text fields when `true` |
| `MAX_AGENT_TEXT_CHARS` | `4000` | Truncate `last_thinking` / `final_answer` |
| `STORE_PAYLOADS` | `false` | Raw SSE payloads in `events.payload_meta` |
| `NTFY_SERVER` / `NTFY_TOPIC` | empty | Push notifications |

## HTTP API (summary)

| Method | Path | Auth |
|--------|------|------|
| GET | `/health`, `/api/health` | — |
| GET | `/dashboard`, `/runs/{id}` | Basic* |
| GET | `/settings/auth` | Basic* |
| POST | `/watch/start` | Basic* |
| POST | `/watch/thread/{id}/poll` | Basic* |
| POST | `/sync/deerflow` | Basic* |
| GET | `/runs/{id}/agent-output` | Basic* |

\* Optional HTTP Basic when set in `.env`.

<details>
<summary>Full API list</summary>

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/runs` | List runs |
| GET | `/runs/{id}/detail` | Run + events JSON |
| GET | `/runs/{id}/agent-output` | `last_thinking`, `final_answer`, enabled flags |
| POST | `/runs/{id}/finish` | Manual finish |
| POST | `/runs/{id}/fail` | Manual fail |

</details>

### Watch example

```bash
curl -u user:pass -X POST http://127.0.0.1:8090/watch/start \
  -H "Content-Type: application/json" \
  -d '{
    "input": {"messages": [{"role": "user", "content": "Say ok"}]},
    "config": {"recursion_limit": 100, "configurable": {}},
    "stream_mode": ["updates", "custom", "messages-tuple"]
  }'
```

### Agent output example

```bash
# Enable final answer in .env: STORE_FINAL_ANSWER=true
curl -s http://127.0.0.1:8090/runs/{run_id}/agent-output | jq .
curl -X POST http://127.0.0.1:8090/watch/thread/{thread_id}/poll
```

## Host paths (run detail)

When `DEERFLOW_DATA_BASE` is set and mounted read-only in Docker, each run page shows copyable paths:

- `{DEERFLOW_DATA_BASE}/users/{user_id}/agents/{agent}/SOUL.md`
- `.../threads/{thread_id}/user-data`

`user_id` from JWT in session or `DEERFLOW_USER_ID`.

## Project layout

```text
app/
  agent_text.py   Thinking / final-answer extraction (privacy-safe)
  main.py         FastAPI routes
  db.py           SQLite + migrations
  settings.py     Env config
  templates/      dashboard, run_detail (collapsible agent output)
  static/         dashboard.css, refresh.js
watcher/
  sse_client.py   SSE watch + live thinking
  poll_client.py  UI-started run sync + terminal snapshots
  deerflow_client.py
tests/
  test_agent_text.py
scripts/
  check_privacy.py
  verify_agent_output_live.py   Live DeerFlow verification helper
```

## Resource usage

Typical Docker footprint: **~80 MiB RAM**, CPU near idle with short spikes on sync (~60s).

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## Privacy

- `STORE_PAYLOADS=false` by default — no raw event bodies in DB.
- Agent text uses **sanitized** fields only; user/human messages and tool dumps are not copied into `last_thinking`.
- `final_answer` is taken from thread **state** on completion, not from SSE stream chunks.

```bash
python scripts/check_privacy.py --db ./data/status.db --needle "access_token="
```

## Agent output (behavior)

| Mode | `last_thinking` | `final_answer` |
|------|-----------------|----------------|
| **SSE watch** | Live: `reasoning_content` chunks + normalized node/tool status | After run ends: last AI `content` from state |
| **Poll (UI chat)** | On sync: joined `reasoning_content` from state | Terminal only, if `STORE_FINAL_ANSWER=true` |

Set `STORE_FINAL_ANSWER=true` (or `STORE_AGENT_TEXT=true`) to persist final replies. Re-poll a thread to backfill finished runs:

```bash
curl -X POST http://127.0.0.1:8090/watch/thread/{thread_id}/poll
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| DeerFlow 401 | `/settings/auth` or valid cookie/bearer in `.env` |
| Docker cannot reach DeerFlow | `network_mode: host` or correct `DEERFLOW_BASE_URL` |
| Run stuck `running` | `POST /watch/thread/{id}/poll` |
| False **stale** | Increase `STALE_AFTER_SECONDS` |
| Old finished rows show same tokens | Re-poll thread to refresh snapshots |
| Empty **Last thinking** on UI run | Ensure `STORE_LAST_THINKING=true`, then poll thread |
| **Final answer** empty | Set `STORE_FINAL_ANSWER=true`, poll after run is `finished` |

## License

[MIT](LICENSE) — Copyright (c) 2026 memfis-west

## Suggested GitHub topics

`deerflow`, `fastapi`, `langgraph`, `sqlite`, `monitoring`, `agent-ops`, `self-hosted`
