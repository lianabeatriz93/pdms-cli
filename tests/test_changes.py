"""What changed since the running services started: the commit each instance keeps, reading it from .git, and
pdms ui's monitor (old code, services that install on their next start, the commits since)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pdms_cli import actions, instances, repos
from pdms_cli.config import Config, Repo, Setup, Stack
from pdms_cli.instances import Instance
from pdms_cli.ui import changes


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "pdms"
    for svc in ("lead/a", "lead/b", "lead/c"):
        (root / "backend" / svc).mkdir(parents=True)
        (root / "backend" / svc / "pyproject.toml").write_text("[tool.poetry]\n")
        (root / "backend" / svc / "main.py").write_text("app = None\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    return root


def test_the_commit_is_read_from_git_like_git_does(repo) -> None:
    assert repos.git_commit(repo) == git(repo, "rev-parse", "HEAD")
    git(repo, "pack-refs", "--all")  # branches moved to packed-refs
    assert repos.git_commit(repo) == git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    assert repos.git_commit(repo) == git(repo, "rev-parse", "HEAD")
    assert repos.git_commit(repo / "nowhere") == ""


def test_an_instance_keeps_the_commit_it_started_from(repo, monkeypatch) -> None:
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms")
    assert actions.commit_of(cfg, repo / "backend" / "lead" / "a") == git(repo, "rev-parse", "HEAD")
    assert actions.commit_of(cfg, Path("/elsewhere/svc")) == ""
    older = {"key": "a@1", "pid": 1, "service": "x", "host": "", "port": 1, "user": "u", "db": "d", "reload": False,
             "log": "", "started_at": ""}
    assert Instance(**older).commit == ""  # instances.json written by an older pdms


def test_the_monitor_finds_old_code_pending_installs_and_commits(repo, monkeypatch) -> None:
    backend = repo / "backend"
    first = git(repo, "rev-parse", "HEAD")
    cfg = Config(repos={"pdms": Repo(path=str(repo))}, current_repo="pdms",
                 stacks={"s": Stack(services=["lead/a", "lead/b", "lead/c"])}, setup=Setup(stack="s"))
    running = {
        "a@1": Instance(key="a@1", pid=1, service=str(backend / "lead/a"), host="", port=1, user="u", db="d",
                        reload=False, log="", started_at="2026-10-03T10:00:00", deps={"lead-a": "old"}, commit=first),
        "b@2": Instance(key="b@2", pid=1, service=str(backend / "lead/b"), host="", port=2, user="u", db="d",
                        reload=False, log="", started_at="2026-10-03T10:05:00", deps={"lead-b": "same"}, commit=first),
    }
    monkeypatch.setattr(instances, "load", lambda: running)
    monkeypatch.setattr(Instance, "alive", lambda self: True)
    monkeypatch.setattr(changes.installer, "changed_parts",
                        lambda deps, service: ["core"] if service.name == "a" else [])
    monkeypatch.setattr(changes.installer, "is_up_to_date", lambda service: False)
    (repo / "README.md").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "fix(core): pagination keeps the filters")

    found = changes.look(cfg)
    assert found["stale"] == [{"key": "a@1", "service": "lead/a", "parts": ["core"]}]
    assert found["pending"] == [{"service": "lead/c"}]  # b runs; c does not and installs on its next start
    assert [c["subject"] for c in found["commits"]] == ["fix(core): pagination keeps the filters"]
    assert found["head"] == git(repo, "rev-parse", "HEAD")[:12]


def test_the_monitor_tells_the_page_only_when_something_changed(monkeypatch) -> None:
    told = []
    monitor = changes.Changes(lambda: told.append(1))
    result = {"stale": [], "pending": [], "commits": [], "head": "abc"}
    monkeypatch.setattr(changes, "look", lambda cfg: dict(result))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config()))
    monitor._running = True
    monitor._work()
    monitor._running = True
    monitor._work()
    assert len(told) == 1 and monitor.summary()["head"] == "abc"
