from watcher.poll_client import _map_run_status


def test_map_interrupted_as_failed():
    assert _map_run_status("interrupted") == "failed"


def test_map_success_as_finished():
    assert _map_run_status("success") == "finished"


def test_map_unknown_not_running():
    assert _map_run_status("weird") is None
