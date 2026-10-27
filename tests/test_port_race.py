"""Services started or woken at the same time: each change of the registry is kept, and each gets its own port."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pdms_cli import actions, instances
from pdms_cli.config import Config, Database, Defaults, DevUser


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    monkeypatch.setattr(actions, "commit_of", lambda cfg, service: "")
    return Config(users={"agent": DevUser("u2", "a@x.com")}, dbs={"local": Database("localhost")},
                  defaults=Defaults(port=8080))


def register(service: Path, port: int, pid: int = 0) -> instances.Instance:
    return instances.register_asleep(service, host="127.0.0.1", port=port, user="agent", db="local", reload=True)


def test_changes_made_at_the_same_time_are_all_kept(tmp_path) -> None:
    """Many threads registering at once (the proxy wakes each service in its own thread)."""
    def add(n: int) -> None:
        register(tmp_path / f"svc{n}", 9000 + n)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(instances.load()) == 30
    assert not list(instances.registry_path().parent.glob("*.tmp"))


def test_the_lock_can_be_taken_again_by_its_thread(tmp_path) -> None:
    with instances.registry_lock():
        with instances.registry_lock():
            register(tmp_path / "svc", 9100)
        instances.forget("svc@9100")
    assert instances.load() == {}


def test_a_port_taken_since_it_was_planned_moves_to_the_next_free(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    started = []
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: started.append((cmd, kw)) or kw)
    launch = actions.plan_service(cfg, tmp_path / "lead-get", user_name="agent", db_name="local", port=8090)
    register(tmp_path / "other", 8090)  # while lead-get installed, another service took 8090
    actions.start_service(cfg, launch)
    cmd, kw = started[0]
    assert kw["port"] == 8091 and cmd[cmd.index("--port") + 1] == "8091"


def test_a_port_something_else_listens_on_is_skipped(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: port != 8090)
    launch = actions.ServiceLaunch(**{**actions.plan_service(
        cfg, tmp_path / "lead-get", user_name="agent", db_name="local", port=8091, on_demand=True).__dict__,
        "port": 8090})
    assert actions.start_service(cfg, launch).port == 8091  # registered asleep on the free one


def test_two_services_woken_together_get_their_own_ports(cfg, monkeypatch, tmp_path) -> None:
    """The proxy wakes each in its own thread. Each one's port was free in the registry for a moment (forgotten
    to be planned again), and the other could take it: both ended up on one port and neither answered."""
    listening: dict[int, str] = {}  # what a real start would bind
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: port not in listening)

    def start(service, cmd, env, **kw):
        time.sleep(0.05)  # spawning takes a moment, during which the port is not bound yet
        port = kw["port"]
        assert port not in listening, f"{service.name} on {port}, taken by {listening.get(port)}"
        listening[port] = service.name
        instance = instances.Instance(key=instances.make_key(service, port), pid=0, service=str(service),
                                      host=kw["host"], port=port, user=kw["user"], db=kw["db"], reload=kw["reload"],
                                      log="", started_at="now", on_demand=True)
        registry = instances.load()
        registry[instance.key] = instance
        instances.save(registry)
        return instance

    monkeypatch.setattr(actions.instances, "start", start)
    asleep = [register(tmp_path / "trip-type-list", 28100), register(tmp_path / "lead-place-get", 28101)]
    woke: list[instances.Instance] = []
    errors: list[BaseException] = []

    def wake(inst: instances.Instance) -> None:
        try:
            woke.append(actions.wake_service(cfg, inst))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=wake, args=(inst,)) for inst in asleep]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sorted((Path(inst.service).name, inst.port) for inst in woke) == [
        ("lead-place-get", 28101), ("trip-type-list", 28100)]
    assert sorted(instances.load()) == ["lead-place-get@28101", "trip-type-list@28100"]
