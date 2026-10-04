"""A local copy of a database in pdms's own Postgres (:mod:`localdb`): ``pdms db local refresh`` and snapshots.

The refresh copies a source database (by default ``pdm_template_dev`` on the server of the ``web-dev`` alias: the
schema with its Flyway history and the reference data) into the ``pdms`` database of pdms's Postgres, then applies
the migrations of the repo it does not have yet, and registers the alias ``pdms-local`` for it. Only ``public`` is
copied: the ``configuration`` schema (which the dev user may not read) is built from the repo's own migrations
first, since public's views use it. The source is only
read: ``pg_dump`` runs in a throwaway container of the same Postgres image (nothing to install), on the host's
network so it goes through the same tunnel as the services.

Snapshots keep the copy as it is (``CREATE DATABASE … TEMPLATE``) to come back to it in seconds after a test that
changed it.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import IO

from . import actions, instances, localdb, migrations
from .config import Config, Database
from .i18n import _

ALIAS = "pdms-local"
SOURCE_ALIAS = "web-dev"
SOURCE_DATABASE = "pdm_template_dev"
# Copied from the source: public. configuration is created and filled by the repo's own migrations (510.01–05) and
# the dev user may not read it, so it is built from them in the copy; backup_data only keeps old migrations' backups.
COPIED_SCHEMAS = ("public",)
REBUILT_SCHEMAS = ("configuration",)
SNAPSHOT_PREFIX = "pdms_snap_"
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
    state_path().write_text(json.dumps(data, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------------------------- refresh


def source(cfg: Config, alias: str = SOURCE_ALIAS, database: str = SOURCE_DATABASE) -> Database:
    """The database to copy: ``database`` on the server of alias ``alias``, with its user and password."""
    actions.require(cfg.dbs, _("database"), alias)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,63}", database):
        raise actions.ActionError(_("'{name}' is not a database name.", name=database))
    return replace(cfg.dbs[alias], database=database)


def dump_command(db: Database) -> list[str]:
    """``docker run`` of pg_dump (custom format, to stdout) for ``db``; the password travels as PGPASSWORD."""
    host = db.host.strip()
    network: list[str] = []
    if sys.platform.startswith("linux"):
        network = ["--network", "host"]
        try:  # the host's /etc/hosts (a devo-cli tunnel: the RDS name → 127.0.0.6) is not the container's
            address = socket.gethostbyname(host)
        except OSError:
            address = ""
        if address and address != host:
            network += ["--add-host", f"{host}:{address}"]
    elif host.lower() in migrations.LOCAL_HOSTS:
        host = "host.docker.internal"  # Docker Desktop (macOS / Windows)
    return [
        "docker", "run", "--rm", *network, "-e", "PGPASSWORD", localdb.IMAGE,
        "pg_dump", "-h", host, "-p", str(db.port), "-U", db.user, "-d", db.database,
        "--format=custom", "--no-owner", "--no-acl", *(f"--schema={schema}" for schema in COPIED_SCHEMAS),
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


def rebuild_schemas(repo: Path, output: IO[str]) -> list[str]:
    """Create the schemas the copy does not take from the source, running their migrations in the copy."""
    ran = []
    for schema in REBUILT_SCHEMAS:
        for path in schema_migrations(repo, schema):
            output.write(f"$ psql -f {path.name}\n")
            output.flush()
            result = subprocess.run(
                ["docker", "exec", "-i", localdb.CONTAINER, "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", localdb.USER,
                 "-d", localdb.MAIN_DB, "-f", "-"],
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


def refresh(cfg: Config, output: IO[str], *, alias: str = SOURCE_ALIAS, database: str = SOURCE_DATABASE,
            migrations_repo: Path | None = None) -> dict:
    """Copy ``database`` (server of ``alias``) into the local copy, replacing it, then migrate it with the repo's
    Flyway migrations (when ``migrations_repo`` is given) and register its alias. What it did, also kept for
    ``pdms db local status`` and the Data screen."""
    src = source(cfg, alias, database)
    localdb.up()
    started = datetime.now().astimezone()
    output.write(_("Copying {database} from {host} into {target}…", database=database, host=src.host,
                   target=f"{localdb.CONTAINER}/{localdb.MAIN_DB}") + "\n")
    output.flush()
    # Replaced, not merged: the copy is what the source has. Its connections end (services reconnect).
    localdb.psql(f'drop database if exists "{localdb.MAIN_DB}" with (force)')
    localdb.psql(f'create database "{localdb.MAIN_DB}"')
    rebuilt = rebuild_schemas(migrations_repo, output) if migrations_repo else []  # before: public's views use them
    dump = subprocess.Popen(dump_command(src), stdout=subprocess.PIPE, stderr=output,
                            env={**os.environ, "PGPASSWORD": src.password})
    restore = subprocess.Popen(
        ["docker", "exec", "-i", localdb.CONTAINER, "pg_restore", "-U", localdb.USER, "-d", localdb.MAIN_DB,
         "--no-owner", "--no-acl"],
        stdin=dump.stdout, stdout=output, stderr=subprocess.STDOUT)
    assert dump.stdout is not None
    dump.stdout.close()  # pg_restore owns the pipe now: pg_dump gets SIGPIPE if pg_restore dies
    restored, dumped = restore.wait(), dump.wait()
    if dumped != 0:
        raise actions.ActionError(_("pg_dump of {database} failed (exit code {code}); the log says why.",
                                    database=database, code=dumped))
    if restored not in (0, 1):  # 1: warnings (e.g. an extension that already exists)
        raise actions.ActionError(_("pg_restore failed (exit code {code}); the log says why.", code=restored))
    migrated = None
    if migrations_repo:
        target = localdb.database(localdb.MAIN_DB, localdb.state()["port"] or localdb.PORT)
        code = _run(migrations.docker_command(migrations_repo, target, "migrate"), output,
                    {**os.environ, "DB_PASSWORD": target.password})
        if code != 0:
            raise actions.ActionError(_("The copy is there, but Flyway migrate failed (exit code {code}); the log "
                                        "says why.", code=code))
        migrated = str(migrations_repo)
    name = register_alias(cfg)
    result = {
        "alias": name, "source": f"{alias}:{database}", "host": src.host, "at": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now().astimezone() - started).total_seconds(), 1),
        "size": localdb.databases().get(localdb.MAIN_DB, 0), "migrated": migrated, "rebuilt": rebuilt,
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
    localdb.psql(f"select pg_terminate_backend(pid) from pg_stat_activity where datname = '{localdb.MAIN_DB}' "
                 "and pid <> pg_backend_pid()")
    localdb.psql(f'create database "{target}" template "{localdb.MAIN_DB}"')
    data = load_state()
    data.setdefault("snapshots", {})[name] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_state(data)
    return {"name": name, "size": localdb.databases().get(target, 0)}


def restore_snapshot(name: str) -> None:
    """Make the local copy what the snapshot keeps (the snapshot stays, to restore it again)."""
    source_db = _snapshot_db(name)
    if source_db not in localdb.databases():
        raise actions.ActionError(_("There is no snapshot '{name}'.", name=name))
    localdb.psql(f'drop database if exists "{localdb.MAIN_DB}" with (force)')
    localdb.psql(f'create database "{localdb.MAIN_DB}" template "{source_db}"')


def delete_snapshot(name: str) -> None:
    target = _snapshot_db(name)
    localdb.psql(f'drop database if exists "{target}" with (force)')
    data = load_state()
    data.get("snapshots", {}).pop(name, None)
    _save_state(data)
