from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.constants import ACTIVE_STATUSES, TERMINAL_STATUSES, TIMELINE_LABELS
from app.db import Database, utc_now
from app.deerflow_credentials import (
    clear_stored_session,
    effective_auth,
    login_with_password,
    save_session_cookie,
    save_stored_auth,
    try_env_bootstrap_login,
    verify_deerflow_auth,
)
from app.deerflow_paths import (
    agent_name_to_folder,
    build_host_paths,
    resolve_user_id,
)
from app.models import (
    HealthResponse,
    RunFailRequest,
    RunFinishRequest,
    WatchStartRequest,
    WatchStartResponse,
)
from app.notifications import notify_run_if_needed
from app.time_utils import parse_utc
from app.sanitize import (
    format_thread_value_counts,
    parse_progress_counts_snapshot,
    progress_counts_from_mapping,
)
from app.settings import Settings, get_settings
from app.stale import stale_loop
from app.sync import sync_deerflow_threads
from watcher.deerflow_client import DeerflowClient
from watcher.poll_client import PollClient
from watcher.sse_client import SSEWatcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic(auto_error=False)

_db: Database | None = None
_stop = asyncio.Event()


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def _check_basic(
    settings: Settings,
    creds: HTTPBasicCredentials | None,
) -> None:
    if not settings.basic_auth_enabled:
        return
    if creds is None:
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})
    ok_user = secrets.compare_digest(creds.username, settings.basic_auth_user)
    ok_pass = secrets.compare_digest(creds.password, settings.basic_auth_password)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


async def require_auth(
    settings: Settings = Depends(get_settings),
    creds: HTTPBasicCredentials | None = Depends(security),
) -> None:
    _check_basic(settings, creds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    settings = get_settings()
    _db = Database(settings.tracker_db_path, settings.schema_path)
    await _db.connect()
    await try_env_bootstrap_login(settings)
    removed = await _db.prune_poll_spam_events()
    if removed:
        logger.info("Removed %d legacy poll spam events", removed)
    task = asyncio.create_task(stale_loop(settings, _db, _stop))
    yield
    _stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await _db.close()


app = FastAPI(title="agent-status-tracker", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


async def _notify_if_needed(run_id: str, status: str) -> None:
    await notify_run_if_needed(get_db(), run_id, status)


async def _run_sse_watch(thread_id: str, body: dict[str, Any]) -> None:
    settings = get_settings()
    db = get_db()
    watcher = SSEWatcher(settings, db)
    try:
        run_id, _ = await watcher.watch_stream(
            thread_id,
            body,
            provisional_run_id=body.get("_provisional_run_id"),
        )
        if run_id:
            run = await db.get_run(run_id)
            if run:
                await _notify_if_needed(run_id, run["status"])
    except PermissionError:
        logger.error("DeerFlow auth failed for SSE watch thread_id=%s", thread_id)
        if body.get("_provisional_run_id"):
            await db.upsert_run(
                run_id=body["_provisional_run_id"],
                thread_id=thread_id,
                status="failed",
                error="DeerFlow authentication required",
                last_event_type="auth_error",
            )
    except Exception as e:
        logger.exception("SSE watch failed: %s", e)


async def _health_payload() -> tuple[HealthResponse, dict[str, Any]]:
    settings = get_settings()
    db = get_db()
    db_ok = "ok" if await db.ping() else "error"
    storage = await db.get_storage_stats() if db_ok == "ok" else {}
    payload = HealthResponse(
        status="ok",
        db=db_ok,
        deerflow_base_url=settings.deerflow_base_url,
        runs_count=int(storage.get("runs_count") or 0),
        events_count=int(storage.get("events_count") or 0),
        last_record_at=storage.get("last_record_at"),
        last_run_event_at=storage.get("last_run_event_at"),
        last_event_created_at=storage.get("last_event_at"),
    )
    return payload, storage


@app.get("/api/health")
async def health_api() -> HealthResponse:
    payload, _ = await _health_payload()
    return payload


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    payload, storage = await _health_payload()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="health.html",
        context={
            "health": payload,
            "db_path": get_settings().tracker_db_path,
            "storage": storage,
        },
    )


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.post("/sync/deerflow")
async def sync_deerflow(_: None = Depends(require_auth)):
    """Drop tracker runs for threads deleted in DeerFlow; remove orphan pending-* rows."""
    settings = get_settings()
    try:
        result = await sync_deerflow_threads(get_db(), DeerflowClient(settings), settings)
    except PermissionError:
        raise HTTPException(401, "DeerFlow authentication required")
    return result


@app.get("/settings/auth", response_class=HTMLResponse)
async def settings_auth_page(
    request: Request,
    msg: str | None = Query(None),
    ok: int | None = Query(None),
    _: None = Depends(require_auth),
):
    auth = await verify_deerflow_auth(get_settings())
    return TEMPLATES.TemplateResponse(
        request=request,
        name="settings_auth.html",
        context={
            "auth": auth,
            "flash": msg,
            "flash_ok": ok == 1,
        },
    )


@app.post("/settings/auth/login")
async def settings_auth_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    _: None = Depends(require_auth),
):
    settings = get_settings()
    try:
        cookie = await login_with_password(settings, email=email, password=password)
        save_session_cookie(settings, cookie, source="login_form")
        auth = await verify_deerflow_auth(settings)
        if not auth.get("ok"):
            raise RuntimeError(auth.get("message") or "verify failed")
    except PermissionError:
        return RedirectResponse(
            "/settings/auth?msg=Неверный+email+или+пароль&ok=0",
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            "/settings/auth?msg=Не+удалось+войти+в+DeerFlow&ok=0",
            status_code=303,
        )
    return RedirectResponse("/settings/auth?msg=Вход+выполнен&ok=1", status_code=303)


@app.post("/settings/auth/cookie")
async def settings_auth_cookie(
    cookie: str = Form(...),
    _: None = Depends(require_auth),
):
    settings = get_settings()
    raw = cookie.strip()
    if "access_token=" not in raw and raw.count(".") >= 2:
        raw = f"access_token={raw}"
    save_session_cookie(settings, raw, source="paste_cookie")
    auth = await verify_deerflow_auth(settings)
    q = "Cookie+сохранён&ok=1" if auth.get("ok") else "Cookie+сохранён,+но+DeerFlow+не+принял&ok=0"
    return RedirectResponse(f"/settings/auth?msg={q}", status_code=303)


@app.post("/settings/auth/bearer")
async def settings_auth_bearer(
    token: str = Form(...),
    _: None = Depends(require_auth),
):
    settings = get_settings()
    raw = token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    save_stored_auth(settings, cookie="", bearer=raw, source="paste_bearer")
    auth = await verify_deerflow_auth(settings)
    q = "Token+сохранён&ok=1" if auth.get("ok") else "Token+сохранён,+но+DeerFlow+не+принял&ok=0"
    return RedirectResponse(f"/settings/auth?msg={q}", status_code=303)


@app.post("/settings/auth/logout")
async def settings_auth_logout(_: None = Depends(require_auth)):
    clear_stored_session(get_settings())
    return RedirectResponse("/settings/auth?msg=Сессия+удалена&ok=1", status_code=303)


@app.get("/api/auth/status")
async def auth_status(_: None = Depends(require_auth)):
    return await verify_deerflow_auth(get_settings())


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    synced: int = Query(0),
    _: None = Depends(require_auth),
):
    settings = get_settings()
    auth = await verify_deerflow_auth(settings)
    db = get_db()
    if synced:
        try:
            await sync_deerflow_threads(db, DeerflowClient(settings), settings)
        except PermissionError:
            auth = await verify_deerflow_auth(settings)
    all_runs = await db.list_runs(limit=200)
    client = DeerflowClient(settings)
    token_threads = {
        str(r["thread_id"])
        for r in all_runs
        if r.get("thread_id")
        and r["status"] in ACTIVE_STATUSES | {"finished", "failed", "stale"}
    }
    thread_tokens = await _load_thread_token_map(client, token_threads)

    def _rows(statuses: set[str], time_field: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        items = [r for r in all_runs if r["status"] in statuses]
        enriched = [
            _enrich_dashboard_row(r, time_field=time_field, thread_tokens=thread_tokens)
            for r in items
        ]
        if time_field == "finished_at":
            enriched.sort(key=_terminal_sort_key, reverse=True)
        else:
            enriched.sort(key=lambda r: r.get("last_event_at") or "", reverse=True)
        return enriched[:limit] if limit else enriched

    active_rows = _rows(ACTIVE_STATUSES, "last_event_at")
    stale_rows = _rows({"stale"}, "last_event_at")
    failed_rows = _rows({"failed"}, "finished_at", limit=30)
    finished_rows = _rows({"finished"}, "finished_at", limit=30)
    groups = [
        {"label": "active", "second_time_col": "updated", "rows": active_rows},
        {"label": "stale", "second_time_col": "updated", "rows": stale_rows},
        {"label": "failed", "second_time_col": "finished", "rows": failed_rows},
        {"label": "finished", "second_time_col": "finished", "rows": finished_rows},
    ]
    db_ok = await db.ping()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"groups": groups, "synced": synced, "auth": auth, "db_ok": db_ok},
    )


def _short_id(value: str, n: int = 8) -> str:
    if not value or len(value) <= n:
        return value or ""
    return f"{value[:n]}…"


def _format_duration_sec(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def _run_duration_seconds(run: dict[str, Any]) -> int | None:
    sec = run.get("duration_sec")
    if sec is not None and sec != "":
        return int(sec)
    started = parse_utc(run.get("started_at") or run.get("created_at"))
    if not started:
        return None
    if run.get("status") in ACTIVE_STATUSES:
        end = datetime.now(timezone.utc)
    else:
        end = parse_utc(run.get("finished_at") or run.get("last_event_at"))
    if not end:
        return None
    return max(0, int((end - started).total_seconds()))


def _duration_display(run: dict[str, Any]) -> str:
    sec = _run_duration_seconds(run)
    return _format_duration_sec(sec) if sec is not None else "—"


def _terminal_sort_key(run: dict[str, Any]) -> str:
    return str(run.get("finished_at") or run.get("last_event_at") or run.get("updated_at") or "")


def _format_token_count(n: int | None) -> str:
    if not n:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _enrich_dashboard_row(
    run: dict[str, Any],
    *,
    time_field: str,
    thread_tokens: dict[str, dict[str, int | str]] | None = None,
) -> dict[str, Any]:
    row = dict(run)
    row["display_started"] = run.get("started_at") or run.get("created_at") or "—"
    if time_field == "finished_at":
        row["display_second_time"] = (
            run.get("finished_at") or run.get("last_event_at") or "—"
        )
    else:
        row["display_second_time"] = run.get("last_event_at") or "—"
    sec = _run_duration_seconds(run)
    row["display_duration"] = _format_duration_sec(sec) if sec is not None else "—"
    tid = str(run.get("thread_id") or "")
    usage = (thread_tokens or {}).get(tid) or {}
    run_tokens = int(run.get("total_tokens") or 0)
    live_total = int(usage.get("state_tokens") or usage.get("total_tokens") or 0)

    if run.get("status") in TERMINAL_STATUSES and run_tokens > 0:
        total = run_tokens
        live_marker = False
    elif run.get("status") in ACTIVE_STATUSES:
        total = live_total or run_tokens
        live_marker = usage.get("source") == "state" and live_total > 0
    else:
        total = run_tokens or live_total
        live_marker = False

    if total:
        row["display_tokens"] = _format_token_count(total)
        if live_marker:
            row["display_tokens"] += " · live"
    elif run.get("status") in ACTIVE_STATUSES:
        row["display_tokens"] = "pending"
    else:
        row["display_tokens"] = "—"
    return row


async def _load_thread_token_map(
    client: DeerflowClient,
    thread_ids: set[str],
) -> dict[str, dict[str, int | str]]:
    cache: dict[str, dict[str, int | str]] = {}

    async def _one(tid: str) -> None:
        try:
            usage = await client.get_thread_token_usage(tid, prefer_state=True)
            if usage:
                cache[tid] = usage
        except Exception:
            pass

    if thread_ids:
        await asyncio.gather(*[_one(tid) for tid in thread_ids])
    return cache


def _display_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        if e.get("event_type") == "run_progress" and not (
            e.get("node") or e.get("step") or e.get("error") or e.get("message")
        ):
            continue
        out.append(e)
    return out[-20:]


def _short_time(ts: str | None) -> str:
    if not ts:
        return "—"
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:8].rstrip("Z")
    return s[-8:] if len(s) >= 8 else s


def _timeline_rows(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for e in events:
        et = str(e.get("event_type") or "")
        label = TIMELINE_LABELS.get(et, et.replace("_", " ")[:12] or "event")
        parts: list[str] = []
        msg = (e.get("message") or "").strip()
        if msg:
            parts.append(msg)
        if e.get("node"):
            parts.append(f"node={e['node']}")
        if e.get("step") and (not msg or str(e["step"]) not in msg):
            parts.append(f"step {e['step']}")
        if e.get("model") and (not msg or str(e["model"]) not in msg):
            parts.append(str(e["model"]))
        if e.get("status") and et not in (
            "run_started",
            "run_finished",
            "run_failed",
            "run_progress",
        ):
            parts.append(str(e["status"]))
        if e.get("error"):
            parts.append(str(e["error"])[:200])
        detail = " · ".join(parts) if parts else (str(e.get("status") or "") or "—")
        rows.append(
            {
                "time": _short_time(e.get("created_at")),
                "label": label,
                "detail": detail,
                "css": f"tl-{label}",
            }
        )
    return rows


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(
    run_id: str,
    request: Request,
    _: None = Depends(require_auth),
):
    db = get_db()
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await db.backfill_terminal_fields(run_id)
    run = await db.get_run(run_id) or run
    events = _display_events(await db.list_events(run_id, limit=100))
    timeline = _timeline_rows(events)

    settings = get_settings()
    client = DeerflowClient(settings)
    counts = parse_progress_counts_snapshot(run.get("progress_snapshot"))
    progress: dict[str, Any] = {}
    if run.get("thread_id"):
        try:
            progress = await client.get_thread_progress(run["thread_id"])
            if run.get("status") in ACTIVE_STATUSES:
                counts = progress_counts_from_mapping(progress)
        except Exception:
            pass
    thread_counts = format_thread_value_counts(counts)

    cookie, bearer = effective_auth(settings)
    user_id = run.get("deerflow_user_id") or resolve_user_id(
        env_user_id=settings.deerflow_user_id,
        auth_cookie=cookie,
        bearer_token=bearer,
    )
    agent_folder = run.get("agent_folder") or agent_name_to_folder(
        str(progress["agent_name"]) if progress.get("agent_name") else None
    )

    host_paths: list[dict[str, str | bool]] = []
    if user_id:
        raw = build_host_paths(
            data_base=settings.deerflow_data_base,
            user_id=user_id,
            thread_id=run["thread_id"],
            agent_folder=agent_folder,
        )
        for label, key in (("SOUL.md", "soul_md"), ("user-data", "user_data"), ("thread dir", "thread_dir")):
            p = raw.get(key)
            if p:
                host_paths.append(
                    {"label": label, "path": p, "exists": Path(p).exists()}
                )
        if raw.get("agent_dir"):
            host_paths.append(
                {
                    "label": "agent dir",
                    "path": raw["agent_dir"],
                    "exists": Path(raw["agent_dir"]).exists(),
                }
            )

    return TEMPLATES.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "run": run,
            "timeline": timeline,
            "thread_counts": thread_counts,
            "short_run_id": _short_id(run["run_id"], 12),
            "short_thread_id": _short_id(run["thread_id"], 12),
            "duration_display": _duration_display(run),
            "display_tokens": _format_token_count(run.get("total_tokens") or None),
            "host_paths": host_paths,
            "agent_display": agent_folder or run.get("assistant_id") or "—",
        },
    )


@app.get("/api/runs")
async def list_runs_api(
    status: str | None = None,
    thread_id: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_auth),
):
    return await get_db().list_runs(status=status, thread_id=thread_id, limit=limit, offset=offset)


@app.get("/runs/{run_id}/detail")
async def get_run_json(run_id: str, _: None = Depends(require_auth)):
    db = get_db()
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    events = await db.list_events(run_id, limit=100)
    return {"run": run, "events": events}


@app.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    limit: int = Query(100, le=500),
    _: None = Depends(require_auth),
):
    db = get_db()
    if not await db.get_run(run_id):
        raise HTTPException(404, "Run not found")
    return await db.list_events(run_id, limit=limit)


@app.post("/watch/start", response_model=WatchStartResponse)
async def watch_start(
    req: WatchStartRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_auth),
):
    settings = get_settings()
    db = get_db()
    client = DeerflowClient(settings)

    thread_id = req.thread_id
    if not thread_id:
        try:
            thread_id = await client.create_thread()
        except PermissionError:
            raise HTTPException(401, "DeerFlow authentication required")
        except Exception as e:
            raise HTTPException(502, f"Failed to create thread: {e}")

    stream_modes = req.stream_mode or settings.stream_modes_list
    body: dict[str, Any] = {
        "input": req.input,
        "config": req.config or {"recursion_limit": 100, "configurable": {}},
        "stream_mode": stream_modes,
    }

    provisional = f"pending-{thread_id[:12]}"
    await db.upsert_run(
        run_id=provisional,
        thread_id=thread_id,
        status="queued",
        last_event_type="watch_queued",
    )
    body["_provisional_run_id"] = provisional

    background_tasks.add_task(_run_sse_watch, thread_id, body)

    return WatchStartResponse(
        run_id=None,
        thread_id=thread_id,
        status="queued",
        dashboard_url=f"/runs/{provisional}",
    )


@app.post("/watch/thread/{thread_id}/poll")
async def watch_poll(thread_id: str, _: None = Depends(require_auth)):
    settings = get_settings()
    try:
        updated = await PollClient(settings, get_db()).poll_thread(thread_id)
    except PermissionError:
        raise HTTPException(401, "DeerFlow authentication required")
    return {"thread_id": thread_id, "updated_run_ids": updated}


@app.post("/runs/{run_id}/finish")
async def manual_finish(run_id: str, req: RunFinishRequest, _: None = Depends(require_auth)):
    db = get_db()
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await db.upsert_run(
        run_id=run_id,
        thread_id=run["thread_id"],
        status="finished",
        result_summary=req.summary,
        artifact_path=req.artifact_path,
        result_url=req.result_url,
        last_event_type="manual_finish",
        finished_at=utc_now(),
    )
    await _notify_if_needed(run_id, "finished")
    return {"run_id": run_id, "status": "finished"}


@app.post("/runs/{run_id}/fail")
async def manual_fail(run_id: str, req: RunFailRequest, _: None = Depends(require_auth)):
    db = get_db()
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await db.upsert_run(
        run_id=run_id,
        thread_id=run["thread_id"],
        status="failed",
        error=req.error,
        last_event_type="manual_fail",
    )
    await _notify_if_needed(run_id, "failed")
    return {"run_id": run_id, "status": "failed"}


def run_server() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.tracker_host,
        port=s.tracker_port,
        reload=False,
    )


if __name__ == "__main__":
    run_server()
