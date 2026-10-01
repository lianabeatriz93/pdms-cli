"""pdms ui server: only this machine's own page, with the session token, reaches the API."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from pdms_cli import actions, instances
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
    for path in ("/", "/api/state", "/api/stream", "/static/app.js"):
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
    response, _body, _conn = request(port, "/static/app.js", cookie(port))
    assert response.status == 200 and response.getheader("Content-Type") == "text/javascript; charset=utf-8"
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/.hidden", "/static/missing.js"):
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


def test_a_stale_stack_says_so_without_terminal_markup(ui, repo) -> None:
    port, _hub, _states, _jobs = ui
    (repo / "lead/lead-get/main.py").unlink()
    status, data = post(port, "/api/stacks/leads/up")
    assert status == 400 and "'lead/lead-get' is no longer a service" in data["error"]
    assert "pdms stack edit" in data["error"] and "[bold]" not in data["error"]


def test_save_and_remove_stacks(ui, repo, machine) -> None:
    port, _hub, _states, _jobs = ui
    _response, raw, _conn = request(port, "/api/services", cookie(port))
    assert json.loads(raw) == {"root": str(repo), "services": ["lead/lead-get", "lead/lead-list", "user/user-me"]}

    def save(name, services, new=True, **extra):
        return post(port, f"/api/stacks/{name}/save", {"services": services, "new": new, **extra})

    assert save("bad%20name", ["user/user-me"]) == (400, {"error": "Use only letters, numbers, '-' or '_'"})
    assert save("leads", ["user/user-me"]) == (400, {"error": "That name already exists"})
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
