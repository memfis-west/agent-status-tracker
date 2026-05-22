from watcher.sse_client import parse_sse_chunk


def test_multiline_data():
    raw = "event: metadata\nid: 1\ndata: {\"run_id\":\ndata: \"r1\"}\n\n"
    msgs = parse_sse_chunk(raw)
    assert len(msgs) >= 1
    assert msgs[0].event == "metadata"
    assert "run_id" in msgs[0].data


def test_heartbeat_comment():
    raw = ": heartbeat\n\n"
    msgs = parse_sse_chunk(raw)
    assert any(m.is_heartbeat for m in msgs)


def test_end_event():
    raw = "event: end\ndata: {}\n\n"
    msgs = parse_sse_chunk(raw)
    assert msgs[0].event == "end"
