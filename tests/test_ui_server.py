"""pdms ui server: only this machine's own page, with the session token, reaches the API."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, instances, proxy, routes
from pdms_cli.config import Config, Database, DevUser, Repo, Stack
from pdms_cli.instances import Health, Instance
from pdms_cli.ui import jobs as ui_jobs
from pdms_cli.ui import server as ui_server
from pdms_cli.ui import state as ui_state

TOKEN = "s3cret-token"


@pytest.fixture
def ui():
    """A running UI server on a free port with a fake state; yields ``(port, hub, states, jobs)``."""
    states = [{"instances": [], "n": 0}]
    hub = ui_server.Hub(build=lambda: states[-1], interval=0.05)
    jobs = ui_jobs.Jobs(hub.poke)
    server = ui_server.make_server("127.0.0.1", 0, TOKEN, hub, jobs)
    thread = threading.Thread(target=ui_server.serve, args=(server, hub), daemon=True)
    thread.start()
    yield server.server_address[1], hub, states, jobs
    server.shutdown()
    thread.join(5)


def request(port: int, path: str, headers: dict[str, str] | None = None, method: str = "GET", body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    response = conn.getresponse()
    return response, response.read(), conn


def post(port: int, path: str, body: dict | None = None, **headers: str):
    """A POST like the page sends it: JSON, from its own origin, with the cookie."""
    base = {**cookie(port), "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    response, raw, _conn = request(port, path, {**base, **headers}, "POST", json.dumps(body or {}).encode())
    return response.status, json.loads(raw or b"{}") if raw.startswith(b"{") else {}


def events_of(response):
    """``(name, data)`` of each server-sent event, as they come."""
    name, data = "message", []
    while True:
        line = response.fp.readline().decode().rstrip("\n")
        if line.startswith("event: "):
            name = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])
        elif not line and data:
            yield name, json.loads("".join(data))
            name, data = "message", []


def wait_until(condition, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def cookie(port: int) -> dict[str, str]:
    return {"Cookie": f"pdms_ui_{port}={TOKEN}"}


def test_the_token_is_swapped_for_a_strict_cookie(ui) -> None:
    port, _hub, _states, _jobs = ui
    response, _body, _conn = request(port, f"/?token={TOKEN}")
    assert response.status == 303 and response.getheader("Location") == "/"
    set_cookie = response.getheader("Set-Cookie")
    assert set_cookie.startswith(f"pdms_ui_{port}={TOKEN};") and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie

    response, body, _conn = request(port, "/", cookie(port))
    assert response.status == 200 and b"<title>pdms</title>" in body
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")


@pytest.mark.parametrize("headers", [{}, {"Cookie": "pdms_ui_1=other"}, {"Cookie": "garbage;;=="}])
def test_without_the_token_nothing_is_served(ui, headers) -> None:
    port, _hub, _states, _jobs = ui
    for path in ("/", "/api/state", "/api/stream", "/static/js/main.js"):
        response, _body, _conn = request(port, path, headers)
        assert response.status == 401, path
    assert request(port, "/?token=wrong")[0].status == 403


def test_other_hosts_and_other_pages_are_rejected(ui) -> None:
    port, _hub, _states, _jobs = ui
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host.
    assert request(port, "/api/state", {**cookie(port), "Host": f"evil.example:{port}"})[0].status == 421
    assert request(port, "/api/state", {**cookie(port), "Origin": "http://evil.example"})[0].status == 403
    assert request(port, "/api/state", {**cookie(port), "Origin": "null"})[0].status == 403
    assert request(port, "/api/state", {**cookie(port), "Origin": f"http://localhost:{port}"})[0].status == 200


def test_static_files_stay_inside_the_static_folder(ui) -> None:
    port, _hub, _states, _jobs = ui
    for path in ("/static/js/main.js", "/static/i18n.js"):
        response, _body, _conn = request(port, path, cookie(port))
        assert response.status == 200 and response.getheader("Content-Type") == "text/javascript; charset=utf-8"
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/.hidden", "/static/missing.js",
                 "/static/js/../../server.py", "/static/js/.hidden", "/static/../ui/server.py", "/static/js/x/y.js",
                 "/static/.js/main.js"):
        assert request(port, path, cookie(port))[0].status == 404, path


def test_state_and_stream(ui) -> None:
    port, hub, states, _jobs = ui
    response, body, _conn = request(port, "/api/state", cookie(port))
    assert response.status == 200 and json.loads(body) == {"instances": [], "n": 0}

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{port}", **cookie(port)})
    stream = conn.getresponse()
    assert stream.status == 200 and stream.getheader("Content-Type") == "text/event-stream"

    def next_event() -> dict:
        lines = []
        while (line := stream.fp.readline().decode().rstrip("\n")) or not lines:
            if line.startswith("data: "):
                lines.append(line[len("data: "):])
        return json.loads("".join(lines))

    assert next_event()["n"] == 0
    states.append({"instances": [], "n": 1})  # something changed in the state files
    assert next_event()["n"] == 1
    assert hub.watchers == 1
    conn.close()


def test_the_state_never_carries_database_passwords(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(
        users={"agent": DevUser("u1", "a@x.com")},
        dbs={"shared": Database("db.example.com", user="app", password="hunter2")},
        last_user="agent", last_db="shared",
    )
    inst = Instance(
        key="lead-tp-list@8081", pid=1, service=str(tmp_path / "lead-tp-list"), host="0.0.0.0", port=8081,
        user="agent", db="shared", reload=True, log="x.log", started_at="2026-09-30T10:00:00",
    )
    monkeypatch.setattr(ui_state.instances, "load", lambda: {inst.key: inst})
    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {inst.key: Health("ok")})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    state = ui_state.build_state(cfg)
    assert state["user"] == "agent" and state["db"] == "shared" and state["proxy"] is None
    assert state["instances"][0]["key"] == "lead-tp-list@8081" and state["instances"][0]["status"] == "ok"
    text = json.dumps(state)
    assert "hunter2" not in text and "db.example.com" not in text


# --------------------------------------------------------------------------- actions


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A config and a registry with a live instance (this test process) and a stopped one, each with a log."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(
        users={"agent": DevUser("u1", "a@x.com"), "boss": DevUser("u2", "b@x.com")},
        dbs={"local": Database("localhost"), "shared": Database("db.example.com", password="hunter2", protected=True)},
        last_user="agent", last_db="local",
    )
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(Config, "save", lambda self: None)

    def instance(key: str, pid: int, created: float) -> Instance:
        log = instances.log_path(key)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(f"{key} line {n}\n" for n in range(1, 6)), encoding="utf-8")
        return Instance(
            key=key, pid=pid, created=created, service=str(tmp_path / key.split("@")[0]), host="0.0.0.0",
            port=int(key.split("@")[1]), user="agent", db="local", reload=True, log=str(log),
            started_at="2026-10-01T10:00:00",
        )

    me = os.getpid()
    alive, stopped = instance("svc@8081", me, instances.creation_time(me)), instance("old@8082", me, 1.0)
    instances.save({alive.key: alive, stopped.key: stopped})
    return cfg


def test_actions_only_come_from_the_page_itself(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    body = json.dumps({}).encode()
    no_origin = {**cookie(port), "Content-Type": "application/json"}
    assert request(port, "/api/clean", no_origin, "POST", body)[0].status == 403
    other = {**no_origin, "Origin": "http://evil.example"}
    assert request(port, "/api/clean", other, "POST", body)[0].status == 403
    no_cookie = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    assert request(port, "/api/clean", no_cookie, "POST", body)[0].status == 401
    # A form or a "simple" fetch from another page could send text/plain without a preflight.
    assert post(port, "/api/clean", **{"Content-Type": "text/plain"})[0] == 415
    assert request(port, "/api/clean", {**no_origin, "Origin": f"http://127.0.0.1:{port}"}, "POST", b"[1")[0].status == 400
    assert instances.load().keys() == {"svc@8081", "old@8082"}  # nothing happened


def test_stop_runs_as_a_job_and_reports_its_errors(ui, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    release = threading.Event()
    monkeypatch.setattr(actions, "stop_service", lambda inst: release.wait(5) and instances.forget(inst.key))

    assert post(port, "/api/instances/svc%408081/stop") == (202, {"job": "svc@8081"})
    assert jobs.snapshot()["svc@8081"]["phase"] == "stopping"
    status, data = post(port, "/api/instances/svc@8081/stop")
    assert status == 400 and "busy" in data["error"]
    release.set()
    wait_until(lambda: not jobs.snapshot())
    assert "svc@8081" not in instances.load()

    def broken(inst):
        raise actions.ActionError("could not stop it")

    monkeypatch.setattr(actions, "stop_service", broken)
    assert post(port, "/api/instances/old@8082/stop")[0] == 202
    wait_until(lambda: jobs.snapshot().get("old@8082", {}).get("error"))
    assert jobs.snapshot()["old@8082"]["error"] == "could not stop it"
    assert post(port, "/api/instances/old@8082/dismiss")[0] == 200 and not jobs.snapshot()
    assert post(port, "/api/instances/nope@1/stop") == (400, {"error": "There is no instance nope@1."})
    assert post(port, "/api/instances/old@8082/explode")[0] == 404


def test_restart_asks_before_a_protected_database_and_logs_the_install(ui, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    calls = []

    def restart_service(cfg, inst, *, user_name, db_name, confirmed, install):
        install(Path(inst.service))
        calls.append((inst.key, user_name, db_name, confirmed))
        return inst

    def install_service(service, output):
        output.write("Installing dependencies from lock file\n")

    monkeypatch.setattr(actions, "restart_service", restart_service)
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: install is True)
    monkeypatch.setattr(actions, "install_service", install_service)

    status, data = post(port, "/api/instances/svc@8081/restart", {"user": "boss", "db": "shared"})
    assert status == 409 and data == {"decision": "protected_database", "name": "shared"}
    assert not calls and not jobs.snapshot()  # nothing was stopped

    assert post(port, "/api/instances/svc@8081/restart", {"user": "ghost"}) == (
        400, {"error": "'ghost' does not exist (user). Available: agent, boss"},
    )
    assert post(port, "/api/instances/svc@8081/restart", {"install": "yes"})[0] == 400

    body = {"user": "boss", "db": "shared", "confirmed": True, "install": True}
    assert post(port, "/api/instances/svc@8081/restart", body)[0] == 202
    wait_until(lambda: calls and not jobs.snapshot())
    assert calls == [("svc@8081", "boss", "shared", True)]
    assert machine.last_user == "boss" and machine.last_db == "shared"  # the defaults next time, like the CLI

    response, raw, _conn = request(port, "/api/logs?key=svc@8081&which=install", cookie(port))
    assert response.status == 200 and json.loads(raw)["lines"] == ["Installing dependencies from lock file"]


def test_restart_needs_the_local_elasticmq_when_it_published_there(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    registry = instances.load()
    registry["svc@8081"].events = "local"
    instances.save(registry)
    monkeypatch.setattr(ui_jobs.events, "running", lambda port: False)
    status, data = post(port, "/api/instances/svc@8081/restart")
    assert status == 409 and data["decision"] == "local_events_down" and "pdms events up" in data["error"]


def test_forget_only_stopped_instances(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    status, data = post(port, "/api/instances/svc@8081/forget")
    assert status == 400 and "stop it first" in data["error"]
    assert post(port, "/api/clean") == (200, {"forgotten": ["old@8082"]})
    assert instances.load().keys() == {"svc@8081"}


def test_strays_are_adopted_or_stopped_and_looked_for_again(ui, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    monkeypatch.setattr(ui_jobs.Config, "load", staticmethod(lambda: Config()))
    calls = []
    monkeypatch.setattr(ui_jobs.actions, "adopt_strays", lambda cfg, keys: calls.append(("adopt", keys)) or [
        SimpleNamespace(key="svc@8090")])
    monkeypatch.setattr(ui_jobs.actions, "stop_strays", lambda cfg, keys: calls.append(("stop", keys)) or keys)
    ui_state._strays["at"] = time.monotonic()  # a fresh scan, which an action must throw away
    assert post(port, "/api/strays/adopt") == (200, {"adopted": ["svc@8090"]})
    assert ui_state._strays["at"] == 0.0
    assert post(port, "/api/strays/stop", {"keys": ["svc@8091"]}) == (200, {"stopped": ["svc@8091"]})
    assert post(port, "/api/strays/stop", {"keys": "svc@8091"})[0] == 400
    assert calls == [("adopt", None), ("stop", ["svc@8091"])]


def test_logs_tail_and_unknown_logs(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    response, raw, _conn = request(port, "/api/logs?key=svc@8081&lines=2", cookie(port))
    assert response.status == 200
    assert json.loads(raw) == {"key": "svc@8081", "which": "current", "exists": True,
                               "lines": ["svc@8081 line 4", "svc@8081 line 5"]}
    response, raw, _conn = request(port, "/api/logs?key=svc@8081&which=previous", cookie(port))
    assert json.loads(raw)["exists"] is False
    for query in ("key=nope@1", "key=svc@8081&which=../../etc/passwd", "key=../registry"):
        assert request(port, f"/api/logs?{query}", cookie(port))[0].status == 404, query
    assert request(port, "/api/logs?key=svc@8081", {})[0].status == 401


def test_the_log_stream_follows_the_file_across_a_restart(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    log = instances.log_path("svc@8081")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/logs/stream?key=svc@8081&lines=1", headers={"Host": f"127.0.0.1:{port}", **cookie(port)})
    response = conn.getresponse()
    assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream"
    stream = events_of(response)
    assert next(stream) == ("lines", ["svc@8081 line 5"])

    with open(log, "a", encoding="utf-8") as fh:
        fh.write("GET /docs 200\n")
    assert next(stream) == ("lines", ["GET /docs 200"])

    fresh = log.with_name("fresh.log")
    fresh.write_text("# pdms restarted\n", encoding="utf-8")
    os.replace(fresh, log)  # what a restart does: the old log rotates, a new file starts
    assert next(stream) == ("reset", [])
    assert next(stream) == ("lines", ["# pdms restarted"])
    conn.close()


def test_the_state_lists_users_and_databases_without_secrets(machine, monkeypatch) -> None:
    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {i.key: Health("ok") for i in items})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    jobs = {"svc@8081": {"key": "svc@8081", "action": "restart", "phase": "installing"}}
    state = ui_state.build_state(machine, jobs)
    assert state["users"] == ["agent", "boss"]
    assert state["dbs"] == [{"name": "local", "protected": False}, {"name": "shared", "protected": True}]
    assert state["jobs"] == jobs
    assert "hunter2" not in json.dumps(state) and "db.example.com" not in json.dumps(state)


def test_needs_install_follows_the_flag_and_the_settings(monkeypatch, tmp_path) -> None:
    cfg = Config()
    monkeypatch.setattr(actions.runner, "poetry_python", lambda service: "python")
    monkeypatch.setattr(actions.installer, "is_up_to_date", lambda service: True)
    assert actions.needs_install(cfg, tmp_path, True) is True
    assert actions.needs_install(cfg, tmp_path, False) is False
    cfg.defaults.smart_install = True
    assert actions.needs_install(cfg, tmp_path, None) is False  # nothing changed since the last install
    monkeypatch.setattr(actions.installer, "is_up_to_date", lambda service: False)
    assert actions.needs_install(cfg, tmp_path, None) is cfg.defaults.install


# --------------------------------------------------------------------------- stacks


@pytest.fixture
def repo(machine, monkeypatch, tmp_path):
    """The machine's config with a current repo of three services and a stack ``leads`` of two of them."""
    backend = tmp_path / "pdms" / "backend"
    for svc in ("lead/lead-list", "lead/lead-get", "user/user-me"):
        (backend / svc).mkdir(parents=True)
        (backend / svc / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")
        (backend / svc / "main.py").write_text("app = None\n", encoding="utf-8")
    machine.repos = {"pdms": Repo(str(tmp_path / "pdms"))}
    machine.current_repo = "pdms"
    machine.stacks = {"leads": Stack(["lead/lead-list", "lead/lead-get"])}
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.events, "running", lambda port: False)  # events go to AWS
    return backend.resolve()


def test_up_asks_before_a_protected_database_and_installs_each_service(ui, repo, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    started = []
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch, install=None: started.append(launch))
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: service.name == "lead-get")
    monkeypatch.setattr(actions, "install_service", lambda service, output: output.write(f"installed {service.name}\n"))

    status, data = post(port, "/api/stacks/leads/up", {"db": "shared"})
    assert status == 409 and data == {"decision": "protected_database", "name": "shared"} and not started

    assert post(port, "/api/stacks/leads/up", {"db": "shared", "confirmed": True})[0] == 202
    wait_until(lambda: len(started) == 2 and not jobs.snapshot())
    assert [launch.service for launch in started] == [repo / "lead/lead-list", repo / "lead/lead-get"]
    assert {launch.user_name for launch in started} == {"agent"} and started[0].port != started[1].port
    assert machine.last_db == "shared"
    get = started[1]
    log = ui_jobs.install_log(instances.make_key(get.service, get.port))
    assert log.read_text(encoding="utf-8") == "installed lead-get\n"


    def failing(service, output):
        output.write("Because lead-get depends on boto3 (^9), version solving failed.\n")
        raise actions.ActionError("poetry lock failed (exit code 1).")

    started.clear()
    monkeypatch.setattr(actions, "install_service", failing)
    assert post(port, "/api/stacks/leads/up", {"db": "local"})[0] == 202
    wait_until(lambda: jobs.snapshot().get("stack:leads", {}).get("error"))
    job = jobs.snapshot()["stack:leads"]
    assert job["error"] == "poetry lock failed (exit code 1)." and job["log_key"].startswith("lead-get@")
    assert [launch.service.name for launch in started] == ["lead-list"]  # stopped at the failing service
    _response, raw, _conn = request(port, f"/api/logs?key={job['log_key']}&which=install", cookie(port))
    assert json.loads(raw)["lines"] == ["Because lead-get depends on boto3 (^9), version solving failed."]


def test_up_and_down_with_nothing_to_do(ui, repo, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    monkeypatch.setattr(actions, "running_by_service",
                        lambda: {str(repo / "lead/lead-list"): [8081], str(repo / "lead/lead-get"): [8082]})
    assert post(port, "/api/stacks/leads/up") == (200, {"job": None})
    monkeypatch.setattr(actions, "stack_instances", lambda cfg, name, root: [])
    assert post(port, "/api/stacks/leads/down") == (200, {"job": None})
    assert not jobs.snapshot()


def test_down_stops_each_running_service(ui, repo, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    running = [instances.load()["svc@8081"]]
    stopped = []
    monkeypatch.setattr(actions, "stack_instances", lambda cfg, name, root: running if name == "leads" else [])
    monkeypatch.setattr(actions, "stop_service", lambda inst: stopped.append(inst.key))
    assert post(port, "/api/stacks/leads/down") == (202, {"job": "stack:leads"})
    wait_until(lambda: stopped and not jobs.snapshot())
    assert stopped == ["svc@8081"]


def test_start_one_service_asks_first_and_logs_the_install(ui, repo, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    started = []
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch, install=None: started.append(launch))
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: install is True)
    monkeypatch.setattr(actions, "install_service", lambda service, output: output.write("installed\n"))
    monkeypatch.setattr(actions, "port_available", lambda host, port, taken=None: port != 9100)
    monkeypatch.setattr(actions.runner, "next_free_port", lambda host, port, taken=None: 9101)

    assert post(port, "/api/run", {"service": "user/nope"}) == (
        400, {"error": "Not services of the current repo: user/nope"},
    )
    assert post(port, "/api/run", {"service": "user/user-me", "port": "8080"})[0] == 400
    assert post(port, "/api/run", {})[0] == 400
    status, data = post(port, "/api/run", {"service": "user/user-me", "port": 9100})
    assert status == 409 and data == {"decision": "port_busy", "port": 9100, "free": 9101}
    status, data = post(port, "/api/run", {"service": "user/user-me", "port": 9101, "db": "shared"})
    assert status == 409 and data == {"decision": "protected_database", "name": "shared"}
    assert not started and not jobs.snapshot()

    body = {"service": "user/user-me", "port": 9101, "user": "boss", "db": "shared", "confirmed": True, "install": True}
    status, data = post(port, "/api/run", body)
    assert status == 202 and data == {"job": "user-me@9101"}
    wait_until(lambda: started and not jobs.snapshot())
    launch = started[0]
    assert (launch.service, launch.port, launch.user_name, launch.db_name) == (repo / "user/user-me", 9101, "boss", "shared")
    assert machine.last_user == "boss" and machine.last_db == "shared"
    assert ui_jobs.install_log("user-me@9101").read_text(encoding="utf-8") == "installed\n"


def test_start_one_service_takes_the_next_free_port(ui, repo, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    started = []
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch, install=None: started.append(launch))
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: False)
    monkeypatch.setattr(actions, "suggested_port", lambda cfg, host: 9102)
    assert post(port, "/api/run", {"service": "lead/lead-get"}) == (202, {"job": "lead-get@9102"})
    wait_until(lambda: started and not jobs.snapshot())
    assert started[0].user_name == "agent" and started[0].db_name == "local"  # the last ones, like pdms run


def test_a_stale_stack_says_so_without_terminal_markup(ui, repo) -> None:
    port, _hub, _states, _jobs = ui
    (repo / "lead/lead-get/main.py").unlink()
    status, data = post(port, "/api/stacks/leads/up")
    assert status == 400 and "'lead/lead-get' is no longer a service" in data["error"]
    assert "pdms stack edit" in data["error"] and "[bold]" not in data["error"]


def test_save_and_remove_stacks(ui, repo, machine) -> None:
    port, _hub, _states, _jobs = ui
    _response, raw, _conn = request(port, "/api/services", cookie(port))
    assert json.loads(raw) == {
        "root": str(repo), "services": ["lead/lead-get", "lead/lead-list", "user/user-me"], "consumers": [],
    }

    def save(name, services, new=True, **extra):
        return post(port, f"/api/stacks/{name}/save", {"services": services, "new": new, **extra})

    assert save("bad%20name", ["user/user-me"]) == (
        400, {"error": "Use only letters, numbers, '-' or '_'", "field": "name"}
    )
    assert save("leads", ["user/user-me"]) == (400, {"error": "That name already exists", "field": "name"})
    assert save("me", ["user/nope"]) == (400, {"error": "Not services of the current repo: user/nope"})
    assert save("me", [])[0] == 400
    assert save("me", "user/user-me")[0] == 400
    assert save("me", ["user/user-me"], user="ghost")[0] == 400
    assert save("me", ["user/user-me", "lead/lead-get"], user="boss", db="shared") == (200, {})
    assert machine.stacks["me"] == Stack(["user/user-me", "lead/lead-get"], user="boss", db="shared")

    assert save("leads", ["lead/lead-list"], new=False) == (200, {})
    assert machine.stacks["leads"] == Stack(["lead/lead-list"])
    assert save("ghosts", ["lead/lead-list"], new=False)[0] == 400

    assert post(port, "/api/stacks/me/remove") == (200, {})
    assert "me" not in machine.stacks
    assert post(port, "/api/stacks/me/remove")[0] == 400


def test_the_state_shows_which_stack_services_run(repo, machine, monkeypatch) -> None:
    registry = instances.load()
    registry["svc@8081"].service = str(repo / "lead/lead-list")
    instances.save(registry)
    monkeypatch.setattr(ui_state.instances, "health_all",
                        lambda items: {i.key: Health("ok" if i.alive() else "stopped") for i in items})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    assert ui_state.build_state(machine)["stacks"] == [{
        "name": "leads", "user": "", "db": "",
        "services": [{"path": "lead/lead-list", "running": ["svc@8081"]}, {"path": "lead/lead-get", "running": []}],
    }]



def test_the_local_sns_is_a_row_with_its_log(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    from pdms_cli import events

    log = events.sns_log_path()
    log.write_text("2026-10-01T10:00:00+00:00 account-publish-ev → sns-account-publish.fifo\n  {}\n", encoding="utf-8")
    _response, raw, _conn = request(port, "/api/logs?key=sns", cookie(port))
    assert json.loads(raw)["lines"] == ["2026-10-01T10:00:00+00:00 account-publish-ev → sns-account-publish.fifo", "  {}"]
    assert request(port, "/api/logs?key=sns&which=install", cookie(port))[0].status == 404

    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {i.key: Health("ok") for i in items})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: True)
    sns = ui_state.build_state(machine)["sns"]
    assert sns["key"] == "sns" and sns["queue"] == "pdms-sns" and sns["status"] == "ok" and sns["last_publish"]
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    log.unlink()
    assert ui_state.build_state(machine)["sns"] == {"key": "sns", "queue": "pdms-sns", "status": "off", "last_publish": ""}


# --------------------------------------------------------------------------- proxy


@pytest.fixture
def proxy_repo(repo, machine, monkeypatch):
    """The repo with a frontend, a dev Terraform environment and two routes, one of them served by ``svc@8081``."""
    root = repo.parent
    (root / "frontend").mkdir()
    (root / "frontend" / ".env").write_text("VITE_APP_API_URL=https://api.example.com/dev\n", encoding="utf-8")
    for env in ("dev", "qa"):
        (root / "infra" / "infra_auto" / "environments" / env).mkdir(parents=True)
    registry = instances.load()
    registry["svc@8081"].service = str(repo / "lead/lead-list")
    instances.save(registry)
    monkeypatch.setattr(actions.routes, "load_routes", lambda root, env: [
        routes.Route("GET", "/api/v1/leads", "lead/lead-list"), routes.Route("GET", "/api/v1/me", "user/user-me"),
    ])
    monkeypatch.setattr(actions, "free_port", lambda host, port: port)
    return root


def test_proxy_options_and_routes(ui, proxy_repo) -> None:
    port, _hub, _states, _jobs = ui
    _response, raw, _conn = request(port, "/api/proxy/options", cookie(port))
    assert json.loads(raw) == {"port": 28800, "env": "dev", "envs": ["dev", "qa"],
                               "remote": "https://api.example.com/dev", "frontend": True}
    _response, raw, _conn = request(port, "/api/proxy/routes", cookie(port))
    assert json.loads(raw) == {"env": "dev", "remote": "https://api.example.com/dev", "routes": [
        {"method": "GET", "path": "/api/v1/leads", "service": "lead/lead-list", "target": "local", "key": "svc@8081",
         "note": ""},
        {"method": "GET", "path": "/api/v1/me", "service": "user/user-me", "target": "remote", "key": "", "note": ""},
    ]}
    assert request(port, "/api/proxy/routes?env=../../etc", cookie(port))[0].status == 400
    response, raw, _conn = request(port, "/api/proxy/routes?env=prod", cookie(port))
    assert response.status == 400 and "No Terraform for 'prod'" in json.loads(raw)["error"]


def test_proxy_start_asks_first_and_runs_as_a_job(ui, proxy_repo, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    started = []

    def start_proxy(plan):
        started.append(plan)
        return actions.ProxyStarted(1, plan.port, proxy.log_path())

    monkeypatch.setattr(actions, "start_proxy", start_proxy)
    monkeypatch.setattr(actions, "wait_for_proxy", lambda started: "ok")

    def busy(host, port):
        raise actions.PortBusy(port, port + 1)

    with monkeypatch.context() as m:
        m.setattr(actions, "free_port", busy)
        assert post(port, "/api/proxy/start", {"port": 8000}) == (
            409, {"decision": "port_busy", "port": 8000, "free": 8001},
        )
    assert post(port, "/api/proxy/start", {"port": 8001}) == (
        409, {"decision": "point_frontend", "url": "http://localhost:8001"},
    )
    for body in ({"port": "8001"}, {"port": 0}, {"port": True}, {"env": "../x"}, {"frontend": "yes"},
                 {"user": "ghost", "frontend": False}):
        assert post(port, "/api/proxy/start", body)[0] == 400, body
    assert not started and not jobs.snapshot()

    body = {"port": 8001, "env": "qa", "user": "boss", "frontend": True, "remote": "https://other.example.com/qa/"}
    assert post(port, "/api/proxy/start", body) == (202, {"job": "proxy"})
    wait_until(lambda: started and not jobs.snapshot())
    plan = started[0]
    assert (plan.port, plan.env, plan.user_name, plan.remote) == (8001, "qa", "boss", "https://other.example.com/qa")
    assert machine.repos["pdms"].remote == "https://other.example.com/qa"  # saved for the repo, like pdms proxy
    env_local = proxy_repo / "frontend" / ".env.local"
    assert "VITE_APP_API_URL=http://localhost:8001" in env_local.read_text(encoding="utf-8")
    assert proxy.frontend_change() == str(env_local)


def test_a_proxy_that_dies_while_starting_says_why(ui, proxy_repo, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    stopped = []

    def start_proxy(plan):
        proxy.log_path().parent.mkdir(parents=True, exist_ok=True)
        proxy.log_path().write_text("# proxy\nOSError: [Errno 98] Address already in use\n", encoding="utf-8")
        return actions.ProxyStarted(1, plan.port, proxy.log_path())

    monkeypatch.setattr(actions, "start_proxy", start_proxy)
    monkeypatch.setattr(actions, "wait_for_proxy", lambda started: "stopped")
    monkeypatch.setattr(actions, "stop_proxy", lambda: stopped.append(True))
    assert post(port, "/api/proxy/start", {"frontend": False, "no_remote": True})[0] == 202
    wait_until(lambda: jobs.snapshot().get("proxy", {}).get("error"))
    assert jobs.snapshot()["proxy"]["error"] == "The proxy exited while starting: OSError: [Errno 98] Address already in use"
    assert stopped == [True]
    assert post(port, "/api/proxy/start", {"frontend": False})[0] == 202  # a failed start does not block the next


def test_the_proxy_is_started_only_once(ui, proxy_repo, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    monkeypatch.setattr(actions.proxy, "running_proxy", lambda: {"pid": 7, "port": 8000})
    status, data = post(port, "/api/proxy/start", {"frontend": False})
    assert status == 400 and data["error"] == "The proxy is already running on port 8000 (pid 7)."


def test_the_state_shows_the_running_proxy(proxy_repo, machine, monkeypatch) -> None:
    running = {"pid": 7, "port": 8001, "repo": str(proxy_repo), "env": "qa", "remote": "", "as": "boss",
               "started_at": "2026-10-01T10:00:00", "background": True, "timeout": 300}
    monkeypatch.setattr(ui_state.proxy, "running_proxy", lambda: running)
    monkeypatch.setattr(ui_state.instances, "responds", lambda host, port: True)
    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {i.key: Health("ok") for i in items})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    proxy.remember_frontend_change({"path": "/repo/frontend/.env.local", "proxy_url": "x", "previous": {}})
    assert ui_state.build_state(machine)["proxy"] == {
        "key": "proxy@8001", "pid": 7, "port": 8001, "repo": str(proxy_repo), "repo_alias": "pdms", "env": "qa",
        "remote": "", "as": "boss", "timeout": 300, "frontend": "/repo/frontend/.env.local", "started_at": "2026-10-01T10:00:00",
        "background": True, "status": "ok",
    }


# --------------------------------------------------------------------------- local events


@pytest.fixture
def elasticmq(repo, machine, monkeypatch):
    """The repo with a broker and an event map, and a fake local ElasticMQ: ``up``, its ``counts``, what was
    ``sent``, ``purged`` and whether it was ``stopped``."""
    from pdms_cli import events

    broker = repo / "broker" / "broker-sqs-event"
    broker.mkdir(parents=True)
    (broker / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")
    (broker / "main.py").write_text("app = None\n", encoding="utf-8")
    event_map = events.EventMap(
        queues={"broker.fifo": events.Queue("broker.fifo"), "email.fifo": events.Queue("email.fifo")},
        consumers={"broker.fifo": events.Consumer("broker/broker-sqs-event", "main.handler"),
                   "email.fifo": events.Consumer("notification/email-notify", "main.handler")},
        routes={"email-notify": "email.fifo", "sms-notify": "email.fifo"}, broker_queue="broker.fifo",
    )
    fake = {"up": True, "counts": {"broker.fifo": {"visible": 0, "in_flight": 0},
                                   "email.fifo": {"visible": 2, "in_flight": 1},
                                   "pdms-sns": {"visible": 5, "in_flight": 0}},
            "sent": [], "purged": [], "stopped": False}
    monkeypatch.setattr(events, "load_event_map", lambda root, env="dev": event_map)
    monkeypatch.setattr(events, "is_up", lambda port: fake["up"])
    monkeypatch.setattr(events, "running", lambda port: fake["up"])
    monkeypatch.setattr(events, "queue_counts", lambda port: fake["counts"])
    monkeypatch.setattr(events, "send", lambda port, queue, body, fifo=True: fake["sent"].append((queue, body, fifo)) or "m-123456789")
    monkeypatch.setattr(events, "purge", lambda port, queue: fake["purged"].append(queue))
    monkeypatch.setattr(events, "stop", lambda: fake.update(stopped=True) or True)
    return fake


def get(port: int, path: str):
    response, raw, _conn = request(port, path, cookie(port))
    return response.status, json.loads(raw)


def test_events_queues_and_map(ui, elasticmq) -> None:
    port, _hub, _states, _jobs = ui
    status, data = get(port, "/api/events/queues")
    assert status == 200 and data["up"] and data["broker"] == "broker.fifo" and data["broker_service"]
    queues = {queue["name"]: queue for queue in data["queues"]}
    assert list(queues) == ["broker.fifo", "email.fifo", "pdms-sns"]
    assert queues["email.fifo"] == {
        "name": "email.fifo", "fifo": True, "source": "terraform", "types": 2, "visible": 2, "in_flight": 1,
        "consumer": "notification/email-notify", "running": "", "broker": False, "sns": False,
    }
    assert queues["pdms-sns"]["sns"] and not queues["pdms-sns"]["fifo"] and queues["broker.fifo"]["broker"]

    elasticmq["up"] = False
    _status, data = get(port, "/api/events/queues")
    assert not data["up"] and [q["visible"] for q in data["queues"]] == [None, None]  # the repo's, without counts

    assert get(port, "/api/events/map") == (200, {"broker": "broker.fifo", "types": [
        {"type": "email-notify", "queue": "email.fifo", "consumer": "notification/email-notify"},
        {"type": "sms-notify", "queue": "email.fifo", "consumer": "notification/email-notify"},
    ]})


def test_events_peek_and_template(ui, elasticmq, repo, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    from pdms_cli import events

    monkeypatch.setattr(events, "peek", lambda port, queue, limit: [{
        "MessageId": "m1", "Body": '{"type": "email-notify"}',
        "Attributes": {"ApproximateReceiveCount": "2", "SentTimestamp": "1790850000000"},
    }])
    assert get(port, "/api/events/peek?queue=email.fifo") == (200, {"queue": "email.fifo", "messages": [
        {"id": "m1", "body": '{"type": "email-notify"}', "receives": 2, "sent": 1790850000000, "group": ""},
    ]})
    status, data = get(port, "/api/events/peek?queue=nope")
    assert status == 400 and "Unknown queue 'nope'" in data["error"]

    monkeypatch.setattr(events, "event_template", lambda root, event_type: {"to": "", "subject": ""})
    assert get(port, "/api/events/template?type=email-notify") == (200, {"to": "", "subject": ""})
    assert get(port, "/api/events/template?type=email.fifo")[0] == 400

    elasticmq["up"] = False
    status, data = get(port, "/api/events/peek?queue=email.fifo")
    assert status == 400 and data["error"] == "ElasticMQ is not running. Start it with pdms events up."


def test_events_send_and_purge(ui, elasticmq) -> None:
    port, _hub, _states, _jobs = ui
    status, data = post(port, "/api/events/send", {"target": "email-notify", "body": '{"to": "a@x.com"}'})
    assert status == 200 and data == {"id": "m-123456789", "queue": "broker.fifo", "routed_to": "email.fifo",
                                      "consumer": "notification/email-notify", "consumed": False}
    queue, body, fifo = elasticmq["sent"][-1]
    assert queue == "broker.fifo" and fifo and json.loads(body)["to"] == "a@x.com"

    status, data = post(port, "/api/events/send", {"target": "email-notify", "body": "{}", "direct": True})
    assert data["queue"] == "email.fifo" and data["routed_to"] == ""
    assert post(port, "/api/events/send", {"target": "email-notify", "body": "{"})[0] == 400
    assert post(port, "/api/events/send", {"target": ""})[0] == 400
    assert post(port, "/api/events/send", {"target": "email.fifo", "body": {"a": 1}})[0] == 400

    assert post(port, "/api/events/purge", {"queues": ["email.fifo"]}) == (200, {"purged": ["email.fifo"]})
    assert post(port, "/api/events/purge", {"queues": []}) == (200, {"purged": ["email.fifo", "pdms-sns"]})
    assert post(port, "/api/events/purge", {"queues": ["nope"]})[0] == 400
    assert post(port, "/api/events/purge", {"queues": "email.fifo"})[0] == 400
    assert elasticmq["purged"] == ["email.fifo", "email.fifo", "pdms-sns"]

    elasticmq["up"] = False
    status, data = post(port, "/api/events/send", {"target": "email-notify"})
    assert status == 400 and "ElasticMQ is not running" in data["error"] and "[bold]" not in data["error"]


def test_events_up_asks_before_a_protected_database_and_starts_the_broker(ui, elasticmq, repo, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    started, launched = [], []
    monkeypatch.setattr(actions, "start_events", lambda cfg, queues: started.append(sorted(queues)) or "created")
    monkeypatch.setattr(actions, "plan_service", lambda cfg, service, **kw: SimpleNamespace(
        service=service, port=0, **kw))
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch: launched.append(launch))
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: False)

    status, data = post(port, "/api/events/up", {"db": "shared"})
    assert status == 409 and data == {"decision": "protected_database", "name": "shared"} and not started
    assert post(port, "/api/events/up", {"broker": "yes"})[0] == 400

    assert post(port, "/api/events/up", {"user": "boss", "db": "shared", "confirmed": True}) == (
        202, {"job": "events:elasticmq"},
    )
    wait_until(lambda: launched and not jobs.snapshot())
    assert started == [["broker.fifo", "email.fifo"]]
    launch = launched[0]
    assert launch.service == repo / "broker/broker-sqs-event" and launch.user_name == "boss"
    assert launch.db_name == "shared" and launch.events_mode == "local"

    launched.clear()
    assert post(port, "/api/events/up", {"broker": False, "db": "shared"})[0] == 202  # no broker: no database asked
    wait_until(lambda: len(started) == 2 and not jobs.snapshot())
    assert not launched


def test_events_up_failures_stay_on_the_page(ui, elasticmq, monkeypatch) -> None:
    port, _hub, _states, jobs = ui

    def broken(cfg, queues):
        raise actions.ActionError("Docker is not available: [bold]docker[/] not found")

    monkeypatch.setattr(actions, "start_events", broken)
    assert post(port, "/api/events/up", {"broker": False})[0] == 202
    wait_until(lambda: jobs.snapshot().get("events:elasticmq", {}).get("error"))
    assert post(port, "/api/events/dismiss") == (200, {}) and not jobs.snapshot()


def test_events_down_stops_the_consumers_and_elasticmq(ui, elasticmq, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    registry = instances.load()
    registry["svc@8081"].queue = "email.fifo"
    instances.save(registry)
    stopped = []
    monkeypatch.setattr(actions, "stop_service", lambda inst: stopped.append(inst.key))
    assert post(port, "/api/events/down") == (202, {"job": "events:elasticmq"})
    wait_until(lambda: elasticmq["stopped"] and not jobs.snapshot())
    assert stopped == ["svc@8081"]


# --------------------------------------------------------------------------- settings


def test_settings_carry_no_password_until_asked_for_one(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    machine.stacks = {"leads": Stack(["lead/a"], user="agent", db="shared")}
    status, data = get(port, "/api/config")
    assert status == 200 and "hunter2" not in json.dumps(data)
    shared = next(db for db in data["dbs"] if db["name"] == "shared")
    assert shared["has_password"] and shared["protected"] and shared["stacks"] == ["leads"] and "password" not in shared
    assert data["users"][0] == {
        "name": "agent", "user_id": "u1", "username": "a@x.com", "first_name": "", "last_name": "", "roles": "",
        "stacks": ["leads"],
    }
    assert data["defaults"]["port"] == 28100 and data["choices"]["events"] == ["auto", "local", "aws"]

    assert post(port, "/api/dbs/shared/password") == (200, {"password": "hunter2"})
    assert post(port, "/api/dbs/nope/password")[0] == 400
    response, _raw, _conn = request(port, "/api/dbs/shared/password", {**cookie(port), "Content-Type": "application/json"},
                                    "POST", b"{}")
    assert response.status == 403  # not from the page itself


def test_save_and_remove_databases(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    form = {"host": "db.example.com", "port": 5433, "database": "pdm", "user": "app", "password": None}
    assert post(port, "/api/dbs/shared/save", {**form, "protected": True}) == (200, {"name": "shared"})
    assert machine.dbs["shared"].password == "hunter2" and machine.dbs["shared"].port == 5433  # null keeps it
    assert post(port, "/api/dbs/shared/save", {**form, "password": ""})[0] == 200
    assert machine.dbs["shared"].password == "" and not machine.dbs["shared"].protected
    assert post(port, "/api/dbs/local/save", {**form, "new": True}) == (
        400, {"error": "That name already exists", "field": "name"})
    assert post(port, "/api/dbs//save", {**form, "new": True}) == (400, {"error": "Required field", "field": "name"})
    assert post(port, "/api/dbs/qa/save", {**form, "port": "x", "new": True}) == (
        400, {"error": "Must be a number between 1 and 65535", "field": "port"})
    assert post(port, "/api/dbs/qa/save", {**form, "host": ["no"], "new": True}) == (400, {"error": "host must be a text"})
    assert post(port, "/api/dbs/qa/save", {**form, "new": True}) == (200, {"name": "qa"})

    machine.stacks = {"leads": Stack(["lead/a"], db="qa")}
    assert post(port, "/api/dbs/qa/remove") == (200, {"stacks": ["leads"]})
    assert "qa" not in machine.dbs and machine.stacks["leads"].db == ""
    assert post(port, "/api/dbs/qa/remove")[0] == 400


def test_test_a_saved_database_or_the_form(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    tried = []

    def connect(db, timeout):
        tried.append((db.host, db.password, timeout))
        if db.host == "down.example.com":
            raise OSError("connection refused")
        return "PostgreSQL 16.4, compiled by gcc"

    monkeypatch.setattr(actions.runner, "test_connection", connect)
    assert post(port, "/api/dbs/test", {"name": "shared"}) == (200, {"version": "PostgreSQL 16.4"})
    form = {"host": "other.example.com", "port": 5432, "database": "pdm", "user": "app", "password": None}
    assert post(port, "/api/dbs/test", {**form, "name": "shared"})[0] == 200  # the saved password, not saved again
    assert post(port, "/api/dbs/test", {**form, "host": "down.example.com", "name": ""}) == (
        400, {"error": "connection refused"})
    assert post(port, "/api/dbs/test", {**form, "user": "", "name": ""}) == (
        400, {"error": "Required field", "field": "user"})
    assert tried == [("db.example.com", "hunter2", 15), ("other.example.com", "hunter2", 15),
                     ("down.example.com", "", 15)]
    assert machine.dbs["shared"].host == "db.example.com"


def test_save_and_remove_users(ui, machine) -> None:
    port, _hub, _states, _jobs = ui
    form = {"user_id": " u3 ", "username": "c@x.com", "first_name": "Carla", "last_name": "", "roles": "TPR.Agent"}
    assert post(port, "/api/users/carla/save", {**form, "new": True}) == (200, {"name": "carla"})
    assert machine.users["carla"] == DevUser("u3", "c@x.com", "Carla", "", "TPR.Agent")
    assert post(port, "/api/users/carla/save", {**form, "username": ""}) == (
        400, {"error": "Required field", "field": "username"})
    assert post(port, "/api/users/carla/remove") == (200, {"stacks": []})
    assert post(port, "/api/users/carla/password") == (404, {"error": "not found"})


def test_save_defaults_checks_each_kind_and_applies_the_language(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    languages = []
    monkeypatch.setattr(ui_jobs.i18n, "set_language", languages.append)
    assert post(port, "/api/defaults/save", {"reload": "yes"}) == (400, {"error": "must be true or false", "field": "reload"})
    assert post(port, "/api/defaults/save", {"port": True}) == (400, {"error": "must be a number", "field": "port"})
    assert post(port, "/api/defaults/save", {"env": {"A": 1}}) == (400, {"error": "must be texts by name", "field": "env"})
    assert post(port, "/api/defaults/save", {"env": {"BAD NAME": "1"}}) == (
        400, {"error": "'BAD NAME' is not a valid variable name", "field": "env"})
    assert post(port, "/api/defaults/save", {"logging_level": "TRACE"})[1]["field"] == "logging_level"
    assert machine.defaults.reload is True and languages == []

    assert post(port, "/api/defaults/save", {"language": "es", "port": "9000", "reload": False, "env": {"X": "1"}}) == (200, {})
    assert (machine.defaults.language, machine.defaults.port, machine.defaults.reload) == ("es", 9000, False)
    assert machine.defaults.env == {"X": "1"} and machine.defaults.host == "0.0.0.0" and languages == ["es"]


def test_the_page_has_an_icon(ui) -> None:
    port, _hub, _states, _jobs = ui
    response, body, _conn = request(port, "/static/icon.svg", cookie(port))
    assert response.status == 200 and response.getheader("Content-Type") == "image/svg+xml" and body.startswith(b"<svg")
    _response, page, _conn = request(port, "/", cookie(port))
    assert b'<link rel="icon" href="/static/icon.svg"' in page


def test_user_roles_must_be_roles_of_the_repo(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    monkeypatch.setattr(ui_jobs.actions, "known_roles", lambda cfg: ["TPR.Agent", "TPR.Supervisor"])
    assert get(port, "/api/config")[1]["roles"] == ["TPR.Agent", "TPR.Supervisor"]
    form = {"user_id": "u3", "username": "c@x.com", "roles": "TPR.Agent,Boss", "new": True}
    assert post(port, "/api/users/carla/save", form) == (
        400, {"error": "Unknown roles: Boss. Available: TPR.Agent, TPR.Supervisor", "field": "roles"})
    machine.users["boss"].roles = "Legacy.Role"  # an import kept a role the repo does not know
    assert post(port, "/api/users/boss/save", {"user_id": "u2", "username": "b@x.com",
                                              "roles": "Legacy.Role, TPR.Supervisor"})[0] == 200
    assert machine.users["boss"].roles == "Legacy.Role,TPR.Supervisor"


def test_export_and_import_the_settings(ui, machine, monkeypatch, tmp_path) -> None:
    port, _hub, _states, _jobs = ui
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("", encoding="utf-8")  # not a first setup
    status, exported = post(port, "/api/config/export", {"sections": ["dbs", "users"], "secrets": False})
    assert status == 200 and exported["filename"].endswith(".toml") and "hunter2" not in exported["text"]
    assert "hunter2" in post(port, "/api/config/export", {"sections": ["dbs"], "secrets": True})[1]["text"]
    assert post(port, "/api/config/export", {"sections": []}) == (400, {"error": "Nothing selected."})
    assert post(port, "/api/config/export", {"sections": ["repos"]})[0] == 400

    from pdms_cli import transfer
    theirs = Config(users={"agent": DevUser("u1", "other@x.com"), "carla": DevUser("u3", "c@x.com")},
                    dbs={"shared": Database("db.example.com", protected=True)})
    text = transfer.export_document(theirs, ["users", "dbs"], secrets=False)
    status, plan = post(port, "/api/config/import/plan", {"text": text})
    assert status == 200 and plan["sections"] == ["users", "dbs"] and plan["first_setup"] is False
    users = next(item for item in plan["plans"] if item["section"] == "users")
    assert (users["added"], users["changed"], users["missing"]) == (["carla"], ["agent"], ["boss"])
    assert next(item for item in plan["plans"] if item["section"] == "dbs")["same"] == ["shared"]  # keeps the password
    assert post(port, "/api/config/import/plan", {"text": "not = [toml"})[0] == 400

    saved = []
    monkeypatch.setattr(ui_jobs.actions, "import_config", lambda result: saved.append(result) or tmp_path / "bak")
    monkeypatch.setattr(ui_jobs.i18n, "set_language", lambda lang: None)
    assert post(port, "/api/config/import/apply", {"text": text, "sections": ["users"], "overwrite": []}) == (
        200, {"changed": True, "backup": str(tmp_path / "bak"), "no_password": ["local"]})
    assert saved[-1].users["agent"].username == "a@x.com" and "carla" in saved[-1].users  # mine kept, new added
    post(port, "/api/config/import/apply", {"text": text, "sections": ["users"], "overwrite": [["users", "agent"]]})
    assert saved[-1].users["agent"].username == "other@x.com" and "boss" in saved[-1].users
    post(port, "/api/config/import/apply", {"text": text, "sections": ["users"], "replace": True})
    assert sorted(saved[-1].users) == ["agent", "carla"]
    assert post(port, "/api/config/import/apply", {"text": text, "sections": ["dbs"]}) == (
        200, {"changed": False, "backup": None, "no_password": []})
    assert post(port, "/api/config/import/apply", {"text": text, "sections": ["users"], "overwrite": ["x"]})[0] == 400


def test_import_users_from_a_database(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    from pdms_cli.userimport import DbUser

    asked = []

    def fetch(db, **kwargs):
        asked.append((db.host, kwargs["search"], kwargs["role"], kwargs["include_inactive"]))
        return [DbUser("u1", "a@x.com", "Ana", "Gent", ["TRANSPORTATION_PR_AGENT"], True),
                DbUser("u9", "n@x.com", "New", "One", ["TRANSPORTATION_PR_SUPERVISOR", "WEIRD"], False)]

    monkeypatch.setattr(actions.userimport, "fetch_users", fetch)
    status, data = post(port, "/api/import-users/search", {"db": "shared", "search": " ana ", "role": "TPR.Agent",
                                                           "inactive": True})
    assert status == 200 and asked == [("db.example.com", "ana", "TRANSPORTATION_PR_AGENT", True)]
    assert [(u["imported_as"], u["dev_roles"], u["unknown_roles"]) for u in data["users"]] == [
        ("agent", "TPR.Agent", []), ("", "TPR.Supervisor,WEIRD", ["WEIRD"])]
    assert post(port, "/api/import-users/search", {"db": "nope"})[0] == 400

    assert post(port, "/api/import-users/apply", {"users": data["users"]}) == (200, {"added": ["n"], "updated": ["agent"]})
    assert machine.users["n"].roles == "TPR.Supervisor,WEIRD" and machine.users["agent"].first_name == "Ana"
    assert post(port, "/api/import-users/apply", {"users": []}) == (400, {"error": "Nothing selected."})


def test_the_state_carries_the_language_and_the_server_follows_it(machine, monkeypatch) -> None:
    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {i.key: Health("ok") for i in items})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    monkeypatch.delenv("PDMS_LANG", raising=False)
    machine.defaults.language = "es"
    hub, _jobs = ui_server.make_app()
    languages = []
    monkeypatch.setattr(ui_server.i18n, "set_language", languages.append)
    assert hub.build()["language"] == "es" and languages == ["es"]  # a change made in the CLI reaches the server
    monkeypatch.setenv("PDMS_LANG", "en")
    assert hub.build()["language"] == "en"


def test_debug_and_open_code_go_through_the_actions(ui, machine, monkeypatch) -> None:
    port, _hub, _states, _jobs = ui
    calls = []
    setup = actions.DebugSetup(Path("/r/.vscode/launch.json"), "pdms: svc · agent @ local :8081", None, Path("/e"), Path("/r"))
    monkeypatch.setattr(ui_jobs.actions, "debug_instance", lambda cfg, inst: calls.append(inst.key) or setup)
    monkeypatch.setattr(ui_server.actions, "open_code", lambda cfg, path, line: calls.append((path, line)))
    assert post(port, "/api/instances/svc@8081/debug") == (
        200, {"name": "pdms: svc · agent @ local :8081", "launch": str(Path("/r/.vscode/launch.json")), "backup": ""})
    assert post(port, "/api/code/open", {"path": "/r/main.py", "line": 9}) == (200, {})
    assert post(port, "/api/code/open", {"path": "/r/main.py", "line": "9"})[0] == 400
    assert post(port, "/api/instances/nope@1/debug")[0] == 400
    assert calls == ["svc@8081", ("/r/main.py", 9)]


def test_a_kept_proxy_request_is_shown_without_secrets_and_sent_again(ui, machine, monkeypatch) -> None:
    from pdms_cli import captures

    port, _hub, _states, _jobs = ui
    ident = "0a1b2c3d"
    captures.Recorder().record(captures.entry(
        ident, "PUT", "/api/v1/lead/sp/8812", 500, "lead-sp-update@28105", 0.041,
        [("Authorization", "Bearer s3cret"), ("Content-Type", "application/json")], b'{"status": "IN_REVIEW"}',
        [("Content-Type", "application/json")], b'{"detail": "Internal Server Error"}'))
    monkeypatch.setattr(actions.proxy, "running_proxy", lambda: {"port": 28800})
    response, raw, _conn = request(port, f"/api/proxy/request?id={ident}", cookie(port))
    data = json.loads(raw)
    assert response.status == 200 and data["replay"] == "" and "s3cret" not in raw.decode()
    assert data["request"]["request"]["headers"][0] == ["Authorization", "Bearer (hidden)"]
    assert data["curl"].startswith("curl -i -X PUT http://localhost:28800/api/v1/lead/sp/8812 ")
    for bad in ("ffffffff", "../../x", ""):
        assert request(port, f"/api/proxy/request?id={bad}", cookie(port))[0].status == 400

    sent = []
    monkeypatch.setattr(actions.captures, "replay", lambda capture, to: sent.append((capture["id"], to)) or (500, 0.04))
    assert post(port, "/api/proxy/replay", {"id": ident}) == (200, {"status": 500, "ms": 40})
    assert sent == [(ident, 28800)]
    monkeypatch.setattr(actions.proxy, "running_proxy", lambda: None)
    status, data = post(port, "/api/proxy/replay", {"id": ident})
    assert status == 400 and "not running" in data["error"]


def test_events_ready_says_what_starting_them_needs_and_shows_a_real_route(ui, machine, monkeypatch) -> None:
    from pdms_cli import events

    port, _hub, _states, _jobs = ui
    event_map = events.EventMap(
        queues={"broker.fifo": events.Queue("broker.fifo"), "credential.fifo": events.Queue("credential.fifo")},
        consumers={"broker.fifo": events.Consumer("broker/broker-sqs-event", "h"),
                   "credential.fifo": events.Consumer("lead/lead-epic-ride-notify-ev", "h")},
        routes={"LEAD_SP_UPDATED": "credential.fifo"}, broker_queue="broker.fifo",
    )
    monkeypatch.setattr(ui_jobs.actions, "load_events", lambda cfg: (Path("/r"), event_map))
    monkeypatch.setattr(ui_jobs.events, "docker_available", lambda: (True, "29.8.1"))
    monkeypatch.setattr(ui_jobs.events, "is_up", lambda port: False)
    monkeypatch.setattr(ui_jobs.actions, "port_available", lambda host, port: False)
    response, raw, _conn = request(port, "/api/events/ready", cookie(port))
    assert response.status == 200 and json.loads(raw) == {
        "docker": {"ok": True, "version": "29.8.1"}, "port": {"port": 9324, "free": False},
        "example": {"type": "LEAD_SP_UPDATED", "queue": "credential.fifo", "consumer": "lead-epic-ride-notify-ev",
                    "broker": "broker.fifo"},
        "queues": 2,
    }
