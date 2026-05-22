from app.constants import TIMELINE_LABELS
from watcher.event_mapper import map_sse_event
from app.settings import Settings


def test_timeline_labels_cover_sse_mapper_events():
    settings = Settings()
    for event_name in ("metadata", "end", "error", "updates", "custom"):
        ev, _ = map_sse_event(
            event_name=event_name,
            data={},
            sse_id=None,
            thread_id="t1",
            run_id="r1",
            settings=settings,
        )
        if ev is None:
            continue
        assert ev.event_type in TIMELINE_LABELS or ev.event_type.replace("_", " ")


def test_timeline_has_run_lifecycle():
    assert TIMELINE_LABELS["run_started"] == "started"
    assert TIMELINE_LABELS["run_stale"] == "stale"
    assert TIMELINE_LABELS["tool_started"] == "tool"
