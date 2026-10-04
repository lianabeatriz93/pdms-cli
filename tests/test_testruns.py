"""Tests of the services (pdms test and the Tests screen): only local databases, one project at a time per database,
JUnit results kept per project, and the projects the branch's changes touch."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, cli, runner, testruns
from pdms_cli.config import Config, Database, Repo
from pdms_cli.ui import jobs as ui_jobs
from pdms_cli.ui.testing import Tests as Runner

WEB = Database(host="pdm-cluster.cluster-x.us-east-1.rds.amazonaws.com", database="pdm", protected=True)
LOCAL = Database(host="localhost", port=5434, database="pdms_sync", user="local", password="local")
OTHER = Database(host="127.0.0.1", port=5439, database="alivi_pdms", user="local", password="local")

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5" time="1.2">
<testcase classname="tests.test_details" file="tests/test_details.py" line="10" name="test_ok" time="0.1"/>
<testcase classname="tests.test_details" file="tests/test_details.py" line="20" name="test_ok2" time="0.2"/>
<testcase classname="tests.test_details.TestDetails" file="tests/test_details.py" line="87" name="test_mobility" time="0.3">
<failure message="KeyError: 'mobility_type'">def test_mobility(client):
&gt;       assert body["mobility_type"] == "WHEELCHAIR"
E       KeyError: 'mobility_type'

.venv/lib/python3.12/site-packages/x.py:5: in call
tests/test_details.py:88: KeyError</failure></testcase>
<testcase classname="tests.test_details" file="tests/test_details.py" line="99" name="test_fixture" time="0">
<error message="failed on setup with &quot;boom&quot;">conftest.py:12: RuntimeError</error></testcase>
<testcase classname="tests.test_details" file="tests/test_details.py" line="120" name="test_later" time="0">
<skipped message="later"/></testcase>
</testsuite></testsuites>
"""


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout.strip()


def project(backend: Path, name: str, deps: dict[str, str] | None = None, service: bool = True) -> Path:
    path = backend / name
    (path / "tests").mkdir(parents=True)
    lines = ["[tool.poetry.dependencies]"] + [f'{dep} = {{path = "{where}", develop = false}}'
                                              for dep, where in (deps or {}).items()]
    (path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    if service:
        (path / "main.py").write_text("app = None\n")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "pdms"
    backend = root / "backend"
    project(backend, "common/core", service=False)
    project(backend, "lead/lead-a", {"core": "../../common/core"})
    project(backend, "lead/lead-b")
    (backend / "lead" / "no-tests").mkdir(parents=True)
    (backend / "lead" / "no-tests" / "pyproject.toml").write_text("")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    return root


# ----------------------------------------------------------------------------------------- only local databases


def test_only_local_databases_are_offered_and_accepted() -> None:
    cfg = Config(dbs={"web-dev": WEB, "local": LOCAL, "same": LOCAL, "tests": OTHER,
                      "tunnel": Database(host="127.0.0.6", database="pdm")})
    assert testruns.local_dbs(cfg) == ["local", "tests"]  # "same" is the same database as "local"
    assert testruns.require_local(cfg, "local") is LOCAL
    with pytest.raises(actions.ActionError, match="shared"):
        testruns.require_local(cfg, "web-dev")
    with pytest.raises(actions.ActionError, match="shared"):
        testruns.require_local(cfg, "tunnel")
    with pytest.raises(actions.ActionError):
        testruns.require_local(cfg, "nope")


def test_the_run_gets_the_local_database_and_development_mode_off(monkeypatch) -> None:
    """DEVELOPMENT_MODE makes restapi_fastapi skip the token check: tests of unauthorized requests would get 200. It is
    set empty (not left out), so a service's .env cannot turn it on."""
    cfg = Config(dbs={"local": LOCAL})
    cfg.defaults.env = {"DB_PG_CONNECTION_STR": WEB.url(), "OTHER": "1"}
    monkeypatch.setenv("DEVELOPMENT_MODE", "true")
    monkeypatch.setenv("DB_PG_CONNECTION_STR", WEB.url())
    env = testruns.test_env(cfg, LOCAL)
    assert env["DB_PG_CONNECTION_STR"] == LOCAL.url()
    assert env["DEVELOPMENT_MODE"] == ""
    assert "OTHER" not in env and "LOGGING_LEVEL" not in env
    assert "PATH" in env
    with pytest.raises(actions.ActionError):
        testruns.test_env(cfg, WEB)


def test_pytest_writes_a_junit_report_unless_given_its_own(tmp_path) -> None:
    xml = tmp_path / "r.xml"
    cmd = testruns.pytest_command(xml, ["-k", "x"])
    assert cmd[1:] == ["run", "pytest", f"--junitxml={xml}", "-o", "junit_family=xunit1", "-k", "x"]
    assert f"--junitxml={xml}" not in testruns.pytest_command(xml, ["--junitxml=mine.xml"])


# ----------------------------------------------------------------------------------------- results


def test_a_report_gives_counts_and_failures_with_their_line(tmp_path) -> None:
    xml = tmp_path / "r.xml"
    xml.write_text(REPORT)
    report = testruns.parse(xml, tmp_path / "svc")
    assert {k: report[k] for k in ("tests", "passed", "failed", "errors", "skipped")} == {
        "tests": 5, "passed": 2, "failed": 1, "errors": 1, "skipped": 1}
    first, second = report["failures"]
    assert first["name"] == "tests/test_details.py::TestDetails::test_mobility"
    assert second["name"] == "tests/test_details.py::test_fixture"
    assert first["message"] == "KeyError: 'mobility_type'"
    assert (first["path"], first["line"]) == (str(tmp_path / "svc" / "tests" / "test_details.py"), 88)  # not .venv
    assert second["kind"] == "error"
    assert (second["path"], second["line"]) == (str(tmp_path / "svc" / "conftest.py"), 12)
    assert testruns.parse(tmp_path / "missing.xml", tmp_path) is None


def test_results_are_kept_per_project_and_say_how_it_went(repo) -> None:
    backend = repo / "backend"
    paths = testruns.files(backend, "lead/lead-a")
    paths["xml"].parent.mkdir(parents=True)
    paths["xml"].write_text(REPORT)
    kept = testruns.record(backend, "lead/lead-a", db="local", code=1, started=time.time(), seconds=1.23,
                           commit="abc", origin="cli")
    assert kept["outcome"] == "failed" and kept["seconds"] == 1.2 and kept["origin"] == "cli"
    assert testruns.record(backend, "lead/lead-b", db="local", code=4, started=0, seconds=0, commit="",
                           origin="ui")["outcome"] == "broken"  # pytest itself failed: no report
    assert testruns.record(backend, "common/core", db="local", code=testruns.NO_TESTS, started=0, seconds=0,
                           commit="", origin="ui")["outcome"] == "empty"
    assert set(testruns.results(backend)) == {"lead/lead-a", "lead/lead-b", "common/core"}
    other = repo.parent / "copy" / "backend"  # another checkout keeps its own
    assert testruns.results(other) == {}


# ----------------------------------------------------------------------------------------- affected


def test_projects_are_services_and_packages_with_tests(repo) -> None:
    backend = repo / "backend"
    assert testruns.projects(backend) == ["common/core", "lead/lead-a", "lead/lead-b"]
    assert testruns.kind(backend / "common/core") == "package"
    assert testruns.kind(backend / "lead/lead-a") == "service"


def test_affected_are_the_projects_the_branch_and_uncommitted_files_touch(repo) -> None:
    backend = repo / "backend"
    assert testruns.affected(backend) == {"branch": "main", "files": 0, "items": []}
    git(repo, "checkout", "-q", "-b", "feat/x")
    (backend / "common" / "core" / "money.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "core")
    found = testruns.affected(backend)
    assert found["branch"] == "main" and found["files"] == 1
    assert found["items"] == [{"project": "common/core", "why": ["own"]},
                              {"project": "lead/lead-a", "why": ["common/core"]}]
    (backend / "lead" / "lead-b" / "new.py").write_text("")  # untracked
    (repo / "README.md").write_text("outside the backend")
    assert [i["project"] for i in testruns.affected(backend)["items"]] == ["common/core", "lead/lead-a", "lead/lead-b"]


# ----------------------------------------------------------------------------------------- pdms ui runs


def fake_runs(monkeypatch, seconds: float = 0.2):
    """run_tests without poetry: records which database ran which project, and when."""
    seen: list[tuple[str, str, float, float]] = []
    modes: dict[str, bool] = {}

    def run_tests(cfg, backend, project, db, *, install=None, dev_mode=False, started=lambda proc: None,
                  cancelled=lambda: False):
        modes[project] = dev_mode
        begin = time.monotonic()
        while time.monotonic() - begin < seconds and not cancelled():
            time.sleep(0.01)
        seen.append((project, db, begin, time.monotonic()))
        return {}

    monkeypatch.setattr(testruns, "run_tests", run_tests)
    fake_runs.modes = modes  # project → dev_mode it ran with
    return seen


def test_each_local_database_runs_one_project_at_a_time(repo, monkeypatch) -> None:
    cfg = Config(dbs={"local": LOCAL, "tests": OTHER, "web-dev": WEB})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    seen = fake_runs(monkeypatch)
    tests = Runner()
    backend = repo / "backend"
    with pytest.raises(actions.ActionError, match="shared"):
        tests.run(cfg, backend, ["lead/lead-a"], ["local", "web-dev"])
    with pytest.raises(actions.ActionError, match="No local database"):
        tests.run(cfg, backend, ["lead/lead-a"], [])
    with pytest.raises(actions.ActionError, match="not a project"):
        tests.run(cfg, backend, ["../etc"], ["local"])
    started = tests.run(cfg, backend, ["common/core", "lead/lead-a", "lead/lead-b"], ["local", "tests"])
    assert started == {"queued": ["common/core", "lead/lead-a", "lead/lead-b"], "workers": 2}
    deadline = time.monotonic() + 5
    while len(seen) < 3 or tests.summary()["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert sorted(p for p, *_ in seen) == ["common/core", "lead/lead-a", "lead/lead-b"]
    for db in ("local", "tests"):  # never two at once on the same database
        spans = sorted((b, e) for _p, d, b, e in seen if d == db)
        assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    assert {d for _p, d, *_ in seen} == {"local", "tests"}
    assert tests.summary()["version"] == 3


def test_stop_empties_the_queue_and_cancels_what_runs(repo, monkeypatch) -> None:
    cfg = Config(dbs={"local": LOCAL})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    seen = fake_runs(monkeypatch, seconds=5)
    tests = Runner()
    tests.run(cfg, repo / "backend", ["lead/lead-a", "lead/lead-b"], ["local"])
    deadline = time.monotonic() + 5
    while not tests.summary()["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert tests.summary()["queued"] == ["lead/lead-b"]
    assert tests.stop() == ["lead/lead-a"]
    while tests.summary()["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert [p for p, *_ in seen] == ["lead/lead-a"] and seen[0][3] - seen[0][2] < 2


def test_a_real_run_keeps_the_result_and_its_log(repo, monkeypatch) -> None:
    """run_tests with a stand-in for poetry that writes the report pytest would write."""
    backend = repo / "backend"
    cfg = Config(dbs={"local": LOCAL})
    fake = repo / "fake_poetry.py"
    fake.write_text(
        "import os, sys\n"
        "xml = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--junitxml='))\n"
        f"open(xml, 'w').write({REPORT!r})\n"
        "print('DB', os.environ['DB_PG_CONNECTION_STR'])\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(runner, "poetry", lambda: str(fake))
    monkeypatch.setattr(testruns, "pytest_command", lambda xml, extra=None: [
        sys.executable, str(fake), "run", "pytest", f"--junitxml={xml}"])
    result = testruns.run_tests(cfg, backend, "lead/lead-b", "local", install=False)
    assert result["outcome"] == "failed" and result["failed"] == 1 and result["db"] == "local"
    log = testruns.files(backend, "lead/lead-b")["log"].read_text()
    assert f"DB {LOCAL.url()}" in log
    with pytest.raises(actions.ActionError, match="shared"):
        testruns.run_tests(Config(dbs={"web-dev": WEB}), backend, "lead/lead-b", "web-dev", install=False)


# ----------------------------------------------------------------------------------------- CLI and API


def test_pdms_test_refuses_a_shared_database_and_explains_without_a_local_one(repo, monkeypatch) -> None:
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms", dbs={"web-dev": WEB})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0))
    result = CliRunner().invoke(cli.app, ["test", "lead-b", "--db", "web-dev", "-n"])
    assert result.exit_code != 0 and "shared" in result.output
    result = CliRunner().invoke(cli.app, ["test", "lead-b", "-n"])
    assert result.exit_code != 0 and "No local database for tests" in result.output
    assert not calls  # pytest never ran


def test_pdms_test_runs_on_the_local_database_and_keeps_the_result(repo, monkeypatch) -> None:
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms", dbs={"web-dev": WEB, "local": LOCAL})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(runner, "ensure_poetry", lambda: None)
    ran = {}

    def run(cmd, cwd, env):
        ran.update(cmd=cmd, cwd=cwd, db=env["DB_PG_CONNECTION_STR"])
        Path(next(a for a in cmd if a.startswith("--junitxml=")).split("=", 1)[1]).write_text(REPORT)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(cli.app, ["test", "core", "-n", "--", "-x"])
    assert result.exit_code == 1, result.output
    assert ran["db"] == LOCAL.url() and Path(ran["cwd"]) == repo / "backend" / "common" / "core"
    assert ran["cmd"][-1] == "-x"
    kept = testruns.results(repo / "backend")["common/core"]
    assert kept["origin"] == "cli" and kept["db"] == "local" and kept["failed"] == 1


def test_the_tests_screen_gets_projects_results_affected_and_only_local_dbs(repo, monkeypatch) -> None:
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms", dbs={"web-dev": WEB, "local": LOCAL})
    info = ui_jobs.tests_info(cfg, Runner())
    assert [p["project"] for p in info["projects"]] == ["common/core", "lead/lead-a", "lead/lead-b"]
    assert info["projects"][0]["kind"] == "package" and info["projects"][0]["result"] is None
    assert [d["name"] for d in info["dbs"]] == ["local"]
    assert info["compose"] is False
    (repo / "backend" / testruns.TEST_DB_COMPOSE).write_text("services: {}\n")
    assert ui_jobs.tests_info(cfg, Runner())["compose"] is True


def test_the_test_database_of_the_compose_file_is_started_and_registered(repo, monkeypatch) -> None:
    backend = repo / "backend"
    cfg = Config(dbs={"web-dev": WEB})
    monkeypatch.setattr(cfg, "save", lambda: None)
    with pytest.raises(actions.ActionError, match="docker-compose_tests.yml"):
        testruns.start_test_db(cfg, backend)
    (backend / testruns.TEST_DB_COMPOSE).write_text("services: {}\n")
    monkeypatch.setattr(testruns.events, "docker_available", lambda: (True, "27"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(actions, "check_connection", lambda db, timeout: "PostgreSQL 18")
    assert testruns.start_test_db(cfg, backend) == "tests"
    assert calls[0][:3] == ["docker", "compose", "-f"] and calls[0][-1] == testruns.TEST_DB_SERVICE
    assert (cfg.dbs["tests"].port, cfg.dbs["tests"].database) == (5439, "alivi_pdms")
    assert testruns.start_test_db(cfg, backend) == "tests"  # registered once
    assert testruns.local_dbs(cfg) == ["tests"]


def test_runs_wait_for_their_turn_without_threads_left_behind(repo, monkeypatch) -> None:
    cfg = Config(dbs={"local": LOCAL})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    fake_runs(monkeypatch, seconds=0.3)
    tests = Runner()
    before = threading.active_count()
    tests.run(cfg, repo / "backend", ["lead/lead-a"], ["local"])
    tests.run(cfg, repo / "backend", ["lead/lead-a", "lead/lead-b"], ["local"])  # lead-a is not queued twice
    deadline = time.monotonic() + 5
    while tests.summary()["running"] or tests.summary()["queued"] or threading.active_count() > before:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert tests.summary()["version"] == 2


def test_development_mode_is_off_unless_asked_and_kept_with_the_result(repo, monkeypatch) -> None:
    cfg = Config(dbs={"local": LOCAL})
    assert testruns.test_env(cfg, LOCAL, dev_mode=True)["DEVELOPMENT_MODE"] == "true"
    backend = repo / "backend"
    (backend / "lead" / "lead-a" / ".env").write_text("DEVELOPMENT_MODE=True\n")
    assert testruns.env_dev_mode(backend / "lead" / "lead-a") and not testruns.env_dev_mode(backend / "lead" / "lead-b")
    kept = testruns.record(backend, "lead/lead-a", db="local", code=0, started=0, seconds=0, commit="", origin="ui",
                           dev_mode=True)
    assert kept["dev_mode"] is True
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    fake_runs(monkeypatch, seconds=0.05)
    tests = Runner()
    tests.run(cfg, backend, ["lead/lead-a"], ["local"], dev_mode=True)
    deadline = time.monotonic() + 5
    while tests.summary()["running"] or tests.summary()["queued"] or "lead/lead-a" not in fake_runs.modes:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert fake_runs.modes == {"lead/lead-a": True}


def test_pdms_test_asks_about_development_mode_only_when_the_env_turns_it_on(repo, monkeypatch) -> None:
    from pdms_cli.commands import testing

    service = repo / "backend" / "lead" / "lead-a"
    assert testing.ask_dev_mode(service, None) is False  # no .env
    assert testing.ask_dev_mode(service, True) is True
    (service / ".env").write_text("DEVELOPMENT_MODE=true\n")
    monkeypatch.setattr(testing, "interactive_terminal", lambda: False)
    assert testing.ask_dev_mode(service, None) is False  # no terminal to ask: off, as deployed
    asked = []
    monkeypatch.setattr(testing, "interactive_terminal", lambda: True)
    monkeypatch.setattr(testing.questionary, "confirm",
                        lambda text, default: asked.append(default) or type("Q", (), {"unsafe_ask": lambda self: True})())
    assert testing.ask_dev_mode(service, None) is True and asked == [False]
    assert testing.ask_dev_mode(service, False) is False and len(asked) == 1  # the flag answers it
