"""Flyway migrations (the pdms-db-migrations repo), run with the same Docker image as the CodePipeline.

Since PDMP-467 the schema is managed by Flyway in its own repository; the Alembic migrations of
``backend/common/sync-database`` are frozen. From pdms only the read-only ``info`` and ``validate`` can run against
any database; ``migrate`` only against a local one (shared databases are migrated by the pipeline).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .config import Database

# public.ecr.aws, no authentication; the image flyway_migrate.yml / flyway_validate.yml run on (Flyway 11.20.2).
IMAGE = "public.ecr.aws/w3g2g9j8/epicride/flyway-postgres:11.20.2-1"
READ_ONLY = ("info", "validate")
COMMANDS = (*READ_ONLY, "migrate")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def is_migrations_repo(path: Path) -> bool:
    return (path / "flyway.toml").is_file() and (path / "migrations").is_dir()


def find_upwards(start: Path) -> Path | None:
    start = start.resolve()
    for path in (start, *start.parents):
        if is_migrations_repo(path):
            return path
    return None


def siblings(root: Path) -> list[Path]:
    """Migration repos next to a PDMS checkout (e.g. ~/Code/Alivi/pdms-db-migrations)."""
    try:
        return sorted(p for p in root.resolve().parent.iterdir() if p.is_dir() and is_migrations_repo(p))
    except OSError:
        return []


def _suffix(name: str) -> str:
    """Version-like suffix of a folder name: pdms_v2 -> v2, pdms-db-migrations-v2 -> v2, pdms -> ''."""
    match = re.search(r"[-_.](v?\d+|next|new|old)$", name.lower())
    return match.group(1) if match else ""


def best_match(root: Path, candidates: list[Path]) -> Path | None:
    """The candidate whose suffix matches the PDMS checkout's (pdms_v2 <-> pdms-db-migrations-v2), if exactly one."""
    matching = [c for c in candidates if _suffix(c.name) == _suffix(root.name)]
    return matching[0] if len(matching) == 1 else None


def is_local(db: Database) -> bool:
    return db.host.strip().lower() in LOCAL_HOSTS and not db.protected


def image_present() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pull_image(attempts: int = 3, wait: float = 10) -> tuple[bool, str]:
    """Download the image (about 360 MB, once), retrying: public ECR throttles anonymous pulls."""
    last = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(["docker", "pull", IMAGE], capture_output=True, text=True)
        if result.returncode == 0:
            return True, ""
        last = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        last = last[0]
        if attempt < attempts:
            time.sleep(wait * attempt)
    return False, last


def docker_command(repo: Path, db: Database, command: str) -> list[str]:
    """``docker run`` for a Flyway command. The password travels as an environment variable, never in argv."""
    host, network = db.host, []
    if host.strip().lower() in LOCAL_HOSTS:
        if sys.platform.startswith("linux"):
            network = ["--network", "host"]  # the container shares the host's localhost
        else:
            host = "host.docker.internal"  # Docker Desktop (macOS / Windows)
    config_files = ",".join(f for f in ("flyway.toml", "placeholder.toml") if (repo / f).is_file())
    extra = ["-ignoreMigrationPatterns=*:pending"] if command == "validate" else []  # as flyway_validate.sh
    return [
        "docker", "run", "--rm", "--entrypoint", "flyway", *network,
        "-v", f"{repo.resolve()}:/work", "-w", "/work",
        "-e", f"DB_HOST={host}", "-e", f"DB_PORT={db.port}", "-e", f"DB_NAME={db.database}",
        "-e", f"DB_USER={db.user}", "-e", "DB_PASSWORD",
        IMAGE, f"-configFiles={config_files}", "-environment=local", *extra, command,
    ]


# --------------------------------------------------------------------------- status, read-only and without Docker

MIGRATION = re.compile(r"^(?P<kind>[VR])(?P<version>[^_]*)__(?P<description>.+)\.sql$")
LINE_BREAK = re.compile(r"\r\n|\r|\n")


@dataclass
class FlywaySettings:
    locations: list[Path]
    table: str = "flyway_schema_history"
    schema: str = "public"


def settings(repo: Path) -> FlywaySettings:
    """The locations, history table and schema of ``flyway.toml`` (its local environment's schema)."""
    data = tomlkit.parse((repo / "flyway.toml").read_text(encoding="utf-8")).unwrap()
    flyway = data.get("flyway", {})
    locations = [repo / str(item).removeprefix("filesystem:") for item in flyway.get("locations", ["migrations"])
                 if not str(item).startswith("classpath:")]
    schemas = data.get("environments", {}).get("local", {}).get("schemas") or flyway.get("schemas") or ["public"]
    return FlywaySettings(locations, str(flyway.get("table", "flyway_schema_history")), str(schemas[0]))


def version_key(version: str) -> tuple[int, ...]:
    """Flyway compares versions part by part as numbers (``.`` and ``_`` separate them)."""
    return tuple(int(part) for part in re.split(r"[._]", version) if part.isdigit())


def checksum(text: str) -> int:
    """Flyway's checksum of a migration: CRC32 of its lines without line breaks (and without a BOM), signed."""
    lines = LINE_BREAK.split(text.removeprefix("﻿"))
    if lines and lines[-1] == "":
        lines.pop()  # Java's readLine gives no empty line after the last line break
    crc = 0
    for line in lines:
        crc = zlib.crc32(line.encode("utf-8"), crc)
    return crc - (1 << 32) if crc >= 1 << 31 else crc


def files(repo: Path, flyway: FlywaySettings | None = None) -> list[dict]:
    """The migrations of the repo: ``{kind, version, description, script, checksum}`` (script relative to its
    location, as Flyway records it)."""
    found = []
    for location in (flyway or settings(repo)).locations:
        for path in sorted(location.rglob("*.sql")) if location.is_dir() else []:
            match = MIGRATION.match(path.name)
            if not match:
                continue
            found.append({
                "kind": "versioned" if match["kind"] == "V" else "repeatable", "version": match["version"],
                "description": match["description"].replace("_", " "), "script": path.relative_to(location).as_posix(),
                "checksum": checksum(path.read_text(encoding="utf-8", errors="replace")),
            })
    return found


def history(db: Database, flyway: FlywaySettings, timeout: int) -> list[dict] | None:
    """The rows of Flyway's history table, oldest first; None when Flyway never ran on this database."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password,
                         connect_timeout=timeout) as conn:
        conn.read_only = True
        table = sql.Identifier(flyway.schema, flyway.table)
        if conn.execute("select to_regclass(%s)", [f'"{flyway.schema}"."{flyway.table}"']).fetchone()[0] is None:
            return None
        rows = conn.execute(sql.SQL(
            "select version, description, type, script, checksum, installed_on, success from {} order by installed_rank"
        ).format(table)).fetchall()
    keys = ("version", "description", "type", "script", "checksum", "installed_on", "success")
    return [dict(zip(keys, row)) for row in rows]


def status(repo: Path, db: Database, timeout: int = 15) -> dict:
    """What ``flyway info`` would say, read straight from the history table and the repo's files.

    States: ``applied``, ``pending``, ``failed``, ``outdated`` (a repeatable changed since it ran: it runs again) and
    ``missing`` (applied, but not in this checkout: another branch, or an old checkout)."""
    flyway = settings(repo)
    local = files(repo, flyway)
    rows = history(db, flyway, timeout)
    applied = [row for row in rows or [] if row["type"] not in ("BASELINE", "SCHEMA", "DELETE")]
    by_version = {version_key(row["version"]): row for row in applied if row["version"]}
    last_repeatable = {row["description"]: row for row in applied if not row["version"]}
    items = []
    for item in local:
        row = (by_version.get(version_key(item["version"])) if item["kind"] == "versioned"
               else last_repeatable.get(item["description"]))
        if row is None:
            state = "pending"
        elif not row["success"]:
            state = "failed"
        elif item["kind"] == "repeatable" and row["checksum"] != item["checksum"]:
            state = "outdated"
        else:
            state = "applied"
        items.append({**{k: item[k] for k in ("kind", "version", "description", "script")}, "state": state,
                      "installed_on": row["installed_on"].isoformat(timespec="seconds") if row and row["installed_on"] else ""})
    known = {version_key(item["version"]) for item in local if item["kind"] == "versioned"}
    items += [{"kind": "versioned", "version": row["version"], "description": row["description"],
               "script": row["script"], "state": "missing",
               "installed_on": row["installed_on"].isoformat(timespec="seconds") if row["installed_on"] else ""}
              for key, row in by_version.items() if key not in known]
    items.sort(key=lambda item: (item["kind"] != "versioned", version_key(item["version"]), item["description"]))
    counts = {state: sum(item["state"] == state for item in items)
              for state in ("applied", "pending", "failed", "outdated", "missing")}
    return {"repo": str(repo), "branch": git_branch(repo), "flyway": rows is not None, "migrations": items,
            "counts": counts, "table": f"{flyway.schema}.{flyway.table}"}


def git_branch(repo: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
