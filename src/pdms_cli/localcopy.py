"""A local copy of a database in pdms's own Postgres (:mod:`localdb`): ``pdms db local refresh`` and snapshots.

The refresh copies a source database (by default ``pdm_template_dev`` on the server of the ``web-dev`` alias: the
schema with its Flyway history and the reference data) into the ``pdms`` database of pdms's Postgres, then applies
the migrations of the repo it does not have yet, and registers the alias ``pdms-local`` for it. ``public`` is
copied, and ``configuration`` too when the source's user may read all of it; otherwise ``configuration`` is built
from the repo's own migrations first, since public's views use it. The source is only
read: ``pg_dump`` runs in a throwaway container of the same Postgres image (nothing to install), on the host's
network so it goes through the same tunnel as the services.

Snapshots keep the copy as it is (``CREATE DATABASE … TEMPLATE``) to come back to it in seconds after a test that
changed it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import IO

from . import actions, docker, images, instances, localdb, migrations
from .config import Config, Database, write_json
from .i18n import _

ALIAS = "pdms-local"
SOURCE_ALIAS = "web-dev"
SOURCE_DATABASE = "pdm_template_dev"
# Copied from the source: public, and configuration when its user may read every table and sequence there; else
# configuration is built in the copy from the repo's own migrations that create and fill it (510.01–05).
# backup_data only keeps old migrations' backups and is never copied.
COPIED_SCHEMAS = ("public",)
REBUILT_SCHEMAS = ("configuration",)
SNAPSHOT_PREFIX = "pdms_snap_"
# Where a refresh or a snapshot restore builds the new copy: ``pdms`` is only replaced by it once it is complete, so a
# source that cannot be read (the tunnel down) or a failed restore leaves the copy as it was.
STAGING_DB = "pdms_refresh"
RESTORE_DB = "pdms_restore"  # the same for a snapshot restore, its own name so a refresh at once cannot empty it
SNAPSHOT_ATTEMPTS = 5  # a snapshot copies the copy while nobody uses it; services reconnect in the meantime
REFRESH_TIMEOUT = 3600  # seconds pg_dump | pg_restore may take: a stalled tunnel must not hang the refresh forever
SNAPSHOT_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")


def state_path() -> Path:
    return instances.state_dir() / "local-copy.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    write_json(state_path(), data, indent=1)


# ---------------------------------------------------------------------------------------------- refresh


def source(cfg: Config, alias: str = SOURCE_ALIAS, database: str = SOURCE_DATABASE) -> Database:
    """The database to copy: ``database`` on the server of alias ``alias``, with its user and password."""
    actions.require(cfg.dbs, _("database"), alias)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,63}", database):
        raise actions.ActionError(_("'{name}' is not a database name.", name=database))
    return replace(cfg.dbs[alias], database=database)


def unreadable(db: Database, schema: str, timeout: int = 30) -> list[str]:
    """What of ``schema`` the source's user may not read (tables and sequences, or the schema itself); [] if all."""
    from . import postgres

    with postgres.connect(db, timeout) as conn:
        conn.read_only = True
        if not conn.execute("select to_regnamespace(%s) is not null and has_schema_privilege(%s, 'USAGE')",
                            [f'"{schema}"', schema]).fetchone()[0]:
            return [schema]
        rows = conn.execute(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = %s and c.relkind in ('r', 'p', 'v', 'm', 'S') "
            "and not has_table_privilege(c.oid, 'SELECT') order by 1", [schema]).fetchall()
    return [f"{schema}.{name}" for (name,) in rows]


def schemas_to_copy(db: Database, output: IO[str]) -> tuple[list[str], list[str]]:
    """The schemas pg_dump takes from ``db`` and the ones to build from the repo's migrations instead."""
    copied, rebuilt = list(COPIED_SCHEMAS), []
    for schema in REBUILT_SCHEMAS:
        try:
            missing = unreadable(db, schema)
        except Exception as exc:  # noqa: BLE001 - pg_dump says it better if the source cannot be reached at all
            missing = [str(exc).splitlines()[0] if str(exc) else type(exc).__name__]
        if missing:
            output.write(_("{schema} is built from the repo's migrations: {user} may not read {what}.", schema=schema,
                           user=db.user, what=", ".join(missing)) + "\n")
            rebuilt.append(schema)
        else:
            copied.append(schema)
    return copied, rebuilt


def dump_command(db: Database, schemas: tuple[str, ...] | list[str] = COPIED_SCHEMAS, name: str = "") -> list[str]:
    """``docker run`` of pg_dump (custom format, to stdout) for ``db``; the password travels as PGPASSWORD. ``name``:
    the container's, to remove it if it has to be stopped."""
    host, network = docker.reach(db.host)
    return [
        "docker", "run", "--rm", *(["--name", name] if name else []), *network, "-e", "PGPASSWORD", localdb.IMAGE,
        "pg_dump", "-h", host, "-p", str(db.port), "-U", db.user, "-d", db.database,
        "--format=custom", "--no-owner", "--no-acl", *(f"--schema={schema}" for schema in schemas),
        "--extension=*",  # with --schema, extensions (pg_trgm for the gin indexes) would be left out
    ]


def schema_migrations(repo: Path, schema: str) -> list[Path]:
    """The repo's versioned migrations that create and fill ``schema`` (``…__create_configuration_schema.sql``,
    ``…__seed_configuration_nom_status_data.sql``), in version order."""
    found = []
    for path in repo.glob("migrations/versioned/**/*.sql"):
        match = migrations.MIGRATION.match(path.name)
        if match and (f"{schema}_" in match["description"] or f"_{schema}" in match["description"]):
            found.append((migrations.version_key(match["version"]), path))
    return [path for _key, path in sorted(found)]


def rebuild_schemas(repo: Path, output: IO[str], schemas: tuple[str, ...] | list[str] = REBUILT_SCHEMAS,
                    database: str = localdb.MAIN_DB) -> list[str]:
    """Create the schemas the copy does not take from the source, running their migrations in ``database``."""
    ran = []
    for schema in schemas:
        for path in schema_migrations(repo, schema):
            output.write(f"$ psql -f {path.name}\n")
            output.flush()
            result = subprocess.run(
                ["docker", "exec", "-i", localdb.CONTAINER, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", localdb.USER,
                 "-d", database, "-f", "-"],
                input=path.read_bytes(), stdout=output, stderr=subprocess.STDOUT, timeout=600)
            if result.returncode != 0:
                raise actions.ActionError(_("{file} failed in the copy (exit code {code}); the log says why.",
                                            file=path.name, code=result.returncode))
            ran.append(path.name)
    return ran


def using_copy(cfg: Config) -> list[str]:
    """The running instances whose database is the local copy (its connections end when it is replaced)."""
    copy = localdb.database(localdb.MAIN_DB, localdb.state()["port"] or localdb.PORT)
    aliases = {name for name, db in cfg.dbs.items()
               if migrations.is_local(db) and (db.port, db.database) == (copy.port, copy.database)}
    return [inst.key for inst in instances.load().values() if inst.db in aliases and inst.alive()]


def alias(cfg: Config, port: int = 0) -> str:
    """The alias of the local copy, or "" when there is none yet (``port``: known already, no docker call)."""
    port = port or localdb.state()["port"] or localdb.PORT
    return next((name for name, db in cfg.dbs.items()
                 if migrations.is_local(db) and (db.port, db.database) == (port, localdb.MAIN_DB)), "")


def register_alias(cfg: Config) -> str:
    """The alias of the local copy (``pdms-local``, created the first time)."""
    copy = localdb.database(localdb.MAIN_DB, localdb.state()["port"] or localdb.PORT)
    for name, db in cfg.dbs.items():
        if migrations.is_local(db) and (db.port, db.database) == (copy.port, copy.database):
            return name
    name = ALIAS if ALIAS not in cfg.dbs else f"{ALIAS}-{copy.port}"
    return actions.save_db(cfg, name, copy, new=True)


def _run(cmd: list[str], output: IO[str], env: dict[str, str] | None = None) -> int:
    output.write("$ " + " ".join(cmd[:4]) + " …\n")
    output.flush()
    return subprocess.run(cmd, stdout=output, stderr=subprocess.STDOUT, env=env, timeout=3600).returncode


def pending_after_copy(cfg: Config, repo: Path, alias: str = SOURCE_ALIAS,
                       database: str = SOURCE_DATABASE) -> actions.Unmigrated | None:
    """What of the checkout the copy will lack once copied: the source's own Flyway history against the repo's files
    (read-only). None when nothing; raises when the source cannot be read."""
    src = source(cfg, alias, database)
    known = migrations.status(repo, src, cfg.defaults.db_timeout)
    target = localdb.database(localdb.MAIN_DB, localdb.state()["port"] or localdb.PORT)
    item = actions.behind_of(register_name(cfg), target, repo, known) if known["flyway"] else None
    return actions.Unmigrated(item.alias, target, repo, item.versions, [], item.uncommitted) \
        if item and item.versions else None  # changed ones are the source's business: it was copied as it is


def register_name(cfg: Config) -> str:
    """The alias the local copy has, or will get (see :func:`register_alias`)."""
    return alias(cfg) or ALIAS


def _drop_staging() -> None:
    try:
        localdb.psql(f'drop database if exists "{STAGING_DB}" with (force)')
    except actions.ActionError:  # the error that got here says more; the next refresh drops it anyway
        pass


def _swap_in(built: str) -> None:
    """Make ``built`` the local copy: drop ``pdms`` (its connections end) and give ``built`` its name."""
    localdb.psql(f'drop database if exists "{localdb.MAIN_DB}" with (force)')
    localdb.psql(f'alter database "{built}" rename to "{localdb.MAIN_DB}"')


def _dump_and_restore(src: Database, schemas: list[str], output: IO[str], database: str,
                      timeout: float = REFRESH_TIMEOUT) -> None:
    """pg_dump of ``src`` piped into pg_restore in :data:`STAGING_DB`. Raises when either fails, when pg_restore
    reports errors other than harmless ones (:func:`localdb.restore_problems`), or after ``timeout`` seconds."""
    name = f"pdms-dump-{os.getpid()}"
    dump = subprocess.Popen(dump_command(src, schemas, name), stdout=subprocess.PIPE, stderr=output,
                            env={**os.environ, "PGPASSWORD": src.password})
    restore = subprocess.Popen(
        ["docker", "exec", "-i", localdb.CONTAINER, "pg_restore", "-U", localdb.USER, "-d", STAGING_DB,
         "--no-owner", "--no-acl"],
        stdin=dump.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    assert dump.stdout is not None and restore.stdout is not None
    dump.stdout.close()  # pg_restore owns the pipe now: pg_dump gets SIGPIPE if pg_restore dies
    timed_out = threading.Event()

    def stop() -> None:
        for process in (restore, dump):
            if process.poll() is None:
                process.kill()
        try:  # the container of pg_dump keeps running once its docker client is gone
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass

    def too_long() -> None:
        timed_out.set()
        stop()

    timer = threading.Timer(timeout, too_long)
    timer.daemon = True
    timer.start()
    said = []
    try:
        for line in restore.stdout:  # shown as it comes, and kept to tell harmless errors from real ones
            output.write(line)
            output.flush()
            said.append(line)
        restored, dumped = restore.wait(), dump.wait()
    except BaseException:  # Ctrl+C, or the log could not be written: nothing is left running
        stop()
        raise
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise actions.ActionError(_("Copying {database} took over {minutes} minutes and was stopped: is the tunnel "
                                    "still up?", database=database, minutes=round(timeout / 60)))
    if dumped != 0:
        raise actions.ActionError(_("pg_dump of {database} failed (exit code {code}); the log says why.",
                                    database=database, code=dumped))
    problems = localdb.restore_problems("".join(said))
    if restored not in (0, 1) or problems:
        raise actions.ActionError(_("pg_restore failed (exit code {code}); the log says why.", code=restored)
                                  + (f" {problems[0][:200]}" if problems else ""))


def refresh(cfg: Config, output: IO[str], *, alias: str = SOURCE_ALIAS, database: str = SOURCE_DATABASE,
            migrations_repo: Path | None = None, migrate: bool = True) -> dict:
    """Copy ``database`` (server of ``alias``) into the local copy, replacing it, then migrate it with the repo's
    Flyway migrations (with ``migrations_repo``, when ``migrate``) and register its alias. What it did, also kept for
    ``pdms db local status`` and the Data screen."""
    src = source(cfg, alias, database)
    images.require([localdb.IMAGE, *([migrations.IMAGE] if migrations_repo else [])])
    localdb.up()
    started = datetime.now().astimezone()
    output.write(_("Copying {database} from {host} into {target}…", database=database, host=src.host,
                   target=f"{localdb.CONTAINER}/{localdb.MAIN_DB}") + "\n")
    output.flush()
    # Built aside and swapped in at the end: until then the copy stays as it was, whatever fails.
    localdb.psql(f'drop database if exists "{STAGING_DB}" with (force)')  # a refresh that was cut short
    localdb.psql(f'create database "{STAGING_DB}"')
    try:
        copied, to_rebuild = schemas_to_copy(src, output)
        # Before the restore: public's views use them.
        rebuilt = rebuild_schemas(migrations_repo, output, to_rebuild, STAGING_DB) \
            if migrations_repo and to_rebuild else []
        _dump_and_restore(src, copied, output, database)
    except BaseException:
        _drop_staging()
        raise
    # Replaced, not merged: the copy is what the source has. Its connections end (services reconnect).
    _swap_in(STAGING_DB)
    migrated = None
    if migrations_repo and migrate:
        target = localdb.database(localdb.MAIN_DB, localdb.state()["port"] or localdb.PORT)
        try:
            actions.apply_migrations(actions.Unmigrated(register_name(cfg), target, migrations_repo, ["*"]), output)
        except actions.ActionError as exc:
            raise actions.ActionError(_("The copy is there, but: {error}", error=exc.message)) from exc
        migrated = str(migrations_repo)
    name = register_alias(cfg)
    result = {
        "alias": name, "source": f"{alias}:{database}", "host": src.host, "at": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now().astimezone() - started).total_seconds(), 1),
        "size": localdb.databases().get(localdb.MAIN_DB, 0), "migrated": migrated, "copied": copied, "rebuilt": rebuilt,
    }
    _save_state({**load_state(), "refresh": result})
    output.write(_("Done: {alias} ({size:.1f} MB).", alias=name, size=result["size"] / 1048576) + "\n")
    return result


# ---------------------------------------------------------------------------------------------- snapshots


def _snapshot_db(name: str) -> str:
    if not SNAPSHOT_NAME.fullmatch(name):
        raise actions.ActionError(_("A snapshot name has lowercase letters, digits and _ (up to 40): '{name}' "
                                    "is not one.", name=name))
    return f"{SNAPSHOT_PREFIX}{name}"


def snapshots() -> list[dict]:
    """The snapshots of the local copy: name, size and when they were taken (newest first)."""
    taken = load_state().get("snapshots", {})
    found = [{"name": db[len(SNAPSHOT_PREFIX):], "size": size, "at": taken.get(db[len(SNAPSHOT_PREFIX):], "")}
             for db, size in localdb.databases().items() if db.startswith(SNAPSHOT_PREFIX)]
    return sorted(found, key=lambda snap: snap["at"], reverse=True)


def save_snapshot(name: str) -> dict:
    """Keep the local copy as it is now. Its open connections end for a moment (Postgres copies a database only
    while nobody uses it); services reconnect by themselves."""
    target = _snapshot_db(name)
    if target in localdb.databases():
        raise actions.ActionError(_("There is a snapshot '{name}' already.", name=name))
    for attempt in range(SNAPSHOT_ATTEMPTS):
        localdb.psql(f"select pg_terminate_backend(pid) from pg_stat_activity where datname = '{localdb.MAIN_DB}' "
                     "and pid <> pg_backend_pid()")
        try:
            localdb.psql(f'create database "{target}" template "{localdb.MAIN_DB}"')
            break
        except actions.ActionError as exc:  # a service reconnected in between: its connections end again
            if "being accessed by other users" not in exc.message or attempt == SNAPSHOT_ATTEMPTS - 1:
                raise
            time.sleep(0.3)
    data = load_state()
    data.setdefault("snapshots", {})[name] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_state(data)
    return {"name": name, "size": localdb.databases().get(target, 0)}


def restore_snapshot(name: str) -> None:
    """Make the local copy what the snapshot keeps (the snapshot stays, to restore it again)."""
    source_db = _snapshot_db(name)
    if source_db not in localdb.databases():
        raise actions.ActionError(_("There is no snapshot '{name}'.", name=name))
    # Copied aside first: a copy that fails (the snapshot in use) leaves the local copy as it was.
    localdb.psql(f'drop database if exists "{RESTORE_DB}" with (force)')
    localdb.psql(f'create database "{RESTORE_DB}" template "{source_db}"')
    _swap_in(RESTORE_DB)


def delete_snapshot(name: str) -> None:
    target = _snapshot_db(name)
    localdb.psql(f'drop database if exists "{target}" with (force)')
    data = load_state()
    data.get("snapshots", {}).pop(name, None)
    _save_state(data)
