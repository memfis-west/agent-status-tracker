#!/usr/bin/env python3
"""Live verification of agent output fields on DeerFlow + tracker."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "status.db"
ENV_PATH = ROOT / ".env"
BASE = "http://127.0.0.1:8090"

# UI-started thread (poll_sync): finished run with known state-backed answer
UI_THREAD = "b70f7a5b-172e-4696-a564-4b3709752f2d"
UI_RUN = "2fbabaf9-cc6f-45f9-870c-232c7b847ab9"

PRIVACY_NEEDLES = [
    "access_token=",
    "csrf_token=",
    "Bearer eyJ",
    "-----BEGIN",
    '"messages":',
    "password=",
]


def http_json(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def db_row(run_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def clear_final(run_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE runs SET final_answer = NULL, final_answer_at = NULL WHERE run_id = ?",
        (run_id,),
    )
    conn.commit()
    conn.close()


def set_env_flag(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pat.search(text):
        text = pat.sub(line, text)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def restart_tracker() -> None:
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    for _ in range(30):
        try:
            http_json("GET", "/api/health", timeout=2)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("tracker did not become healthy")


def privacy_scan() -> list[str]:
    hits: list[str] = []
    conn = sqlite3.connect(DB_PATH)
    text_cols = [
        ("runs", c)
        for c in (
            "result_summary",
            "error",
            "last_thinking",
            "final_answer",
            "progress_snapshot",
        )
    ] + [
        ("events", c)
        for c in ("message", "error", "payload_meta")
    ]
    for table, col in text_cols:
        try:
            for row in conn.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL"):
                val = str(row[1] or "")
                for needle in PRIVACY_NEEDLES:
                    if needle.lower() in val.lower():
                        hits.append(f"{table}.rowid={row[0]}.{col} contains {needle!r}")
        except sqlite3.OperationalError:
            pass
    conn.close()
    store_payloads = False
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("STORE_PAYLOADS="):
            store_payloads = line.split("=", 1)[1].strip().lower() in ("1", "true", "yes")
    if store_payloads:
        hits.append("STORE_PAYLOADS is enabled in .env")
    return hits


async def state_final_answer(thread_id: str) -> str | None:
    sys.path.insert(0, str(ROOT))
    from app.agent_text import extract_final_answer_from_state
    from app.settings import get_settings
    from watcher.deerflow_client import DeerflowClient

    client = DeerflowClient(get_settings())
    state = await client.get_thread_state(thread_id)
    return extract_final_answer_from_state(state)


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def main() -> int:
    results: list[bool] = []

    print("\n=== 1. STORE_FINAL_ANSWER=false (UI-started poll) ===")
    set_env_flag("STORE_FINAL_ANSWER", "false")
    set_env_flag("STORE_LAST_THINKING", "true")
    restart_tracker()
    clear_final(UI_RUN)
    http_json("POST", f"/watch/thread/{UI_THREAD}/poll")
    api = http_json("GET", f"/runs/{UI_RUN}/agent-output")
    row = db_row(UI_RUN)
    t1 = (
        api.get("store_final_answer_enabled") is False
        and row.get("final_answer") is None
        and api.get("final_answer") is None
    )
    if t1:
        ok("final_answer not saved; API store_final_answer_enabled=false; DB NULL")
    else:
        fail(
            f"enabled={api.get('store_final_answer_enabled')} "
            f"db_final={row.get('final_answer') is not None}"
        )
    results.append(t1)

    print("\n=== 2. STORE_FINAL_ANSWER=true (terminal state, UI-started) ===")
    set_env_flag("STORE_FINAL_ANSWER", "true")
    restart_tracker()
    clear_final(UI_RUN)
    http_json("POST", f"/watch/thread/{UI_THREAD}/poll")
    row = db_row(UI_RUN)
    db_ans = row.get("final_answer") or ""
    state_ans = asyncio.run(state_final_answer(UI_THREAD)) or ""
    t2a = len(db_ans) > 100
    t2b = len(state_ans) > 100 and db_ans == state_ans
    t2c = len(db_ans) > 200  # not a tiny SSE chunk
    if t2a and t2b:
        ok(f"final_answer len={len(db_ans)} matches state.values.messages (full answer)")
    else:
        fail(f"db_len={len(db_ans)} state_len={len(state_ans)} match={db_ans[:80]==state_ans[:80]}")
    if t2c:
        ok("answer length indicates full message, not stream chunk")
    else:
        fail(f"answer too short ({len(db_ans)} chars)")
    results.extend([t2a and t2b, t2c])

    print("\n=== 3. STORE_LAST_THINKING=true (tracker SSE watch) ===")
    set_env_flag("STORE_LAST_THINKING", "true")
    restart_tracker()
    started = http_json(
        "POST",
        "/watch/start",
        {
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": "Say TRACKER_THINKING_SMOKE then one short line about your step.",
                    }
                ]
            },
            "config": {"recursion_limit": 50, "configurable": {}},
        },
        timeout=15,
    )
    thread_id = started["thread_id"]
    print(f"  SSE thread_id={thread_id}")
    run_id: str | None = None
    saw_thinking = False
    thinking_too_long = False
    for i in range(90):
        time.sleep(2)
        runs = http_json("GET", f"/api/runs?thread_id={thread_id}&limit=5")
        for r in runs:
            rid = r.get("run_id") or ""
            if rid.startswith("pending-"):
                continue
            run_id = rid
            lt = r.get("last_thinking") or ""
            fa = r.get("final_answer") or ""
            if lt:
                saw_thinking = True
                if len(lt) > 1000 and not re.search(
                    r"\b(thinking|todo|tool|progress|step|node|update|subagent|artifact)\b",
                    lt,
                    re.I,
                ):
                    thinking_too_long = True
            if r.get("status") in ("finished", "failed"):
                break
        if run_id and runs and runs[0].get("status") in ("finished", "failed"):
            break
    if not run_id:
        fail("no run_id resolved from SSE watch")
        results.append(False)
    else:
        row = db_row(run_id)
        lt = row.get("last_thinking") or ""
        fa = row.get("final_answer") or ""
        t3a = saw_thinking or bool(lt)
        t3b = not thinking_too_long
        if t3a:
            ok(f"SSE run {run_id[:8]}… last_thinking captured (len={len(lt)})")
        else:
            fail("no last_thinking during SSE run")
        if t3b:
            ok("long AI answer not stored as last_thinking (heuristic)")
        else:
            fail(f"last_thinking looks like long answer (len={len(lt)})")
        if fa and lt and len(fa) > 500 and lt == fa:
            fail("last_thinking equals long final_answer")
            results.append(False)
        else:
            ok("last_thinking distinct from final_answer")
            results.append(t3a and t3b)

    print("\n=== 4. Privacy (sqlite text columns) ===")
    hits = privacy_scan()
    t4 = len(hits) == 0
    if t4:
        ok("no forbidden needles in DB text columns; STORE_PAYLOADS off")
    else:
        for h in hits[:10]:
            fail(h)
    results.append(t4)

    print("\n=== Summary ===")
    passed = sum(1 for x in results if x)
    total = len(results)
    print(f"{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
