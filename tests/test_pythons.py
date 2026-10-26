"""Virtualenvs on a Python their project no longer accepts: found, asked about, and recreated only when agreed."""

from __future__ import annotations

import pytest
from pdms_cli import actions, doctor, pythons, repos, runner
from pdms_cli.config import Config

OLD = (3, 10, 13)
NEW = (3, 12, 3)


def service(root, name: str, spec: str = ">=3.12,<3.14", venv: str | None = "3.10.13"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text(f'[tool.poetry.dependencies]\npython = "{spec}"\n', encoding="utf-8")
    (folder / "main.py").write_text("app = None\n", encoding="utf-8")
    if venv:
        (folder / ".venv").mkdir()
        (folder / ".venv" / "pyvenv.cfg").write_text(
            f"home = /usr/bin\nimplementation = CPython\nversion_info = {venv}.final.0\n", encoding="utf-8")
    return folder


@pytest.fixture
def found(monkeypatch):
    """This computer has Python 3.10 and 3.12."""
    pythons._installed = None
    monkeypatch.setattr(pythons, "_candidates", lambda: ["/py/3.10", "/py/3.12", "/py/3.12-again"])
    versions = {"/py/3.10": OLD, "/py/3.12": NEW, "/py/3.12-again": NEW}
    monkeypatch.setattr(pythons, "_version_of", lambda program: versions[program])
    yield
    pythons._installed = None


@pytest.mark.parametrize(("spec", "version", "ok"), [
    (">=3.12,<3.14", OLD, False), (">=3.12,<3.14", NEW, True), (">=3.12,<3.14", (3, 14, 0), False),
    (">=3.9,<3.14", OLD, True), ("^3.12", (3, 13, 1), True), ("^3.12", (4, 0, 0), False),
    ("~3.12", (3, 12, 9), True), ("~3.12", (3, 13, 0), False), ("3.12.*", (3, 12, 1), True),
    ("3.12.*", (3, 13, 0), False), ("~=3.12", (3, 13, 0), True), (">=3.9 || ^3.12", OLD, True),
    ("3.12", NEW, True), (">= 3.12, < 3.14", NEW, True), ("banana", OLD, None),
])
def test_the_constraints_poetry_writes(spec, version, ok) -> None:
    assert pythons.accepts(spec, version) is ok


def test_a_virtualenv_the_project_no_longer_accepts(tmp_path, found) -> None:
    old = service(tmp_path, "old")
    item = pythons.mismatch(old)
    assert item is not None and item.describe() == {
        "service": "old", "path": str(old), "have": "3.10.13", "wants": ">=3.12,<3.14", "use": "3.12.3",
        "program": "/py/3.12",
    }
    assert pythons.mismatch(service(tmp_path, "fine", venv="3.12.3")) is None
    assert pythons.mismatch(service(tmp_path, "wide", spec=">=3.9,<3.14")) is None
    assert pythons.mismatch(service(tmp_path, "new", venv=None)) is None  # poetry makes it on a good one
    assert pythons.mismatch(service(tmp_path, "odd", spec="banana")) is None  # pdms says nothing it cannot read
    assert pythons.suggested(">=3.12,<3.14") == "3.12"


def test_no_accepted_python_installed(tmp_path, found) -> None:
    item = pythons.mismatch(service(tmp_path, "future", spec=">=3.14"))
    assert item is not None and item.use is None


def test_the_user_decides_before_anything_is_recreated(tmp_path, found) -> None:
    cfg = Config()
    old, fine = service(tmp_path, "old"), service(tmp_path, "fine", venv="3.12.3")
    with pytest.raises(actions.PythonMismatch) as asked:
        actions.check_pythons(cfg, [old, fine], None, None)
    assert [item.service for item in asked.value.items] == [old]
    assert actions.check_pythons(cfg, [old, fine], None, True) == {old: "/py/3.12"}
    assert actions.check_pythons(cfg, [old, fine], None, False) == {}
    assert actions.check_pythons(cfg, [old], False, None) == {}  # no install, nothing to ask
    cfg.defaults.install = False
    assert actions.check_pythons(cfg, [old], None, None) == {}
    with pytest.raises(actions.ActionError, match="uv python install 3.14"):
        actions.check_pythons(Config(), [service(tmp_path, "later", spec=">=3.14")], None, True)


def test_recreating_makes_a_new_virtualenv_before_installing(tmp_path, monkeypatch) -> None:
    old = service(tmp_path, "old")
    (old / ".venv" / "stale").write_text("x", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd[1:]))
    runner.install(old, python="/py/3.12")
    assert not (old / ".venv").exists()
    assert calls == [["env", "use", "/py/3.12"], ["lock"], ["install"]]
    calls.clear()
    runner.install(service(tmp_path, "other"))
    assert calls == [["lock"], ["install"]]


def test_several_services_install_on_their_new_python(tmp_path, monkeypatch) -> None:
    old, fine = service(tmp_path, "old"), service(tmp_path, "fine", venv="3.12.3")
    installed = []
    monkeypatch.setattr(actions, "needs_install", lambda cfg, path, install: False)
    monkeypatch.setattr(actions, "install_service",
                        lambda path, output=None, only=None, python=None: installed.append((path.name, python)))
    logs = {old: tmp_path / "old.log", fine: tmp_path / "fine.log"}
    actions.install_services(Config(), logs, None, recreate={old: "/py/3.12"})
    assert installed == [("old", "/py/3.12")]  # fine was up to date


def test_the_terminal_asks_and_recreates(tmp_path, found, monkeypatch) -> None:
    old = service(tmp_path, "old")
    monkeypatch.setattr("pdms_cli.commands.run.interactive_terminal", lambda: True)
    answers = []

    class Confirm:
        def __init__(self, answer: bool) -> None:
            self.answer = answer

        def unsafe_ask(self) -> bool:
            return self.answer

    monkeypatch.setattr("pdms_cli.commands.run.questionary.confirm",
                        lambda message, default: answers.append(message) or Confirm(True))
    done = []
    monkeypatch.setattr(actions, "install_service", lambda path, output=None, only=None, python=None:
                        done.append((path.name, python)))
    from pdms_cli.commands import run

    run.ensure_installed(Config(), old, None)
    assert done == [("old", "/py/3.12")] and len(answers) == 1
    monkeypatch.setattr("pdms_cli.commands.run.interactive_terminal", lambda: False)
    done.clear()
    monkeypatch.setattr(actions, "needs_install", lambda cfg, path, install: False)
    run.ensure_installed(Config(), old, None)  # no terminal: it keeps its Python
    assert done == []


def test_doctor_tells_how_many_are_left_behind(tmp_path, found, monkeypatch) -> None:
    backend = tmp_path / "backend"
    service(backend, "lead/old")
    service(backend, "lead/older")
    service(backend, "lead/fine", venv="3.12.3")
    monkeypatch.setattr(repos, "active_backend", lambda cfg: backend)
    checks = {check.name: check for check in doctor.check_service_pythons(Config())}
    assert checks["Python for the services"].status == doctor.OK
    assert "3.12.3" in checks["Python for the services"].detail
    left = checks["Virtualenvs of the services"]
    assert (left.status, left.fix) == (doctor.WARN, "services") and left.detail.startswith("2 run Python 3.10.13")


def test_the_page_gets_a_decision_and_answers_it(tmp_path, found, monkeypatch) -> None:
    from pdms_cli.ui import server as ui_server

    old = service(tmp_path, "old")
    with pytest.raises(actions.PythonMismatch) as asked:
        actions.check_pythons(Config(), [old], None, None)
    body = ui_server.decision_body(asked.value)
    assert body["decision"] == "python_mismatch" and body["services"][0]["use"] == "3.12.3" and "old" in body["error"]
    assert ui_server.launch_options({"python": True})["python"] is True
    with pytest.raises(actions.ActionError):
        ui_server.launch_options({"python": "yes"})
