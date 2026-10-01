"""What ``pdms ui`` does to services: the same actions as the CLI, run in a thread so the page stays live.

A job belongs to one instance (or the proxy) and shows in the state while it runs, with its phase; a failed one
stays there with its error until the next action on that instance or until it is dismissed. Everything that needs
an answer (a protected database, the local ElasticMQ) is checked before the job starts, so the page can ask first.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import actions, events, instances, proxy
from ..config import Config
from ..i18n import _


def install_log(key: str) -> Path:
    """Where the install a restart runs writes its output (the service's own log is rotated when it starts)."""
    return instances.log_path(key).with_suffix(".install.log")


@dataclass
class Job:
    key: str
    action: str  # stop | restart
    phase: str  # stopping | installing | starting
    started: float
    error: str = ""
    installed: bool = False  # the install log belongs to this job

    @property
    def running(self) -> bool:
        return not self.error


class Jobs:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

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

    def run(self, key: str, action: str, phase: str, work: Callable[[Job], object]) -> Job:
        """Start ``work`` in a thread; :class:`actions.ActionError` if something is already running on ``key``."""
        with self._lock:
            current = self._jobs.get(key)
            if current and current.running:
                raise actions.ActionError(_("{key} is busy ({phase}).", key=key, phase=current.phase))
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

    # ------------------------------------------------------------------ services

    def stop(self, key: str) -> Job:
        if proxy.is_key(key):
            running = proxy.running_proxy()
            if not running:
                raise actions.ActionError(_("The proxy is not running."))
            return self.run(proxy.display_key(running), "stop", "stopping", lambda _job: actions.stop_proxy(running))
        inst = find(key)
        return self.run(inst.key, "stop", "stopping", lambda _job: actions.stop_service(inst))

    def restart(
        self, key: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job:
        """Restart on the same port; raises the decisions before stopping anything, like ``pdms restart``."""
        inst = find(key)
        cfg = Config.load()
        user, db = user or inst.user, db or inst.db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        if db != inst.db:
            actions.check_database(cfg, db, confirmed)
        if (inst.events == "local" or inst.is_consumer) and not events.running(cfg.defaults.events_port):
            raise actions.LocalEventsDown(cfg.defaults.events_port)
        if self.busy(inst.key):
            raise actions.ActionError(_("{key} is busy.", key=inst.key))
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            def install_step(service: Path) -> None:
                if not actions.needs_install(cfg, service, install):
                    return
                log = install_log(job.key)
                log.parent.mkdir(parents=True, exist_ok=True)
                self.phase(job, "installing", installed=True)
                with open(log, "w", encoding="utf-8", errors="replace") as output:
                    actions.install_service(service, output)
                self.phase(job, "starting")

            actions.restart_service(cfg, inst, user_name=user, db_name=db, confirmed=True, install=install_step)

        return self.run(inst.key, "restart", "stopping", work)


def find(key: str) -> instances.Instance:
    inst = instances.load().get(key)
    if not inst:
        raise actions.ActionError(_("There is no instance {key}.", key=key))
    return inst


def forget(key: str) -> None:
    """Forget a stopped instance (its log stays, like ``pdms ps --clean``)."""
    inst = find(key)
    if inst.alive():
        raise actions.ActionError(_("{key} is running; stop it first.", key=key))
    instances.forget(key)


def forget_stopped() -> list[str]:
    stopped = [i.key for i in instances.load().values() if not i.alive()]
    for key in stopped:
        instances.forget(key)
    return stopped
