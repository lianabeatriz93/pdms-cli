"""What pdms ps and pdms ui say about a running service, from whether it answers and what its log shows."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from pdms_cli import instances

STARTED = "INFO:     Started server process [42]\nINFO:     Waiting for application startup.\n" \
          "INFO:     Application startup complete.\n"
REQUESTS = 'INFO:     127.0.0.1:5000 - "GET /leads HTTP/1.1" 200 OK\n'
RELOAD = "WARNING:  WatchFiles detected changes in 'main.py'. Reloading...\nINFO:     Shutting down\n" \
         "INFO:     Finished server process [42]\n"
BROKEN = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'nope'\n"


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A service that is alive but does not answer (``responds`` is False), with the log the test writes."""
    monkeypatch.setattr(instances, "responds", lambda *args, **kwargs: False)
    log = tmp_path / "lead-tp-list@8080.log"
    inst = instances.Instance(
        key="lead-tp-list@8080", pid=os.getpid(), service="/x/lead-tp-list", host="0.0.0.0", port=8080, user="u",
        db="d", reload=True, log=str(log), started_at=datetime.now().isoformat(),
    )

    def state(text: str) -> str:
        log.write_text(text, encoding="utf-8")
        return instances.health(inst).state

    return state


def test_a_started_service_that_does_not_answer_is_busy_not_starting(service):
    assert service("# pdms start\n") == "starting"
    assert service("# pdms start\n" + STARTED + REQUESTS * 3) == "busy"


def test_a_request_that_failed_does_not_make_it_look_broken(service):
    assert service("# pdms start\n" + STARTED + REQUESTS + "ERROR:    Exception in ASGI application\n" + BROKEN) == "busy"


def test_after_a_reload_it_is_starting_again_until_the_new_process_starts(service):
    assert service("# pdms start\n" + STARTED + RELOAD) == "starting"
    assert service("# pdms start\n" + STARTED + RELOAD + BROKEN) == "error"
    assert service("# pdms start\n" + STARTED + RELOAD + BROKEN + STARTED) == "busy"


def test_a_long_log_still_finds_when_it_started(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("# pdms start\n" + STARTED + REQUESTS * 20_000, encoding="utf-8")  # ~1 MB, many read chunks
    assert instances.serving(str(log))
    assert instances.last_marker(str(log), ("Reloading...",)) == ""
    assert not instances.serving(str(tmp_path / "missing.log"))
