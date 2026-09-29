"""pdms migrate: Flyway through Docker, with info/validate anywhere and migrate only locally."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdms_cli import migrations
from pdms_cli.config import Config, Database, Repo


def migrations_repo(path: Path) -> Path:
    (path / "migrations").mkdir(parents=True)
    (path / "flyway.toml").write_text("[flyway]\n", encoding="utf-8")
    return path


def test_detection_and_matching(tmp_path):
    pdms_v2 = tmp_path / "pdms_v2"
    pdms_v2.mkdir()
    old, v2 = migrations_repo(tmp_path / "pdms-db-migrations"), migrations_repo(tmp_path / "pdms-db-migrations-v2")
    assert migrations.siblings(pdms_v2) == [old.resolve(), v2.resolve()]
    assert migrations.best_match(pdms_v2, migrations.siblings(pdms_v2)) == v2.resolve()
    assert migrations.best_match(tmp_path / "pdms", migrations.siblings(pdms_v2)) == old.resolve()
    assert migrations.find_upwards(v2 / "migrations") == v2.resolve()
    assert migrations.find_upwards(tmp_path) is None


@pytest.mark.parametrize("host,protected,local", [
    ("localhost", False, True), ("127.0.0.1", False, True), ("localhost", True, False),
    ("pdm-cluster.cluster-x.us-east-1.rds.amazonaws.com", False, False),
])
def test_only_local_unprotected_databases_can_be_migrated(host, protected, local):
    assert migrations.is_local(Database(host, protected=protected)) is local


def test_docker_command_keeps_the_password_out_of_argv(tmp_path, monkeypatch):
    repo = migrations_repo(tmp_path / "pdms-db-migrations")
    (repo / "placeholder.toml").write_text("", encoding="utf-8")
    db = Database("localhost", port=5434, database="pdm", user="u", password="s3cret")
    monkeypatch.setattr(migrations.sys, "platform", "linux")
    cmd = migrations.docker_command(repo, db, "validate")
    assert "s3cret" not in " ".join(cmd) and "DB_PASSWORD" in cmd
    assert cmd[cmd.index("--network") + 1] == "host" and "DB_HOST=localhost" in cmd
    assert cmd[cmd.index(migrations.IMAGE) + 1:] == [
        "-configFiles=flyway.toml,placeholder.toml", "-environment=local", "-ignoreMigrationPatterns=*:pending", "validate",
    ]
    monkeypatch.setattr(migrations.sys, "platform", "darwin")
    cmd = migrations.docker_command(repo, db, "info")
    assert "--network" not in cmd and "DB_HOST=host.docker.internal" in cmd and cmd[-1] == "info"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    root = tmp_path / "pdms"
    (root / "backend" / "snakesdk").mkdir(parents=True)
    migrations_repo(tmp_path / "pdms-db-migrations")
    cfg = Config(repos={"pdms": Repo(path=str(root))}, current_repo="pdms")
    cfg.dbs = {"web-dev": Database("db.example.com", protected=True), "local": Database("localhost", port=5434)}
    cfg.save()
    from pdms_cli.cli import app

    return lambda *args: CliRunner().invoke(app, list(args))


def test_migrate_is_refused_on_shared_databases(cli):
    result = cli("migrate", "migrate", "-d", "web-dev")
    assert result.exit_code == 1 and "only runs against a local database" in result.output


def test_other_flyway_commands_are_not_allowed(cli):
    for command in ("clean", "repair", "baseline"):
        result = cli("migrate", command, "-d", "local")
        assert result.exit_code == 1 and "not allowed from pdms" in result.output


def test_the_sibling_migrations_repo_is_found_and_remembered(cli, tmp_path, monkeypatch):
    from pdms_cli import cli as cli_module, events

    monkeypatch.setattr(events, "docker_available", lambda: (True, "test"))
    calls = []
    monkeypatch.setattr(cli_module.subprocess, "run", lambda cmd, env: calls.append((cmd, env)) or type("R", (), {"returncode": 0})())
    result = cli("migrate", "info", "-d", "web-dev")
    assert result.exit_code == 0, result.output
    cmd, env = calls[0]
    assert cmd[-1] == "info" and env["DB_PASSWORD"] == ""
    assert Config.load().repos["pdms"].migrations == str((tmp_path / "pdms-db-migrations").resolve())
