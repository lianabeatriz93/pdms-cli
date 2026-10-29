"""The Data screen: pdms's Postgres, the local copy, migrations, test databases."""

from __future__ import annotations

import threading
from pathlib import Path

from ... import actions, images, instances, localcopy, localdb, migrations, testruns
from ...config import Config
from ...i18n import _
from . import core, services, settings_screen
from .core import DATA_KEY, IMAGES_KEY, TEST_DBS_KEY, Job
from .services import ServiceJobs
from .testdbs import TestDbJobs

__all__ = ["DataJobs", "data_action", "data_info", "migration_status"]


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
        "tests": [{"name": name, "size": sizes.get(name, 0), "migrations": testruns.migration_state(cfg, name)}
                  for name in localdb.test_databases(sizes)] if sizes else [],
        "test_count": localdb.TEST_COUNT,
    }


def data_action(verb: str, body: dict) -> dict:
    """Snapshots and test databases: quick, so answered right away."""
    name = settings_screen._text(body, "name")
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
    name = actions.require(cfg.dbs, _("database"), settings_screen._text(body, "db"))
    repo = actions.current_migrations_repo(cfg)
    try:
        return {"db": name, **migrations.status(repo, cfg.dbs[name], cfg.defaults.db_timeout)}
    except (OSError, ValueError) as exc:
        raise actions.ActionError(_("Could not read {path}: {error}", path=repo, error=exc)) from exc
    except Exception as exc:  # noqa: BLE001 - the driver's errors (no VPN, wrong password...)
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise actions.ActionError(_("Could not query {name}: {error}", name=name, error=first)) from exc


class DataJobs(ServiceJobs, TestDbJobs):
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
            log = core.install_log(IMAGES_KEY)
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
        behind = actions.behind_of(name, database, repo, known) if known["flyway"] else None
        if not known["flyway"]:
            raise actions.ActionError(_("{name} has no Flyway history: its tables were made another way, and Flyway "
                                        "would run every migration over them. pdms only applies migrations to a "
                                        "database Flyway already manages.", name=name))
        images.require([migrations.IMAGE])
        key = f"migrate:{name}"

        def work(job: Job) -> None:
            log = core.install_log(key)
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase(job, f"migrating {name}", installed=True, log_key=key)
            if behind is None:
                return
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.apply_migrations(behind, output)

        return self.run(key, "migrate", f"migrating {name}", work)

    def restore_snapshot(self, name: str) -> Job:
        """Stop the running services that use the local copy, restore the snapshot, start them again as they were."""
        cfg = Config.load()
        if name not in {snap["name"] for snap in localcopy.snapshots()}:
            raise actions.ActionError(_("There is no snapshot '{name}'.", name=name))
        using = [services.find(key) for key in localcopy.using_copy(cfg)]

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
                    host=inst.host, reload=inst.reload, events_mode="local" if inst.events == "local" else None,
                    on_demand=inst.on_demand), awake=True)

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
        alias = settings_screen._text(body, "from", localcopy.SOURCE_ALIAS)
        database = settings_screen._text(body, "database", localcopy.SOURCE_DATABASE)
        localcopy.source(cfg, alias, database)  # an unknown alias or a bad name is said now, not in the job
        using = localcopy.using_copy(cfg) if localdb.state()["running"] else []
        if using and body.get("confirmed") is not True:
            raise actions.Decision(_("{n} running services use the local copy ({keys}): their connections end while "
                                     "it is replaced.", n=len(using), keys=", ".join(using)))
        answer = body.get("migrate")
        if answer not in (None, True, False):
            raise actions.ActionError("migrate must be true, false or null (ask)")
        try:
            repo = actions.current_migrations_repo(cfg)
        except actions.ActionError:
            repo = None  # no migrations repo: copied as the source has it
        migrate = bool(repo) and answer is not False
        if repo and answer is None:
            try:
                behind = localcopy.pending_after_copy(cfg, repo, alias, database)
            except Exception:  # noqa: BLE001 - the source cannot be read now: migrated as before, without asking
                behind = None
            if behind:
                raise actions.MigrationsPending([behind])
        images.require([localdb.IMAGE, *([migrations.IMAGE] if migrate else [])])

        def work(job: Job) -> None:
            log = core.install_log(DATA_KEY)
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase(job, "copying", installed=True, log_key=DATA_KEY)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                localcopy.refresh(cfg, output, alias=alias, database=database, migrations_repo=repo, migrate=migrate)

        return self.run(DATA_KEY, "refresh", "copying", work)

    def prepare_test_dbs(self, names: list[str]) -> Job:
        """Make test databases copies of the local copy and apply what they lack of the checkout, one after the
        other (Data → Prepare from the local copy)."""
        for name in names:
            testruns.require_test_db(name)
        if not names:
            raise actions.ActionError("names must be the test databases to prepare")
        if localdb.MAIN_DB not in localdb.databases():
            raise actions.ActionError(_("There is no local copy yet: make it first (Data → Make the local copy, or "
                                        "pdms db local refresh)."))
        images.require([migrations.IMAGE])

        def work(job: Job) -> None:
            log = core.install_log(TEST_DBS_KEY)
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                for name in names:
                    self.phase(job, f"preparing {name}", installed=True, log_key=TEST_DBS_KEY)
                    testruns.prepare_from_copy(Config.load(), name, output)

        return self.run(TEST_DBS_KEY, "prepare", f"preparing {names[0]}", work)

    def migrate_test_dbs(self, names: list[str]) -> Job:
        """Apply what test databases with Flyway's history lack of the checkout (Data → Apply)."""
        for name in names:
            testruns.require_test_db(name)
        images.require([migrations.IMAGE])

        def work(job: Job) -> None:
            log = core.install_log(TEST_DBS_KEY)
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                for name in names:
                    self.phase(job, f"migrating {name}", installed=True, log_key=TEST_DBS_KEY)
                    testruns.migrate_test_db(Config.load(), name, output)

        return self.run(TEST_DBS_KEY, "migrate", f"migrating {names[0]}", work)
