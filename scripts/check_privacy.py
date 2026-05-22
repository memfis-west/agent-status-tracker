#!/usr/bin/env python3
"""Scan SQLite tracker DB for forbidden needle strings."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


TEXT_COLUMNS = {
    "runs": [
        "run_id",
        "thread_id",
        "session_id",
        "trace_id",
        "assistant_id",
        "status",
        "current_step",
        "current_node",
        "last_event_type",
        "model",
        "result_summary",
        "error",
        "artifact_path",
        "result_url",
    ],
    "events": [
        "run_id",
        "thread_id",
        "session_id",
        "event_type",
        "source_event",
        "sse_id",
        "node",
        "step",
        "status",
        "model",
        "artifact_path",
        "result_url",
        "message",
        "error",
        "trace_id",
        "payload_meta",
    ],
}


def scan(db_path: str, needle: str) -> list[str]:
    hits: list[str] = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table, cols in TEXT_COLUMNS.items():
            for col in cols:
                try:
                    q = f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE ?"
                    for row in conn.execute(q, (f"%{needle}%",)):
                        hits.append(f"{table}.rowid={row[0]}.{col}")
                except sqlite3.OperationalError:
                    pass
    finally:
        conn.close()
    return hits


def main() -> int:
    p = argparse.ArgumentParser(description="Privacy needle check for tracker DB")
    p.add_argument("--db", required=True, help="Path to status.db")
    p.add_argument("--needle", required=True, help="Forbidden substring to search")
    args = p.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    hits = scan(args.db, args.needle)
    if hits:
        print("FAIL: needle found in:")
        for h in hits:
            print(f"  {h}")
        return 1
    print("OK: needle not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
