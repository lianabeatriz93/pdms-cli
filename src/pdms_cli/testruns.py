"""The tests of the services and packages of a repo: ``pdms test`` and the Tests screen of pdms ui.

Tests only ever run against a local database (:func:`migrations.is_local`): a test run writes and deletes rows, and
the shared databases hold other people's data. pdms always sets ``DB_PG_CONNECTION_STR`` for the run, so the
service's own ``.env`` (which may point to web-dev) is never used. Services share tables, so two projects only test
at the same time on different databases. Without a local database, pdms can start the one of
``backend/docker-compose_tests.yml``.

Each run writes a JUnit XML file, its output and a summary under ``<state>/tests/<repo>/``, one set per project.
"Affected by my changes" are the projects whose own files, or the files of a local package they install
(``common/core``), differ from where the branch left the main branch, uncommitted files included.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import IO

from . import actions, events, installer, instances, migrations, repos, runner
from .config import Config, Database, DevUser
from .i18n import _

TEST_DB_COMPOSE = "docker-compose_tests.yml"  # in the backend folder; the database the pipeline's tests use
TEST_DB_SERVICE = "db_pipeline_tests"
TEST_DB_NAME = "tests"
TEST_DB = Database(host="localhost", port=5439, database="alivi_pdms", user="local", password="local")
SKIP_DIRS = {*runner.SKIP_DIRS, ".venv", "venv", "dist", "build"}
MAX_FAILURES = 50  # kept per project; a broken fixture can fail hundreds of tests the same way
MAX_FAILURE_LINES = 30
NO_TESTS = 5  # pytest's exit code when it collected nothing
FRAME = re.compile(r"^(?P<path>[^\s:][^:]*\.py):(?P<line>\d+): ")


# ---------------------------------------------------------------------------------------------- local databases


def local_dbs(cfg: Config) -> list[str]:
    """The databases tests may use: local ones, one name per real database (two aliases of the same one count once)."""
    seen: set[tuple[str, int, str]] = set()
    names = []
    for name, db in cfg.dbs.items():
        where = (db.host.strip().lower(), db.port, db.database)
        if migrations.is_local(db) and where not in seen:
            seen.add(where)
            names.append(name)
    return names


def require_local(cfg: Config, name: str) -> Database:
    """The database ``name``, or :class:`actions.ActionError` when it is unknown or not local."""
    actions.require(cfg.dbs, _("database"), name)
    db = cfg.dbs[name]
    if not migrations.is_local(db):
        raise actions.ActionError(_(
            "Tests only run against a local database; '{name}' ({host}) is shared. Use a local one: {names}.",
            name=name, host=db.host, names=", ".join(local_dbs(cfg)) or _("none yet"),
        ))
    return db


def no_local_db_hint(backend: Path | None) -> str:
    compose = backend / TEST_DB_COMPOSE if backend else None
    if compose and compose.is_file():
        return _("No local database for tests. Start the one of {file} with pdms test --start-db, or add one with "
                 "pdms db add.", file=TEST_DB_COMPOSE)
    return _("No local database for tests. Add one with pdms db add (host localhost).")


def test_db_compose(backend: Path) -> Path:
    compose = backend / TEST_DB_COMPOSE
    if not compose.is_file():
        raise actions.ActionError(_("{file} is not in {path}.", file=TEST_DB_COMPOSE, path=backend))
    return compose


def start_test_db(cfg: Config, backend: Path, output: IO[str] | None = None, wait: float = 60) -> str:
    """Start the database of ``backend/docker-compose_tests.yml``, wait until it answers and register it as
    ``tests`` (an existing local alias of it is reused). Returns the alias."""
    compose = test_db_compose(backend)
    ok, detail = events.docker_available()
    if not ok:
        raise actions.ActionError(_("Docker is not available: {detail}", detail=detail or _("docker not found")))
    cmd = ["docker", "compose", "-f", str(compose), "up", "-d", TEST_DB_SERVICE]
    try:
        result = subprocess.run(cmd, cwd=backend, stdout=output or subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise actions.ActionError(_("{cmd} failed: {error}", cmd=" ".join(cmd[:3]), error=exc)) from exc
    if result.returncode != 0:
        raise actions.ActionError(_("{cmd} failed (exit code {code}).", cmd="docker compose up", code=result.returncode))
    name = next((n for n, db in cfg.dbs.items() if migrations.is_local(db) and (db.port, db.database)
                 == (TEST_DB.port, TEST_DB.database)), "")
    if not name:
        name = TEST_DB_NAME if TEST_DB_NAME not in cfg.dbs else f"{TEST_DB_NAME}-{TEST_DB.port}"
        actions.save_db(cfg, name, replace(TEST_DB), new=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            actions.check_connection(cfg.dbs[name], 3)
            return name
        except actions.ActionError as exc:
            if time.monotonic() > deadline:
                raise actions.ActionError(_("The test database does not answer yet: {error}", error=exc.message)) from exc
            time.sleep(1)


# ---------------------------------------------------------------------------------------------- projects


def projects(backend: Path, max_depth: int = 4) -> list[str]:
    """Every folder of ``backend`` with a pyproject.toml and a tests folder (services and packages), relative."""
    found: list[str] = []
    for dirpath, dirnames, _files in os.walk(backend):
        path = Path(dirpath)
        depth = len(path.relative_to(backend).parts)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        if depth >= max_depth:
            dirnames.clear()
        if (path / "pyproject.toml").is_file() and (path / "tests").is_dir():
            found.append(path.relative_to(backend).as_posix())
    return sorted(found)


def kind(path: Path) -> str:
    return "service" if runner.is_service(path) else "package"


def resolve_project(backend: Path, project: str) -> Path:
    path = (backend / project).resolve()
    if not path.is_relative_to(backend.resolve()) or not (path / "pyproject.toml").is_file():
        raise actions.ActionError(_("'{name}' is not a project of {path}.", name=project, path=backend))
    return path


# ---------------------------------------------------------------------------------------------- running


def results_dir(backend: Path) -> Path:
    """Where the results of one repo's backend go (two checkouts of the same repo keep theirs apart)."""
    tag = hashlib.sha256(str(backend.resolve()).encode()).hexdigest()[:10]
    return instances.state_dir() / "tests" / f"{backend.resolve().parent.name}-{tag}"


def files(backend: Path, project: str) -> dict[str, Path]:
    base = results_dir(backend) / project.replace("/", "__")
    return {"xml": base.with_suffix(".xml"), "log": base.with_suffix(".log"), "json": base.with_suffix(".json")}


def log_key(project: str) -> str:
    return f"test:{project}"


def pytest_command(xml: Path, extra: list[str] | None = None) -> list[str]:
    """pytest with a JUnit report (xunit1 adds each test's file and line), unless ``extra`` names its own."""
    extra = list(extra or [])
    report = [] if any(a.startswith(("--junitxml", "--junit-xml")) for a in extra) else [
        f"--junitxml={xml}", "-o", "junit_family=xunit1"]
    return [runner.poetry(), "run", "pytest", *report, *extra]


def test_env(cfg: Config, db: Database, user: DevUser | None = None) -> dict[str, str]:
    """The environment of a test run. The database goes last, so nothing (not even defaults.env) replaces it."""
    if not migrations.is_local(db):  # the callers checked; this is the last line of defence
        raise actions.ActionError(_("Tests only run against a local database."))
    return {
        **runner.poetry_environ(),
        "DEVELOPMENT_MODE": "True",
        "LOGGING_LEVEL": cfg.defaults.logging_level,
        **cfg.defaults.env,
        **(user.env() if user else {}),
        "DB_PG_CONNECTION_STR": db.url(),
    }


def run_tests(cfg: Config, backend: Path, project: str, db_name: str, *, install: bool | None = None,
              started: Callable[[subprocess.Popen], None] = lambda proc: None,
              cancelled: Callable[[], bool] = lambda: False) -> dict | None:
    """Run one project's tests on ``db_name`` with the output in its log, and keep the result (see :func:`record`).
    ``started`` gets the process (to stop it); a run ``cancelled`` says so in its log and keeps no result. Used by
    pdms ui; the CLI shows the output in the terminal."""
    db = require_local(cfg, db_name)
    path = resolve_project(backend, project)
    paths = files(backend, project)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    paths["xml"].unlink(missing_ok=True)
    begin = time.time()
    with open(paths["log"], "w", encoding="utf-8", errors="replace") as log:
        if actions.needs_install(cfg, path, install):
            log.write("$ poetry lock && poetry install\n")
            log.flush()
            actions.install_service(path, log)
        if cancelled():
            log.write(_("Stopped from pdms ui.") + "\n")
            return None
        cmd = pytest_command(paths["xml"])
        log.write(f"$ {' '.join(cmd[1:])}   # DB {db_name}\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=path, env=test_env(cfg, db), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **instances.detach_options())
        started(proc)
        code = proc.wait()
        if cancelled():
            log.write("\n" + _("Stopped from pdms ui.") + "\n")
            return None
    return record(backend, project, db=db_name, code=code, started=begin, seconds=time.time() - begin,
                  commit=repos.git_commit(backend.parent), origin="ui")


# ---------------------------------------------------------------------------------------------- results


def _location(text: str, project: Path) -> tuple[str, int]:
    """The deepest frame of a failure's traceback inside the project (pytest prints ``file.py:88: Error``)."""
    found = ("", 0)
    for line in text.splitlines():
        match = FRAME.match(line.strip())
        if not match:
            continue
        path = Path(match["path"])
        path = path if path.is_absolute() else project / path
        if ".venv" in path.parts or "site-packages" in path.parts:
            continue
        found = (str(path), int(match["line"]))
    return found


def test_id(case: ET.Element) -> str:
    """A test as pytest names it: ``tests/test_x.py::TestClass::test_name`` (the class comes from classname)."""
    name, test_file, classname = case.get("name") or "", case.get("file") or "", case.get("classname") or ""
    if not test_file:
        return f"{classname.replace('.', '/')}::{name}" if classname else name
    module = test_file.removesuffix(".py").replace("/", ".")
    inside = classname[len(module) + 1:] if classname.startswith(module + ".") else ""
    return "::".join(part for part in (test_file, inside.replace(".", "::"), name) if part)


def parse(xml: Path, project: Path) -> dict | None:
    """Counts and failures of a JUnit XML report; None when there is no readable report."""
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return None
    counts = {"tests": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures = []
    seconds = 0.0
    for case in root.iter("testcase"):
        counts["tests"] += 1
        try:
            seconds += float(case.get("time") or 0)
        except ValueError:
            pass
        problem = next((case.find(tag) for tag in ("failure", "error") if case.find(tag) is not None), None)
        if case.find("skipped") is not None and problem is None:
            counts["skipped"] += 1
            continue
        if problem is None:
            continue
        counts["failed" if problem.tag == "failure" else "errors"] += 1
        if len(failures) >= MAX_FAILURES:
            continue
        text = (problem.text or "").rstrip()
        path, line = _location(text, project)
        if not path and (test_file := case.get("file")):
            path, line = str(project / test_file), int(case.get("line") or 0) + 1  # xunit1 lines start at 0
        failures.append({
            "name": test_id(case),
            "kind": problem.tag,
            "message": (problem.get("message") or "").strip().splitlines()[0][:300] if problem.get("message") else "",
            "path": path,
            "line": line,
            "text": "\n".join(text.splitlines()[-MAX_FAILURE_LINES:]),
        })
    counts["passed"] = counts["tests"] - counts["failed"] - counts["errors"] - counts["skipped"]
    return {**counts, "test_seconds": round(seconds, 2), "failures": failures}


def record(backend: Path, project: str, *, db: str, code: int, started: float, seconds: float, commit: str,
           origin: str) -> dict:
    """Read the run's report and keep its summary next to it: the Tests screen shows it, from the CLI too."""
    paths = files(backend, project)
    report = parse(paths["xml"], backend / project)
    if report is None:
        outcome = "empty" if code == NO_TESTS else "broken"  # pytest itself failed: the log says why
        report = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "test_seconds": 0, "failures": []}
    elif code == NO_TESTS or report["tests"] == 0:
        outcome = "empty"
    else:
        outcome = "failed" if report["failed"] or report["errors"] or code != 0 else "passed"
    result = {
        "project": project, "outcome": outcome, "code": code, "db": db, "commit": commit[:12], "origin": origin,
        "at": datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
        "seconds": round(seconds, 1), **report,
    }
    paths["json"].parent.mkdir(parents=True, exist_ok=True)
    paths["json"].write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def results(backend: Path) -> dict[str, dict]:
    """The last result of each project of ``backend`` that ran its tests, by project."""
    found = {}
    for path in sorted(results_dir(backend).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("project"), str):
            found[data["project"]] = data
    return found


# ---------------------------------------------------------------------------------------------- affected


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def base_of(root: Path) -> tuple[str, str]:
    """``(main branch, commit where HEAD left it)``: origin's default branch, else main or master; ``("", "")``
    when there is none."""
    candidates = []
    if (head := _git(root, "rev-parse", "--abbrev-ref", "origin/HEAD")) and head.strip() != "origin/HEAD":
        candidates.append(head.strip())
    candidates += ["origin/main", "main", "origin/master", "master"]
    for ref in candidates:
        if (base := _git(root, "merge-base", "HEAD", ref)) and base.strip():
            return ref, base.strip()
    return "", ""


def changed_files(root: Path) -> tuple[str, list[str]]:
    """``(main branch, files)``: the files that differ from where the branch left it, uncommitted and new ones
    included, relative to ``root``."""
    branch, base = base_of(root)
    changed: set[str] = set()
    diff = _git(root, "diff", "--name-only", "--no-renames", base) if base else _git(root, "diff", "--name-only", "HEAD")
    changed.update((diff or "").splitlines())
    changed.update((_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    return branch, sorted(f for f in changed if f)


def dependencies(project: Path) -> list[Path]:
    """Every local path dependency of ``project``, transitive ones included (develop or installed as a copy)."""
    found: list[Path] = []
    pending = installer.path_dependencies(project)
    while pending:
        directory, _develop = pending.pop(0)
        if directory in found or directory == project or not directory.is_dir():
            continue
        found.append(directory)
        pending.extend(installer.path_dependencies(directory))
    return found


def affected(backend: Path) -> dict:
    """``{"branch", "files", "items": [{"project", "why"}]}``: the projects the changes of the branch touch. ``why``
    lists "own" for its own files and the backend-relative folder of each changed package it installs."""
    backend = backend.resolve()
    root = backend.parent
    branch, changed = changed_files(root)
    # Every folder that holds a changed file: a project or package is touched when its own folder is one of them
    # (a set lookup each, instead of comparing every file with every folder).
    touched: set[Path] = set()
    for name in changed:
        path = (root / name).resolve()
        if path.is_relative_to(backend):
            touched.update(path.parents)
    items = []
    if touched:
        for project in projects(backend):
            folder = (backend / project).resolve()
            why = ["own"] if folder in touched else []
            for dep in dependencies(folder):
                if dep in touched and dep.is_relative_to(backend):
                    why.append(dep.relative_to(backend).as_posix())
            if why:
                items.append({"project": project, "why": why})
    return {"branch": branch, "files": len(changed), "items": items}
