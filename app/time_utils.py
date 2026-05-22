"""UTC timestamp parsing shared by API and poll."""

from __future__ import annotations

from datetime import datetime


def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        if "+" not in s[10:]:
            s += "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None
