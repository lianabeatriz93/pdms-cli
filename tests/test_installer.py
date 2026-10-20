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


def venv_python(service: Path) -> Path:
    from pdms_cli import runner

    python = service / ".venv" / ("Scripts/python.exe" if runner.WINDOWS else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("")
    return python


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


def test_copied_dependencies_lists_every_copy_transitively(tmp_path: Path) -> None:
    service = make_repo(tmp_path)
    common = (tmp_path / "backend" / "common").resolve()
    assert installer.copied_dependencies(service) == [common / "core", common / "base"]


def test_install_force_reinstalls_the_copied_dependencies(tmp_path: Path, monkeypatch) -> None:
    from pdms_cli import runner

    service = make_repo(tmp_path)
    python = venv_python(service)
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    runner.install(service)
    assert [c[1] for c in calls] == ["lock", "install", "-m"]
    assert calls[2][0] == str(python)  # the virtualenv's python, without starting poetry
    assert "--force-reinstall" in calls[2] and "--no-deps" in calls[2]
    assert [Path(p).name for p in calls[2][-2:]] == ["core", "base"]


def test_install_with_only_skips_poetry(tmp_path: Path, monkeypatch) -> None:
    from pdms_cli import runner

    service = make_repo(tmp_path)
    venv_python(service)
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    runner.install(service, only=[tmp_path / "backend/common/base"])
    assert len(calls) == 1 and calls[0][1:5] == ["-m", "pip", "install", "--quiet"]
    assert calls[0][-1] == str(tmp_path / "backend/common/base")
    calls.clear()
    runner.install(service, only=[])
    assert calls == []


def test_poetry_never_uses_the_virtualenv_pdms_runs_in(tmp_path: Path, monkeypatch) -> None:
    """Under ``uv run`` (or an activated venv) poetry would install into pdms's own environment."""
    from pdms_cli import runner
    from pdms_cli.config import Database, Defaults, DevUser

    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "pdms-venv"))
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path / "conda"))
    monkeypatch.setenv("KEEP_ME", "1")
    service = make_repo(tmp_path)
    envs = []
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda cmd, **kw: envs.append(kw["env"]) or runner.subprocess.CompletedProcess(cmd, 1, ""))
    runner.install(service)
    runner.poetry_python(service)
    envs.append(runner.build_env(Defaults(), DevUser("u1", "a@x.com"), Database("localhost")))
    assert len(envs) == 6  # lock, install, env info + pip, env info, the service
    for env in envs:
        assert "VIRTUAL_ENV" not in env and "CONDA_PREFIX" not in env and env["KEEP_ME"] == "1"


def test_only_code_of_copied_libraries_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    service = make_repo(tmp_path)
    common = (tmp_path / "backend" / "common").resolve()
    (service / "poetry.lock").write_text("# lock\n")
    assert installer.code_only_changes(service) is None  # never installed
    installer.remember(service)
    assert installer.code_only_changes(service) == []

    touch(tmp_path / "backend/common/base/base/utils.py", "y = 3\n")  # copied, transitive
    touch(tmp_path / "backend/common/storage/storage/s3.py", "z = 3\n")  # editable: nothing to do
    assert installer.code_only_changes(service) == [common / "base"]
    touch(tmp_path / "backend/common/core/core/models.py", "x = 3\n")
    assert installer.code_only_changes(service) == [common / "base", common / "core"]

    touch(service / "poetry.lock", "# lock\n")  # rewritten, the same content: still only code
    assert installer.code_only_changes(service) == [common / "base", common / "core"]


def test_any_manifest_change_takes_a_whole_install(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    service = make_repo(tmp_path)
    for changed in ("lead/svc/poetry.lock", "common/base/pyproject.toml", "common/storage/pyproject.toml"):
        installer.remember(service)
        touch(tmp_path / "backend/common/core/core/models.py", f"x = '{changed}'\n")
        path = tmp_path / "backend" / changed
        touch(path, (path.read_text() if path.exists() else "") + "\n# a new dependency\n")
        assert installer.code_only_changes(service) is None, changed


def test_records_of_older_versions_still_count(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    service = make_repo(tmp_path)
    path = installer.fingerprints_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({str(service): installer.fingerprint(service)}))
    assert installer.is_up_to_date(service)
    assert installer.code_only_changes(service) is None  # without parts: a whole install
