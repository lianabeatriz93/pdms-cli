"""Incremental log reading."""

from __future__ import annotations

from pdms_cli.logview import LogFollower


def test_reads_only_new_complete_lines(tmp_path):
    log = tmp_path / "svc.log"
    log.write_text("old 1\nold 2\n")
    follower = LogFollower(str(log))
    assert follower.skip_to_tail(1) == ["old 2"]
    assert follower.read_new() == []

    with log.open("a") as fh:
        fh.write("new 1\nhalf")
    assert follower.read_new() == ["new 1"]
    with log.open("a") as fh:
        fh.write(" line\n")
    assert follower.read_new() == ["half line"]


def test_starts_over_when_the_log_is_rewritten(tmp_path):
    log = tmp_path / "svc.log"
    log.write_text("a long first run of the service\n")
    follower = LogFollower(str(log))
    follower.skip_to_tail(10)
    log.write_text("restart\n")  # restart truncates the log
    assert follower.read_new() == ["restart"]


def test_missing_file_is_not_an_error(tmp_path):
    follower = LogFollower(str(tmp_path / "nope.log"))
    assert follower.skip_to_tail(5) == []
    assert follower.read_new() == []


def test_restart_keeps_the_previous_log(tmp_path):
    from pdms_cli import instances

    log = tmp_path / "svc@8080.log"
    log.write_text("Traceback: the crash we want to keep\n")
    instances.rotate_log(log)
    assert not log.exists()
    assert instances.previous_log_path(log).read_text().startswith("Traceback")

    log.write_text("")  # an empty log does not overwrite the kept one
    instances.rotate_log(log)
    assert instances.previous_log_path(log).read_text().startswith("Traceback")


def test_endpoints_are_read_from_the_openapi_spec():
    from pdms_cli.instances import endpoints

    spec = {"paths": {
        "/api/v1/leads/tp/{entity_id}": {"get": {"summary": "Detail"}, "delete": {}, "parameters": []},
        "/api/v1/leads/tp": {"post": {"summary": "Create"}, "get": {"summary": "List"}},
    }}
    found = [(e.method, e.path, e.summary) for e in endpoints(spec)]
    assert found == [
        ("GET", "/api/v1/leads/tp", "List"),
        ("POST", "/api/v1/leads/tp", "Create"),
        ("GET", "/api/v1/leads/tp/{entity_id}", "Detail"),
        ("DELETE", "/api/v1/leads/tp/{entity_id}", ""),
    ]
    assert endpoints({}) == []
