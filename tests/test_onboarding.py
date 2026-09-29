"""Guided setup: what is still missing, and exported files offered for import."""

from __future__ import annotations

import os
from pathlib import Path

from pdms_cli import onboarding
from pdms_cli.config import Config, Database, DevUser, Repo


def make_repo(root: Path) -> Path:
    (root / "backend" / "snakesdk").mkdir(parents=True)
    return root


def make_migrations(root: Path) -> Path:
    (root / "migrations").mkdir(parents=True)
    (root / "flyway.toml").write_text("")
    return root


def test_an_empty_config_misses_everything_but_the_migrations():
    # Without a repo there is nothing to attach the migrations to yet.
    assert onboarding.pending(Config()) == ["repo", "dbs", "users"]


def test_a_complete_config_misses_nothing(tmp_path):
    repo = make_repo(tmp_path / "pdms")
    migs = make_migrations(tmp_path / "pdms-db-migrations")
    cfg = Config(
        repos={"pdms": Repo(path=str(repo), migrations=str(migs))}, current_repo="pdms",
        dbs={"local": Database(host="localhost", user="postgres", password="secret")},
        users={"sup": DevUser(user_id="1", username="sup@example.com")},
    )
    assert onboarding.pending(cfg) == []


def test_imported_databases_without_password_are_pending(tmp_path):
    cfg = Config(dbs={"dev": Database(host="dev.example.com", user="app"), "local": Database(host="localhost",
                                                                                           password="x")})
    assert "passwords" in onboarding.pending(cfg)
    assert "dbs" not in onboarding.pending(cfg)


def test_a_repo_whose_folder_is_gone_is_pending_again(tmp_path):
    cfg = Config(repos={"pdms": Repo(path=str(tmp_path / "gone"))}, current_repo="pdms")
    assert onboarding.pending(cfg)[0] == "repo"


def test_migrations_are_pending_when_missing_or_invalid(tmp_path):
    repo = make_repo(tmp_path / "pdms")
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms")
    assert "migrations" in onboarding.pending(cfg)
    cfg.repos["pdms"].migrations = str(tmp_path / "not-there")
    assert "migrations" in onboarding.pending(cfg)
    cfg.repos["pdms"].migrations = str(make_migrations(tmp_path / "pdms-db-migrations"))
    assert "migrations" not in onboarding.pending(cfg)


def test_find_exports_lists_newest_first_and_ignores_other_files(tmp_path):
    downloads, here = tmp_path / "Downloads", tmp_path / "here"
    downloads.mkdir(), here.mkdir()
    old = here / "pdms-config-2026-01-01.toml"
    new = downloads / "pdms-config-team.toml"
    for i, path in enumerate((old, new)):
        path.write_text("[pdms]\n")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    (here / "other.toml").write_text("")
    (here / "pdms-config-folder.toml").mkdir()
    assert onboarding.find_exports([here, downloads, tmp_path / "missing"]) == [new.resolve(), old.resolve()]


def test_find_exports_does_not_repeat_a_folder_listed_twice(tmp_path):
    (tmp_path / "pdms-config.toml").write_text("")
    assert onboarding.find_exports([tmp_path, tmp_path]) == [(tmp_path / "pdms-config.toml").resolve()]
