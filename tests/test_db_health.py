"""The line to each database (pdms_cli/health.py), pdms ui's monitor of the databases in use, the branch in the
status bar and Doctor's Network section."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from pdms_cli import doctor, health, instances, repos
from pdms_cli.config import Config, Database
from pdms_cli.ui import health as ui_health

# conftest.py replaces these with quick answers for every test; the real ones are tried here, against local sockets.
REAL_ROUTE = health.route
REAL_LINE = health.line


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                pass

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    stop.set()
    server.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --------------------------------------------------------------------------- the way to a database


def test_localhost_is_this_machine() -> None:
    assert REAL_ROUTE(Database("localhost", port=5434)).kind == "local"
    assert REAL_ROUTE(Database("127.0.0.1")).kind == "local"


def test_a_remote_name_that_resolves_here_is_a_tunnel(monkeypatch, listener) -> None:
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "127.0.0.1")
    way = REAL_ROUTE(Database("pdm-cluster.cluster-x.us-east-1.rds.amazonaws.com", port=listener))
    assert (way.kind, way.address, way.up) == ("tunnel", f"127.0.0.1:{listener}", True)

    down = REAL_ROUTE(Database("pdm-cluster.cluster-x.us-east-1.rds.amazonaws.com", port=free_port()))
    assert down.kind == "tunnel" and down.up is False and down.process == ""


def test_other_names_go_straight_out(monkeypatch) -> None:
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "10.0.4.20")
    assert REAL_ROUTE(Database("db.internal")).kind == "direct"

    def unknown(host):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "gethostbyname", unknown)
    assert REAL_ROUTE(Database("nowhere.example")).kind == "direct"


def test_the_line_is_one_connection_to_the_internet(monkeypatch, listener) -> None:
    monkeypatch.setattr(health, "LINE_HOST", ("127.0.0.1", listener))
    ms = REAL_LINE()
    assert ms is not None and 0 <= ms < 1000
    monkeypatch.setattr(health, "LINE_HOST", ("127.0.0.1", free_port()))
    assert REAL_LINE(timeout=0.5) is None


def test_durations_read_well() -> None:
    assert (health.describe_ms(4.2), health.describe_ms(370.4), health.describe_ms(1840)) == ("4 ms", "370 ms", "1.84 s")


# --------------------------------------------------------------------------- one open connection per database


class FakeConn:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.closed, self.queries = fail, False, 0

    def execute(self, sql):
        if self.fail:
            raise OSError("server closed the connection unexpectedly\nThis probably means...")
        self.queries += 1
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        self.closed = True


def test_the_meter_keeps_the_connection_and_measures_one_round_trip(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(health, "connect", lambda db, timeout: opened.append(FakeConn()) or opened[-1])
    meter = health.Meter()
    db = Database("h")
    assert meter.measure("web-dev", db)[1] == ""
    assert meter.measure("web-dev", db)[1] == ""
    assert len(opened) == 1 and opened[0].queries == 2  # select 1 twice on the same connection

    edited = Database("other-host")
    meter.measure("web-dev", edited)  # the database changed: a new connection, the old one closed
    assert len(opened) == 2 and opened[0].closed

    meter.forget(set())
    assert opened[1].closed


def test_a_broken_connection_reports_and_reconnects(monkeypatch) -> None:
    conns = iter([FakeConn(fail=True), FakeConn()])
    monkeypatch.setattr(health, "connect", lambda db, timeout: next(conns))
    meter = health.Meter()
    assert meter.measure("web-dev", Database("h")) == (None, "server closed the connection unexpectedly")
    ms, error = meter.measure("web-dev", Database("h"))
    assert error == "" and ms is not None


# --------------------------------------------------------------------------- pdms ui's monitor


@pytest.fixture
def machine(monkeypatch):
    cfg = Config(dbs={"web-dev": Database("db.example.com", protected=True), "local": Database("localhost"),
                      "qa": Database("qa.example.com")})
    cfg.last_db = "web-dev"
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def test_in_use_is_the_current_database_and_those_of_running_services(machine, monkeypatch) -> None:
    alive = instances.Instance(key="a@1", pid=1, service="/x/a", host="", port=1, user="u", db="local", reload=False,
                               log="", started_at="")
    monkeypatch.setattr(instances, "load", lambda: {"a@1": alive})
    monkeypatch.setattr(instances.Instance, "alive", lambda self: True)
    assert list(ui_health.in_use(machine)) == ["web-dev", "local"]


def test_the_monitor_keeps_an_hour_of_round_trips(machine, monkeypatch) -> None:
    monkeypatch.setattr(instances, "load", lambda: {})
    samples = iter([(370.4, ""), (None, "timeout expired"), (410.0, "")])

    class Meter:
        def measure(self, name, db, timeout):
            return next(samples)

        def forget(self, keep):
            pass

    changes = []
    monitor = ui_health.Health(lambda: changes.append(1), meter=Meter())
    for _ in range(3):
        monitor.measure()
    (item,) = monitor.summary()["dbs"]
    assert item["name"] == "web-dev" and item["ms"] == 410 and item["history"] == [370, None, 410]
    assert item["route"]["kind"] == "direct" and len(changes) == 3

    machine.last_db = ""  # no longer in use: it leaves the status bar
    monitor.measure()
    assert monitor.summary()["dbs"] == []


# --------------------------------------------------------------------------- the branch and Doctor


def test_the_branch_comes_from_git_head(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feat/request-insight\n")
    assert repos.git_branch(tmp_path) == "feat/request-insight"
    (tmp_path / ".git" / "HEAD").write_text("214d1ef0a1b2c3d4e5f6\n")
    assert repos.git_branch(tmp_path) == "214d1ef0"

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (tmp_path / "gitdir").mkdir()
    (tmp_path / "gitdir" / "HEAD").write_text("ref: refs/heads/main\n")
    (worktree / ".git").write_text(f"gitdir: {tmp_path / 'gitdir'}\n")
    assert repos.git_branch(worktree) == "main"
    assert repos.git_branch(tmp_path / "nothing") == ""


def test_doctor_checks_the_tunnels_and_the_line(monkeypatch) -> None:
    cfg = Config(dbs={"web-dev": Database("rds.example.com", port=5432), "local": Database("localhost")})
    ways = {"rds.example.com": health.Route("tunnel", "127.0.0.6:5432", False), "localhost": health.Route("local")}
    monkeypatch.setattr(health, "route", lambda db: ways[db.host])
    monkeypatch.setattr(health, "line", lambda timeout=3: 1400.0)
    checks = doctor.check_network(cfg)
    assert [(c.name, c.status) for c in checks] == [("Tunnel to web-dev", doctor.FAIL), ("Internet", doctor.WARN)]
    assert "127.0.0.6:5432" in checks[0].detail and "1.40 s" in checks[1].detail

    ways["rds.example.com"] = health.Route("tunnel", "127.0.0.6:5432", True, "socat")
    monkeypatch.setattr(health, "line", lambda timeout=3: 40.0)
    assert [(c.status, c.detail) for c in doctor.check_network(cfg)] == [
        (doctor.OK, "127.0.0.6:5432 · socat"), (doctor.OK, "40 ms to 1.1.1.1")]


def test_doctor_says_when_a_database_is_slow(monkeypatch) -> None:
    cfg = Config(dbs={"web-dev": Database("rds.example.com")})
    monkeypatch.setattr(health, "probe", lambda db, timeout: ("PostgreSQL 16.4", 370.0))
    (check,) = doctor.check_databases(cfg, 5)
    assert check.status == doctor.WARN and check.detail.endswith("PostgreSQL 16.4 · 370 ms per round trip")
