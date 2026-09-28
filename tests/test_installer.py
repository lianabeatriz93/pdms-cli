"""Smart install fingerprints."""

from __future__ import annotations

import os
import time
from pathlib import Path

from pdms_cli import installer


def project(path: Path, deps: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(f"[tool.poetry]\nname = '{path.name}'\n[tool.poetry.group.dev.dependencies]\n{deps}")
    return path


def touch(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    later = time.time() + 5
    os.utime(path, (later, later))


def make_repo(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    project(backend / "common" / "base")
    project(backend / "common" / "core", 'base = { path = "../base", develop = false }\n')
    project(backend / "common" / "storage")
    service = project(
        backend / "lead" / "svc",
        'core = { path = "../../common/core", develop = false }\n'
        'storage = { path = "../../common/storage", develop = true }\n',
    )
    touch(backend / "common" / "core" / "core" / "models.py", "x = 1\n")
    touch(backend / "common" / "base" / "base" / "utils.py", "y = 1\n")
    touch(backend / "common" / "storage" / "storage" / "s3.py", "z = 1\n")
    return service


def test_path_dependencies_are_resolved_with_develop_flag(tmp_path):
    service = make_repo(tmp_path)
    deps = dict((p.name, develop) for p, develop in installer.path_dependencies(service))
    assert deps == {"core": False, "storage": True}


def test_code_of_copied_dependencies_changes_the_fingerprint(tmp_path):
    service = make_repo(tmp_path)
    before = installer.fingerprint(service)
    touch(tmp_path / "backend/common/core/core/models.py", "x = 2\n")
    assert installer.fingerprint(service) != before


def test_transitive_copied_dependencies_count_too(tmp_path):
    service = make_repo(tmp_path)
    before = installer.fingerprint(service)
    touch(tmp_path / "backend/common/base/base/utils.py", "y = 2\n")
    assert installer.fingerprint(service) != before


def test_editable_dependencies_only_count_their_pyproject(tmp_path):
    service = make_repo(tmp_path)
    before = installer.fingerprint(service)
    touch(tmp_path / "backend/common/storage/storage/s3.py", "z = 2\n")
    assert installer.fingerprint(service) == before
    touch(tmp_path / "backend/common/storage/pyproject.toml", "[tool.poetry]\nname = 'storage'\nversion = '2'\n")
    assert installer.fingerprint(service) != before


def test_tests_and_caches_are_ignored(tmp_path):
    service = make_repo(tmp_path)
    before = installer.fingerprint(service)
    touch(tmp_path / "backend/common/core/tests/test_models.py", "assert True\n")
    touch(tmp_path / "backend/common/core/core/__pycache__/models.cpython-310.pyc", "bytecode")
    assert installer.fingerprint(service) == before


def test_remember_and_is_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    service = make_repo(tmp_path)
    assert not installer.is_up_to_date(service)
    installer.remember(service)
    assert installer.is_up_to_date(service)
    touch(service / "poetry.lock", "# changed\n")
    assert not installer.is_up_to_date(service)


def test_parts_tell_which_library_changed(tmp_path):
    service = make_repo(tmp_path)
    recorded = installer.dependency_fingerprints(service)
    assert set(recorded) == {installer.SERVICE_PART, "core", "base", "storage"}
    assert installer.changed_parts(recorded, service) == []

    touch(tmp_path / "backend/common/base/base/utils.py", "y = 3\n")  # copied, transitive
    touch(tmp_path / "backend/common/storage/storage/s3.py", "z = 3\n")  # editable: picked up live
    assert installer.changed_parts(recorded, service) == ["base"]

    touch(service / "poetry.lock", "# relocked\n")
    assert installer.changed_parts(recorded, service) == [installer.SERVICE_PART, "base"]


def test_unknown_install_state_reports_nothing(tmp_path):
    service = make_repo(tmp_path)
    assert installer.changed_parts({}, service) is None
    assert installer.changed_parts(None, service) is None
