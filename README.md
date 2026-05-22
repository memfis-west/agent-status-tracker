# agent-status-tracker

Lightweight operational monitor for **DeerFlow** agent runs (port **8090**). Not Langfuse — no prompt archive, no full trace UI.

## Features

| Area | What you get |
|------|----------------|
| Status | `queued`, `running`, `tool`, `subagent`, `finished`, `failed`, `stale` |
| UI | Dashboard + run detail (Jinja2, dark theme) |
| Mode A | `POST /watch/start` — create/watch run via SSE |
| Mode B | Poll DeerFlow threads (UI chats, backfill) |
| Tokens | Active: live from thread state · Finished: per-run snapshot at completion |
| Alerts | Optional ntfy on finish / fail / stale |
| Storage | SQLite (`data/status.db`), privacy-safe events |

## Requirements

- Python 3.12+ or Docker
- DeerFlow at `DEERFLOW_BASE_URL` (default `http://127.0.0.1:2026`)
- DeerFlow auth: cookie, bearer, or email/password via `/settings/auth`

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env — at minimum DeerFlow auth
docker compose up --build -d
curl -s http://127.0.0.1:8090/api/health | jq .
open http://127.0.0.1:8090/dashboard
```

Uses **`network_mode: host`** so the container can reach DeerFlow on `127.0.0.1:2026` (Linux).

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

## Project layout

```text
app/
  main.py              # FastAPI routes, dashboard
  db.py                # SQLite
  constants.py         # status sets, timeline labels
  notifications.py     # ntfy dedupe
  stale.py             # background stale + sync loop
  sync.py              # import/prune vs DeerFlow
  templates/           # dashboard, run detail, health, auth
  static/              # dashboard.css, refresh.js
watcher/
  deerflow_client.py   # DeerFlow HTTP API
  poll_client.py       # passive sync
  sse_client.py        # live watch
  event_mapper.py      # SSE → normalized events
tests/
scripts/check_privacy.py
```

## Dashboard sections

| Section | Runs | Columns |
|---------|------|---------|
| **active** | `queued`, `running`, `tool`, `subagent` | started · **updated** · status · run · thread · duration · tokens · model |
| **stale** | tracker marked inactive (see below) | started · **updated** · … |
| **failed** | terminal fail | started · **finished** · … |
| **finished** | terminal ok | started · **finished** · … |

**Tokens:** `· live` on active = sum from LangGraph `state.messages`. Finished rows use a **snapshot** taken when that run completed (same method), not a shared thread total on every row.

## Stale logic

A run becomes **`stale`** when:

1. Tracker still has status in `queued` / `running` / `tool` / `subagent`, and  
2. `last_event_at` is older than `STALE_AFTER_SECONDS` (default **600**).

Background loop (`stale_loop`) every `STALE_CHECK_INTERVAL_SECONDS` (default **60**):

1. `sync_deerflow_threads` (import + prune deleted threads)  
2. `mark_stale_runs`  
3. ntfy for new stale runs (if configured)

Stale is **tracker silence**, not necessarily DeerFlow failure. Long tools without poll/SSE updates can false-positive — increase `STALE_AFTER_SECONDS`.

Next poll from DeerFlow can move the run back to `running` / `finished`.

## Sync with DeerFlow

Tracker DB is **not** auto-deleted when you delete a chat in DeerFlow UI.

- Dashboard → **Sync**, or `POST /sync/deerflow`  
- Background: same sync inside stale loop (~every 60s)  
- Per thread: `POST /watch/thread/{thread_id}/poll`

Sync discovers threads via `POST /api/threads/search` (limit `DEERFLOW_THREAD_SEARCH_LIMIT`, default 50), polls each, removes runs for 404 threads.

## HTTP API

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | — | HTML status + DB stats |
| GET | `/api/health` | — | JSON |
| GET | `/dashboard` | Basic* | Main UI |
| GET | `/runs/{id}` | Basic* | Run detail |
| GET | `/api/runs` | Basic* | List runs |
| GET | `/runs/{id}/detail` | Basic* | Run + events JSON |
| POST | `/watch/start` | Basic* | Start SSE watch |
| POST | `/watch/thread/{id}/poll` | Basic* | Poll one thread |
| POST | `/sync/deerflow` | Basic* | Import + prune |
| POST | `/runs/{id}/finish` | Basic* | Manual finish |
| POST | `/runs/{id}/fail` | Basic* | Manual fail |
| GET | `/settings/auth` | Basic* | DeerFlow login UI |

\* Basic = optional `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` when set in `.env`.

### Watch start example

```bash
curl -u user:pass -X POST http://127.0.0.1:8090/watch/start \
  -H "Content-Type: application/json" \
  -d '{
    "input": {"messages": [{"role": "user", "content": "Say ok"}]},
    "config": {"recursion_limit": 100, "configurable": {}},
    "stream_mode": ["updates", "custom", "messages-tuple"]
  }'
```

Response `status` is **`queued`** until SSE assigns the real `run_id`. `dashboard_url` may point at `pending-{thread_prefix}`.

## ntfy

Set `NTFY_SERVER` and `NTFY_TOPIC`. Notifications (no prompt text):

| Status | When |
|--------|------|
| finished | SSE end, manual finish |
| failed | SSE error, manual fail |
| stale | stale loop |

Poll-only finish does **not** send ntfy unless you add it later.

## DeerFlow auth

1. **Recommended:** `http://127.0.0.1:8090/settings/auth` — email/password → `data/deerflow_session.json` (mode 600)  
2. `DEERFLOW_LOGIN_EMAIL` + `DEERFLOW_LOGIN_PASSWORD` in `.env` — bootstrap on start  
3. `DEERFLOW_AUTH_COOKIE` / `DEERFLOW_BEARER_TOKEN` in `.env`  
4. Enable **Basic auth** on the tracker if exposed on LAN  

## Configuration (`.env`)

See `.env.example`. Important keys:

| Variable | Default | Purpose |
|----------|---------|---------|
| `STALE_AFTER_SECONDS` | 600 | Inactivity → stale |
| `STALE_CHECK_INTERVAL_SECONDS` | 60 | Background loop period |
| `DEERFLOW_THREAD_SEARCH_LIMIT` | 50 | Threads per sync |
| `DEERFLOW_DATA_BASE` | — | Host paths on run detail (ro-mount in Docker) |
| `TRACKER_STREAM_MODES` | updates,custom,messages-tuple | SSE modes (`tools` stripped) |

`TRACKER_PORT` affects local `python -m app.main` only; Docker CMD uses port **8090** fixed.

## Resource usage (typical)

~**80 MiB** RAM, CPU ~0.2% idle, short spikes during sync/poll. SQLite grows with events; manual retention:

```sql
DELETE FROM events WHERE created_at < datetime('now', '-30 days');
DELETE FROM runs WHERE status IN ('finished', 'failed')
  AND updated_at < datetime('now', '-30 days');
```

## Privacy

```bash
python scripts/check_privacy.py --db ./data/status.db --needle "secret phrase"
```

`STORE_PAYLOADS=false` by default — events store metadata only.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## Troubleshooting

| Issue | Action |
|-------|--------|
| 401 DeerFlow | `/settings/auth` or `.env` cookie/bearer |
| Docker cannot reach DeerFlow | `network_mode: host` or fix `DEERFLOW_BASE_URL` |
| Run stuck `running` | Poll thread; check SSE auth |
| False stale | Increase `STALE_AFTER_SECONDS` |
| Same tokens on old finished rows | Re-poll thread to refresh snapshots |
| Health page | `/health` (HTML) · `/api/health` (JSON) |

## What this is not

- Langfuse / full observability  
- Per-message token UI (thread-level / snapshot only)  
- Automatic GDPR purge  
