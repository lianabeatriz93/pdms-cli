"""pdms doctor checks."""

from __future__ import annotations

import os
import stat

import pytest

from pdms_cli import doctor
from pdms_cli.config import Config, Database, DevUser


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_missing_poetry_is_a_failure(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    checks = statuses(doctor.check_tools())
    assert checks["Poetry"] == doctor.FAIL


def test_python_for_services_is_found_by_name(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name if name == "python3.11" else None)
    assert doctor.python_versions_available() == ["3.11"]


def test_config_checks(home):
    cfg = Config(users={"sup": DevUser("u", "s@x.com")}, dbs={"local": Database("localhost")})
    assert doctor.check_config(cfg)[0].status == doctor.WARN  # file not created yet
    cfg.save()
    checks = statuses(doctor.check_config(cfg))
    assert checks["File"] == doctor.OK and checks["Users"] == doctor.OK
    assert checks["Databases"] == doctor.WARN  # "local" has no password
    if os.name == "posix":
        assert checks["Permissions"] == doctor.OK
        os.chmod(home / "cfg" / "config.toml", 0o644)
        assert statuses(doctor.check_config(cfg))["Permissions"] == doctor.WARN
        assert stat.S_IMODE((home / "cfg" / "config.toml").stat().st_mode) == 0o644


def test_unreachable_database_is_a_failure(home):
    cfg = Config(dbs={"nowhere": Database("127.0.0.1", port=1, user="x", password="y")})
    [check] = doctor.check_databases(cfg, timeout=2)
    assert check.status == doctor.FAIL and "pdms db edit nowhere" in check.hint


def test_completion_detection(tmp_path):
    assert doctor.completion_installed(tmp_path) is None
    (tmp_path / ".bash_completions").mkdir()
    (tmp_path / ".bash_completions" / "pdms.sh").write_text("")
    assert doctor.completion_installed(tmp_path).endswith("pdms.sh")


def test_run_all_without_repo_or_config_does_not_crash(home):
    checks = doctor.run_all(Config(), databases=False)
    assert {c.section for c in checks} >= {"pdms", "Repo"}
    assert all(c.status in (doctor.OK, doctor.WARN, doctor.FAIL) for c in checks)
