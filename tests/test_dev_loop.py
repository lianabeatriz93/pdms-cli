"""From pdms ui to the code: tracebacks that open VS Code at the failing line, and debugging a running service."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from pdms_cli import actions, instances, vscode
from pdms_cli.config import Config, Database, DevUser, Repo
from pdms_cli.ui import recent as ui_recent
from pdms_cli.ui import server as ui_server
from pdms_cli.ui import state as ui_state

TRACEBACK = """INFO:     Will watch for changes in these directories: ['{svc}']
Process SpawnProcess-1:
Traceback (most recent call last):
  File "/usr/lib/python3.10/multiprocessing/process.py", line 314, in _bootstrap
    self.run()
  File "{svc}/main.py", line 9, in <module>
    from app.routes import router
ModuleNotFoundError: No module named 'app.routes'
WARNING:  WatchFiles detected changes in 'main.py'. Reloading...
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A registered repo with one service (a git root, as VS Code opens it) and a config pointing at it."""
    root = tmp_path / "pdms"
    service = root / "backend" / "lead" / "lead-sp-update"
    service.mkdir(parents=True)
    (root / ".git").mkdir()
    (service / "main.py").write_text("import app.routes\n", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = Config(
        users={"agent": DevUser("u1", "a@x.com")}, dbs={"local": Database("localhost", password="pw")},
        repos={"pdms": Repo(str(root))}, current_repo="pdms",
    )
    monkeypatch.setattr(Config, "save", lambda self: None)
    return cfg, service


def test_the_latest_traceback_ends_at_its_exception(tmp_path):
    log = tmp_path / "x.log"
    log.write_text(TRACEBACK.format(svc="/s") + "Traceback (most recent call last):\n  File \"/s/b.py\", line 2, in f\n"
                   "    boom()\nRuntimeError: second\nINFO:     after\n", encoding="utf-8")
    assert instances.last_traceback(str(log)) == [
        "Traceback (most recent call last):", '  File "/s/b.py", line 2, in f', "    boom()", "RuntimeError: second",
    ]
    log.write_text("INFO:     Application startup complete.\n", encoding="utf-8")
    assert instances.last_traceback(str(log)) == []


def test_only_frames_inside_a_registered_repo_link_to_the_code(repo, tmp_path):
    cfg, service = repo
    log = tmp_path / "svc.log"
    log.write_text(TRACEBACK.format(svc=service), encoding="utf-8")
    lines = ui_state.traceback_state(cfg, str(log))
    linked = [line for line in lines if "path" in line]
    assert linked == [{"text": f'  File "{service}/main.py", line 9, in <module>', "path": f"{service}/main.py", "line": 9}]
    assert lines[-1] == {"text": "ModuleNotFoundError: No module named 'app.routes'"}


def test_open_code_only_opens_files_of_registered_repos(repo, tmp_path, monkeypatch):
    cfg, service = repo
    opened = []
    monkeypatch.setattr(vscode, "open_in_code", lambda folder, file=None, line=0: opened.append((folder, file, line)))
    actions.open_code(cfg, str(service / "main.py"), 9)
    assert opened == [(cfg.repos["pdms"].root, service / "main.py", 9)]
    outside = tmp_path / "elsewhere.py"
    outside.write_text("", encoding="utf-8")
    for path in (str(outside), str(service / "missing.py"), str(service)):
        with pytest.raises(actions.ActionError):
            actions.open_code(cfg, path, 1)
    assert len(opened) == 1


def test_code_runs_detached_with_the_folder_and_the_line(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(vscode, "code_command", lambda: "/bin/code")
    monkeypatch.setattr(vscode.subprocess, "Popen", lambda cmd, **kwargs: started.append(cmd))
    vscode.open_in_code(tmp_path, tmp_path / "main.py", 9)
    assert started == [["/bin/code", str(tmp_path), "-g", f"{tmp_path / 'main.py'}:9"]]
    monkeypatch.setattr(vscode, "code_command", lambda: None)
    with pytest.raises(RuntimeError, match="Install 'code' command in PATH"):
        vscode.open_in_code(tmp_path)


def test_debugging_an_instance_stops_it_and_writes_its_configuration(repo, monkeypatch):
    cfg, service = repo
    inst = instances.Instance(
        key="lead-sp-update@28105", pid=os.getpid(), service=str(service), host="0.0.0.0", port=28105, user="agent",
        db="local", reload=True, log="", started_at=datetime.now().isoformat(),
    )
    steps = []
    monkeypatch.setattr(actions.runner, "poetry_python", lambda path: Path(sys.executable))
    monkeypatch.setattr(vscode, "code_command", lambda: "/bin/code")
    monkeypatch.setattr(actions, "stop_service", lambda instance: steps.append(("stop", instance.key)))
    monkeypatch.setattr(actions, "free_port", lambda host, port: steps.append(("port", port)) or port)
    monkeypatch.setattr(vscode, "open_in_code", lambda folder, file=None, line=0: steps.append(("code", folder)))

    setup = actions.debug_instance(cfg, inst)
    assert steps == [("stop", inst.key), ("port", 28105), ("code", cfg.repos["pdms"].root)]
    config = json.loads(setup.launch_json.read_text(encoding="utf-8"))["configurations"][0]
    assert config["name"] == setup.name == "pdms: lead-sp-update · agent @ local :28105"
    assert config["args"] == ["main:app", "--host", "0.0.0.0", "--port", "28105"]  # no --reload: breakpoints work
    assert config["cwd"] == "${workspaceFolder}/backend/lead/lead-sp-update"
    assert "pw" not in setup.launch_json.read_text(encoding="utf-8")  # the password is only in the env file
    assert "pw" in setup.env_file.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(setup.env_file.stat().st_mode) == 0o600


def test_debugging_checks_everything_before_stopping_the_instance(repo, monkeypatch):
    cfg, service = repo
    inst = instances.Instance(
        key="x@1", pid=os.getpid(), service=str(service), host="0.0.0.0", port=1, user="agent", db="local",
        reload=True, log="", started_at="", events="local",
    )
    monkeypatch.setattr(actions, "stop_service", lambda instance: pytest.fail("stopped it"))
    monkeypatch.setattr(actions.runner, "poetry_python", lambda path: None)
    with pytest.raises(actions.ActionError, match="no virtualenv"):
        actions.debug_instance(cfg, inst)
    monkeypatch.setattr(actions.runner, "poetry_python", lambda path: Path(sys.executable))
    monkeypatch.setattr(vscode, "code_command", lambda: None)
    with pytest.raises(actions.ActionError, match="code command"):
        actions.debug_instance(cfg, inst)
    monkeypatch.setattr(vscode, "code_command", lambda: "/bin/code")
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    with pytest.raises(actions.LocalEventsDown):
        actions.debug_instance(cfg, inst)


# --------------------------------------------------------------------------- Recent and notifications


def snapshot(*instances_, jobs=None):
    return {"instances": [{"key": key, "status": status, "detail": detail} for key, status, detail in instances_],
            "jobs": jobs or {}}


def test_recent_says_what_changed_and_tells_the_desktop_only_what_happened_by_itself():
    told = []
    feed = ui_recent.Recent(notify=lambda title, message: told.append(message))
    feed.observe(snapshot(("a@1", "ok", ""), ("b@2", "ok", ""), ("c@3", "error", "")))
    assert feed.items() == [] and told == []  # the first state is only remembered

    feed.observe(snapshot(("a@1", "error", "ModuleNotFoundError: x"), ("b@2", "stopped", ""), ("c@3", "ok", "")))
    assert [(e["event"], e["key"]) for e in feed.items()] == [("recovered", "c@3"), ("exited", "b@2"), ("failed", "a@1")]
    assert told == ["2 problems: a@1, b@2"]

    feed.observe(snapshot(("a@1", "error", "x"), jobs={"stack:sp": {"action": "up", "error": ""},
                                                        "a@1": {"action": "restart", "error": ""}}))
    feed.observe(snapshot(("a@1", "ok", ""), jobs={"a@1": {"action": "restart", "error": "poetry install failed"}}))
    events = [(e["event"], e["key"], e.get("action")) for e in feed.items()[:2]]
    assert events == [("done", "stack:sp", "up"), ("job-failed", "a@1", "restart")]
    assert told == ["2 problems: a@1, b@2"]  # asked for on the page: it shows there, no notification

    feed.observe(snapshot(("d@4", "ok", "")))
    feed.observe(snapshot(("d@4", "error", "boom")), notify=False)  # defaults.notify off
    assert len(told) == 1 and (feed.items()[0]["event"], feed.items()[0]["key"]) == ("failed", "d@4")


def test_one_failure_reads_on_its_own():
    assert ui_recent.notification_text([{"event": "failed", "key": "a@1", "detail": "boom"}]) == "a@1 failed to load: boom"
    assert ui_recent.notification_text([{"event": "exited", "key": "a@1", "detail": ""}]) == "a@1 stopped by itself."


def test_the_hub_keeps_building_without_a_page_while_notifications_are_on():
    built, on = [], {"value": True}
    hub = ui_server.Hub(build=lambda: built.append(1) or {"n": len(built)}, interval=0.01, always=lambda: on["value"])
    hub.start()
    try:
        deadline = time.monotonic() + 5
        while len(built) < 3:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        on["value"] = False
        time.sleep(0.05)
        count = len(built)
        time.sleep(0.1)
        assert len(built) == count  # no page and no notifications: nothing to build for
    finally:
        hub.stop()
