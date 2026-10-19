"""Services on demand: asleep until the proxy gets a request for them, asleep again when idle."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pdms_cli import actions, events, instances, proxy, routes
from pdms_cli.config import Config, Database, Defaults, DevUser, Stack

from test_proxy import Echo, TERRAFORM, call, start


@pytest.fixture
def cfg(monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    return Config(users={"agent": DevUser("u2", "a@x.com")}, dbs={"local": Database("localhost")},
                  defaults=Defaults(port=8080))


def asleep(service: Path, port: int = 8081) -> instances.Instance:
    return instances.register_asleep(service, host="127.0.0.1", port=port, user="agent", db="local", reload=True)


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


# --------------------------------------------------------------------------- the registry


def test_an_asleep_instance_has_no_process_but_keeps_its_port(tmp_path) -> None:
    inst = asleep(tmp_path / "lead-tp-list")
    assert inst.asleep and inst.present() and not inst.alive()
    assert instances.health(inst).state == "asleep"
    assert 8081 in instances.running_ports()  # nobody else gets it meanwhile
    instances.kill_tree(inst.pid)  # pid 0: nothing to stop
    instances.stop(inst)
    assert instances.load() == {}


def test_sleep_stops_the_process_and_keeps_the_instance(tmp_path) -> None:
    proc = sleeper()
    try:
        inst = instances.Instance(
            key="lead-tp-list@8081", pid=proc.pid, service=str(tmp_path / "lead-tp-list"), host="127.0.0.1",
            port=8081, user="agent", db="local", reload=True, log="", started_at="2026-10-19T10:00:00",
            created=instances.creation_time(proc.pid), on_demand=True,
        )
        instances.save({inst.key: inst})
        slept = instances.sleep(inst, timeout=5)
        assert proc.wait(timeout=10) is not None
        assert slept.asleep and instances.load()[inst.key].asleep and instances.load()[inst.key].started_at == ""
    finally:
        proc.kill()


# --------------------------------------------------------------------------- planning and starting


def test_plan_follows_the_setting_unless_told(cfg, tmp_path) -> None:
    service = tmp_path / "lead-tp-list"
    assert not actions.plan_service(cfg, service, user_name="agent", db_name="local").on_demand
    cfg.defaults.on_demand = True
    assert actions.plan_service(cfg, service, user_name="agent", db_name="local").on_demand
    assert not actions.plan_service(cfg, service, user_name="agent", db_name="local", on_demand=False).on_demand


def test_a_consumer_is_never_on_demand(cfg, monkeypatch, tmp_path) -> None:
    queue, handler = events.Queue("q"), events.Consumer("broker/broker-sqs-event", "main.handler")
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: (queue, handler))
    monkeypatch.setattr(actions, "events_setup", lambda cfg, service, mode: actions.EventsSetup({}, "local", "local"))
    launch = actions.plan_service(cfg, tmp_path, user_name="agent", db_name="local", on_demand=True)
    assert not launch.on_demand


def test_on_demand_registers_it_asleep_after_installing(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.instances, "start", lambda *a, **kw: pytest.fail("started a process"))
    installed = []
    launch = actions.plan_service(cfg, tmp_path / "lead-tp-list", user_name="agent", db_name="local", port=8090,
                                  on_demand=True)
    inst = actions.start_service(cfg, launch, install=installed.append)
    assert installed == [tmp_path / "lead-tp-list"]  # the venv is ready when the first request comes
    assert inst.asleep and (inst.port, inst.user, inst.db) == (8090, "agent", "local")


def test_awake_starts_it_now_and_remembers_it_is_on_demand(cfg, monkeypatch, tmp_path) -> None:
    started = []
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: started.append(kw) or kw)
    launch = actions.plan_service(cfg, tmp_path / "lead-tp-list", user_name="agent", db_name="local", port=8090,
                                  on_demand=True)
    actions.start_service(cfg, launch, awake=True)
    assert started[0]["on_demand"] is True


def test_a_stack_says_on_off_or_as_the_setting(cfg, monkeypatch, tmp_path) -> None:
    for name in ("a", "b"):
        (tmp_path / "lead" / name).mkdir(parents=True)
        (tmp_path / "lead" / name / "pyproject.toml").write_text("")
        (tmp_path / "lead" / name / "main.py").write_text("")
    cfg.stacks["s"] = Stack(services=["lead/a", "lead/b"])

    def plan(**kw) -> list[bool]:
        return [launch.on_demand for launch in actions.plan_stack(cfg, "s", tmp_path, user_name="agent",
                                                                  db_name="local", **kw).services]

    assert plan() == [False, False]
    cfg.defaults.on_demand = True
    assert plan() == [True, True]
    cfg.stacks["s"].on_demand = "off"
    assert plan() == [False, False]
    cfg.defaults.on_demand = False
    cfg.stacks["s"].on_demand = "on"
    assert plan() == [True, True]
    assert plan(on_demand=False) == [False, False]  # pdms up --always-on


def test_asleep_services_count_as_there_for_up_and_down(cfg, tmp_path) -> None:
    (tmp_path / "lead" / "a").mkdir(parents=True)
    (tmp_path / "lead" / "a" / "pyproject.toml").write_text("")
    (tmp_path / "lead" / "a" / "main.py").write_text("")
    cfg.stacks["s"] = Stack(services=["lead/a"])
    inst = asleep(tmp_path / "lead" / "a")
    assert not actions.plan_stack(cfg, "s", tmp_path, user_name="agent", db_name="local").services
    assert actions.stack_instances(cfg, "s", tmp_path) == [inst]


def test_waking_starts_it_as_registered_on_its_port(cfg, monkeypatch, tmp_path) -> None:
    started = []
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    monkeypatch.setattr(actions.instances, "start", lambda service, cmd, env, **kw: started.append((cmd, kw)) or kw)
    actions.wake_service(cfg, asleep(tmp_path / "lead-tp-list"))
    cmd, kw = started[0]
    assert cmd[-2:] == ["--port", "8081"] and kw["user"] == "agent" and kw["on_demand"] is True
    assert "lead-tp-list@8081" not in instances.load()  # the real start registers it again (faked here)


def test_a_failed_wake_leaves_it_asleep(cfg, monkeypatch, tmp_path) -> None:
    def broken(*args, **kwargs):
        raise OSError("no poetry")

    monkeypatch.setattr(actions.instances, "start", broken)
    monkeypatch.setattr(actions, "installed_parts", lambda service: None)
    with pytest.raises(OSError):
        actions.wake_service(cfg, asleep(tmp_path / "lead-tp-list"))
    assert instances.load()["lead-tp-list@8081"].asleep


def test_always_on_starts_an_asleep_one_and_on_demand_keeps_a_running_one(cfg, monkeypatch, tmp_path) -> None:
    inst = asleep(tmp_path / "lead-tp-list")
    woke = []
    monkeypatch.setattr(actions, "wake_service", lambda cfg, instance: woke.append(instance.key) or instance)
    actions.set_on_demand(cfg, inst, False)
    assert woke == [inst.key] and not instances.load()[inst.key].on_demand
    actions.set_on_demand(cfg, instances.load()[inst.key], True)
    assert woke == [inst.key] and instances.load()[inst.key].on_demand


# --------------------------------------------------------------------------- the proxy


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "pdms"
    (root / "backend" / "snakesdk").mkdir(parents=True)
    for service in ("lead-tp-list", "lead-tp-create"):
        (root / "backend" / "lead" / service).mkdir(parents=True)
    tf = routes.terraform_dir(root, "dev")
    tf.mkdir(parents=True)
    for name, text in TERRAFORM.items():
        (tf / name).write_text(text)
    return root


@pytest.fixture
def local():
    server = start(Echo, "local")
    yield server
    server.shutdown()


def serve(gateway: proxy.Gateway):
    server = start(proxy.make_handler(gateway), "proxy")
    return server, server.server_address[1]


def test_the_first_request_wakes_the_service_and_waits_for_it(repo, local) -> None:
    service = repo / "backend" / "lead" / "lead-tp-list"
    asleep(service, port=local.server_address[1])
    woken, notes = [], []

    def wake(inst):
        time.sleep(0.3)  # starting takes a moment: the other request waits for the same start
        woken.append(inst.key)
        awake = instances.Instance(**{**inst.__dict__, "pid": os.getpid(), "started_at": "2026-10-19T10:00:00"})
        instances.save({**instances.load(), awake.key: awake})
        return awake

    gateway = proxy.Gateway(routes=routes.load_routes(repo), backend=repo / "backend", remote=None, wake=wake,
                            note=notes.append)
    server, port = serve(gateway)
    try:
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(call(port, "GET", "/api/v1/leads/tp")))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert [response.status for response, _data in answers] == [200, 200]
        assert all(data["server"] == "local" for _response, data in answers)
        assert len(woken) == 1
        assert notes[0].startswith("waking lead-tp-list@") and "awake in" in notes[1]
    finally:
        server.shutdown()


def test_a_service_that_cannot_wake_gets_a_clear_503(repo) -> None:
    asleep(repo / "backend" / "lead" / "lead-tp-list")

    def wake(inst):
        raise proxy.WakeFailed("ElasticMQ is not running")

    gateway = proxy.Gateway(routes=routes.load_routes(repo), backend=repo / "backend", remote=None, wake=wake,
                            note=lambda text: None)
    server, port = serve(gateway)
    try:
        response, data = call(port, "GET", "/api/v1/leads/tp")
        assert response.status == 503 and "ElasticMQ is not running" in data["detail"]
    finally:
        server.shutdown()


def test_any_failure_to_wake_is_a_503_not_a_dropped_connection(repo) -> None:
    asleep(repo / "backend" / "lead" / "lead-tp-list")

    def wake(inst):
        raise OSError("poetry: not found")

    gateway = proxy.Gateway(routes=routes.load_routes(repo), backend=repo / "backend", remote=None, wake=wake,
                            note=lambda text: None)
    server, port = serve(gateway)
    try:
        response, data = call(port, "GET", "/api/v1/leads/tp")
        assert response.status == 503 and "could not start: poetry: not found" in data["detail"]
    finally:
        server.shutdown()


def test_an_asleep_service_is_not_in_the_docs(repo) -> None:
    asleep(repo / "backend" / "lead" / "lead-tp-list")
    gateway = proxy.Gateway(routes=routes.load_routes(repo), backend=repo / "backend", remote=None)
    assert gateway.local_specs() == []
    assert gateway.target(routes.load_routes(repo)[0]).kind == "local"  # but its routes are local


def test_idle_services_are_put_to_sleep(repo) -> None:
    proc = sleeper()
    try:
        inst = instances.Instance(
            key="lead-tp-list@8081", pid=proc.pid, service=str(repo / "backend" / "lead" / "lead-tp-list"),
            host="127.0.0.1", port=8081, user="agent", db="local", reload=True, log="",
            started_at="2026-10-19T10:00:00", created=instances.creation_time(proc.pid), on_demand=True,
        )
        always = instances.Instance(**{**inst.__dict__, "key": "lead-tp-create@8082", "on_demand": False,
                                       "service": str(repo / "backend" / "lead" / "lead-tp-create")})
        instances.save({inst.key: inst, always.key: always})
        notes = []
        gateway = proxy.Gateway(routes=[], backend=repo / "backend", remote=None, idle_seconds=lambda: 60,
                                note=notes.append)
        assert gateway.put_idle_to_sleep(now=1000) == []  # first seen: it counts from now
        gateway._in_progress[inst.key] = 1
        assert gateway.put_idle_to_sleep(now=1061) == []  # it is answering a request
        gateway._in_progress[inst.key] = 0
        gateway.used(inst.key)
        assert gateway.put_idle_to_sleep(now=time.monotonic() + 30) == []
        assert gateway.put_idle_to_sleep(now=time.monotonic() + 61) == [inst.key]
        assert proc.wait(timeout=10) is not None
        assert instances.load()[inst.key].asleep and not instances.load()[always.key].asleep
        assert notes == ["lead-tp-list@8081 asleep after 1 min without requests"]
    finally:
        proc.kill()


def test_another_repos_services_are_left_alone(repo, tmp_path) -> None:
    inst = instances.Instance(
        key="x@8081", pid=os.getpid(), service=str(tmp_path / "other" / "backend" / "x"), host="127.0.0.1",
        port=8081, user="agent", db="local", reload=True, log="", started_at="", on_demand=True,
    )
    instances.save({inst.key: inst})
    gateway = proxy.Gateway(routes=[], backend=repo / "backend", remote=None, idle_seconds=lambda: 0)
    assert gateway.sleepy(now=10**9) == []


# --------------------------------------------------------------------------- the CLI


def test_ps_shows_asleep_ones_and_clean_keeps_them_while_stop_all_does_not(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from pdms_cli import cli

    monkeypatch.setattr(actions, "strays", lambda cfg: [])
    inst = asleep(tmp_path / "lead-tp-list")
    output = CliRunner().invoke(cli.app, ["ps", "--clean"], env={"COLUMNS": "200", "PDMS_LANG": "en"}).output
    assert "asleep" in output and "proxy is not running" in output
    assert inst.key in instances.load()
    CliRunner().invoke(cli.app, ["stop", "--all"], env={"PDMS_LANG": "en"})
    assert instances.load() == {}  # else the proxy would wake it later
