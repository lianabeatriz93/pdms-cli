"""pdms from scripts: errors on stderr, --yes instead of a prompt, JSON lists, .env values that stay values."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pdms_cli import cli
from pdms_cli.commands.run import dotenv_value
from pdms_cli.config import Config, Database, DevUser, Stack


@pytest.fixture
def saved(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    cfg = Config(users={"agent": DevUser("u1", "a@x.com")},
                 dbs={"local": Database("localhost", password="s3cret"), "web": Database("db.example.com")},
                 stacks={"tp": Stack(services=["lead/a"])})
    cfg.save()
    return cfg


def pdms(*args: str):
    return CliRunner().invoke(cli.app, list(args), env={"COLUMNS": "200"})


def test_errors_go_to_stderr_not_into_the_output(saved) -> None:
    result = pdms("db", "remove", "nope")
    assert result.exit_code == 1
    assert "✗" in result.stderr and "✗" not in result.stdout


@pytest.mark.parametrize("group, name", [("db", "web"), ("user", "agent"), ("stack", "tp")])
def test_removing_without_a_terminal_needs_yes(saved, group, name) -> None:
    result = pdms(group, "remove", name)
    assert result.exit_code == 1 and "--yes" in result.stderr
    assert name in getattr(Config.load(), {"db": "dbs", "user": "users", "stack": "stacks"}[group])
    assert pdms(group, "remove", name, "--yes").exit_code == 0
    assert name not in getattr(Config.load(), {"db": "dbs", "user": "users", "stack": "stacks"}[group])


def test_lists_as_json_never_show_passwords(saved) -> None:
    dbs = json.loads(pdms("db", "list", "--json").stdout)
    assert dbs["local"]["has_password"] is True and "password" not in dbs["local"]
    assert "s3cret" not in pdms("db", "list", "--json").stdout
    assert json.loads(pdms("user", "list", "--json").stdout)["agent"]["user_id"] == "u1"
    assert json.loads(pdms("stack", "list", "--json").stdout)["tp"]["services"] == ["lead/a"]
    assert json.loads(pdms("ps", "--json").stdout) == []


def test_pdms_without_a_terminal_prints_its_help(saved) -> None:
    result = pdms()
    assert result.exit_code == 0 and "Usage" in result.stdout


def test_dotenv_values_keep_quotes_dollars_and_line_breaks() -> None:
    assert dotenv_value('pa"ss$word\\x\nnext') == 'pa\\"ss\\$word\\\\x\\nnext'
