"""pdms ui: the Repos tab (add, edit, use, remove, the switch of what runs) and the Doctor screen."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, doctor as diagnostics, instances
from pdms_cli.config import Config, Repo
from pdms_cli.instances import Instance
from pdms_cli.ui import jobs as ui_jobs
from pdms_cli.ui import server as ui_server
from pdms_cli.ui.control import Control
from pdms_cli.ui.doctor import Doctor

from test_repos import make_repo
from test_ui_server import TOKEN, get, post, wait_until


@pytest.fixture
def two_repos(monkeypatch, tmp_path):
    """A config with repos ``one`` (current) and ``two``, each with lead/lead-tp-list, in memory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    one, two = make_repo(tmp_path / "one"), make_repo(tmp_path / "two")
    cfg = Config(repos={"one": Repo(str(one)), "two": Repo(str(two))}, current_repo="one")
    saved = []
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(Config, "save", lambda self: saved.append(self.current_repo))
    return cfg, one, two


@pytest.fixture
def ui(two_repos):
    picked = []
    control = Control("")
    control.pick_folder = lambda start: picked.append(start) or "/picked"
    hub = ui_server.Hub(build=lambda: {"instances": []}, interval=0.05)
    jobs = ui_jobs.Jobs(hub.poke)
    server = ui_server.make_server("127.0.0.1", 0, TOKEN, hub, jobs, control)
    thread = threading.Thread(target=ui_server.serve, args=(server, hub), daemon=True)
    thread.start()
    yield server.server_address[1], jobs, picked
    server.shutdown()
    thread.join(5)


def running_from(root: Path, port: int = 8101) -> Instance:
    me = os.getpid()
    inst = Instance(key=f"lead-tp-list@{port}", pid=me, created=instances.creation_time(me),
                    service=str(root / "backend" / "lead" / "lead-tp-list"), host="0.0.0.0", port=port, user="agent",
                    db="local", reload=True, log=str(root / "x.log"), started_at="2026-10-02T10:00:00")
    instances.save({inst.key: inst})
    return inst


# --------------------------------------------------------------------------- repos


def test_the_repos_tab_lists_each_repo(ui, two_repos) -> None:
    port, _jobs, _picked = ui
    cfg, one, _two = two_repos
    running_from(one)
    status, data = get(port, "/api/config")
    assert status == 200 and data["pick_folder"] is True
    rows = {row["name"]: row for row in data["repos"]}
    assert rows["one"]["current"] and rows["one"]["running"] == 1 and rows["one"]["exists"]
    assert not rows["two"]["current"] and rows["two"]["running"] == 0


def test_add_looks_at_the_folder_first(ui, two_repos, tmp_path) -> None:
    port, _jobs, _picked = ui
    cfg, one, _two = two_repos
    three = make_repo(tmp_path / "three", services=("a/svc-a", "b/svc-b"))
    status, data = post(port, "/api/repos/check", {"path": str(three / "backend")})
    assert status == 200 and data == {"root": str(three), "services": 2, "name": "three", "registered": ""}
    assert post(port, "/api/repos/check", {"path": str(one)})[1]["registered"] == "one"
    status, data = post(port, "/api/repos/check", {"path": str(tmp_path)})
    assert status == 400 and data["field"] == "path"

    status, data = post(port, "/api/repos/add", {"path": str(three), "name": "three", "remote": "nope"})
    assert status == 400 and data["field"] == "remote" and "three" not in cfg.repos  # checked before saving
    status, data = post(port, "/api/repos/add", {"path": str(three), "name": "v3", "remote": "https://api.x/dev/"})
    assert status == 200 and data == {"name": "v3"} and cfg.repos["v3"].remote == "https://api.x/dev"


def test_edit_and_remove(ui, two_repos) -> None:
    port, _jobs, _picked = ui
    cfg, _one, _two = two_repos
    assert post(port, "/api/repos/one/save", {"name": "first", "migrations": "", "remote": ""}) == (200, {"name": "first"})
    assert cfg.current_repo == "first"
    assert post(port, "/api/repos/first/remove") == (200, {"current": "two"})
    assert list(cfg.repos) == ["two"]


def test_use_asks_what_to_do_with_what_runs_from_the_old_repo(ui, two_repos, monkeypatch) -> None:
    port, jobs, _picked = ui
    cfg, one, two = two_repos
    inst = running_from(one)
    status, data = post(port, "/api/repos/two/use")
    assert status == 409 and data["decision"] == "repo_switch" and data["old"] == "one"
    assert data["running"] == [{"key": inst.key, "movable": True}] and cfg.current_repo == "one"  # nothing changed

    stopped, started = [], []
    monkeypatch.setattr(actions, "stop_service", lambda current: stopped.append(current.key))

    def plan_service(cfg, path, **options):
        started.append((path, options))
        return SimpleNamespace(service=path, port=options["port"])

    monkeypatch.setattr(actions, "plan_service", plan_service)
    monkeypatch.setattr(actions, "needs_install", lambda *a, **k: False)
    monkeypatch.setattr(actions, "start_service", lambda cfg, launch: None)
    status, data = post(port, "/api/repos/two/use", {"running": "move"})
    assert status == 200 and data["job"] == ui_jobs.REPO_KEY and cfg.current_repo == "two"
    wait_until(lambda: ui_jobs.REPO_KEY not in jobs.snapshot())
    assert stopped == [inst.key]
    (path, options), = started
    assert path == two / "backend" / "lead" / "lead-tp-list" and options["port"] == inst.port
    assert options["user_name"] == "agent" and options["db_name"] == "local"


def test_use_with_nothing_running_or_keeping_it(ui, two_repos) -> None:
    port, _jobs, _picked = ui
    cfg, one, _two = two_repos
    assert post(port, "/api/repos/two/use")[1]["job"] is None and cfg.current_repo == "two"
    running_from(cfg.repos["two"].root)
    assert post(port, "/api/repos/one/use", {"running": "keep"})[1]["job"] is None and cfg.current_repo == "one"
    assert post(port, "/api/repos/two/use", {"running": "later"})[0] == 400


def test_the_folder_picker_is_only_in_the_window(ui) -> None:
    port, _jobs, picked = ui
    assert post(port, "/api/ui/pick-folder", {"start": "/home"}) == (200, {"path": "/picked"}) and picked == ["/home"]


# --------------------------------------------------------------------------- doctor


def test_doctor_runs_in_the_background_and_keeps_the_last_result(monkeypatch) -> None:
    gate = threading.Event()

    def run_all(cfg, *, databases, timeout):
        gate.wait(5)
        return [diagnostics.Check("Repo", "Current repo", diagnostics.OK, "one"),
                diagnostics.Check("Configuration", "Users", diagnostics.WARN, "0", "pdms user add", fix="add_user"),
                diagnostics.Check("Database connections", "web", diagnostics.FAIL, "timeout", "VPN?", fix="edit_db:web")]

    monkeypatch.setattr(diagnostics, "run_all", run_all)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config()))
    changes = []
    doctor = Doctor(lambda: changes.append(True))
    assert doctor.run(databases=True) and not doctor.run()  # one at a time
    assert doctor.summary()["running"] and doctor.latest() is None
    gate.set()
    wait_until(lambda: doctor.latest() is not None)
    summary = doctor.summary()
    assert summary["counts"] == {"ok": 1, "warn": 1, "fail": 1} and not summary["running"]
    assert [c["fix"] for c in summary["problems"]] == ["add_user", "edit_db:web"]
    assert doctor.latest()["databases"] is True and changes


def test_doctor_endpoints(ui, monkeypatch) -> None:
    port, jobs, _picked = ui
    monkeypatch.setattr(diagnostics, "run_all", lambda cfg, *, databases, timeout: [
        diagnostics.Check("pdms", "Version", diagnostics.OK, "0.2.5")])
    assert get(port, "/api/doctor") == (200, {"latest": None, "running": False})
    assert post(port, "/api/doctor/run", {"databases": "yes"})[0] == 400
    assert post(port, "/api/doctor/run", {"databases": False})[0] == 202
    wait_until(lambda: jobs.doctor.latest() is not None)
    status, data = get(port, "/api/doctor")
    assert status == 200 and data["latest"]["checks"][0]["name"] == "Version"


def test_doctor_problems_say_how_pdms_ui_fixes_them(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    checks = {c.name: c for c in diagnostics.check_config(Config())}
    assert checks["Users"].fix == "add_user" and checks["Databases"].fix == "add_db"
    assert diagnostics.check_repo(Config())[0].fix == "add_repo"


def test_migration_status_of_a_database(ui, two_repos, monkeypatch, tmp_path) -> None:
    port, _jobs, _picked = ui
    cfg, one, _two = two_repos
    from pdms_cli import migrations
    from pdms_cli.config import Database

    cfg.dbs = {"local": Database("localhost")}
    status, data = post(port, "/api/migrations/status", {"db": "local"})
    assert status == 400 and "Settings → Repos" in data["error"]  # no migrations repo for 'one'
    flyway = tmp_path / "pdms-db-migrations"
    (flyway / "migrations").mkdir(parents=True)
    (flyway / "flyway.toml").write_text('[flyway]\nlocations = ["filesystem:migrations"]\n', encoding="utf-8")
    cfg.repos["one"].migrations = str(flyway)
    monkeypatch.setattr(migrations, "history", lambda db, settings, timeout: None)
    status, data = post(port, "/api/migrations/status", {"db": "local"})
    assert status == 200 and data["db"] == "local" and data["flyway"] is False and data["migrations"] == []

    def unreachable(db, settings, timeout):
        raise RuntimeError("connection timeout expired\nmore details")

    monkeypatch.setattr(migrations, "history", unreachable)
    status, data = post(port, "/api/migrations/status", {"db": "local"})
    assert status == 400 and data["error"] == "Could not query local: connection timeout expired"
    assert post(port, "/api/migrations/status", {"db": "nope"})[0] == 400
