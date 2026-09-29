"""Flyway migrations (the pdms-db-migrations repo), run with the same Docker image as the CodePipeline.

Since PDMP-467 the schema is managed by Flyway in its own repository; the Alembic migrations of
``backend/common/sync-database`` are frozen. From pdms only the read-only ``info`` and ``validate`` can run against
any database; ``migrate`` only against a local one (shared databases are migrated by the pipeline).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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
