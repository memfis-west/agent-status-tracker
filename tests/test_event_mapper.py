from app.settings import Settings
from watcher.event_mapper import map_sse_event


def test_metadata_run_started():
    settings = Settings(store_payloads=False)
    ev, meta = map_sse_event(
        event_name="metadata",
        data={"run_id": "r1", "thread_id": "t1"},
        sse_id="1",
        thread_id="t1",
        run_id=None,
        settings=settings,
    )
    assert ev is not None
    assert ev.event_type == "run_started"
    assert ev.run_id == "r1"
    assert meta is None


def test_end_finished():
    settings = Settings(store_payloads=False)
    ev, _ = map_sse_event(
        event_name="end",
        data={},
        sse_id=None,
        thread_id="t1",
        run_id="r1",
        settings=settings,
    )
    assert ev.event_type == "run_finished"
    assert ev.status == "finished"


def test_error_failed():
    settings = Settings(store_payloads=False)
    ev, _ = map_sse_event(
        event_name="error",
        data={"error": "boom"},
        sse_id=None,
        thread_id="t1",
        run_id="r1",
        settings=settings,
    )
    assert ev.event_type == "run_failed"
    assert "boom" in (ev.error or "")
