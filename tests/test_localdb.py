"""pdms's own Postgres: the container with its volume, and the test databases (the only ones tests run on)."""

from __future__ import annotations

import pytest

from pdms_cli import actions, localdb


class FakeDocker:
    """``docker`` as localdb calls it: a container that exists once run, and psql over a set of databases."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.exists = self.running = False
        self.dbs = {"postgres", "pdms"}

    def __call__(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "inspect":
            return (0, f"{str(self.running).lower()}|5440") if self.exists else (1, "No such object")
        if args[0] == "run":
            self.exists = self.running = True
            return 0, "abc123"
        if args[0] == "start":
            self.running = True
            return 0, ""
        if args[0] == "stop":
            self.running = False
            return 0, ""
        if args[:3] == ("exec", localdb.CONTAINER, "pg_isready"):
            return (0, "accepting") if self.running else (2, "no response")
        if args[:3] == ("exec", localdb.CONTAINER, "psql"):
            sql = args[-1]
            if sql.startswith("select datname"):
                return 0, "\n".join(f"{name}|7600000" for name in sorted(self.dbs))
            if sql.startswith("create database"):
                self.dbs.add(sql.split('"')[1])
                return 0, "CREATE DATABASE"
            if sql.startswith("drop database"):
                self.dbs.discard(sql.split('"')[1])
                return 0, "DROP DATABASE"
        return 1, f"unexpected {args}"


@pytest.fixture
def docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(localdb, "_docker", fake)
    monkeypatch.setattr(localdb.events, "docker_available", lambda: (True, "27"))
    monkeypatch.setattr(localdb.images, "present", lambda name: True)  # not this machine's Docker
    return fake


def test_up_creates_the_container_with_its_volume_once(docker) -> None:
    assert localdb.state() == {"exists": False, "running": False, "port": 0}
    assert localdb.up() == {"exists": True, "running": True, "port": 5440}
    (run,) = [c for c in docker.calls if c[0] == "run"]
    assert "127.0.0.1:5440:5432" in run and f"{localdb.VOLUME}:/var/lib/postgresql" in run
    assert localdb.IMAGE in run and "jit=off" in run
    localdb.down()
    localdb.up()  # stopped: started again, not created again
    assert [c[0] for c in docker.calls].count("run") == 1 and ("start", localdb.CONTAINER) in docker.calls


def test_test_databases_are_created_up_to_a_count_and_only_they_can_be_dropped(docker) -> None:
    localdb.up()
    assert localdb.ensure_test_databases(4) == ["pdms_test_1", "pdms_test_2", "pdms_test_3", "pdms_test_4"]
    assert localdb.ensure_test_databases(4) == []  # already there
    assert localdb.test_databases() == ["pdms_test_1", "pdms_test_2", "pdms_test_3", "pdms_test_4"]
    localdb.recreate_test_database("pdms_test_2")
    assert "pdms_test_2" in docker.dbs
    for name in ("pdms", "pdms_sync", "postgres", "pdms_test_x"):
        with pytest.raises(actions.ActionError, match="not a test database"):
            localdb.remove_test_database(name)
    assert localdb.databases()["pdms"] == 7600000


def test_without_docker_it_says_so(monkeypatch) -> None:
    monkeypatch.setattr(localdb.events, "docker_available", lambda: (False, "permission denied"))
    with pytest.raises(actions.ActionError, match="Docker is not available: permission denied"):
        localdb.up()
