"""Services running outside pdms: found, adopted again and stopped, with real processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from pdms_cli import actions, instances
from pdms_cli.config import Config, Database, DevUser, Repo

WINDOWS = sys.platform == "win32"
# Stands in for uvicorn: it only waits, under the name pdms looks for.
FAKE_SERVER = "import time\nprint('Application startup complete.', flush=True)\ntime.sleep(60)\n"
# Starts the fake server detached, as pdms does, and exits at once: like a pdms whose registry lost the instance.
LAUNCHER = """
import subprocess, sys
log, cwd, *cmd = sys.argv[1:]
options = {"creationflags": 0x00000200 | 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
out = open(log, "w")
print(subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, **options).pid)
"""


@pytest.fixture
def repo(tmp_path) -> Path:
    service = tmp_path / "pdms" / "backend" / "lead" / "lead-tp-list"
    service.mkdir(parents=True)
    (service / "main.py").write_text("app = None\n")
    (service / "pyproject.toml").write_text("[tool.poetry]\nname = 'lead-tp-list'\n")
    (service / "uvicorn").write_text(FAKE_SERVER)
    return tmp_path / "pdms"


@pytest.fixture
def cfg(repo, monkeypatch) -> Config:
    monkeypatch.setattr(Config, "save", lambda self: None)
    return Config(
        users={"supervisor": DevUser("u1", "s@x.com"), "agent": DevUser("u2", "a@x.com")},
        dbs={"local": Database("localhost", user="pdm", password="new-one")},
        repos={"pdms": Repo(path=str(repo))},
    )


def service_of(repo: Path) -> Path:
    return repo / "backend" / "lead" / "lead-tp-list"


def wait_for(condition, what: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := condition():
            return found
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def orphan(repo):
    """A fake service on port 28155 whose parent already exited, writing to its pdms log."""
    service = service_of(repo)
    log = instances.log_path("lead-tp-list@28155")
    log.parent.mkdir(parents=True, exist_ok=True)
    launcher = service.parent / "launcher.py"
    launcher.write_text(LAUNCHER)
    env = {**os.environ, "DEV_USER_ID": "u2", "DEV_USERNAME": "a@x.com",
           "DB_PG_CONNECTION_STR": "postgresql+psycopg2://pdm:old-one@localhost:5432/pdm"}
    out = subprocess.run([sys.executable, str(launcher), str(log), str(service), sys.executable, str(service / "uvicorn"), "main:app",
                          "--reload", "--host", "0.0.0.0", "--port", "28155"], capture_output=True, text=True,
                         env=env, check=True)
    pid = int(out.stdout)
    try:
        yield pid
    finally:
        instances.kill_tree(pid, timeout=5)


def test_launch_of_reads_what_pdms_starts() -> None:
    assert instances.launch_of(["/v/bin/python", "/v/bin/uvicorn", "main:app", "--reload", "--host", "0.0.0.0",
                                "--port", "28101"]) == ("0.0.0.0", 28101, True, "")
    assert instances.launch_of([r"C:\v\Scripts\uvicorn.exe", "main:app", "--port=28102"]) == ("127.0.0.1", 28102, False, "")
    assert instances.launch_of(["python", "/x/sqs_patch/pdms_sqs_poller.py", "--queue-url",
                                "http://localhost:9324/000000000000/broker-sqs-queue.fifo"]) == ("", 0, False,
                                                                                               "broker-sqs-queue.fifo")
    assert instances.launch_of(["python", "-m", "http.server"]) is None
    assert instances.launch_of(["/v/bin/uvicorn", "other:app"]) is None


def test_a_service_whose_pdms_exited_is_found_with_its_user_db_and_log(cfg, repo, orphan) -> None:
    found = wait_for(lambda: actions.strays(cfg), "the orphan")
    [stray] = found
    assert (stray.key, stray.process.pid, stray.process.port, stray.process.host, stray.process.reload) == (
        "lead-tp-list@28155", orphan, 28155, "0.0.0.0", True)
    assert Path(stray.process.service) == service_of(repo).resolve()
    # Matched by DEV_USER_ID and by the database without its password (it changed in the config since).
    assert (stray.user, stray.db, stray.repo) == ("agent", "local", "pdms")
    if not WINDOWS:
        assert stray.process.log == str(instances.log_path("lead-tp-list@28155"))


def test_a_service_of_an_unregistered_repo_is_left_alone(cfg, orphan) -> None:
    cfg.repos = {}
    wait_for(lambda: psutil.pid_exists(orphan), "the orphan")
    assert actions.strays(cfg) == []


def test_a_service_whose_parent_runs_is_not_a_stray(cfg, repo) -> None:
    """A foreground pdms run, VS Code's debugger...: something still holds it."""
    service = service_of(repo)
    proc = subprocess.Popen([sys.executable, str(service / "uvicorn"), "main:app", "--port", "28156"], cwd=service)
    try:
        wait_for(lambda: psutil.pid_exists(proc.pid), "the child")
        assert actions.strays(cfg) == []
    finally:
        proc.kill()
        proc.wait()


def test_adopting_registers_it_again(cfg, orphan) -> None:
    wait_for(lambda: actions.strays(cfg), "the orphan")
    [inst] = actions.adopt_strays(cfg)
    assert (inst.key, inst.pid, inst.port, inst.user, inst.db) == ("lead-tp-list@28155", orphan, 28155, "agent", "local")
    assert inst.alive() and list(instances.load()) == ["lead-tp-list@28155"]
    assert actions.strays(cfg) == []  # pdms knows it now
    if not WINDOWS:
        assert "Application startup complete" in instances.tail(inst.log)


def test_adopting_one_without_a_log_writes_where_its_output_went(cfg, orphan) -> None:
    [stray] = wait_for(lambda: actions.strays(cfg), "the orphan")
    stray.process.log = ""
    inst = instances.adopt(stray.process, user="", db="")
    if WINDOWS:  # the file it writes cannot be moved away: that is its log
        assert inst.log == str(instances.log_path("lead-tp-list@28155"))
    else:
        assert "pdms restart lead-tp-list@28155" in Path(inst.log).read_text(encoding="utf-8")


def test_adopting_on_windows_keeps_the_log_the_service_holds_open(cfg, orphan, monkeypatch) -> None:
    """Simulated on every system: the log cannot be rotated because the service still writes it."""
    [stray] = wait_for(lambda: actions.strays(cfg), "the orphan")
    stray.process.log = ""

    def in_use(log):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(instances, "rotate_log", in_use)
    inst = instances.adopt(stray.process, user="", db="")
    assert inst.log == str(instances.log_path("lead-tp-list@28155"))


def test_stopping_them_ends_the_process(cfg, orphan) -> None:
    wait_for(lambda: actions.strays(cfg), "the orphan")
    assert actions.stop_strays(cfg, ["lead-tp-list@28155"]) == ["lead-tp-list@28155"]
    wait_for(lambda: not instances.process_alive(orphan), "the process to stop")


def test_unknown_keys_are_refused(cfg) -> None:
    with pytest.raises(actions.ActionError):
        actions.adopt_strays(cfg, ["nothing@1"])
