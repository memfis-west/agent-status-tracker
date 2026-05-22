import pytest

from app.sanitize import (
    extract_thread_value_counts,
    format_thread_value_counts,
    progress_counts_from_mapping,
    progress_counts_snapshot,
    redact_headers,
    safe_payload_meta,
    strip_forbidden,
    sum_tokens_from_messages,
    truncate_text,
)


def test_redact_headers():
    h = redact_headers({"Authorization": "Bearer x", "Cookie": "a=b", "Accept": "json"})
    assert h["Authorization"] == "[REDACTED]"
    assert h["Cookie"] == "[REDACTED]"
    assert h["Accept"] == "json"


def test_truncate():
    assert truncate_text("a" * 600, 500) is not None
    assert len(truncate_text("a" * 600, 500) or "") <= 500


def test_strip_forbidden():
    data = {"messages": [{"content": "secret prompt"}], "status": "ok"}
    cleaned = strip_forbidden(data)
    assert "messages" not in cleaned
    assert cleaned.get("status") == "ok"


def test_store_payloads_false():
    assert safe_payload_meta({"status": "x"}, store_payloads=False, max_payload=4000) is None


def test_extract_thread_value_counts():
    values = {
        "messages": [{"type": "human"}, {"type": "ai"}],
        "artifacts": ["/out/a.md"],
        "todos": [
            {"status": "completed"},
            {"status": "pending"},
        ],
    }
    counts = extract_thread_value_counts(values)
    assert counts == {"messages": 2, "artifacts": 1, "todos_done": 1, "todos_total": 2}
    assert format_thread_value_counts(counts) == "msgs 2 · artifacts 1 · todos 1/2"
    assert progress_counts_snapshot(counts) == "2|1|1|2"
    assert sum_tokens_from_messages(
        [
            {"usage_metadata": {"input_tokens": 100, "output_tokens": 20}},
            {"usage_metadata": {"input_tokens": 50, "output_tokens": 10}},
        ]
    ) == {"total_tokens": 180, "total_input_tokens": 150, "total_output_tokens": 30}

    assert progress_counts_from_mapping({"messages": 2, "artifacts": 1}) == {
        "messages": 2,
        "artifacts": 1,
        "todos_done": None,
        "todos_total": None,
    }
