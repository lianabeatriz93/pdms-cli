"""pdms migrate: Flyway through Docker, with info/validate anywhere and migrate only locally."""

from __future__ import annotations

import zlib
from datetime import datetime
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
    from pdms_cli import events
    from pdms_cli.commands import testing

    monkeypatch.setattr(events, "docker_available", lambda: (True, "test"))
    from pdms_cli import images

    monkeypatch.setattr(images, "present", lambda name: True)
    calls = []
    monkeypatch.setattr(testing.subprocess, "run", lambda cmd, env: calls.append((cmd, env)) or type("R", (), {"returncode": 0})())
    result = cli("migrate", "info", "-d", "web-dev")
    assert result.exit_code == 0, result.output
    cmd, env = calls[0]
    assert cmd[-1] == "info" and env["DB_PASSWORD"] == ""
    assert Config.load().repos["pdms"].migrations == str((tmp_path / "pdms-db-migrations").resolve())


def test_pull_retries_and_reports_the_last_error(monkeypatch):
    from pdms_cli import actions, images

    outcomes = iter([1, 1, 0])
    monkeypatch.setattr(images.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": next(outcomes), "stdout": "toomanyrequests: Rate exceeded"})())
    monkeypatch.setattr(images.time, "sleep", lambda s: None)
    images.pull(images.FLYWAY)  # the third attempt works
    monkeypatch.setattr(images.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stdout": "error\ntoomanyrequests: Rate exceeded"})())
    with pytest.raises(actions.ActionError, match="toomanyrequests: Rate exceeded"):
        images.pull(images.FLYWAY, attempts=2)


# --------------------------------------------------------------------------- status without Docker


def flyway_repo(root: Path) -> Path:
    (root / "migrations" / "versioned" / "next_release").mkdir(parents=True)
    (root / "migrations" / "repeatable").mkdir(parents=True)
    (root / "migrations" / "rollback").mkdir(parents=True)
    (root / "flyway.toml").write_text(
        '[flyway]\nlocations = ["filesystem:migrations/repeatable", "filesystem:migrations/versioned"]\n'
        'table = "flyway_schema_history"\n[environments.local]\nschemas = ["public"]\n', encoding="utf-8")
    versioned = root / "migrations" / "versioned" / "next_release"
    (versioned / "V2026.08.25.467.01__create_canary.sql").write_text("create table canary();\n")
    (versioned / "V2026.09.01.474.01__backfill_taxonomy.sql").write_text("update x set y = 1;\n")
    (versioned / "V2026.09.29.443.01__create_relation.sql").write_text("create table r();\n")
    (root / "migrations" / "repeatable" / "R__report_view.sql").write_text("create view v as select 2;\n")
    (root / "migrations" / "rollback" / "V2026.09.01.474.01__backfill_taxonomy.sql").write_text("-- not a location\n")
    return root


def test_flyway_checksum_is_crc32_of_the_lines() -> None:
    assert migrations.checksum("") == 0
    same = migrations.checksum("select 1;\nselect 2;\n")
    assert migrations.checksum("select 1;\r\nselect 2;") == same == migrations.checksum("﻿select 1;\rselect 2;\n")
    unsigned = zlib.crc32(b"select 2;", zlib.crc32(b"select 1;"))
    assert same == (unsigned - (1 << 32) if unsigned >= 1 << 31 else unsigned)  # Java's int: signed


def test_files_follow_the_locations_of_flyway_toml(tmp_path) -> None:
    repo = flyway_repo(tmp_path)
    found = migrations.files(repo)
    assert [(f["kind"], f["version"]) for f in found] == [
        ("repeatable", ""), ("versioned", "2026.08.25.467.01"), ("versioned", "2026.09.01.474.01"),
        ("versioned", "2026.09.29.443.01")]  # rollback/ is not a location
    assert found[1]["description"] == "create canary" and found[1]["script"] == "next_release/V2026.08.25.467.01__create_canary.sql"


def test_status_compares_the_history_with_the_files(tmp_path, monkeypatch) -> None:
    repo = flyway_repo(tmp_path)
    view = next(f for f in migrations.files(repo) if f["kind"] == "repeatable")
    now = datetime(2026, 9, 30, 12, 0)
    rows = [
        {"version": "1", "description": "<< Flyway Baseline >>", "type": "BASELINE", "script": "", "checksum": None,
         "installed_on": now, "success": True},
        {"version": "2026.08.25.467.01", "description": "create canary", "type": "SQL", "script": "x", "checksum": 1,
         "installed_on": now, "success": True},
        {"version": "2026.09.01.474.01", "description": "backfill taxonomy", "type": "SQL", "script": "x", "checksum": 1,
         "installed_on": now, "success": False},
        {"version": "2026.07.01.100.01", "description": "from another branch", "type": "SQL", "script": "old.sql",
         "checksum": 1, "installed_on": now, "success": True},
        {"version": None, "description": "report view", "type": "SQL", "script": "R__report_view.sql",
         "checksum": view["checksum"] + 1, "installed_on": now, "success": True},
    ]
    monkeypatch.setattr(migrations, "history", lambda db, flyway, timeout: rows)
    result = migrations.status(repo, Database("localhost"))
    states = {(m["version"] or m["description"]): m["state"] for m in result["migrations"]}
    assert states == {"2026.07.01.100.01": "missing", "2026.08.25.467.01": "applied", "2026.09.01.474.01": "failed",
                      "2026.09.29.443.01": "pending", "report view": "outdated"}
    assert result["counts"] == {"applied": 1, "pending": 1, "failed": 1, "outdated": 1, "missing": 1}
    assert result["flyway"] and result["table"] == "public.flyway_schema_history"

    rows[-1]["checksum"] = view["checksum"]
    assert {m["description"]: m["state"] for m in migrations.status(repo, Database("x"))["migrations"]}["report view"] == "applied"
    monkeypatch.setattr(migrations, "history", lambda db, flyway, timeout: None)
    never = migrations.status(repo, Database("x"))
    assert not never["flyway"] and never["counts"]["pending"] == 4
