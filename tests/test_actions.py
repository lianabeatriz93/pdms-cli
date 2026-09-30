"""Service actions: no prompts, decisions come back as exceptions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, events
from pdms_cli.config import Config, Database, DevUser
from pdms_cli.instances import Instance


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    return Config(
        users={"supervisor": DevUser("u1", "s@x.com"), "agent": DevUser("u2", "a@x.com")},
        dbs={"local": Database("localhost"), "shared": Database("db.example.com", protected=True)},
    )


@pytest.fixture
def ports(monkeypatch):
    """Ports in use on the machine (``busy``) and held by background instances (``taken``)."""
    state = {"busy": set(), "taken": set()}
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: port not in state["busy"])
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set(state["taken"]))
    return state


@pytest.fixture
def http_service(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    return tmp_path / "lead-tp-list"


def test_free_port_keeps_a_free_port_and_hands_back_the_next_one(ports) -> None:
    assert actions.free_port("0.0.0.0", 8080) == 8080
    ports["busy"].add(8080)
    ports["taken"].add(8081)  # a background instance still booting
    with pytest.raises(actions.PortBusy) as busy:
        actions.free_port("0.0.0.0", 8080)
    assert (busy.value.port, busy.value.free) == (8080, 8082)


def test_check_database_asks_only_for_protected_ones(cfg) -> None:
    actions.check_database(cfg, "local", confirmed=False)
    actions.check_database(cfg, "shared", confirmed=True)
    with pytest.raises(actions.ProtectedDatabase) as asked:
        actions.check_database(cfg, "shared", confirmed=False)
    assert asked.value.name == "shared"


def test_events_setup_decides_without_asking(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    with pytest.raises(actions.ActionError):
        actions.events_setup(cfg, tmp_path, "sqs")
    assert actions.events_setup(cfg, tmp_path, "auto").kind == "aws"
    with pytest.raises(actions.LocalEventsDown):
        actions.events_setup(cfg, tmp_path, "local")


def test_plan_service_for_an_api(cfg, ports, http_service) -> None:
    launch = actions.plan_service(cfg, http_service, user_name="supervisor", db_name="local", port=8081)
    assert (launch.port, launch.host, launch.reload, launch.queue) == (8081, "0.0.0.0", True, "")
    assert launch.cmd[-4:] == ["--host", "0.0.0.0", "--port", "8081"]
    assert launch.events.kind == "aws"


def test_plan_service_rejects_unknown_names_and_busy_ports(cfg, ports, http_service) -> None:
    with pytest.raises(actions.ActionError) as error:
        actions.plan_service(cfg, http_service, user_name="nobody", db_name="local")
    assert "nobody" in error.value.message
    ports["busy"].add(8080)
    with pytest.raises(actions.PortBusy):
        actions.plan_service(cfg, http_service, user_name="supervisor", db_name="local")


def test_plan_service_for_an_sqs_consumer(cfg, ports, monkeypatch, tmp_path) -> None:
    queue, handler = events.Queue("broker-sqs-queue.fifo"), events.Consumer("broker/broker-sqs-event", "main.handler")
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: (queue, handler))
    modes = []
    monkeypatch.setattr(actions, "events_setup",
                        lambda cfg, service, mode: modes.append(mode) or actions.EventsSetup({}, "local", "local"))
    ports["busy"].add(8080)  # consumers do not listen, so a busy default port does not matter
    launch = actions.plan_service(cfg, tmp_path, user_name="supervisor", db_name="local")
    assert (launch.port, launch.queue, modes) == (0, "broker-sqs-queue.fifo", ["local"])
    assert launch.is_consumer


def test_start_service_installs_first_and_records_the_instance(cfg, ports, http_service, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(actions, "installed_parts", lambda service: {"(service)": "abc"})
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: calls.append(("start", kw)) or SimpleNamespace(**kw))
    launch = actions.plan_service(cfg, http_service, user_name="agent", db_name="local", port=8090)
    started = actions.start_service(cfg, launch, install=lambda service: calls.append(("install", service)))
    assert [c[0] for c in calls] == ["install", "start"]
    assert (started.port, started.user, started.db, started.deps) == (8090, "agent", "local", {"(service)": "abc"})


def _instance(tmp_path: Path, **kw) -> Instance:
    return Instance(key="lead-tp-list@8081", pid=1, service=str(tmp_path / "lead-tp-list"), host="0.0.0.0",
                    port=8081, user="supervisor", db="local", reload=True, log="x.log", started_at="", **kw)


def test_restart_asks_for_a_protected_database_before_stopping(cfg, ports, http_service, monkeypatch,
                                                               tmp_path) -> None:
    stopped = []
    monkeypatch.setattr(actions.instances, "stop", stopped.append)
    with pytest.raises(actions.ProtectedDatabase):
        actions.restart_service(cfg, _instance(tmp_path), db_name="shared")
    assert not stopped  # a refused confirmation leaves the instance running


def test_restart_keeps_port_user_and_database(cfg, ports, http_service, monkeypatch, tmp_path) -> None:
    order = []
    monkeypatch.setattr(actions.events, "running", lambda port: True)  # it published to the local ElasticMQ
    monkeypatch.setattr(actions.instances, "stop", lambda inst: order.append("stop"))
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    monkeypatch.setattr(actions.instances, "start",
                        lambda service, cmd, env, **kw: order.append("start") or SimpleNamespace(**kw))
    started = actions.restart_service(cfg, _instance(tmp_path, events="local"), db_name="shared", confirmed=True)
    assert order == ["stop", "start"]
    assert (started.port, started.user, started.db, started.events) == (8081, "supervisor", "shared", "local")
