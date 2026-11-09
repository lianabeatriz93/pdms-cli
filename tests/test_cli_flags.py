"""db/user/stack add and edit with options instead of prompts, and doctor --json: pdms driven from scripts."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pdms_cli import cli, doctor
from pdms_cli.config import Config, Database, DevUser, Repo, Stack


@pytest.fixture
def saved(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    root = tmp_path / "pdms"
    for svc in ("lead/a", "lead/b"):
        (root / "backend" / svc).mkdir(parents=True)
        (root / "backend" / svc / "pyproject.toml").write_text("", encoding="utf-8")
        (root / "backend" / svc / "main.py").write_text("", encoding="utf-8")
    cfg = Config(users={"agent": DevUser("u1", "a@x.com", roles="TPR.Agent")},
                 dbs={"local": Database("localhost", user="pdm", password="s3cret"), "web": Database("db.example.com")},
                 stacks={"tp": Stack(services=["lead/a"], user="agent")},
                 repos={"pdms": Repo(str(root))}, current_repo="pdms")
    cfg.save()
    return cfg


def pdms(*args: str, stdin: str | None = None, env: dict | None = None):
    return CliRunner().invoke(cli.app, list(args), input=stdin, env={"COLUMNS": "200", **(env or {})})


# ---------------------------------------------------------------------------------------------------------- db


def test_db_add_with_options_asks_nothing(saved) -> None:
    result = pdms("db", "add", "qa", "--host", "qa.example.com", "--port", "5433", "--database", "pdm_qa",
                  "--user", "reader", "--password-stdin", "--view-readers", "ana, bob,ana", stdin="pa ss\n")
    assert result.exit_code == 0, result.output
    db = Config.load().dbs["qa"]
    assert (db.host, db.port, db.database, db.user, db.password) == ("qa.example.com", 5433, "pdm_qa", "reader", "pa ss")
    assert db.protected is True  # not localhost: protected as the wizard suggests
    assert db.view_readers == ["ana", "bob"]
    assert "pa ss" not in result.output


def test_db_add_defaults_as_the_wizard(saved) -> None:
    assert pdms("db", "add", "mine", "--user", "me", "--no-ping").exit_code == 0
    db = Config.load().dbs["mine"]
    assert (db.host, db.port, db.database, db.password, db.protected) == ("localhost", 5432, "pdm", "", False)


def test_db_add_password_from_an_environment_variable(saved) -> None:
    result = pdms("db", "add", "qa", "--user", "me", "--password-env", "QA_PW", env={"QA_PW": "from-env"})
    assert result.exit_code == 0, result.output
    assert Config.load().dbs["qa"].password == "from-env"
    missing = pdms("db", "add", "qb", "--user", "me", "--password-env", "PDMS_TEST_NOT_SET")
    assert missing.exit_code == 1 and "PDMS_TEST_NOT_SET" in missing.stderr
    both = pdms("db", "add", "qc", "--user", "me", "--password-env", "QA_PW", "--password-stdin", env={"QA_PW": "x"})
    assert both.exit_code == 1 and "not both" in both.stderr


def test_db_add_without_a_terminal_names_what_is_missing(saved) -> None:
    result = pdms("db", "add", "--host", "h")
    assert result.exit_code == 1
    assert "NAME" in result.stderr and "--user" in result.stderr
    assert set(Config.load().dbs) == {"local", "web"}


def test_db_add_checks_the_alias(saved) -> None:
    result = pdms("db", "add", "local", "--user", "me")
    assert result.exit_code == 1
    assert Config.load().dbs["local"].user == "pdm"


def test_db_edit_changes_only_the_options_given(saved) -> None:
    assert pdms("db", "edit", "local", "--port", "5440", "--protected").exit_code == 0
    db = Config.load().dbs["local"]
    assert (db.host, db.port, db.user, db.password, db.protected) == ("localhost", 5440, "pdm", "s3cret", True)
    assert pdms("db", "edit", "local", "--password-stdin", "--no-protected", stdin="new\n").exit_code == 0
    db = Config.load().dbs["local"]
    assert (db.password, db.protected) == ("new", False)


def test_db_edit_without_options_and_terminal_still_needs_one(saved) -> None:
    assert pdms("db", "edit", "local").exit_code != 0


# -------------------------------------------------------------------------------------------------------- user


def test_user_add_with_options(saved, monkeypatch) -> None:
    result = pdms("user", "add", "sup", "--user-id", "u2", "--username", "s@x.com", "--first-name", "Sue",
                  "--last-name", "Pérez", "--roles", "TPR.Supervisor")
    assert result.exit_code == 0, result.output
    user = Config.load().users["sup"]
    assert (user.user_id, user.username, user.first_name, user.last_name, user.roles) == (
        "u2", "s@x.com", "Sue", "Pérez", "TPR.Supervisor")


def test_user_add_without_a_terminal_names_what_is_missing(saved) -> None:
    result = pdms("user", "add", "sup", "--first-name", "Sue")
    assert result.exit_code == 1 and "--user-id" in result.stderr and "--username" in result.stderr
    assert "sup" not in Config.load().users


def test_user_add_rejects_unknown_roles(saved) -> None:
    result = pdms("user", "add", "sup", "--user-id", "u2", "--username", "s@x.com", "--roles", "Nope.Role")
    assert result.exit_code == 1 and "Nope.Role" in result.stderr


def test_user_edit_changes_only_the_options_given(saved) -> None:
    assert pdms("user", "edit", "agent", "--first-name", "Ana").exit_code == 0
    user = Config.load().users["agent"]
    assert (user.user_id, user.username, user.first_name, user.roles) == ("u1", "a@x.com", "Ana", "TPR.Agent")


# ------------------------------------------------------------------------------------------------------- stack


def test_stack_add_with_options(saved) -> None:
    result = pdms("stack", "add", "two", "--service", "lead/a", "--service", "lead/b,lead/a", "--user", "agent",
                  "--db", "local")
    assert result.exit_code == 0, result.output
    stack = Config.load().stacks["two"]
    assert (stack.services, stack.user, stack.db) == (["lead/a", "lead/b"], "agent", "local")


def test_stack_add_checks_services_users_and_databases(saved) -> None:
    assert pdms("stack", "add", "x", "--service", "lead/nope").exit_code == 1
    assert pdms("stack", "add", "x", "--service", "lead/a", "--db", "nope").exit_code == 1
    assert "x" not in Config.load().stacks


def test_stack_add_without_a_terminal_names_what_is_missing(saved) -> None:
    result = pdms("stack", "add", "--user", "agent")
    assert result.exit_code == 1 and "NAME" in result.stderr and "--service" in result.stderr
    assert pdms("stack", "add", "x", "--running", "--service", "lead/a").exit_code == 1


def test_stack_edit_changes_only_the_options_given(saved) -> None:
    assert pdms("stack", "edit", "tp", "--db", "web").exit_code == 0
    stack = Config.load().stacks["tp"]
    assert (stack.services, stack.user, stack.db) == (["lead/a"], "agent", "web")
    assert pdms("stack", "edit", "tp", "--service", "lead/b", "--user", "").exit_code == 0
    stack = Config.load().stacks["tp"]
    assert (stack.services, stack.user, stack.db) == (["lead/b"], "", "web")


# ------------------------------------------------------------------------------------------------------ doctor


@pytest.mark.parametrize("statuses, code", [((doctor.OK, doctor.WARN), 0), ((doctor.OK, doctor.FAIL), 1)])
def test_doctor_json_keeps_the_exit_code(saved, monkeypatch, statuses, code) -> None:
    checks = [doctor.Check("pdms", f"check {n}", status, "detail", "hint") for n, status in enumerate(statuses)]
    monkeypatch.setattr(doctor, "run_all", lambda cfg, databases, timeout: checks)
    result = pdms("doctor", "--json", "--no-db")
    assert result.exit_code == code
    data = json.loads(result.stdout)
    assert [c["status"] for c in data] == list(statuses)
    assert data[0] == {"section": "pdms", "name": "check 0", "status": "ok", "detail": "detail", "hint": "hint",
                       "fix": ""}
