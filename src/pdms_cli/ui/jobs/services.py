"""Starting, restarting, stopping and debugging services, and the ones outside pdms."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from ... import actions, events, frontend, instances, proxy, repos, runner
from ...config import Config
from ...i18n import _
from .. import state as ui_state
from . import core, repo_info
from .core import Job, JobsCore

__all__ = ["ServiceJobs", "adopt_strays", "find", "forget", "forget_stopped", "stop_strays", "stray_keys"]


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


def stray_keys(body: dict) -> list[str] | None:
    keys = body.get("keys")
    if keys is None:
        return None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise actions.ActionError("keys must be a list of instance keys (or missing for every one)")
    return keys


def adopt_strays(body: dict) -> dict:
    try:
        adopted = actions.adopt_strays(Config.load(), stray_keys(body))
    finally:
        ui_state.forget_strays()
    return {"adopted": [inst.key for inst in adopted]}


def stop_strays(body: dict) -> dict:
    try:
        stopped = actions.stop_strays(Config.load(), stray_keys(body))
    finally:
        ui_state.forget_strays()
    return {"stopped": stopped}


def forget_stopped() -> list[str]:
    stopped = [i.key for i in instances.load().values() if not i.present()]  # asleep ones are still there
    for key in stopped:
        instances.forget(key)
    return stopped


class ServiceJobs(JobsCore):
    def stop(self, key: str) -> Job:
        if frontend.is_key(key):
            current = frontend.running()
            if not current:
                raise actions.ActionError(_("The frontend is not running."))
            return self.run(frontend.KEY, "stop", "stopping", lambda _job: actions.stop_frontend(current))
        if proxy.is_key(key):
            running = proxy.running_proxy()
            if not running:
                raise actions.ActionError(_("The proxy is not running."))
            return self.run(proxy.display_key(running), "stop", "stopping", lambda _job: actions.stop_proxy(running))
        inst = find(key)
        return self.run(inst.key, "stop", "stopping", lambda _job: actions.stop_service(inst))

    def on_demand(self, key: str, on: bool) -> Job | None:
        """Make an instance on demand or always on, right away; a job only when an asleep one has to start."""
        inst = find(key)
        if self.busy(inst.key):
            raise actions.ActionError(_("{key} is busy.", key=inst.key))
        cfg = Config.load()
        if on or not inst.asleep:
            actions.set_on_demand(cfg, inst, on)
            self.on_change()
            return None
        return self.run(inst.key, "start", "starting", lambda _job: actions.set_on_demand(cfg, inst, on))

    def sleep(self, key: str) -> Job:
        """Stop a running instance now and keep it asleep until the proxy gets a request for it."""
        inst = find(key)
        if self.busy(inst.key):
            raise actions.ActionError(_("{key} is busy.", key=inst.key))
        if not inst.alive():
            raise actions.ActionError(_("{key} is not running.", key=inst.key))
        return self.run(inst.key, "stop", "stopping", lambda _job: actions.sleep_service(inst))

    def restart(
        self, key: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, python: bool | None = None, migrate: bool | None = None, views: bool | None = None,
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
        recreate = actions.check_pythons(cfg, [Path(inst.service)], install, python)
        behind = actions.check_migrations(cfg, [db], migrate)
        needs = actions.check_test_views(cfg, [(Path(inst.service), db)], views)
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.migrate_first(job, behind)
            self.views_first(cfg, job, needs)

            def install_step(service: Path) -> None:
                self.install(cfg, job, service, inst.key, install, recreate=recreate)
                self.phase(job, "starting")

            actions.restart_service(cfg, inst, user_name=user, db_name=db, confirmed=True, install=install_step)

        return self.run(inst.key, "restart", "stopping", work)

    def restart_many(
        self, keys: list[str], *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, stack: str | None = None, remember: bool = False, python: bool | None = None,
        migrate: bool | None = None, views: bool | None = None,
    ) -> list[Job]:
        """Restart several instances on their ports, one after the other (each its own job, waiting its turn).

        Without ``user``/``db`` each keeps its own. Everything is checked before anything stops: unknown or busy
        instances, a protected database, the local ElasticMQ. With ``stack`` and ``remember`` the stack keeps the new
        user and database for these services (see :func:`actions.remember_in_stack`)."""
        if not keys:
            raise actions.ActionError(_("Pick the services to restart."))
        found = [find(key) for key in dict.fromkeys(keys)]
        cfg = Config.load()
        if user:
            actions.require(cfg.users, _("user"), user)
        if db:
            actions.require(cfg.dbs, _("database"), db)
            if any(db != inst.db for inst in found):
                actions.check_database(cfg, db, confirmed)
        needs_events = any(inst.events == "local" or inst.is_consumer for inst in found)
        if needs_events and not events.running(cfg.defaults.events_port):
            raise actions.LocalEventsDown(cfg.defaults.events_port)
        busy = [inst.key for inst in found if self.busy(inst.key)]
        if busy:
            raise actions.ActionError(_("{key} is busy.", key=", ".join(busy)))
        recreate = actions.check_pythons(cfg, [Path(inst.service) for inst in found], install, python)
        behind = actions.check_migrations(cfg, [db or inst.db for inst in found], migrate)
        needs = actions.check_test_views(cfg, [(Path(inst.service), db or inst.db) for inst in found], views)
        prepared: list[bool] = []  # the test views, by the first restart
        if remember:
            if not stack or not (user or db):
                raise actions.ActionError("remember needs a stack and a user or a database")
            root = repos.active_backend(cfg)
            for inst in found:
                actions.remember_in_stack(cfg, stack, root, [Path(inst.service)], user or inst.user, db or inst.db)
        if user or db:
            actions.remember_profile(cfg, user or found[0].user, db or found[0].db)
        turn = threading.Lock()  # one at a time: installs and starts do not compete
        migrated: set[str] = set()  # each database once, by the first restart that uses it

        def restart_one(inst: instances.Instance) -> Callable[[Job], None]:
            def work(job: Job) -> None:
                with turn:
                    due = [item for item in behind if item.alias == (db or inst.db) and item.alias not in migrated]
                    migrated.update(item.alias for item in due)
                    self.migrate_first(job, due)
                    if not prepared:
                        prepared.append(True)
                        self.views_first(cfg, job, needs)
                    self.phase(job, "stopping")

                    def install_step(service: Path) -> None:
                        self.install(cfg, job, service, inst.key, install, recreate=recreate)
                        self.phase(job, "starting")

                    actions.restart_service(cfg, inst, user_name=user or inst.user, db_name=db or inst.db,
                                            confirmed=True, install=install_step)
            return work

        return [self.run(inst.key, "restart", "waiting", restart_one(inst)) for inst in found]

    def debug(self, key: str) -> dict:
        """Hand the instance over to VS Code (see :func:`actions.debug_instance`); quick, so not a job."""
        inst = find(key)
        if self.busy(inst.key):
            raise actions.ActionError(_("{key} is busy.", key=inst.key))
        setup = actions.debug_instance(Config.load(), inst)
        return {"name": setup.name, "launch": str(setup.launch_json), "backup": str(setup.backup or "")}

    def start(
        self, service: str, *, port: int | None = None, user: str | None = None, db: str | None = None,
        install: bool | None = None, confirmed: bool = False, on_demand: bool | None = None, python: bool | None = None,
        migrate: bool | None = None, views: bool | None = None,
    ) -> Job:
        """Start one service of the current repo in the background, like ``pdms run -b``. Without ``port`` it takes
        the next free one; a busy ``port``, a protected database or a stopped local ElasticMQ (for a consumer) are
        raised before anything starts."""
        cfg = Config.load()
        root, known = repo_info.repo_services(cfg)
        if service not in known:
            raise actions.ActionError(_("Not services of the current repo: {names}", names=service))
        path = root / service
        user, db = user or cfg.last_user, db or cfg.last_db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        launch = actions.plan_service(
            cfg, path, user_name=user, db_name=db, port=port or actions.suggested_port(cfg, cfg.defaults.host),
            on_demand=on_demand,
        )
        actions.check_database(cfg, db, confirmed)
        key = instances.make_key(launch.service, launch.port)
        if self.busy(key):
            raise actions.ActionError(_("{key} is busy.", key=key))
        recreate = actions.check_pythons(cfg, [launch.service], install, python)
        behind = actions.check_migrations(cfg, [db], migrate)
        needs = actions.check_test_views(cfg, [(launch.service, db)], views)
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.migrate_first(job, behind)
            self.views_first(cfg, job, needs)
            self.install(cfg, job, launch.service, key, install, recreate=recreate)
            self.phase(job, "starting")
            actions.start_service(cfg, launch)

        return self.run(key, "start", "starting", work, once=launch.service.name)

    def start_many(
        self, services: list[str], *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, on_demand: bool | None = None, python: bool | None = None, migrate: bool | None = None,
        views: bool | None = None,
    ) -> list[Job]:
        """Start several services of the current repo with one user and database, each on its own free port and one
        after the other (each its own job, waiting its turn). Everything is checked once, before anything starts:
        unknown or busy services, a protected database, their Python, the migrations and the test views."""
        if not services:
            raise actions.ActionError(_("Pick the services to start."))
        cfg = Config.load()
        root, known = repo_info.repo_services(cfg)
        unknown = [service for service in services if service not in known]
        if unknown:
            raise actions.ActionError(_("Not services of the current repo: {names}", names=", ".join(unknown)))
        user, db = user or cfg.last_user, db or cfg.last_db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        taken = set(instances.running_ports())  # the ports picked here too, so no two of them get the same one
        launches = []
        for service in dict.fromkeys(services):
            port = runner.next_free_port(cfg.defaults.host, cfg.defaults.port, taken)
            launch = actions.plan_service(cfg, root / service, user_name=user, db_name=db, port=port, on_demand=on_demand)
            taken.add(launch.port)
            launches.append(launch)
        actions.check_database(cfg, db, confirmed)
        keys = [instances.make_key(launch.service, launch.port) for launch in launches]
        busy = [key for key in keys if self.busy(key)]
        if busy:
            raise actions.ActionError(_("{key} is busy.", key=", ".join(busy)))
        recreate = actions.check_pythons(cfg, [launch.service for launch in launches], install, python)
        behind = actions.check_migrations(cfg, [db], migrate)
        needs = actions.check_test_views(cfg, [(launch.service, db) for launch in launches], views)
        actions.remember_profile(cfg, user, db)
        turn = threading.Lock()  # one at a time: installs and starts do not compete
        prepared: list[bool] = []  # the migrations and test views, by the first start

        def start_one(launch: actions.ServiceLaunch, key: str) -> Callable[[Job], None]:
            def work(job: Job) -> None:
                with turn:
                    if not prepared:
                        prepared.append(True)
                        self.migrate_first(job, behind)
                        self.views_first(cfg, job, needs)
                    self.install(cfg, job, launch.service, key, install, recreate=recreate)
                    self.phase(job, "starting")
                    actions.start_service(cfg, launch)
            return work

        return [self.run(key, "start", "waiting", start_one(launch, key), once=launch.service.name)
                for launch, key in zip(launches, keys)]

    def views_first(self, cfg: Config, job: Job, needs: list[actions.TestViewNeed]) -> None:
        """Make the test copies of the views the user agreed to (see :func:`actions.check_test_views`)."""
        if not needs:
            return
        key = "test-views"
        log = core.install_log(key)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.phase(job, "preparing test views", installed=True, log_key=key)
        with open(log, "w", encoding="utf-8", errors="replace") as output:
            actions.prepare_test_views(cfg, needs, output)

    def migrate_first(self, job: Job, behind: list[actions.Unmigrated]) -> None:
        """Apply the migrations the user agreed to (see :func:`actions.check_migrations`), each with its log."""
        for item in behind:
            key = f"migrate:{item.alias}"
            log = core.install_log(key)
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase(job, f"migrating {item.alias}", installed=True, log_key=key)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.apply_migrations(item, output)

    def install(self, cfg: Config, job: Job, service: Path, key: str, install: bool | None, label: str = "",
                recreate: dict[Path, str] | None = None) -> None:
        """Install ``service`` if needed, writing the output to the install log of instance ``key``; first on
        another Python when it is in ``recreate`` (see :func:`actions.check_pythons`)."""
        python = (recreate or {}).get(service)
        if not python and not actions.needs_install(cfg, service, install):
            return
        log = core.install_log(key)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.phase(job, f"installing {label}".strip(), installed=True, log_key=key)
        with open(log, "w", encoding="utf-8", errors="replace") as output:
            if python:  # a new virtualenv on another Python first
                actions.install_service(service, output, None, python)
            else:
                actions.install_service(service, output, actions.quick_install(cfg, service, install))
