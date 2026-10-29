"""Local databases behind pdms-db-migrations: found before services start on them, applied only when agreed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import actions, images, migrations
from pdms_cli.commands import run
from pdms_cli.config import Config, Database


def state(*pending: str, flyway: bool = True) -> dict:
    return {"flyway": flyway, "migrations": [
        {"kind": "versioned", "version": "2026.08.25.467.01", "description": "canary", "state": "applied"},
        *({"kind": "versioned", "version": v, "description": "x", "state": "pending"} for v in pending),
        {"kind": "repeatable", "version": "", "description": "MV capacity view", "state": "outdated"},
    ]}


@pytest.fixture
def cfg(monkeypatch, tmp_path) -> Config:
    monkeypatch.setattr(actions, "current_migrations_repo", lambda cfg: tmp_path / "pdms-db-migrations")
    monkeypatch.setattr(images, "require", lambda names: None)
    return Config(dbs={"pdms-local": Database("localhost", 5440, "pdms", "local", "pw"),
                       "web-dev": Database("db.example.com", protected=True)})


def test_only_local_databases_flyway_manages(cfg, monkeypatch) -> None:
    answers = {"pdms": state("2026.09.15.475.01")}
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: answers[db.database])
    item = actions.unmigrated(cfg, "pdms-local")
    assert item is not None and item.versions == ["2026.09.15.475.01", "MV capacity view"]
    assert item.describe() | {"repo": ""} == {"db": "pdms-local", "count": 2, "first": "2026.09.15.475.01",
                                               "last": "MV capacity view", "changed": [], "uncommitted": 0,
                                               "repo": ""}
    assert actions.unmigrated(cfg, "web-dev") is None  # shared: the pipeline migrates it
    answers["pdms"] = state("2026.09.15.475.01", flyway=False)
    assert actions.unmigrated(cfg, "pdms-local") is None  # tables made another way: never migrated by pdms
    answers["pdms"] = {"flyway": True, "migrations": []}
    assert actions.unmigrated(cfg, "pdms-local") is None
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: (_ for _ in ()).throw(OSError("down")))
    assert actions.unmigrated(cfg, "pdms-local") is None  # not knowing does not stop the services


def test_the_user_answers_before_anything_starts(cfg, monkeypatch) -> None:
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: state("2026.10.05.544.01"))
    with pytest.raises(actions.MigrationsPending) as asked:
        actions.check_migrations(cfg, ["pdms-local", "web-dev", "pdms-local"], None)
    assert [item.alias for item in asked.value.items] == ["pdms-local"]
    assert [item.alias for item in actions.check_migrations(cfg, ["pdms-local"], True)] == ["pdms-local"]
    assert actions.check_migrations(cfg, ["pdms-local"], False) == []


def test_applying_runs_flyway_migrate_with_the_pipelines_image(cfg, monkeypatch, tmp_path) -> None:
    calls = []
    prepared = []
    monkeypatch.setattr(actions, "ensure_support_schemas", lambda db: prepared.append(db.database))
    monkeypatch.setattr(actions.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["env"]["DB_PASSWORD"]))
                        or SimpleNamespace(returncode=0))
    item = actions.Unmigrated("pdms-local", cfg.dbs["pdms-local"], tmp_path, ["2026.10.05.544.01"])
    actions.apply_migrations(item)
    cmd, password = calls[0]
    assert cmd[-1] == "migrate" and migrations.IMAGE in cmd and password == "pw" and prepared == ["pdms"]
    calls.clear()
    actions.apply_migrations(actions.Unmigrated("pdms-local", cfg.dbs["pdms-local"], tmp_path, ["V9"], ["443.02"]))
    assert [cmd[-1] for cmd, _password in calls] == ["repair", "migrate"]  # the changed ones' checksums first
    monkeypatch.setattr(actions.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(actions.ActionError, match="Flyway migrate failed on pdms-local"):
        actions.apply_migrations(item)


def test_the_terminal_asks_and_applies(cfg, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: state("2026.10.05.544.01"))
    applied = []
    monkeypatch.setattr(actions, "apply_migrations", lambda item, output=None: applied.append(item.alias))
    monkeypatch.setattr(run, "interactive_terminal", lambda: True)
    monkeypatch.setattr(run.questionary, "confirm", lambda message, default: SimpleNamespace(unsafe_ask=lambda: True))
    run.migrate_first(cfg, ["pdms-local"])
    assert applied == ["pdms-local"]
    monkeypatch.setattr(run.questionary, "confirm", lambda message, default: SimpleNamespace(unsafe_ask=lambda: False))
    run.migrate_first(cfg, ["pdms-local"])
    monkeypatch.setattr(run, "interactive_terminal", lambda: False)  # no terminal: it starts as it is
    run.migrate_first(cfg, ["pdms-local"])
    assert applied == ["pdms-local"]


def test_the_page_gets_a_decision(cfg, monkeypatch) -> None:
    from pdms_cli.ui import api as ui_api

    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: state("2026.10.05.544.01"))
    with pytest.raises(actions.MigrationsPending) as asked:
        actions.check_migrations(cfg, ["pdms-local"], None)
    body = ui_api.decision_body(asked.value)
    assert body["decision"] == "migrations_pending" and body["dbs"][0]["count"] == 2 and "pdms-local" in body["error"]
    assert ui_api.launch_options({"migrate": False})["migrate"] is False
    with pytest.raises(actions.ActionError):
        ui_api.launch_options({"migrate": "yes"})


def test_changed_and_uncommitted_ones_are_told_apart(cfg, monkeypatch) -> None:
    known = {"flyway": True, "migrations": [
        {"kind": "versioned", "version": "443.02", "description": "x", "state": "changed", "uncommitted": False},
        {"kind": "versioned", "version": "544.01", "description": "x", "state": "pending", "uncommitted": True},
        {"kind": "repeatable", "version": "", "description": "MV view", "state": "outdated", "uncommitted": True},
        {"kind": "versioned", "version": "568.01", "description": "x", "state": "pending", "uncommitted": False},
    ]}
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: known)
    item = actions.unmigrated(cfg, "pdms-local")
    assert item is not None and item.versions == ["544.01", "MV view", "568.01"] and item.changed == ["443.02"]
    assert item.uncommitted == ["544.01", "MV view"] and item.describe()["uncommitted"] == 2


def test_uncommitted_files_come_from_git(tmp_path) -> None:
    import subprocess

    repo = tmp_path / "pdms-db-migrations"
    (repo / "migrations").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "migrations" / "V1__a.sql").write_text("select 1;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "a"], check=True)
    assert migrations.uncommitted(repo) == set()
    (repo / "migrations" / "V2__b.sql").write_text("select 2;\n", encoding="utf-8")  # new, not added
    (repo / "migrations" / "V1__a.sql").write_text("select 11;\n", encoding="utf-8")  # changed
    assert migrations.uncommitted(repo) == {"migrations/V1__a.sql", "migrations/V2__b.sql"}


# --------------------------------------------------------------------------- test databases and the local copy


def test_a_test_database_made_from_the_copy_gets_what_it_lacks(cfg, monkeypatch, tmp_path) -> None:
    from pdms_cli import localdb, testruns

    copied, applied = [], []
    monkeypatch.setattr(localdb, "copy_into_test_database", lambda name: copied.append(name))
    monkeypatch.setattr(testruns, "require_test_db", lambda name: Database("localhost", 5440, name, "local", "local"))
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: state("2026.10.05.544.01"))
    monkeypatch.setattr(actions, "apply_migrations", lambda item, output=None: applied.append(item.db.database))
    assert testruns.migration_state(cfg, "pdms_test_1") == {"flyway": True, "pending": 2, "changed": 0,
                                                            "uncommitted": 0}
    assert testruns.prepare_from_copy(cfg, "pdms_test_1") == 2
    assert copied == ["pdms_test_1"] and applied == ["pdms_test_1"]
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: state("x", flyway=False))
    assert testruns.migration_state(cfg, "pdms_test_2")["flyway"] is False
    assert testruns.migrate_test_db(cfg, "pdms_test_2") == 0  # the tests' tables: never migrated over


def test_copying_into_a_test_database_needs_the_copy(monkeypatch) -> None:
    from pdms_cli import localdb

    monkeypatch.setattr(localdb, "databases", lambda: {"pdms_test_1": 1})
    with pytest.raises(actions.ActionError, match="no local copy yet"):
        localdb.copy_into_test_database("pdms_test_1")


def test_the_copy_says_what_it_will_lack_before_copying(cfg, monkeypatch) -> None:
    from pdms_cli import localcopy, localdb

    monkeypatch.setattr(localcopy, "source", lambda cfg, alias="", database="": Database("rds", 5432, database))
    monkeypatch.setattr(localdb, "state", lambda: {"port": 5440, "running": True, "exists": True})
    known = state("2026.10.05.544.01")
    known["migrations"][1]["uncommitted"] = True
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: known)
    item = localcopy.pending_after_copy(cfg, Path("repo"))
    assert item is not None and item.alias == "pdms-local" and item.db.port == 5440
    assert item.versions == ["2026.10.05.544.01", "MV capacity view"] and item.uncommitted == ["2026.10.05.544.01"]
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: {"flyway": True, "migrations": []})
    assert localcopy.pending_after_copy(cfg, Path("repo")) is None


def test_doctor_points_the_copy_to_a_new_copy_and_others_to_repair(cfg, monkeypatch) -> None:
    from pdms_cli import doctor, localcopy

    cfg.dbs["local-dev"] = Database("localhost", 5434, "pdm", "postgres", "x")
    monkeypatch.setattr(localcopy, "alias", lambda cfg, port=0: "pdms-local")
    known = state("2026.10.05.544.01")
    known["migrations"].append({"kind": "versioned", "version": "443.02", "description": "x", "state": "changed"})
    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: known)
    checks = {check.name: check for check in doctor.check_local_migrations(cfg)}
    assert set(checks) == {"Migrations of pdms-local", "Migrations of local-dev"}  # web-dev is not local
    assert checks["Migrations of pdms-local"].detail == "2 migrations pending · 1 changed since they ran"
    assert "pdms db local refresh" in checks["Migrations of pdms-local"].hint
    assert "Repair and apply" in checks["Migrations of local-dev"].hint and checks["Migrations of local-dev"].fix == "data"
