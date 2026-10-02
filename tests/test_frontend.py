"""The PDMS web app run by pdms (dev server or a production build), Home's Start everything and renaming users."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pdms_cli import actions, frontend, instances
from pdms_cli.config import Config, Database, DevUser, Repo, Setup, Stack
from pdms_cli.ui import state as ui_state

from test_ui_server import cookie, machine, post, repo, request, ui, wait_until  # noqa: F401 - fixtures


@pytest.fixture
def web(tmp_path, monkeypatch) -> Path:
    """A repo root with a frontend whose .env points to a remote API; pdms's state in a temporary folder."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "web"
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (root / "frontend" / "yarn.lock").write_text("lock 1\n", encoding="utf-8")
    (root / "frontend" / "src" / "main.tsx").write_text("app\n", encoding="utf-8")
    (root / "frontend" / ".env").write_text(
        "VITE_APP_API_URL=https://api.example.com/dev\nVITE_APP_API_URL_VERSION=api/v1\n", encoding="utf-8")
    return root


@pytest.fixture
def tools(monkeypatch):
    """Node 22 and yarn installed, node_modules up to date, every port free (each can be changed by the test)."""
    found = {"node": "22.12.0", "yarn": "/usr/bin/yarn", "deps": True}
    monkeypatch.setattr(frontend, "node_version", lambda: found["node"])
    monkeypatch.setattr(frontend, "yarn", lambda: found["yarn"])
    monkeypatch.setattr(frontend, "dependencies_ok", lambda root: found["deps"])
    monkeypatch.setattr(actions, "free_port", lambda host, port: port)
    return found


@pytest.fixture
def cfg(web, monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    return Config(repos={"web": Repo(str(web))}, current_repo="web")


def built(root: Path) -> None:
    (root / "frontend" / "dist").mkdir(exist_ok=True)
    (root / "frontend" / "dist" / "index.html").write_text("<html>", encoding="utf-8")
    frontend.remember_build(root)


@pytest.mark.parametrize(("version", "ok"), [("22.12.0", True), ("24.1.0", True), ("20.11.1", False), ("25.0.0", False)])
def test_node_versions(version, ok) -> None:
    assert frontend.node_supported(version) is ok


def test_the_api_url_follows_vite_env_files(web) -> None:
    assert frontend.api_url(web, "dev") == frontend.api_url(web, "build") == "https://api.example.com/dev/api/v1"
    (web / "frontend" / ".env.local").write_text("VITE_APP_API_URL=http://localhost:8000\n", encoding="utf-8")
    (web / "frontend" / ".env.production").write_text("VITE_APP_API_URL=https://api.example.com/prod\n", encoding="utf-8")
    assert frontend.api_url(web, "dev") == "http://localhost:8000/api/v1"
    assert frontend.api_url(web, "build") == "https://api.example.com/prod/api/v1"  # .env.[mode] wins over .env.local
    assert frontend.is_local(frontend.api_url(web, "dev")) and not frontend.is_local(frontend.api_url(web, "build"))


def test_a_build_is_made_again_only_when_something_changed(web) -> None:
    assert frontend.build_needed(web) == "no build yet"
    built(web)
    assert frontend.build_needed(web) == ""
    assert frontend.last_build()["api"] == "https://api.example.com/dev/api/v1"

    (web / "frontend" / ".env.local").write_text("VITE_APP_API_URL=http://localhost:8000\n", encoding="utf-8")
    assert frontend.build_needed(web) == "the API URL changed"
    built(web)
    (web / "frontend" / "yarn.lock").write_text("lock 2\n", encoding="utf-8")
    assert frontend.build_needed(web) == "the dependencies changed"
    built(web)
    later = time.time() + 5
    os.utime(web / "frontend" / "src" / "main.tsx", (later, later))
    assert frontend.build_needed(web) == "the code changed"


def test_plan_frontend_checks_the_tools_and_decides_install_and_build(cfg, web, tools) -> None:
    plan = actions.plan_frontend(cfg)
    assert (plan.mode, plan.port, plan.install, plan.build, plan.url) == ("dev", 3000, False, "", "https://localhost:3000")
    tools["deps"] = False
    assert actions.plan_frontend(cfg).install
    assert not actions.plan_frontend(cfg, install=False).install
    assert actions.plan_frontend(cfg, mode="build").build == "no build yet"
    built(web)
    assert actions.plan_frontend(cfg, mode="build").build == ""
    assert actions.plan_frontend(cfg, mode="build", rebuild=True).build == "rebuild asked"
    with pytest.raises(actions.InvalidValue):
        actions.plan_frontend(cfg, mode="prod")

    tools["node"] = "20.1.0"
    with pytest.raises(actions.ActionError, match="Node 22 to 24 and this is Node 20.1.0"):
        actions.plan_frontend(cfg)
    tools["node"], tools["yarn"] = "22.1.0", None
    with pytest.raises(actions.ActionError, match="yarn was not found"):
        actions.plan_frontend(cfg)


def test_plan_frontend_hands_back_a_busy_port_and_needs_a_frontend(cfg, web, tools, monkeypatch) -> None:
    def busy(host, port):
        raise actions.PortBusy(port, port + 1)

    monkeypatch.setattr(actions, "free_port", busy)
    with pytest.raises(actions.PortBusy) as taken:
        actions.plan_frontend(cfg)
    assert (taken.value.port, taken.value.free) == (3000, 3001)
    (web / "frontend" / "package.json").unlink()
    with pytest.raises(actions.ActionError, match="has no frontend"):
        actions.plan_frontend(cfg)


def test_the_running_frontend_is_remembered_and_stopped(cfg, web, tools, monkeypatch) -> None:
    spawned = []

    def spawn(cmd, cwd, env, log):
        spawned.append((cmd, cwd, env["BROWSER"]))
        return type("Proc", (), {"pid": os.getpid()})()

    monkeypatch.setattr(instances, "spawn", spawn)
    started = actions.start_frontend(actions.plan_frontend(cfg))
    assert spawned == [(["/usr/bin/yarn", "dev", "--port", "3000", "--strictPort"], web / "frontend", "none")]
    assert frontend.running()["mode"] == "dev" and started["api"] == "https://api.example.com/dev/api/v1"
    with pytest.raises(actions.ActionError, match="already running"):
        actions.plan_frontend(cfg)

    killed = []
    monkeypatch.setattr(instances, "kill_tree", lambda pid, created, timeout: killed.append(pid))
    assert actions.stop_frontend() and killed == [os.getpid()]
    assert frontend.running() is None and not actions.stop_frontend()


def test_a_failed_install_says_when_codeartifact_needs_a_login(web, tmp_path, monkeypatch) -> None:
    log = tmp_path / "install.log"
    log.write_text("error https://alivi-609837931903.d.codeartifact.us-east-1.amazonaws.com/npm/: "
                   "Request failed \"401 Unauthorized\"\n", encoding="utf-8")
    monkeypatch.setattr(frontend, "install_command", lambda: ["false"])
    with pytest.raises(actions.ActionError, match="codeartifact-login.sh"):
        actions.install_frontend(web, open(os.devnull, "w"), log)
    log.write_text("error Couldn't find package\n", encoding="utf-8")
    with pytest.raises(actions.ActionError, match="yarn install failed"):
        actions.install_frontend(web, open(os.devnull, "w"), log)


def test_the_state_says_when_a_running_build_points_to_an_old_api(cfg, web, monkeypatch) -> None:
    assert ui_state.frontend_state(cfg)["running"] is False
    built(web)
    monkeypatch.setattr(frontend, "running", lambda: {
        "pid": 1, "port": 3000, "mode": "build", "root": str(web), "api": "x", "started_at": "2026-10-02T09:00:00"})
    monkeypatch.setattr(frontend, "health", lambda current: instances.Health("ok", "https"))
    state = ui_state.frontend_state(cfg)
    assert (state["status"], state["url"], state["stale"]) == ("ok", "https://localhost:3000", "")
    (web / "frontend" / ".env.local").write_text("VITE_APP_API_URL=http://localhost:8000\n", encoding="utf-8")
    state = ui_state.frontend_state(cfg)
    assert state["api"] == "https://api.example.com/dev/api/v1" and state["stale"] == "http://localhost:8000/api/v1"


# --------------------------------------------------------------------------- users and the saved setup


def test_rename_user_also_renames_it_in_stacks() -> None:
    cfg = Config(users={"agent": DevUser("u1", "a@x.com"), "boss": DevUser("u2", "b@x.com")},
                 stacks={"leads": Stack(["a"], user="agent"), "other": Stack(["b"], user="boss")}, last_user="agent")
    cfg.save = lambda: None
    assert actions.rename_user(cfg, "agent", " supervisor ") == "supervisor"
    assert list(cfg.users) == ["supervisor", "boss"] and cfg.users["supervisor"].user_id == "u1"
    assert (cfg.stacks["leads"].user, cfg.stacks["other"].user, cfg.last_user) == ("supervisor", "boss", "supervisor")
    assert actions.rename_user(cfg, "boss", "boss") == "boss"
    with pytest.raises(actions.InvalidValue, match="already exists"):
        actions.rename_user(cfg, "boss", "supervisor")
    with pytest.raises(actions.ActionError):
        actions.rename_user(cfg, "ghost", "x")


def test_the_setup_is_saved_and_a_removed_stack_leaves_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    cfg = Config(stacks={"leads": Stack(["a"])}, setup=Setup(stack="leads", frontend_mode="build", events=True))
    cfg.save()
    assert Config.load().setup == Setup(stack="leads", proxy=True, frontend=True, frontend_mode="build", events=True)
    actions.remove_stack(cfg, "leads")
    assert Config.load().setup.stack == ""


def test_the_ui_saves_the_setup_and_renames_users(ui, repo, machine) -> None:
    port, _hub, _states, _jobs = ui
    assert post(port, "/api/setup/save", {"stack": "leads", "proxy": False, "frontend": True,
                                          "frontend_mode": "build", "events": True}) == (
        200, {"setup": {"stack": "leads", "proxy": False, "frontend": True, "frontend_mode": "build", "events": True}})
    assert post(port, "/api/setup/save", {"stack": "ghost"})[0] == 400
    assert post(port, "/api/setup/save", {"frontend_mode": "prod"})[1]["field"] == "frontend_mode"

    machine.stacks["leads"].user = "agent"
    assert post(port, "/api/users/agent/rename", {"new_name": "supervisor"}) == (200, {"name": "supervisor"})
    assert machine.stacks["leads"].user == "supervisor" and machine.last_user == "supervisor"
    assert post(port, "/api/users/boss/rename", {"new_name": "supervisor"})[1]["field"] == "name"
    profiles = ui_state.profiles(machine)
    assert profiles["boss"] == {"name": "", "roles": ""}


def test_start_everything_with_nothing_to_do_and_stop_everything(ui, repo, machine, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    machine.setup = Setup(stack="", proxy=False, frontend=False)
    assert post(port, "/api/home/start") == (200, {"job": None})

    machine.setup = Setup(stack="leads", proxy=False, frontend=False)
    machine.stacks["leads"].db = "shared"
    assert post(port, "/api/home/start") == (409, {"decision": "protected_database", "name": "shared"})

    started = []
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch, install=None: started.append(launch.service.name))
    monkeypatch.setattr(actions, "needs_install", lambda cfg, service, install: False)
    assert post(port, "/api/home/start", {"confirmed": True}) == (202, {"job": "home"})
    wait_until(lambda: not jobs.snapshot())
    assert started == ["lead-list", "lead-get"]

    stopped = []
    monkeypatch.setattr(actions, "stop_service", lambda inst: stopped.append(inst.key))
    monkeypatch.setattr(actions.events, "container_state", lambda: None)
    assert post(port, "/api/home/stop") == (202, {"job": "home"})
    wait_until(lambda: not jobs.snapshot())
    assert stopped == ["svc@8081"]


def test_the_frontend_logs_and_start_through_the_ui(ui, repo, machine, monkeypatch, tools) -> None:
    port, _hub, _states, jobs = ui
    root = repo.parent
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    frontend.build_log_path().parent.mkdir(parents=True, exist_ok=True)
    frontend.build_log_path().write_text("vite v6.4.2 building for production...\n", encoding="utf-8")
    response, raw, _conn = request(port, "/api/logs?key=frontend&which=build", cookie(port))
    assert response.status == 200 and json.loads(raw)["lines"] == ["vite v6.4.2 building for production..."]

    _response, raw, _conn = request(port, "/api/frontend/options", cookie(port))
    options = json.loads(raw)
    assert (options["available"], options["node"], options["dependencies_ok"], options["build_needed"]) == (
        True, "22.12.0", True, "no build yet")

    calls = []
    monkeypatch.setattr(actions, "build_frontend", lambda root, output=None: calls.append("build"))
    monkeypatch.setattr(actions, "start_frontend", lambda plan: calls.append(plan.mode) or {"port": plan.port})
    monkeypatch.setattr(actions, "wait_for_frontend", lambda started: "ok")
    for body in ({"mode": "prod"}, {"port": "3000"}, {"install": "yes"}):
        assert post(port, "/api/frontend/start", body)[0] == 400, body
    assert post(port, "/api/frontend/start", {"mode": "build"}) == (202, {"job": "frontend"})
    wait_until(lambda: not jobs.snapshot())
    assert calls == ["build", "build"]
