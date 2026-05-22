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
| **Alerts** | Optional [ntfy](https://ntfy.sh) on finish / fail / stale |
| **Privacy** | No prompts/completions in DB by default |

## What it does not do

- Langfuse-style traces, prompt archive, token streams
- Per-message token breakdown (thread-level / run snapshot only)
- Auto-delete when you remove a chat in DeerFlow (use **Sync**)

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

## Configuration

Copy [`.env.example`](.env.example). Main variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEERFLOW_BASE_URL` | `http://127.0.0.1:2026` | DeerFlow API |
| `DEERFLOW_DATA_BASE` | — | Host paths on run detail (mount ro in Docker) |
| `STALE_AFTER_SECONDS` | `600` | Mark run stale after silence |
| `STALE_CHECK_INTERVAL_SECONDS` | `60` | Background sync + stale check |
| `DEERFLOW_THREAD_SEARCH_LIMIT` | `50` | Threads per sync |
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

\* Optional HTTP Basic when set in `.env`.

<details>
<summary>Full API list</summary>

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/runs` | List runs |
| GET | `/runs/{id}/detail` | Run + events JSON |
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

## Host paths (run detail)

When `DEERFLOW_DATA_BASE` is set and mounted read-only in Docker, each run page shows copyable paths:

- `{DEERFLOW_DATA_BASE}/users/{user_id}/agents/{agent}/SOUL.md`
- `.../threads/{thread_id}/user-data`

`user_id` from JWT in session or `DEERFLOW_USER_ID`.

## Project layout

```text
app/           FastAPI, SQLite, templates, static CSS/JS
watcher/       DeerFlow client, SSE watch, poll sync
tests/         pytest
scripts/       check_privacy.py
```

## Resource usage

Typical Docker footprint: **~80 MiB RAM**, CPU near idle with short spikes on sync (~60s).

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## Privacy check

```bash
python scripts/check_privacy.py --db ./data/status.db --needle "phrase-from-prompt"
```

`STORE_PAYLOADS=false` by default.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| DeerFlow 401 | `/settings/auth` or valid cookie/bearer in `.env` |
| Docker cannot reach DeerFlow | `network_mode: host` or correct `DEERFLOW_BASE_URL` |
| Run stuck `running` | `POST /watch/thread/{id}/poll` |
| False **stale** | Increase `STALE_AFTER_SECONDS` |
| Old finished rows show same tokens | Re-poll thread to refresh snapshots |

## License

[MIT](LICENSE) — Copyright (c) 2026 memfis-west

## Suggested GitHub topics

`deerflow`, `fastapi`, `langgraph`, `sqlite`, `monitoring`, `agent-ops`, `self-hosted`
