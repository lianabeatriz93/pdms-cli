"""The Tests screen: runs the tests of several projects in threads, one at a time per local database (services share
tables, so two runs on the same database would step on each other), and keeps what runs and what waits.

The results themselves live in files (:mod:`pdms_cli.testruns`), so ``pdms test`` runs from the CLI show too.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import actions, instances, testruns
from ..config import Config
from ..i18n import _


class Tests:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._running: dict[str, dict] = {}  # project → {"db", "started", "phase"}
        self._procs: dict[str, subprocess.Popen] = {}
        self._workers: set[str] = set()  # databases with a worker thread
        self._cancelled: set[str] = set()  # stopped runs: no result is kept for them
        self._dev_mode: dict[str, bool] = {}  # project → run with DEVELOPMENT_MODE on
        self._backend: Path | None = None
        self._failing = 0
        self._version = 0
        self.error = ""

    def summary(self) -> dict:
        """What the state carries: what runs, what waits, how many projects fail, a version that changes with every
        new result (the screen reloads the results when it moves)."""
        with self._lock:
            return {
                "running": [{"project": p, **info} for p, info in self._running.items()],
                "queued": list(self._queue),
                "failing": self._failing,
                "version": self._version,
                "error": self.error,
            }

    def count_failing(self, backend: Path | None) -> int:
        failing = sum(1 for r in testruns.results(backend).values() if r.get("outcome") in ("failed", "broken")) \
            if backend else 0
        with self._lock:
            changed = failing != self._failing
            self._failing = failing
        if changed:
            self.on_change()
        return failing

    def run(self, cfg: Config, backend: Path, projects: list[str], dbs: list[str], dev_mode: bool = False) -> dict:
        """Queue ``projects`` and start one worker per database of ``dbs`` (pdms's test databases only) that has none.
        ``dev_mode`` runs them with DEVELOPMENT_MODE on (by default it is off, whatever their .env says)."""
        if not dbs:
            raise actions.ActionError(testruns.no_test_db_hint())
        seen_dbs = []
        for name in dbs:
            testruns.require_test_db(name)
            if name not in seen_dbs:
                seen_dbs.append(name)
        known = set(testruns.projects(backend))
        unknown = [p for p in projects if p not in known]
        if unknown:
            raise actions.ActionError(_("'{name}' is not a project of {path}.", name=unknown[0], path=backend))
        with self._lock:
            if self._backend and self._backend != backend and (self._running or self._queue):
                raise actions.ActionError(_("Tests of another repo are running; stop them first."))
            self._backend = backend
            added = [p for p in projects if p not in self._queue and p not in self._running]
            self._queue.extend(added)
            self._dev_mode.update({project: dev_mode for project in added})
            self.error = ""
            start = [db for db in seen_dbs if db not in self._workers][:max(0, len(self._queue))]
            self._workers.update(start)
        for db in start:
            threading.Thread(target=self._work, args=(db,), name=f"pdms-ui-tests-{db}", daemon=True).start()
        self.on_change()
        return {"queued": added, "workers": len(start)}

    def stop(self) -> list[str]:
        """Empty the queue and stop the runs; their results stay as they were."""
        with self._lock:
            self._queue.clear()
            self._dev_mode.clear()
            procs = list(self._procs.items())
            self._cancelled.update(self._running)  # also one still installing: it will not start pytest
            stopped = sorted(self._cancelled)
        for _project, proc in procs:
            instances.kill_tree(proc.pid, timeout=5)
        self.on_change()
        return stopped

    def _next(self, db: str) -> str | None:
        with self._lock:
            if not self._queue:
                self._workers.discard(db)
                return None
            project = self._queue.pop(0)
            self._running[project] = {"db": db, "started": time.time(), "dev_mode": self._dev_mode.pop(project, False)}
            return project

    def _work(self, db: str) -> None:
        while (project := self._next(db)) is not None:
            self.on_change()
            backend = self._backend
            assert backend is not None  # set by run() before any worker starts
            with self._lock:
                dev_mode = self._running[project]["dev_mode"]
            try:
                testruns.run_tests(Config.load(), backend, project, db, dev_mode=dev_mode,
                                   started=lambda proc, p=project: self._started(p, proc),
                                   cancelled=lambda p=project: self._was_cancelled(p))
            except actions.ActionError as exc:
                with self._lock:
                    self.error = f"{project}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - shown on the page instead of lost in a thread
                with self._lock:
                    self.error = f"{project}: {type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._running.pop(project, None)
                    self._procs.pop(project, None)
                    self._cancelled.discard(project)
                    self._version += 1
                self.count_failing(backend)
                self.on_change()

    def _started(self, project: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[project] = proc

    def _was_cancelled(self, project: str) -> bool:
        with self._lock:
            return project in self._cancelled
