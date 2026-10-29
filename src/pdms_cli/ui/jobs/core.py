"""Jobs and their keys: what runs in a thread, its phase, its error."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ... import actions
from ...i18n import _
from ..aws import Aws
from ..changes import Changes
from ..doctor import Doctor
from ..health import Health
from ..testing import Tests

__all__ = [
    "DATA_KEY", "EVENTS_KEY", "HOME_KEY", "IMAGES_KEY", "Job", "JobsCore", "LeftRunning", "REPO_KEY",
    "SWITCH_CHOICES", "TESTS_DB_KEY", "TEST_DBS_KEY", "install_log",
]


EVENTS_KEY = "events:elasticmq"  # the job of pdms events up/down (not an instance: no row of its own in Services)


HOME_KEY = "home"  # the job of Home's Start everything / Stop everything


REPO_KEY = "repo"  # the job of switching the current repo (stopping or moving what ran from the old one)


TESTS_DB_KEY = "tests:db"  # the job of starting pdms's Postgres and creating its test databases


IMAGES_KEY = "images"  # the job of downloading Docker images (asked first: hundreds of MB)


DATA_KEY = "data:postgres"  # the jobs of the Data screen on pdms's Postgres (start, stop, refresh the local copy)


TEST_DBS_KEY = "data:test-dbs"  # preparing the test databases from the local copy, or migrating them


SWITCH_CHOICES = ("keep", "stop", "move")


class LeftRunning(actions.Decision):
    """Switching repos leaves instances of the old one running: keep, stop or move them (``running``)."""

    def __init__(self, switch: actions.RepoSwitch) -> None:
        super().__init__(f"{len(switch.running)} instances run from {switch.old}")
        self.switch = switch


install_log = actions.install_log


@dataclass
class Job:
    key: str
    action: str  # start | stop | restart | up | down
    phase: str  # stopping | installing | starting, with the service for a stack ("installing lead-tp-list")
    started: float
    error: str = ""
    installed: bool = False  # the install log belongs to this job
    log_key: str = ""  # the instance whose install log it wrote last (a stack installs several)

    @property
    def running(self) -> bool:
        return not self.error


class JobsCore:
    """What every kind of job needs: running one, its phase and error."""

    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.doctor = Doctor(on_change)
        self.health = Health(on_change)
        self.aws = Aws(on_change)
        self.changes = Changes(on_change)
        self.tests = Tests(on_change)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {key: asdict(job) for key, job in self._jobs.items()}

    def busy(self, key: str) -> bool:
        with self._lock:
            job = self._jobs.get(key)
            return bool(job and job.running)

    def dismiss(self, key: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job and not job.running:
                del self._jobs[key]
        self.on_change()

    def phase(self, job: Job, phase: str, **changes: object) -> None:
        with self._lock:
            job.phase = phase
            for name, value in changes.items():
                setattr(job, name, value)
        self.on_change()

    def run(self, key: str, action: str, phase: str, work: Callable[[Job], object], *, once: str = "") -> Job:
        """Start ``work`` in a thread; :class:`actions.ActionError` if something is already running on ``key``, or
        (``once``: a service's name) a start of that service on any port: it would run twice."""
        with self._lock:
            current = self._jobs.get(key)
            if current and current.running:
                raise actions.ActionError(_("{key} is busy ({phase}).", key=key, phase=current.phase))
            starting = next((other for other, job in self._jobs.items() if once and job.running
                             and job.action == "start" and other.rsplit("@", 1)[0] == once), "")
            if starting:
                raise actions.ActionError(_("{name} is already starting ({key}).", name=once, key=starting))
            job = self._jobs[key] = Job(key, action, phase, time.time())
        threading.Thread(target=self._work, args=(job, work), name=f"pdms-ui-{action}", daemon=True).start()
        self.on_change()
        return job

    def _work(self, job: Job, work: Callable[[Job], object]) -> None:
        try:
            work(job)
        except actions.ActionError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 - shown on the page instead of lost in a thread
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = ""
        with self._lock:
            if error:
                job.error = error
            elif self._jobs.get(job.key) is job:
                del self._jobs[job.key]  # done: the instance's own health takes over
        self.on_change()

    def failed(self, key: str, action: str, error: str, log_key: str = "") -> None:
        """Show a failure that happened outside a job (found when pdms ui starts)."""
        with self._lock:
            self._jobs[key] = Job(key, action, "", time.time(), error=error, log_key=log_key)
        self.on_change()
