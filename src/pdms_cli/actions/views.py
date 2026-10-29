"""Test copies of materialized views (testviews.py), and the migrations they wait for."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .. import migrations, repos, testviews
from ..config import Config
from ..i18n import _
from . import checks, repo_settings
from .errors import ActionError, Decision, TestViewsPending

__all__ = [
    "TestViewNeed", "_test_view_call", "check_test_views", "create_test_view", "drop_test_view",
    "prepare_test_views", "refresh_test_view", "test_view_needs", "test_views", "use_test_view",
]


def _test_view_call(work: Callable):
    try:
        return work()
    except (Decision, ActionError):
        raise  # the user answers (a protected database), or it says what is wrong already
    except testviews.TestViewError as exc:
        raise ActionError(exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - the driver's errors (cannot connect...)
        raise ActionError(str(exc).strip()[:300]) from exc


def test_views(cfg: Config, db_alias: str = "", suffix: str = "") -> dict:
    """The materialized views of the checkout and their test copies (see :mod:`testviews`)."""
    db_alias = checks.require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    repo = repo_settings.current_migrations_repo(cfg)
    return _test_view_call(lambda: testviews.overview(cfg, repo, db_alias, suffix, cfg.defaults.db_timeout))


def create_test_view(cfg: Config, view: str, db_alias: str = "", suffix: str = "", confirmed: bool = False) -> dict:
    """Make the test copy of ``view`` in ``db_alias``; when its user cannot create there, the SQL to run in a database
    client instead (``{"created": False, "sql", "file"}``). A protected database asks first."""
    db_alias = checks.require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    repo = repo_settings.current_migrations_repo(cfg)

    def work() -> dict:
        found = suffix or testviews.ticket_suffix(testviews.branch(repo))
        if not found:
            raise testviews.TestViewError(_("The branch of {repo} names no ticket (PDMP-123): give a suffix.",
                                            repo=repo.name))
        db = cfg.dbs[db_alias]
        made, sql = testviews.test_sql(repo, view, testviews.check_suffix(found), db.user)
        if not testviews.can_create(db, cfg.defaults.db_timeout):
            return {"created": False, "name": made.name, "sql": sql, "file": str(testviews.sql_file(made, sql))}
        checks.check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        return {"created": True, "name": made.name}

    return _test_view_call(work)


def use_test_view(cfg: Config, view: str, name: str, db_alias: str = "") -> None:
    """Services on ``db_alias`` read ``name`` instead of ``view`` from their next start; it must be there already."""
    db_alias = checks.require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))

    def work() -> None:
        db = cfg.dbs[db_alias]
        if name not in testviews.existing(db, [name], cfg.defaults.db_timeout):
            raise testviews.TestViewError(_("{name} is not in {db} yet: create it first (or run its SQL).", name=name,
                                            db=db_alias))
        if not testviews.readable(db, name, cfg.defaults.db_timeout):
            raise testviews.TestViewError(_("{user} cannot read {name}: run in your database client: {sql}",
                                            user=db.user, name=name, sql=testviews.grant_sql(name, db.user)))
        testviews.use(cfg, view, name, db_alias)

    _test_view_call(work)


def drop_test_view(cfg: Config, view: str, name: str, db_alias: str = "", confirmed: bool = False) -> dict:
    """Services read the real ``view`` again, and its test copy goes; when the user cannot drop it, the SQL to run
    (``{"dropped": False, "sql"}``)."""
    db_alias = checks.require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    if not name.startswith(view + "_") or name == view:
        raise ActionError(_("{name} is not a test copy of {view}.", name=name, view=view))
    testviews.stop_using(cfg, view)

    def work() -> dict:
        db = cfg.dbs[db_alias]
        sql = testviews.drop_sql(name)
        there = testviews.existing(db, [name], cfg.defaults.db_timeout)
        if name not in there:
            testviews.forget(name)
            return {"dropped": True, "name": name}
        if there[name] != db.user:  # only its owner drops it (made from a database client, as root...)
            return {"dropped": False, "name": name, "sql": sql}
        checks.check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        testviews.forget(name)
        return {"dropped": True, "name": name}

    return _test_view_call(work)


def refresh_test_view(cfg: Config, view: str, name: str, db_alias: str = "", confirmed: bool = False) -> dict:
    """Recompute the data of a test copy (a snapshot of when it was made; nothing else refreshes it). Only its owner
    can: anyone else gets the SQL to run (``{"refreshed": False, "sql"}``)."""
    db_alias = checks.require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    if not name.startswith(view + "_") or name == view:
        raise ActionError(_("{name} is not a test copy of {view}.", name=name, view=view))

    def work() -> dict:
        db = cfg.dbs[db_alias]
        sql = testviews.refresh_sql(name)
        there = testviews.existing(db, [name], cfg.defaults.db_timeout)
        if name not in there:
            raise testviews.TestViewError(_("{name} is not in {db}.", name=name, db=db_alias))
        if there[name] != db.user:
            return {"refreshed": False, "name": name, "sql": sql}
        checks.check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        return {"refreshed": True, "name": name}

    return _test_view_call(work)



@dataclass
class TestViewNeed:
    """A view a service about to start reads, changed in the checkout: its test copy in ``db_alias`` and what it
    takes (``exists``: there already; ``current``: made with the SQL your file has now)."""

    view: str
    name: str
    db_alias: str
    services: list[str]
    exists: bool
    current: bool
    can_create: bool
    uncommitted: bool
    sql: str

    @property
    def ready(self) -> bool:
        return self.exists and self.current

    def describe(self) -> dict:
        return {"view": self.view, "name": self.name, "db": self.db_alias, "services": self.services,
                "exists": self.exists, "current": self.current, "can_create": self.can_create,
                "uncommitted": self.uncommitted, "sql": "" if self.ready or self.can_create else self.sql}


def test_view_needs(cfg: Config, launches: Iterable[tuple[Path, str]]) -> list[TestViewNeed]:
    """The views the services of ``launches`` (``(service, db alias)``) read that your checkout changed, on shared
    databases (a local one is migrated instead). Quiet when it cannot tell (no migrations repo, a database down)."""
    by_db: dict[str, list[Path]] = {}
    for service, alias in launches:
        db = cfg.dbs.get(alias)
        if db is not None and not migrations.is_local(db):
            by_db.setdefault(alias, []).append(service)
    if not by_db:
        return []
    try:
        repo = repo_settings.current_migrations_repo(cfg)
    except ActionError:
        return []
    suffix = testviews.ticket_suffix(testviews.branch(repo))
    needs = []
    for alias, services in by_db.items():
        db = cfg.dbs[alias]
        try:
            changed = testviews.modified(repo, db, cfg.defaults.db_timeout)
            if not changed or not suffix:
                continue
            backend = repos.active_backend(cfg)
            readers: dict[str, list[str]] = {}
            for service in dict.fromkeys(services):
                read = testviews.reads(service, backend) if backend else set()
                for view in testviews.narrowed(service.name, read):
                    if view in changed:
                        readers.setdefault(view, []).append(service.name)
            if not readers:
                continue
            copies = {view: testviews.test_sql(repo, view, suffix, db.user) for view in readers}
            there = testviews.existing(db, [made.name for made, _sql in copies.values()], cfg.defaults.db_timeout)
            can = testviews.can_create(db, cfg.defaults.db_timeout)
        except Exception:  # noqa: BLE001 - a database down, an odd file: the services start on the real views
            continue
        for view, (made, sql) in sorted(copies.items()):
            needs.append(TestViewNeed(view, made.name, alias, readers[view], made.name in there,
                                      testviews.made_with(made.name, sql), can, changed[view], sql))
    return needs


def check_test_views(cfg: Config, launches: Iterable[tuple[Path, str]], views: bool | None) -> list[TestViewNeed]:
    """What to prepare before the services start: :class:`TestViewsPending` for the user to answer when ``views``
    is None, the needs when True (a copy they cannot create must be there by now: they ran its SQL), none when False
    (and the services read the real views)."""
    needs = test_view_needs(cfg, launches)
    if not needs:
        return []
    if views is None:
        raise TestViewsPending(needs)
    if views is False:
        for need in needs:
            testviews.stop_using(cfg, need.view)
        return []
    missing = [need.name for need in needs if not need.can_create and not need.exists]
    if missing:
        raise ActionError(_("{names} is not in the database yet: run its SQL first.", names=", ".join(missing)))
    for need in needs:
        db = cfg.dbs[need.db_alias]
        if need.exists and not _test_view_call(lambda need=need, db=db: testviews.readable(db, need.name)):
            raise ActionError(_("{user} cannot read {name}: run in your database client: {sql}", user=db.user,
                                name=need.name, sql=testviews.grant_sql(need.name, db.user)))
    return needs


def prepare_test_views(cfg: Config, needs: list[TestViewNeed], output: IO[str] | None = None) -> None:
    """Make the copies that are missing or older than your SQL (when the user can), remember with which SQL, and
    have the services read them from their start."""
    for need in needs:
        if not need.ready and need.can_create:
            if output:
                output.write(_("Making {name} in {db}...", name=need.name, db=need.db_alias) + "\n")
                output.flush()
            _test_view_call(lambda need=need: testviews.run(cfg.dbs[need.db_alias], need.sql, 3600))
        testviews.remember(need.name, need.db_alias, need.sql)  # made now, or the user ran this SQL
        _test_view_call(lambda need=need: testviews.use(cfg, need.view, need.name, need.db_alias))
