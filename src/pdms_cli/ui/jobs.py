"""What ``pdms ui`` does to services: the same actions as the CLI, run in a thread so the page stays live.

A job belongs to one instance (or the proxy) and shows in the state while it runs, with its phase; a failed one
stays there with its error until the next action on that instance or until it is dismissed. Everything that needs
an answer (a protected database, the local ElasticMQ) is checked before the job starts, so the page can ask first.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path

from .. import __version__, actions, events, frontend, i18n, instances, proxy, repos, routes, runner, transfer, update
from .. import images, localcopy, localdb, migrations, testruns, userimport
from ..config import EVENTS_MODES, LOG_LEVELS, THEMES, Config, Database, Defaults, DevUser, Setup, Stack, config_path
from ..i18n import _
from . import state as ui_state
from . import updates as ui_updates
from .control import Control
from .aws import Aws
from .changes import Changes
from .doctor import Doctor
from .health import Health
from .testing import Tests

EVENTS_KEY = "events:elasticmq"  # the job of pdms events up/down (not an instance: no row of its own in Services)
HOME_KEY = "home"  # the job of Home's Start everything / Stop everything
REPO_KEY = "repo"  # the job of switching the current repo (stopping or moving what ran from the old one)
TESTS_DB_KEY = "tests:db"  # the job of starting pdms's Postgres and creating its test databases
IMAGES_KEY = "images"  # the job of downloading Docker images (asked first: hundreds of MB)
DATA_KEY = "data:postgres"  # the jobs of the Data screen on pdms's Postgres (start, stop, refresh the local copy)
SWITCH_CHOICES = ("keep", "stop", "move")


class LeftRunning(actions.Decision):
    """Switching repos leaves instances of the old one running: keep, stop or move them (``running``)."""

    def __init__(self, switch: actions.RepoSwitch) -> None:
        super().__init__(f"{len(switch.running)} instances run from {switch.old}")
        self.switch = switch


def install_log(key: str) -> Path:
    """Where the install a restart runs writes its output (the service's own log is rotated when it starts)."""
    return instances.log_path(key).with_suffix(".install.log")


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


class Jobs:
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

    def failed(self, key: str, action: str, error: str, log_key: str = "") -> None:
        """Show a failure that happened outside a job (found when pdms ui starts)."""
        with self._lock:
            self._jobs[key] = Job(key, action, "", time.time(), error=error, log_key=log_key)
        self.on_change()

    # ------------------------------------------------------------------ tests

    def run_tests(self, body: dict) -> dict:
        cfg = Config.load()
        projects, dbs = body.get("projects"), body.get("dbs")
        if not _strings(projects) or not projects or not _strings(dbs):
            raise actions.ActionError("projects and dbs must be lists of names")
        dev_mode = body.get("dev_mode", False)
        if not isinstance(dev_mode, bool):
            raise actions.ActionError("dev_mode must be true or false")
        return self.tests.run(cfg, backend(cfg), projects, dbs, dev_mode)

    def start_test_db(self) -> Job:
        """Start pdms's Postgres and create its test databases, like ``pdms db local up``."""
        if not localdb.state()["exists"]:
            images.require([localdb.IMAGE])

        def work(job: Job) -> None:
            self.phase(job, "starting")
            testruns.prepare_test_dbs()

        return self.run(TESTS_DB_KEY, "start", "starting", work)

    # ------------------------------------------------------------------ data

    def pull_images(self, names: list[str], then: str = "", body: dict | None = None) -> Job:
        """Download the images the user agreed to (output in a log), then go on with what needed them: ``then`` is
        "postgres-up", "refresh" or "test-dbs"."""
        unknown = [name for name in names if name not in images.IMAGES]
        if unknown or not names:
            raise actions.ActionError(_("'{name}' is not an image pdms uses.", name=(unknown or [""])[0]))
        follow = {"postgres-up": lambda: self.postgres("up"), "refresh": lambda: self.refresh_copy(body or {}),
                  "test-dbs": self.start_test_db, "": lambda: None}
        if then.startswith("migrate:"):
            follow[then] = lambda: self.migrate_db(then.split(":", 1)[1])
        if then not in follow:
            raise actions.ActionError(f"unknown follow-up {then}")

        def work(job: Job) -> None:
            log = install_log(IMAGES_KEY)
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                for name in names:
                    self.phase(job, f"downloading {name}", installed=True, log_key=IMAGES_KEY)
                    images.pull(name, output)
            threading.Timer(0.1, follow[then]).start()  # after this job ends: the next one may use its key

        return self.run(IMAGES_KEY, "download", "downloading", work)

    def postgres(self, verb: str) -> Job:
        """Start or stop pdms's Postgres (``pdms db local up/down``); starting also creates the test databases."""
        if verb not in ("up", "down"):
            raise actions.ActionError("verb must be up or down")
        if verb == "up" and not localdb.state()["exists"]:
            images.require([localdb.IMAGE])  # asked first (409), never downloaded by surprise

        def work(job: Job) -> None:
            if verb == "up":
                self.phase(job, "starting")
                testruns.prepare_test_dbs()
            else:
                self.phase(job, "stopping")
                localdb.down()

        return self.run(DATA_KEY, "start" if verb == "up" else "stop", "starting" if verb == "up" else "stopping",
                        work)

    def migrate_db(self, name: str) -> Job:
        """Flyway migrate on a local database (``pdms migrate migrate``); shared ones only show their state."""
        cfg = Config.load()
        actions.require(cfg.dbs, _("database"), name)
        database = cfg.dbs[name]
        if not migrations.is_local(database):
            raise actions.ActionError(_("migrate only runs against a local database (localhost, not protected). "
                                        "'{name}' ({host}) is shared: it is migrated by the pdms-db-migrations "
                                        "pipeline.", name=name, host=database.host))
        repo = actions.current_migrations_repo(cfg)
        try:
            known = migrations.status(repo, database, cfg.defaults.db_timeout)
        except Exception as exc:  # noqa: BLE001 - the driver's errors: nothing to migrate without reading it first
            raise actions.ActionError(_("Could not query {name}: {error}", name=name, error=str(exc).strip()[:200])) from exc
        if not known["flyway"]:
            raise actions.ActionError(_("{name} has no Flyway history: its tables were made another way, and Flyway "
                                        "would run every migration over them. pdms only applies migrations to a "
                                        "database Flyway already manages.", name=name))
        images.require([migrations.IMAGE])
        key = f"migrate:{name}"

        def work(job: Job) -> None:
            log = install_log(key)
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase(job, f"migrating {name}", installed=True, log_key=key)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                code = subprocess.run(migrations.docker_command(repo, database, "migrate"), stdout=output,
                                      stderr=subprocess.STDOUT, timeout=3600,
                                      env={**os.environ, "DB_PASSWORD": database.password}).returncode
            if code != 0:
                raise actions.ActionError(_("Flyway migrate failed on {name} (exit code {code}); the log says why.",
                                            name=name, code=code))

        return self.run(key, "migrate", f"migrating {name}", work)

    def restore_snapshot(self, name: str) -> Job:
        """Stop the running services that use the local copy, restore the snapshot, start them again as they were."""
        cfg = Config.load()
        if name not in {snap["name"] for snap in localcopy.snapshots()}:
            raise actions.ActionError(_("There is no snapshot '{name}'.", name=name))
        using = [find(key) for key in localcopy.using_copy(cfg)]

        def work(job: Job) -> None:
            for inst in using:
                self.phase(job, f"stopping {inst.key}")
                actions.stop_service(inst)
            self.phase(job, f"restoring {name}")
            localcopy.restore_snapshot(name)
            for inst in using:
                self.phase(job, f"starting {inst.key}")
                actions.start_service(cfg, actions.plan_service(
                    cfg, Path(inst.service), user_name=inst.user, db_name=inst.db, port=inst.port or None,
                    host=inst.host, reload=inst.reload, events_mode="local" if inst.events == "local" else None))

        return self.run(DATA_KEY, "restore", "stopping", work)

    def use_copy(self, instead_of: str) -> dict:
        """Home's "Use the local copy": the setup's stack moves from ``instead_of`` to the local copy, and the running
        services on ``instead_of`` restart on the copy with their own user."""
        cfg = Config.load()
        actions.require(cfg.dbs, _("database"), instead_of)
        copy = localcopy.alias(cfg)
        if not copy:
            raise actions.ActionError(_("There is no local copy yet: make it in Data first."))
        stack_name = cfg.setup.stack
        stack = cfg.stacks.get(stack_name)
        moved = False
        if stack:
            if stack.db == instead_of:
                stack.db, moved = copy, True
            for own in stack.overrides.values():
                if own.get("db") == instead_of:
                    own["db"], moved = copy, True
            if moved:
                actions.save_stack(cfg, stack_name, stack)
        keys = [inst.key for inst in instances.load().values() if inst.db == instead_of and inst.alive()]
        started = self.restart_many(keys, db=copy, confirmed=True) if keys else []
        return {"copy": copy, "stack": stack_name if moved else "", "restarting": [job.key for job in started]}

    def refresh_copy(self, body: dict) -> Job:
        """``pdms db local refresh`` in a job, its output in the install log of the Data screen's key."""
        cfg = Config.load()
        alias = _text(body, "from", localcopy.SOURCE_ALIAS)
        database = _text(body, "database", localcopy.SOURCE_DATABASE)
        localcopy.source(cfg, alias, database)  # an unknown alias or a bad name is said now, not in the job
        using = localcopy.using_copy(cfg) if localdb.state()["running"] else []
        if using and body.get("confirmed") is not True:
            raise actions.Decision(_("{n} running services use the local copy ({keys}): their connections end while "
                                     "it is replaced.", n=len(using), keys=", ".join(using)))
        try:
            repo = actions.current_migrations_repo(cfg) if body.get("migrate", True) else None
        except actions.ActionError:
            repo = None  # no migrations repo: copied as the source has it
        images.require([localdb.IMAGE, *([migrations.IMAGE] if repo else [])])

        def work(job: Job) -> None:
            log = install_log(DATA_KEY)
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase(job, "copying", installed=True, log_key=DATA_KEY)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                localcopy.refresh(cfg, output, alias=alias, database=database, migrations_repo=repo)

        return self.run(DATA_KEY, "refresh", "copying", work)

    # ------------------------------------------------------------------ repos

    def use_repo(self, alias: str, running: str | None = None) -> dict:
        """Make ``alias`` the current repo, like ``pdms repo use``. What runs from the old one is kept, stopped or
        restarted from the new one (``running``); without an answer :class:`LeftRunning` asks first."""
        cfg = Config.load()
        switch = actions.plan_repo_switch(cfg, alias)
        if running is not None and running not in SWITCH_CHOICES:
            raise actions.ActionError(f"running must be one of: {', '.join(SWITCH_CHOICES)}")
        if switch.running and running is None:
            raise LeftRunning(switch)
        if self.busy(REPO_KEY):
            raise actions.ActionError(_("{key} is busy.", key=REPO_KEY))
        actions.use_repo(cfg, alias)
        answer = {"proxy": switch.proxy, "old": switch.old, "job": None, "left": []}
        if not switch.running or running == "keep":
            return answer
        if running == "move":
            answer["left"] = [inst.key for inst, target in switch.running if target is None]

        def work(job: Job) -> None:
            for inst, target in switch.running:
                if running == "move" and target is None:
                    continue  # not in the new repo: it keeps running
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
                if running == "move":
                    self.phase(job, f"starting {inst.name}")
                    launch = actions.plan_service(cfg, target, user_name=inst.user, db_name=inst.db, port=inst.port,
                                                  host=inst.host, reload=inst.reload)
                    key = instances.make_key(launch.service, launch.port)
                    self.install(cfg, job, launch.service, key, None, inst.name)
                    actions.start_service(cfg, launch)

        answer["job"] = self.run(REPO_KEY, "move" if running == "move" else "stop", "stopping", work).key
        return answer

    # ------------------------------------------------------------------ pdms itself

    def update(self, control: Control, *, restart_proxy: bool = True) -> Job:
        """Install the latest version and restart pdms ui with it (see :mod:`.updates`)."""
        if not update.updates_itself():
            raise actions.ActionError(_("This pdms was not installed with uv tool, so it cannot update itself; "
                                        "run the command shown."))
        target, _checked = update.cached_latest(ui_updates.channel())
        if not target or not update.is_newer(target):
            raise actions.ActionError(_("pdms {version} is the latest version.", version=__version__))
        return self.run(ui_updates.KEY, "update", "starting", lambda job: ui_updates.install(
            control, target, restart_proxy, lambda phase: self.phase(job, phase, log_key=ui_updates.KEY)))

    def after_update(self, note: dict | None) -> None:
        """Right after restarting for an update: say if it failed and start the proxy it stopped."""
        error, saved_proxy = ui_updates.after_restart(note)
        if error:
            self.failed(ui_updates.KEY, "update", error, log_key=ui_updates.KEY)
        if saved_proxy:
            self.run(proxy.KEY, "start", "starting", lambda _job: ui_updates.start_proxy_again(saved_proxy))

    # ------------------------------------------------------------------ services

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
                self.install(cfg, job, service, inst.key, install)
                self.phase(job, "starting")

            actions.restart_service(cfg, inst, user_name=user, db_name=db, confirmed=True, install=install_step)

        return self.run(inst.key, "restart", "stopping", work)

    def restart_many(
        self, keys: list[str], *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, stack: str | None = None, remember: bool = False,
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
        if remember:
            if not stack or not (user or db):
                raise actions.ActionError("remember needs a stack and a user or a database")
            root = repos.active_backend(cfg)
            for inst in found:
                actions.remember_in_stack(cfg, stack, root, [Path(inst.service)], user or inst.user, db or inst.db)
        if user or db:
            actions.remember_profile(cfg, user or found[0].user, db or found[0].db)
        turn = threading.Lock()  # one at a time: installs and starts do not compete

        def restart_one(inst: instances.Instance) -> Callable[[Job], None]:
            def work(job: Job) -> None:
                with turn:
                    self.phase(job, "stopping")

                    def install_step(service: Path) -> None:
                        self.install(cfg, job, service, inst.key, install)
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
        install: bool | None = None, confirmed: bool = False,
    ) -> Job:
        """Start one service of the current repo in the background, like ``pdms run -b``. Without ``port`` it takes
        the next free one; a busy ``port``, a protected database or a stopped local ElasticMQ (for a consumer) are
        raised before anything starts."""
        cfg = Config.load()
        root, known = repo_services(cfg)
        if service not in known:
            raise actions.ActionError(_("Not services of the current repo: {names}", names=service))
        path = root / service
        user, db = user or cfg.last_user, db or cfg.last_db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        launch = actions.plan_service(
            cfg, path, user_name=user, db_name=db, port=port or actions.suggested_port(cfg, cfg.defaults.host),
        )
        actions.check_database(cfg, db, confirmed)
        key = instances.make_key(launch.service, launch.port)
        if self.busy(key):
            raise actions.ActionError(_("{key} is busy.", key=key))
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.install(cfg, job, launch.service, key, install)
            self.phase(job, "starting")
            actions.start_service(cfg, launch)

        return self.run(key, "start", "starting", work)

    def install(self, cfg: Config, job: Job, service: Path, key: str, install: bool | None, label: str = "") -> None:
        """Install ``service`` if needed, writing the output to the install log of instance ``key``."""
        if not actions.needs_install(cfg, service, install):
            return
        log = install_log(key)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.phase(job, f"installing {label}".strip(), installed=True, log_key=key)
        with open(log, "w", encoding="utf-8", errors="replace") as output:
            actions.install_service(service, output)

    # ------------------------------------------------------------------ stacks

    def up(
        self, name: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job | None:
        """Start what is not running of the stack, like ``pdms up``; None when all of it already runs."""
        cfg = Config.load()
        stack = cfg.stacks[actions.require(cfg.stacks, _("stack"), name)]
        root = backend(cfg)
        actions.stack_paths(stack, root)  # a stale stack fails before anything else
        user, db = user or stack.user or cfg.last_user, db or stack.db or cfg.last_db
        plan = actions.plan_stack(cfg, name, root, user_name=user, db_name=db)
        if not plan.services:
            return None
        actions.check_database(cfg, db, confirmed)
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            for launch in plan.services:
                key = instances.make_key(launch.service, launch.port)
                self.install(cfg, job, launch.service, key, install, launch.service.name)
                self.phase(job, f"starting {launch.service.name}")
                actions.start_service(cfg, launch)

        return self.run(stack_key(name), "up", "starting", work)

    def down(self, name: str) -> Job | None:
        """Stop the stack's running services, like ``pdms down``; None when nothing of it runs."""
        cfg = Config.load()
        targets = actions.stack_instances(cfg, name, backend(cfg))
        if not targets:
            return None

        def work(job: Job) -> None:
            for inst in targets:
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)

        return self.run(stack_key(name), "down", "stopping", work)

    # ------------------------------------------------------------------ proxy

    def start_proxy(
        self, *, port: int, env: str = "dev", remote: str | None = None, no_remote: bool = False,
        user: str | None = None, frontend: bool | None = None,
    ) -> Job:
        """Start the proxy in the background, like ``pdms proxy -b``; raises the decisions (busy port, pointing the
        frontend to it) before anything starts. The job ends once the proxy answers."""
        if self.busy(proxy.KEY):
            raise actions.ActionError(_("{key} is busy.", key=proxy.KEY))
        actions.clear_proxy_leftovers()
        cfg = Config.load()
        root = repo_root(cfg)
        repo_routes = actions.proxy_routes(root, env)
        target, _detected = actions.proxy_remote(cfg, root, remote, no_remote)
        plan = actions.plan_proxy(
            cfg, root, repo_routes, port=port, env=env, remote=target, user_name=user, frontend=frontend,
        )

        def work(_job: Job) -> None:
            actions.point_frontend(plan)
            started = actions.start_proxy(plan)
            if actions.wait_for_proxy(started) == "stopped":
                actions.stop_proxy()
                last = instances.tail(str(started.log), 1).strip()
                raise actions.ActionError(_("The proxy exited while starting: {line}", line=last or "?"))

        return self.run(proxy.KEY, "start", "starting", work)

    # ------------------------------------------------------------------ frontend

    def start_frontend(
        self, *, mode: str = "dev", port: int | None = None, install: bool | None = None, rebuild: bool = False,
        restart: bool = False,
    ) -> Job:
        """Start the web app like ``pdms front -b``: yarn install when needed, the build in build mode, then the
        server; a busy port is raised before anything starts. The job ends once it answers. With ``restart`` the
        running one stops first, on the same port (Rebuild)."""
        if self.busy(frontend.KEY):
            raise actions.ActionError(_("{key} is busy.", key=frontend.KEY))
        if restart and (current := frontend.running()):
            port = port or int(current["port"])
            actions.stop_frontend(current)
        plan = actions.plan_frontend(Config.load(), mode=mode, port=port, install=install, rebuild=rebuild)
        return self.run(frontend.KEY, "start", "starting", lambda job: self.frontend_steps(job, plan))

    def frontend_steps(self, job: Job, plan: actions.FrontendLaunch) -> None:
        """Install, build and start the frontend, writing the install and build output to their own logs."""
        if plan.install:
            self.phase(job, "installing frontend", installed=True, log_key=frontend.KEY)
            log = frontend.install_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.install_frontend(plan.root, output, log)
        if plan.mode == "build" and (plan.build or frontend.build_needed(plan.root)):
            self.phase(job, "building frontend", log_key=frontend.KEY)
            log = frontend.build_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.build_frontend(plan.root, output)
        self.phase(job, "starting frontend")
        started = actions.start_frontend(plan)
        if actions.wait_for_frontend(started) == "stopped":
            raise actions.ActionError(_("The frontend exited while starting: {line}",
                                        line=frontend.last_line(frontend.log_path()) or "?"))

    # ------------------------------------------------------------------ Home

    def start_all(self, *, install: bool | None = None, confirmed: bool = False) -> Job | None:
        """Start what is not running of the saved setup, in order: the local events, the stack, the proxy (pointing
        the frontend to it) and the frontend. A protected database or a busy frontend port are raised first; the
        proxy takes the next free port by itself. None when everything already runs."""
        if self.busy(HOME_KEY):
            raise actions.ActionError(_("{key} is busy.", key=HOME_KEY))
        cfg = Config.load()
        setup = cfg.setup
        want_events = setup.events and not events.is_up(cfg.defaults.events_port)
        event_map = actions.load_events(cfg)[1] if want_events else None

        stack_name = ""
        user, db = cfg.last_user, cfg.last_db
        if setup.stack:
            stack = cfg.stacks[actions.require(cfg.stacks, _("stack"), setup.stack)]
            root = backend(cfg)
            live = actions.running_by_service()
            if [path for path in actions.stack_paths(stack, root) if str(path) not in live]:
                stack_name = setup.stack
                user, db = stack.user or cfg.last_user, stack.db or cfg.last_db
                actions.require(cfg.users, _("user"), user)
                actions.require(cfg.dbs, _("database"), db)
                actions.check_database(cfg, db, confirmed)

        repo = repo_root(cfg)
        broker = None
        if event_map is not None and (service := actions.broker_service(repo, event_map)) and user and db \
                and not actions.event_consumers(event_map.broker_queue):
            actions.require(cfg.users, _("user"), user)
            actions.require(cfg.dbs, _("database"), db)
            actions.check_database(cfg, db, confirmed)
            broker = service
        want_frontend = setup.frontend and frontend.exists(repo) and not frontend.running()
        proxy_plan = None
        if setup.proxy and not proxy.running_proxy() and not self.busy(proxy.KEY):
            actions.clear_proxy_leftovers()
            env = "dev"
            target, _detected = actions.proxy_remote(cfg, repo, None, False)
            plan_args = dict(env=env, remote=target, frontend=frontend.exists(repo) or None)
            try:
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=cfg.defaults.proxy_port, **plan_args)
            except actions.PortBusy as busy:
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=busy.free, **plan_args)
        frontend_plan = None
        if want_frontend and not self.busy(frontend.KEY):
            frontend_plan = actions.plan_frontend(cfg, mode=setup.frontend_mode, install=install)
        if event_map is None and not stack_name and proxy_plan is None and frontend_plan is None:
            return None
        if stack_name or broker:
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            if event_map is not None:
                self.phase(job, "starting ElasticMQ")
                actions.start_events(cfg, event_map.queues)
            if broker is not None:
                launch = actions.plan_service(cfg, broker, user_name=user, db_name=db, events_mode="local")
                self.install(cfg, job, broker, instances.make_key(launch.service, launch.port), install, broker.name)
                self.phase(job, f"starting {broker.name}")
                actions.start_service(cfg, launch)
            if stack_name:
                plan = actions.plan_stack(cfg, stack_name, backend(cfg), user_name=user, db_name=db)
                for launch in plan.services:
                    key = instances.make_key(launch.service, launch.port)
                    self.install(cfg, job, launch.service, key, install, launch.service.name)
                    self.phase(job, f"starting {launch.service.name}")
                    actions.start_service(cfg, launch)
            if proxy_plan is not None:
                self.phase(job, "starting proxy")
                actions.point_frontend(proxy_plan)
                started = actions.start_proxy(proxy_plan)
                if actions.wait_for_proxy(started) == "stopped":
                    actions.stop_proxy()
                    raise actions.ActionError(_("The proxy exited while starting: {line}",
                                                line=instances.tail(str(started.log), 1).strip() or "?"))
            if frontend_plan is not None:
                self.frontend_steps(job, frontend_plan)

        return self.run(HOME_KEY, "up", "starting", work)

    def switch_setup(self, name: str) -> list[str]:
        """Make the saved setup ``name`` the current one, stopping what the current one runs and the new one does
        not use: its stack (when another), the proxy and the frontend (when the new one goes without them). The local
        events stay: other services may publish there. Returns the keys of the stop jobs; the page starts the new
        setup (Start everything) once they are done."""
        cfg = Config.load()
        actions.require(cfg.setups, _("setup"), name)
        old, new = cfg.setup, cfg.setups[name]
        stops = []
        if old.stack and old.stack != new.stack and old.stack in cfg.stacks:
            job = self.down(old.stack)
            if job:
                stops.append(job.key)
        running_proxy = proxy.running_proxy()
        if old.proxy and not new.proxy and running_proxy and not self.busy(proxy.display_key(running_proxy)):
            stops.append(self.stop(proxy.display_key(running_proxy)).key)
        if old.frontend and not new.frontend and frontend.running() and not self.busy(frontend.KEY):
            stops.append(self.stop(frontend.KEY).key)
        actions.use_setup(cfg, name)
        self.on_change()
        return stops

    def stop_all(self) -> Job | None:
        """Stop the frontend, the proxy, every background service and the local ElasticMQ; None when nothing runs."""
        current_frontend, running_proxy = frontend.running(), proxy.running_proxy()
        live = [inst for inst in instances.load().values() if inst.alive()]
        container = events.container_state()
        if not (current_frontend or running_proxy or live or (container and container["running"])):
            return None

        def work(job: Job) -> None:
            if current_frontend:
                self.phase(job, "stopping frontend")
                actions.stop_frontend(current_frontend)
            if running_proxy:
                self.phase(job, "stopping proxy")
                actions.stop_proxy(running_proxy)
            for inst in live:
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
            if container and container["running"]:
                self.phase(job, "stopping ElasticMQ")
                events.stop()

        return self.run(HOME_KEY, "down", "stopping", work)

    # ------------------------------------------------------------------ local events

    def events_up(
        self, *, broker: bool = True, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job:
        """Start the local ElasticMQ with every queue of the repo and, with ``broker``, the broker, like
        ``pdms events up``; a protected database for the broker is asked before anything starts."""
        cfg = Config.load()
        root, event_map = actions.load_events(cfg)
        service = actions.broker_service(root, event_map) if broker else None
        if service and actions.event_consumers(event_map.broker_queue):
            service = None  # already running
        if service:
            user, db = user or cfg.last_user, db or cfg.last_db
            if not user or not db:
                raise actions.ActionError(_("Configure a user and a database to run the broker (pdms user add, "
                                            "pdms db add), or start the events without it."))
            actions.require(cfg.users, _("user"), user)
            actions.require(cfg.dbs, _("database"), db)
            actions.check_database(cfg, db, confirmed)
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            actions.start_events(cfg, event_map.queues)
            if service and user and db:
                launch = actions.plan_service(cfg, service, user_name=user, db_name=db, events_mode="local")
                key = instances.make_key(launch.service, launch.port)
                self.install(cfg, job, service, key, install, service.name)
                self.phase(job, f"starting {service.name}")
                actions.start_service(cfg, launch)

        return self.run(EVENTS_KEY, "up", "starting ElasticMQ", work)

    def events_down(self) -> Job:
        """Stop the SQS consumers and the local ElasticMQ (its messages are lost), like ``pdms events down``."""

        def work(job: Job) -> None:
            for inst in actions.event_consumers():
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
            self.phase(job, "stopping ElasticMQ")
            events.stop()

        return self.run(EVENTS_KEY, "down", "stopping", work)


def events_queues(cfg: Config) -> dict:
    """Every queue of the repo (and any other ElasticMQ has) with its messages, consumer and the event types the
    broker routes to it, like ``pdms events status --all``."""
    root, event_map = actions.load_events(cfg)
    port = cfg.defaults.events_port
    up = events.is_up(port)
    counts = events.queue_counts(port) if up else {}
    running = {i.queue: i.key for i in actions.event_consumers()}
    types: dict[str, int] = {}
    for queue in event_map.routes.values():
        types[queue] = types.get(queue, 0) + 1
    names = sorted(set(event_map.queues) | set(counts))
    queues = []
    for name in names:
        queue = event_map.queues.get(name) or events.Queue(name, fifo=name.endswith(".fifo"), source="elasticmq")
        if name == events.SNS_QUEUE:
            queue = events.Queue(name, fifo=False, source="pdms")
        consumer = event_map.consumers.get(name)
        count = counts.get(name)
        queues.append({
            "name": name, "fifo": queue.fifo, "source": queue.source, "types": types.get(name, 0),
            "visible": count["visible"] if count else None, "in_flight": count["in_flight"] if count else None,
            "consumer": consumer.service if consumer else "", "running": running.get(name, ""),
            "broker": name == event_map.broker_queue, "sns": name == events.SNS_QUEUE,
        })
    publishers = [i.key for i in instances.load().values() if i.alive() and i.events == "local"]
    return {"up": up, "endpoint": events.endpoint(port), "queues": queues, "publishers": publishers,
            "broker": event_map.broker_queue, "broker_service": bool(actions.broker_service(root, event_map))}


def events_ready(cfg: Config) -> dict:
    """What starting the local events needs (Docker running, its port free) and a real route of the repo, for the
    Events screen while they are off."""
    docker_ok, docker_version = events.docker_available()
    port = cfg.defaults.events_port
    example = None
    try:
        _root, event_map = actions.load_events(cfg)
    except actions.ActionError:
        event_map = None
    if event_map:
        for event_type, queue in sorted(event_map.routes.items()):
            consumer = event_map.consumers.get(queue)
            if consumer and queue != event_map.broker_queue:
                example = {"type": event_type, "queue": queue, "consumer": consumer.service.rsplit("/", 1)[-1],
                           "broker": event_map.broker_queue}
                break
    return {
        "docker": {"ok": docker_ok, "version": docker_version},
        "port": {"port": port, "free": events.is_up(port) or actions.port_available("127.0.0.1", port)},
        "example": example,
        "queues": len(event_map.queues) if event_map else 0,
    }


def events_map(cfg: Config) -> dict:
    """Every event type, the queue the broker sends it to and its consumer, like ``pdms events map``."""
    _root, event_map = actions.load_events(cfg)
    return {
        "broker": event_map.broker_queue,
        "types": [
            {"type": event_type, "queue": queue,
             "consumer": consumer.service if (consumer := event_map.consumers.get(queue)) else ""}
            for event_type, queue in sorted(event_map.routes.items())
        ],
    }


def events_template(cfg: Config, event_type: str) -> dict:
    root, event_map = actions.load_events(cfg)
    if event_type not in event_map.routes:
        raise actions.ActionError(_("'{target}' is not an event type nor a queue.", target=event_type))
    fields = events.event_template(root, event_type)
    if fields is None:
        raise actions.ActionError(_("No event class found for '{target}' in backend/common/event.", target=event_type))
    return fields


def events_peek(cfg: Config, queue: str, limit: int) -> list[dict]:
    """The messages waiting in ``queue``, without consuming them (``pdms events peek``)."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    if queue not in events.queue_counts(port):
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=queue))
    found = []
    for message in events.peek(port, queue, limit):
        attributes = message.get("Attributes", {})
        sent = attributes.get("SentTimestamp", "")
        found.append({
            "id": message.get("MessageId", ""), "body": message.get("Body", ""),
            "receives": int(attributes.get("ApproximateReceiveCount", 0) or 0),
            "sent": int(sent) if sent.isdigit() else None, "group": attributes.get("MessageGroupId", ""),
        })
    return found


def events_send(cfg: Config, target: str, body: str | None, direct: bool) -> dict:
    """Send an event (through the broker unless ``direct``) or a message to a queue, like ``pdms events send``."""
    _root, event_map = actions.load_events(cfg)
    queue, message = actions.plan_send(event_map, target, body, direct)
    message_id = actions.send_message(cfg, event_map, queue, message)
    routed = event_map.routes.get(target, "") if queue == event_map.broker_queue and target != queue else ""
    consumer = event_map.consumers.get(routed or queue)
    return {"id": message_id, "queue": queue, "routed_to": routed, "consumer": consumer.service if consumer else "",
            "consumed": bool(actions.event_consumers(queue))}


def events_purge(cfg: Config, queues: list[str]) -> list[str]:
    """Delete every message of ``queues`` (all of them that have messages when empty), like ``pdms events purge``."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    counts = events.queue_counts(port)
    unknown = [name for name in queues if name not in counts]
    if unknown:
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=unknown[0]))
    targets = queues or [name for name, count in counts.items() if count["visible"] or count["in_flight"]]
    for name in targets:
        events.purge(port, name)
    return targets


def proxy_options(cfg: Config) -> dict:
    """What the start form offers: the environments with Terraform, the remote it would use, whether there is a
    frontend to point to it. Reads only (the remote is saved when the proxy starts, as in the CLI)."""
    root = repo_root(cfg)
    environments = routes.terraform_dir(root, "dev").parent
    envs = sorted(p.name for p in environments.iterdir() if p.is_dir()) if environments.is_dir() else []
    return {
        "port": cfg.defaults.proxy_port, "env": "dev" if "dev" in envs or not envs else envs[0], "envs": envs,
        "remote": repo_remote(cfg, root), "frontend": (root / "frontend").is_dir(),
    }


def proxy_routes(cfg: Config, env: str | None = None) -> dict:
    """Where each route goes now, like ``pdms proxy routes``: the running proxy's repo, env and remote, or what a
    proxy started now would use."""
    running = proxy.running_proxy()
    if running and running.get("repo"):
        root, env, remote = Path(running["repo"]), env or running.get("env") or "dev", running.get("remote") or None
    else:
        root = repo_root(cfg)
        env, remote = env or "dev", repo_remote(cfg, root) or None
    gateway = proxy.Gateway(routes=actions.proxy_routes(root, env), backend=root / "backend", remote=remote)
    live = gateway.local_instances()
    items = []
    for route in gateway.routes:
        target = gateway.target(route, live)
        items.append({
            "method": route.method, "path": route.path, "service": route.service, "target": target.kind,
            "key": target.instance.key if target.instance else "", "note": target.note,
        })
    return {"env": env, "remote": remote or "", "routes": items}


def repo_remote(cfg: Config, root: Path) -> str:
    """The remote API a proxy started now would use: the one saved for the repo, or the one in ``frontend/.env``."""
    alias = repos.alias_of(cfg, root)
    return (cfg.repos[alias].remote if alias in cfg.repos else "") or repos.remote_from_frontend(root) or ""


def repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def stack_key(name: str) -> str:
    return f"stack:{name}"


def backend(cfg: Config) -> Path:
    root = repos.active_backend(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root.resolve()


def repo_services(cfg: Config) -> tuple[Path, list[str]]:
    """The backend folder and every service in it, as the stacks write them (``lead/lead-tp-list``)."""
    root = backend(cfg)
    return root, [path.relative_to(root).as_posix() for path in runner.find_services_below(root)]


def repo_consumers(root: Path) -> list[str]:
    """The services of the backend folder ``root`` that consume an SQS queue (they take no port)."""
    try:
        event_map = events.load_event_map(root.parent)
    except Exception:  # noqa: BLE001 - a Terraform pdms cannot read only loses the hint
        return []
    return sorted({consumer.service for consumer in event_map.consumers.values()})


def save_stack(name: str, services: list[str], user: str, db: str, new: bool,
               overrides: dict[str, dict[str, str]] | None = None, rename: str = "") -> str:
    """Create (``new``) or replace a stack, and give an existing one another name (``rename``); the services must
    exist in the current repo. Without ``overrides`` an existing stack keeps its own (for the services it still has).
    The stack's name afterwards."""
    cfg = Config.load()
    name = name.strip()
    rename = rename.strip()
    if new:
        actions.check_alias(name, cfg.stacks)
    else:
        actions.require(cfg.stacks, _("stack"), name)
        if rename and rename != name:
            actions.check_alias(rename, cfg.stacks)  # before saving anything
    _root, known = repo_services(cfg)
    unknown = [svc for svc in services if svc not in known]
    if unknown:
        raise actions.ActionError(_("Not services of the current repo: {names}", names=", ".join(unknown)))
    if overrides is None:
        overrides = {} if new else dict(cfg.stacks[name].overrides)
    actions.save_stack(cfg, name, Stack(services=list(dict.fromkeys(services)), user=user, db=db, overrides=overrides))
    return actions.rename_stack(cfg, name, rename) if not new and rename else name


def stack_from_running() -> dict:
    """What the editor starts from to keep the running services as a new stack (see actions.stack_from_running)."""
    cfg = Config.load()
    return asdict(actions.stack_from_running(cfg, backend(cfg)))


def remove_stack(name: str) -> None:
    actions.remove_stack(Config.load(), name)


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


def fix_frontend_api() -> dict:
    return {"done": actions.fix_frontend_api(Config.load())}


def forget_stopped() -> list[str]:
    stopped = [i.key for i in instances.load().values() if not i.alive()]
    for key in stopped:
        instances.forget(key)
    return stopped


# --------------------------------------------------------------------------- settings: databases, users, defaults


def settings(cfg: Config) -> dict:
    """What the Settings tab shows: every database without its password, the users, the defaults and the stacks
    that use each database or user (they ask again when it is deleted)."""
    def used_by(kind: str, name: str) -> list[str]:
        return [stack_name for stack_name, stack in cfg.stacks.items() if getattr(stack, kind) == name]

    return {
        "path": str(config_path()),
        "dbs": [
            {"name": name, **{k: v for k, v in asdict(db).items() if k != "password"},
             "has_password": bool(db.password), "stacks": used_by("db", name)}
            for name, db in cfg.dbs.items()
        ],
        "users": [{"name": name, **asdict(user), "stacks": used_by("user", name)} for name, user in cfg.users.items()],
        "repos": repo_settings(cfg),
        "defaults": asdict(cfg.defaults),
        "roles": actions.known_roles(cfg),
        "sections": list(transfer.SECTIONS),
        "choices": {"language": i18n.LANGUAGES, "logging_level": list(LOG_LEVELS), "events": list(EVENTS_MODES), "theme": list(THEMES)},
    }


def repo_settings(cfg: Config) -> list[dict]:
    """The Repos tab: each repo with its folders, the remote API (saved or read from frontend/.env) and how many
    instances run from it."""
    live = [inst for inst in instances.load().values() if inst.alive()]
    rows = []
    for alias, repo in cfg.repos.items():
        root = repo.root
        rows.append({
            "name": alias, "path": str(root), "exists": root.is_dir(), "backend": repo.backend,
            "migrations": repo.migrations, "remote": repo.remote,
            "remote_found": "" if repo.remote or not root.is_dir() else (repos.remote_from_frontend(root) or ""),
            "current": alias == cfg.current_repo,
            "running": sum(repos.repo_of(cfg, inst.service) == alias for inst in live),
        })
    return rows


def look_at_repo(body: dict) -> dict:
    """What the Add repo dialog says about a folder: its PDMS checkout, how many services and a name for it."""
    cfg = Config.load()
    root = actions.repo_root(_text(body, "path"))
    backend = root / "backend"
    return {"root": str(root), "services": len(runner.find_services_below(backend)) if backend.is_dir() else 0,
            "name": repos.alias_of(cfg, root) or repos.suggest_alias(cfg, root),
            "registered": repos.alias_of(cfg, root) or ""}


def add_repo(body: dict) -> str:
    """Register a repo with its migrations and remote API (each checked before anything is saved)."""
    cfg = Config.load()
    migrations_path = actions.migrations_repo(_text(body, "migrations"))
    remote = actions.remote_api(_text(body, "remote"))
    alias = actions.add_repo(cfg, _text(body, "path"), _text(body, "name"))
    return actions.edit_repo(Config.load(), alias, migrations_path=migrations_path, remote=remote)


def save_repo(name: str, body: dict) -> str:
    return actions.edit_repo(Config.load(), name, new_alias=_text(body, "name", name),
                             migrations_path=_text(body, "migrations"), remote=_text(body, "remote"))


def remove_repo(name: str) -> str:
    return actions.remove_repo(Config.load(), name)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def tests_info(cfg: Config, tests: Tests) -> dict:
    """The Tests screen: every project with its last result, the ones the branch's changes touch, and pdms's test
    databases (the only ones tests run on: they drop and create every table)."""
    root = backend(cfg)
    port = localdb.state()["port"] or localdb.PORT
    found = testruns.results(root)
    tests.count_failing(root)
    return {
        "projects": [{"project": p, "kind": testruns.kind(root / p), "result": found.get(p),
                      "env_dev_mode": testruns.env_dev_mode(root / p)}
                     for p in testruns.projects(root)],
        "affected": testruns.affected(root),
        "dbs": [{"name": name, "where": f"localhost:{port}/{name}"} for name in testruns.test_dbs()],
        "port": localdb.PORT,
        "root": str(root),
    }


def data_info(cfg: Config) -> dict:
    """The Data screen: the databases, pdms's Postgres with the local copy, its snapshots and the test databases."""
    pg = localdb.state()
    sizes: dict[str, int] = {}
    if pg["running"]:
        try:
            sizes = localdb.databases()
        except actions.ActionError:
            sizes = {}
    copy_port = pg["port"] or localdb.PORT
    copy_alias = next((name for name, db in cfg.dbs.items() if migrations.is_local(db)
                       and (db.port, db.database) == (copy_port, localdb.MAIN_DB)), "")
    try:
        snapshots = localcopy.snapshots() if pg["running"] else []
    except actions.ActionError:
        snapshots = []
    containers = localdb.containers_by_port()
    return {
        "dbs": [{"name": name, "host": db.host, "port": db.port, "database": db.database, "protected": db.protected,
                 "local": migrations.is_local(db), "copy": name == copy_alias,
                 "container": containers.get(db.port, "") if migrations.is_local(db) else ""}
                for name, db in cfg.dbs.items()],
        "postgres": {**pg, "container": localdb.CONTAINER, "volume": localdb.VOLUME, "image": localdb.IMAGE,
                     "user": localdb.USER, "default_port": localdb.PORT},
        "copy": {"alias": copy_alias, "size": sizes.get(localdb.MAIN_DB), "refresh": localcopy.load_state().get("refresh"),
                 "source": f"{localcopy.SOURCE_ALIAS}:{localcopy.SOURCE_DATABASE}"},
        "snapshots": snapshots,
        "tests": [{"name": name, "size": sizes.get(name, 0)} for name in localdb.test_databases(sizes)] if sizes else [],
        "test_count": localdb.TEST_COUNT,
    }


def data_action(verb: str, body: dict) -> dict:
    """Snapshots and test databases: quick, so answered right away."""
    name = _text(body, "name")
    if verb == "snapshot-save":
        return localcopy.save_snapshot(name)
    if verb == "snapshot-restore":
        localcopy.restore_snapshot(name)
        return {}
    if verb == "snapshot-delete":
        localcopy.delete_snapshot(name)
        return {}
    if verb == "tests-recreate":
        localdb.recreate_test_database(name)
        return {}
    if verb == "tests-count":
        count = body.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 20:
            raise actions.ActionError("count must be a number from 0 to 20")
        names = testruns.prepare_test_dbs(count)
        for extra in names[count:]:
            localdb.remove_test_database(extra)
        return {"tests": names[:count]}
    raise actions.ActionError(f"unknown action {verb}")


def migration_status(body: dict) -> dict:
    """The Flyway migrations of the current repo against a database: applied, pending... (read-only)."""
    cfg = Config.load()
    name = actions.require(cfg.dbs, _("database"), _text(body, "db"))
    repo = actions.current_migrations_repo(cfg)
    try:
        return {"db": name, **migrations.status(repo, cfg.dbs[name], cfg.defaults.db_timeout)}
    except (OSError, ValueError) as exc:
        raise actions.ActionError(_("Could not read {path}: {error}", path=repo, error=exc)) from exc
    except Exception as exc:  # noqa: BLE001 - the driver's errors (no VPN, wrong password...)
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise actions.ActionError(_("Could not query {name}: {error}", name=name, error=first)) from exc


def _text(body: dict, key: str, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise actions.ActionError(f"{key} must be a text")
    return value


def database_from(body: dict, current: Database | None) -> Database:
    """The database a form sends; a ``password`` left out or null keeps the current one."""
    password = body.get("password")
    if password is None:
        password = current.password if current else ""
    elif not isinstance(password, str):
        raise actions.ActionError("password must be a text, or null to keep the current one")
    return Database(
        host=_text(body, "host"), port=body.get("port", 5432), database=_text(body, "database"),
        user=_text(body, "user"), password=password, driver=current.driver if current else Database.driver,
        protected=body.get("protected") is True,
    )


def save_db(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    current = None if new else cfg.dbs[actions.require(cfg.dbs, _("database"), name)]
    return actions.save_db(cfg, name, database_from(body, current), new=new)


def remove_db(name: str) -> list[str]:
    return actions.remove_db(Config.load(), name)


def db_password(name: str) -> str:
    cfg = Config.load()
    return cfg.dbs[actions.require(cfg.dbs, _("database"), name)].password


def connect_db(body: dict) -> str:
    """Connect to a saved database (only its ``name``) or to the one in a form (its fields; the password of ``name``
    when the form leaves it out); returns the server version."""
    cfg = Config.load()
    name = _text(body, "name")
    if "host" not in body:
        return actions.check_connection(cfg.dbs[actions.require(cfg.dbs, _("database"), name)], cfg.defaults.db_timeout)
    current = cfg.dbs[actions.require(cfg.dbs, _("database"), name)] if name else None
    return actions.check_connection(actions.valid_db(database_from(body, current)), cfg.defaults.db_timeout)


def user_from(body: dict) -> DevUser:
    return DevUser(**{key: _text(body, key) for key in ("user_id", "username", "first_name", "last_name", "roles")})


def save_user(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    return actions.save_user(cfg, name, user_from(body), new=new, roles=actions.known_roles(cfg))


def remove_user(name: str) -> list[str]:
    return actions.remove_user(Config.load(), name)


def rename_user(name: str, body: dict) -> str:
    return actions.rename_user(Config.load(), name, _text(body, "new_name"))


def frontend_options(cfg: Config) -> dict:
    """What the frontend's start form shows: node and yarn, whether node_modules is up to date, where the API goes
    in each mode and whether a build would be made (and why)."""
    root = repo_root(cfg)
    if not frontend.exists(root):
        return {"available": False}
    tool = frontend.yarn()
    return {
        "available": True, "node": frontend.node_version() or "", "yarn": bool(tool), "port": frontend.PORT,
        "dependencies_ok": frontend.dependencies_ok(root) if tool else False,
        "api": {mode: frontend.api_url(root, mode) for mode in frontend.MODES},
        "build_needed": frontend.build_needed(root), "last_build": frontend.last_build(),
    }


def save_setup(body: dict) -> dict:
    """Save what Start everything starts; the stack must exist (empty for none)."""
    cfg = Config.load()
    stack = _text(body, "stack")
    if stack:
        actions.require(cfg.stacks, _("stack"), stack)
    mode = _text(body, "frontend_mode", "dev")
    if mode not in frontend.MODES:
        raise actions.InvalidValue("frontend_mode", _("Must be one of: {choices}", choices=", ".join(frontend.MODES)))
    cfg.setup = Setup(stack=stack, proxy=_bool(body, "proxy"), frontend=_bool(body, "frontend"), frontend_mode=mode,
                      events=_bool(body, "events"))
    if cfg.setup_name in cfg.setups:
        cfg.setups[cfg.setup_name] = replace(cfg.setup)  # a saved setup follows its edits, like the form says
    cfg.save()
    return asdict(cfg.setup)


def save_setup_as(body: dict) -> dict:
    return {"name": actions.save_setup_as(Config.load(), _text(body, "name"))}


def remove_setup(body: dict) -> dict:
    actions.remove_setup(Config.load(), _text(body, "name"))
    return {}


def defaults_from(body: dict, current: Defaults) -> Defaults:
    """The current defaults with the values the form sends, each of the right kind (the action checks the rest)."""
    values = asdict(current)
    for item in fields(Defaults):
        if item.name not in body:
            continue
        value, kind = body[item.name], type(values[item.name])
        if kind is bool:
            ok, expected = isinstance(value, bool), "true or false"
        elif kind is int:
            ok, expected = isinstance(value, (int, str)) and not isinstance(value, bool), "a number"
        elif kind is dict:
            ok, expected = isinstance(value, dict) and all(isinstance(v, str) for v in value.values()), "texts by name"
        else:
            ok, expected = isinstance(value, str), "a text"
        if not ok:
            raise actions.InvalidValue(item.name, f"must be {expected}")
        values[item.name] = value
    return Defaults(**values)


def save_defaults(body: dict) -> None:
    """Save the defaults; the language also applies to this server's messages from now on."""
    cfg = Config.load()
    actions.save_defaults(cfg, defaults_from(body, cfg.defaults))
    i18n.set_language(cfg.defaults.language)


def _bool(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise actions.ActionError(f"{key} must be true or false")
    return value


def _sections(body: dict) -> list[str]:
    sections = body.get("sections")
    if not isinstance(sections, list) or not all(isinstance(section, str) for section in sections):
        raise actions.ActionError("sections must be a list of section names")
    return sections


def export_config(body: dict) -> dict:
    """The file of ``pdms config export`` for the page to download."""
    text = actions.export_config(Config.load(), _sections(body), _bool(body, "secrets"))
    return {"filename": f"pdms-config-{datetime.now():%Y-%m-%d}.toml", "text": text}


def _plans(current: Config, doc: transfer.Document, sections: list[str]) -> list[dict]:
    return [asdict(plan) for plan in transfer.plan_import(current, doc.config, sections)]


def plan_import(body: dict) -> dict:
    """What importing the file ``text`` would do to each of its sections (``pdms config import``'s table)."""
    doc = actions.read_export(_text(body, "text"))
    if not doc.sections:
        raise actions.ActionError(_("Nothing to import."))
    meta = {key: doc.meta.get(key) for key in ("exported_at", "cli_version", "secrets")}
    return {"meta": meta, "sections": doc.sections, "plans": _plans(Config.load(), doc, doc.sections),
            "first_setup": actions.first_setup()}


def apply_import(body: dict) -> dict:
    """Import the chosen sections of ``text``: new entries always, the ``overwrite`` ones (``[section, name]``) over
    the user's own, or exactly the file's content with ``replace``. Keeps a copy of the previous configuration."""
    doc = actions.read_export(_text(body, "text"))
    sections = [section for section in _sections(body) if section in doc.sections]
    if not sections:
        raise actions.ActionError(_("Nothing to import."))
    overwrite = body.get("overwrite", [])
    if not isinstance(overwrite, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in overwrite
    ):
        raise actions.ActionError("overwrite must be a list of [section, name] pairs")
    current = Config.load()
    chosen = {tuple(pair) for pair in overwrite}
    if actions.first_setup():
        chosen = {(plan["section"], name) for plan in _plans(current, doc, sections) for name in plan["changed"]}
    result = transfer.apply_import(current, doc.config, sections, chosen, replace=_bool(body, "replace"))
    if result.to_dict() == current.to_dict():
        return {"changed": False, "backup": None, "no_password": []}
    backup = actions.import_config(result)
    i18n.set_language(result.defaults.language)
    return {"changed": True, "backup": str(backup) if backup else None,
            "no_password": [name for name, db in result.dbs.items() if not db.password]}


def _limit(body: dict) -> int:
    limit = body.get("limit", 200)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise actions.ActionError("limit must be a number between 1 and 1000")
    return limit


def db_users(body: dict) -> dict:
    """The users of a database's ``pdms_user`` table, with the ``DEV_ROLES`` each would get (``pdms user import``)."""
    cfg = Config.load()
    mapping, source = userimport.role_mapping(repos.active_root(cfg))
    limit = _limit(body)
    found = actions.read_db_users(
        cfg, _text(body, "db"), mapping, search=_text(body, "search").strip(),
        role=userimport.internal_role(_text(body, "role"), mapping), inactive=_bool(body, "inactive"), limit=limit,
    )
    known = {user.user_id: name for name, user in cfg.users.items()}
    return {
        "source": "built-in" if source == "built-in" else "repo", "limited": len(found) == limit,
        "users": [
            {**asdict(user), "dev_roles": user.dev_roles(mapping), "unknown_roles": user.unknown_roles(mapping),
             "imported_as": known.get(user.user_id, "")}
            for user in found
        ],
    }


def import_db_users(body: dict) -> dict:
    """Add the picked users (as :func:`db_users` listed them), updating the ones already imported."""
    picked = body.get("users")
    if not isinstance(picked, list) or not all(isinstance(user, dict) for user in picked):
        raise actions.ActionError("users must be a list of users")
    users = []
    for user in picked:
        roles = user.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise actions.ActionError("roles must be a list of role names")
        users.append(userimport.DbUser(
            user_id=_text(user, "user_id"), username=_text(user, "username"), first_name=_text(user, "first_name"),
            last_name=_text(user, "last_name"), roles=roles, is_active=user.get("is_active") is True,
        ))
    if not users or not all(user.user_id and user.username for user in users):
        raise actions.ActionError(_("Nothing selected."))
    cfg = Config.load()
    mapping, _source = userimport.role_mapping(repos.active_root(cfg))
    added, updated = actions.import_users(cfg, users, mapping)
    return {"added": added, "updated": updated}
