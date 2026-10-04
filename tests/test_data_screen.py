"""The Data screen: every database measured while it is open, how each one travels, migrations applied only where
Flyway already manages them, a snapshot restored around the services that use the copy, and Home's "Use the local
copy"."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from pdms_cli import actions, instances, localcopy, localdb, migrations
from pdms_cli.config import Config, Database, DevUser, Stack
from pdms_cli.ui import health as ui_health
from pdms_cli.ui import jobs as ui_jobs

WEB = Database(host="pdm-cluster.x.rds.amazonaws.com", database="pdm", protected=True)
DATA = Database(host="localhost", port=5434, database="pdms_sync", user="local", password="local")
COPY = localdb.database("pdms")


class Meter:
    def __init__(self) -> None:
        self.measured: list[str] = []

    def forget(self, keep: set[str]) -> None:
        pass

    def measure(self, name: str, db: Database, timeout: int) -> tuple[float, str]:
        self.measured.append(name)
        return 3.0, ""


def test_every_database_is_measured_while_the_data_screen_asks(monkeypatch) -> None:
    cfg = Config(dbs={"web-dev": WEB, "local": DATA, "pdms-local": COPY}, last_db="local")
    monkeypatch.setattr(ui_health.Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ui_health.instances, "load", lambda: {})
    monkeypatch.setattr(ui_health.health, "route", lambda db: SimpleNamespace(kind="local", address="", up=None, process=""))
    meter = Meter()
    monitor = ui_health.Health(meter=meter)
    monitor.measure()
    assert meter.measured == ["local"]  # only the one in use
    monitor._all_until = time.monotonic() + 60  # what run(every_database=True) sets
    meter.measured.clear()
    monitor.measure()
    assert meter.measured == ["web-dev", "local", "pdms-local"]


def test_each_port_says_which_container_publishes_it(monkeypatch) -> None:
    out = ("pdms-postgres|127.0.0.1:5440->5432/tcp\n"
           "backend-database-1|0.0.0.0:5434->5432/tcp, [::]:5434->5432/tcp\n"
           "pdms-elasticmq|127.0.0.1:9324->9324/tcp\nquiet|")
    monkeypatch.setattr(localdb, "_docker", lambda *args, timeout=120: (0, out))
    assert localdb.containers_by_port() == {5440: "pdms-postgres", 5434: "backend-database-1", 9324: "pdms-elasticmq"}
    monkeypatch.setattr(localdb, "_docker", lambda *args, timeout=120: (1, "no docker"))
    assert localdb.containers_by_port() == {}


@pytest.fixture
def cfg(monkeypatch, tmp_path) -> Config:
    config = Config(users={"agent": DevUser("u", "a@x.com")}, dbs={"web-dev": WEB, "local": DATA, "pdms-local": COPY})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(actions, "current_migrations_repo", lambda c: tmp_path)
    return config


def test_migrations_are_applied_only_where_flyway_manages_them(cfg, monkeypatch) -> None:
    jobs = ui_jobs.Jobs()
    with pytest.raises(actions.ActionError, match="shared"):
        jobs.migrate_db("web-dev")
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: {"flyway": False, "counts": {}})
    with pytest.raises(actions.ActionError, match="no Flyway history"):
        jobs.migrate_db("local")  # tables made by the ORM: baselineOnMigrate would run everything over them
    started = []
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: {"flyway": True, "counts": {"pending": 2}})
    monkeypatch.setattr(ui_jobs.images, "require", lambda names: None)
    monkeypatch.setattr(jobs, "run", lambda key, action, phase, work: started.append((key, action)) or SimpleNamespace(key=key))
    assert jobs.migrate_db("pdms-local").key == "migrate:pdms-local" and started == [("migrate:pdms-local", "migrate")]


def test_restoring_a_snapshot_stops_and_starts_the_services_that_use_the_copy(cfg, monkeypatch) -> None:
    user = SimpleNamespace(key="lead-a@28100", service="/x/lead-a", user="agent", db="pdms-local", port=28100,
                           host="0.0.0.0", reload=True, events="aws")
    monkeypatch.setattr(localcopy, "snapshots", lambda: [{"name": "fresh"}])
    monkeypatch.setattr(localcopy, "using_copy", lambda c: [user.key])
    monkeypatch.setattr(ui_jobs, "find", lambda key: user)
    steps = []
    monkeypatch.setattr(actions, "stop_service", lambda inst: steps.append(("stop", inst.key)))
    monkeypatch.setattr(localcopy, "restore_snapshot", lambda name: steps.append(("restore", name)))
    monkeypatch.setattr(actions, "plan_service", lambda c, path, **kw: steps.append(("plan", kw["db_name"], kw["port"])) or "launch")
    monkeypatch.setattr(actions, "start_service", lambda c, launch: steps.append(("start", launch)))
    jobs = ui_jobs.Jobs()
    with pytest.raises(actions.ActionError, match="no snapshot"):
        jobs.restore_snapshot("nope")
    jobs.restore_snapshot("fresh")
    deadline = time.monotonic() + 5
    while len(steps) < 4:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert steps == [("stop", "lead-a@28100"), ("restore", "fresh"), ("plan", "pdms-local", 28100), ("start", "launch")]


def test_use_the_local_copy_moves_the_stack_and_restarts_what_runs_on_the_slow_database(cfg, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b"], user="agent", db="web-dev",
                                      overrides={"lead/b": {"db": "web-dev"}})
    cfg.setup.stack = "prospecting"
    monkeypatch.setattr(localdb, "state", lambda: {"exists": True, "running": True, "port": localdb.PORT})
    alive = SimpleNamespace(key="a@1", db="web-dev", alive=lambda: True)
    other = SimpleNamespace(key="b@2", db="local", alive=lambda: True)
    monkeypatch.setattr(instances, "load", lambda: {"a@1": alive, "b@2": other})
    restarted = []
    jobs = ui_jobs.Jobs()
    monkeypatch.setattr(jobs, "restart_many", lambda keys, **kw: restarted.append((keys, kw)) or [SimpleNamespace(key=k) for k in keys])
    result = jobs.use_copy("web-dev")
    assert result == {"copy": "pdms-local", "stack": "prospecting", "restarting": ["a@1"]}
    stack = cfg.stacks["prospecting"]
    assert stack.db == "pdms-local" and stack.overrides["lead/b"]["db"] == "pdms-local"
    assert restarted == [(["a@1"], {"db": "pdms-local", "confirmed": True})]  # only what ran on web-dev
    del cfg.dbs["pdms-local"]
    with pytest.raises(actions.ActionError, match="no local copy"):
        jobs.use_copy("web-dev")


def test_new_screens_stop_being_new_once_opened(monkeypatch, tmp_path) -> None:
    from pdms_cli.ui import state as ui_state

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert ui_state.seen_views() == set()
    assert ui_state.mark_seen("data") == ["tests"]
    assert ui_state.mark_seen("tests") == [] and ui_state.seen_views() == {"data", "tests"}
    with pytest.raises(ValueError):
        ui_state.mark_seen("../etc")
