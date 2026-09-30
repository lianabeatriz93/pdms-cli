"""Service actions: no prompts, decisions come back as exceptions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, events
from pdms_cli.config import Config, Database, DevUser, Stack
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


# --------------------------------------------------------------------------- stacks


@pytest.fixture
def backend(tmp_path, monkeypatch) -> Path:
    """A backend with four services; ``broker/sqs`` is an SQS consumer."""
    for name in ("lead/a", "lead/b", "lead/c", "broker/sqs"):
        (tmp_path / name).mkdir(parents=True)
    monkeypatch.setattr(actions.runner, "is_service", lambda path: path.is_dir())
    queue, handler = events.Queue("broker-sqs-queue.fifo"), events.Consumer("broker/sqs", "main.handler")
    monkeypatch.setattr(actions, "consumer_of",
                        lambda cfg, service: (queue, handler) if service.name == "sqs" else None)
    return tmp_path


def _live(monkeypatch, *items: tuple[Path, int]) -> None:
    live = [Instance(key=f"{p.name}@{port}", pid=1, service=str(p), host="0.0.0.0", port=port, user="supervisor",
                     db="local", reload=True, log="x.log", started_at="") for p, port in items]
    monkeypatch.setattr(actions.instances, "load", lambda: {i.key: i for i in live})
    monkeypatch.setattr(Instance, "alive", lambda self: True)


def test_plan_stack_skips_running_services_and_gives_each_a_free_port(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b", "lead/c"])
    _live(monkeypatch, (backend / "lead/b", 8080))
    ports["taken"].add(8080)
    ports["busy"].add(8081)  # DynamoDB or anything else outside pdms
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    plan = actions.plan_stack(cfg, "prospecting", backend, user_name="supervisor", db_name="local")
    assert plan.running == {backend / "lead/b": [8080]}
    assert [(s.service.name, s.port) for s in plan.services] == [("a", 8082), ("c", 8083)]
    assert plan.events is not None and plan.events.kind == "aws"


def test_plan_stack_with_a_consumer_publishes_locally(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["broker"] = Stack(services=["broker/sqs", "lead/a"])
    _live(monkeypatch)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    with pytest.raises(actions.LocalEventsDown):
        actions.plan_stack(cfg, "broker", backend, user_name="supervisor", db_name="local")
    monkeypatch.setattr(actions.events, "running", lambda port: True)
    plan = actions.plan_stack(cfg, "broker", backend, user_name="supervisor", db_name="local")
    assert [(s.service.name, s.port, s.events.kind) for s in plan.services] == [("sqs", 0, "local"),
                                                                               ("a", 8080, "local")]


def test_plan_stack_when_everything_runs_and_when_the_stack_is_stale(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["one"] = Stack(services=["lead/a"])
    _live(monkeypatch, (backend / "lead/a", 8080))
    plan = actions.plan_stack(cfg, "one", backend, user_name="supervisor", db_name="local")
    assert (plan.services, plan.events) == ([], None)
    cfg.stacks["stale"] = Stack(services=["lead/gone"])
    with pytest.raises(actions.ActionError) as error:
        actions.plan_stack(cfg, "stale", backend, user_name="supervisor", db_name="local")
    assert "lead/gone" in error.value.message


def test_stack_instances_lists_only_its_live_services(cfg, backend, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b"])
    _live(monkeypatch, (backend / "lead/a", 8080), (backend / "lead/c", 8081))
    assert [i.key for i in actions.stack_instances(cfg, "prospecting", backend)] == ["a@8080"]


def test_save_and_remove_stack_validate_first(cfg) -> None:
    with pytest.raises(actions.ActionError):
        actions.save_stack(cfg, "empty", Stack())
    with pytest.raises(actions.ActionError):
        actions.save_stack(cfg, "bad", Stack(services=["lead/a"], user="nobody"))
    actions.save_stack(cfg, "ok", Stack(services=["lead/a"], user="agent", db="local"))
    assert cfg.stacks["ok"].user == "agent"
    actions.remove_stack(cfg, "ok")
    assert "ok" not in cfg.stacks
    with pytest.raises(actions.ActionError):
        actions.remove_stack(cfg, "ok")
