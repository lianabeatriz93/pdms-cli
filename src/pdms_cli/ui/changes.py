"""What changed in the code while pdms ui runs: the running services that run old code (an installed part changed
since they started: they need a reinstall and a restart), the services of Home's setup that will install on their
next start, and the commits since the running services started.

It runs in a thread every :data:`EVERY` seconds: fingerprinting the source trees takes about a second.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .. import installer, instances, repos, runner
from ..config import Config

EVERY = 30
MAX_COMMITS = 8


def commits(root: Path, since_commit: str = "", since_time: str = "") -> list[dict]:
    """The commits of ``root`` after ``since_commit`` (or after ``since_time``), newest first."""
    if not since_commit and not since_time:
        return []
    cmd = ["git", "log", f"-n{MAX_COMMITS}", "--format=%h%x09%s"]
    cmd += [f"{since_commit}..HEAD"] if since_commit else [f"--since={since_time}"]
    try:
        out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [{"commit": line.split("\t", 1)[0], "subject": line.split("\t", 1)[1]}
            for line in out.splitlines() if "\t" in line]


def look(cfg: Config) -> dict:
    """What changed now (see the module's docstring)."""
    root, backend = repos.active_root(cfg), repos.active_backend(cfg)
    if not root or not backend:
        return {"stale": [], "pending": [], "commits": [], "head": ""}
    backend = backend.resolve()
    running = [i for i in instances.load().values() if i.alive() and Path(i.service).resolve().is_relative_to(backend)]
    stale = []
    for inst in running:
        parts = installer.changed_parts(inst.deps, Path(inst.service)) if inst.deps else None
        if parts:
            stale.append({"key": inst.key, "service": Path(inst.service).resolve().relative_to(backend).as_posix(),
                          "parts": parts})
    pending = []
    stack = cfg.stacks.get(cfg.setup.stack)
    live = {Path(i.service).resolve() for i in running}
    for svc in stack.services if stack else []:
        path = (backend / svc).resolve()
        if path not in live and runner.is_service(path) and not installer.is_up_to_date(path):
            pending.append({"service": svc})
    started = [i for i in running if i.commit]
    head = repos.git_commit(root)
    if started:
        oldest = min(started, key=lambda i: i.started_at)
        found = commits(root, since_commit=oldest.commit) if oldest.commit != head else []
    else:
        oldest_time = min((i.started_at for i in running), default="")
        found = commits(root, since_time=oldest_time)
    return {"stale": stale, "pending": pending, "commits": found, "head": head[:12]}


class Changes:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._lock = threading.Lock()
        self._running = False
        self._latest = {"stale": [], "pending": [], "commits": [], "head": "", "at": ""}

    def run(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._work, name="pdms-ui-changes", daemon=True).start()
        return True

    def _work(self) -> None:
        try:
            found = look(Config.load())
        except Exception:  # noqa: BLE001 - a broken checkout must not stop the monitor; next time it may work
            found = None
        with self._lock:
            self._running = False
            if found is None:
                return
            changed = {k: v for k, v in self._latest.items() if k != "at"} != found
            self._latest = {**found, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if changed:
            self.on_change()

    def summary(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def watch(self, stopped: threading.Event) -> None:
        while not stopped.is_set():
            self.run()
            stopped.wait(EVERY)
