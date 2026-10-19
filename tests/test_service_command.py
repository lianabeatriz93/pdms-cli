"""How pdms starts a service cheaply: --reload watching only its code, and its interpreter without poetry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdms_cli import instances, runner


def make_service(tmp_path: Path) -> Path:
    service = tmp_path / "lead-place-get"
    for name in ("lead_place_get/domain/x.py", "tests/test_x.py", ".venv/lib/site.py", "__pycache__/m.py",
                 "docs/readme.md", "alembic/env.py"):
        (service / name).parent.mkdir(parents=True, exist_ok=True)
        (service / name).write_text("", encoding="utf-8")
    (service / "main.py").write_text("", encoding="utf-8")
    return service


def make_venv(service: Path) -> Path:
    python = service / ".venv" / ("Scripts/python.exe" if runner.WINDOWS else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return python


def test_reload_watches_only_the_folders_with_its_code(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert runner.reload_dirs(service) == ["alembic", "lead_place_get"]  # no .venv, tests, caches or docs
    assert runner.reload_dirs(tmp_path / "gone") == []
    cmd = runner.uvicorn_command("0.0.0.0", 28101, True, ["alembic", "lead_place_get"])
    assert cmd[2:] == ["uvicorn", "main:app", "--reload", "--reload-dir", "alembic", "--reload-dir", "lead_place_get",
                       "--host", "0.0.0.0", "--port", "28101"]
    assert "--reload-dir" not in runner.uvicorn_command("0.0.0.0", 28101, False, ["lead_place_get"])


def test_the_in_project_virtualenv_needs_no_poetry(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    python = make_venv(service)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("asked poetry"))
    assert runner.poetry_python(service) == python


def test_poetry_python_without_poetry_or_folder(tmp_path: Path, monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("poetry")

    monkeypatch.setattr(runner.subprocess, "run", missing)
    assert runner.poetry_python(tmp_path / "gone") is None


def test_services_start_with_their_interpreter_instead_of_poetry_run(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    python = make_venv(service)
    env = {"PATH": "/usr/bin", "KEEP": "1"}
    cmd, new_env = runner.direct(service, runner.uvicorn_command("127.0.0.1", 28101, True, ["lead_place_get"]), env)
    assert cmd[:4] == [str(python), "-m", "uvicorn", "main:app"]
    assert new_env["VIRTUAL_ENV"] == str(python.parent.parent) and new_env["KEEP"] == "1"
    assert new_env["PATH"] == str(python.parent) + os.pathsep + "/usr/bin"
    assert instances.launch_of(cmd) == ("127.0.0.1", 28101, True, "")  # still recognised as a pdms service

    poller, _ = runner.direct(service, [runner.poetry(), "run", "python", "poller.py", "--queue-url", "q"], env)
    assert poller == [str(python), "poller.py", "--queue-url", "q"]


def test_direct_leaves_other_commands_and_services_without_a_virtualenv(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    env = {"PATH": "/usr/bin"}
    assert runner.direct(service, ["yarn", "dev"], env) == (["yarn", "dev"], env)
    monkeypatch.setattr(runner, "poetry_python", lambda service: None)
    cmd = runner.uvicorn_command("127.0.0.1", 28101, False)
    assert runner.direct(service, cmd, env) == (cmd, env)  # the install creates it later; poetry run still works
