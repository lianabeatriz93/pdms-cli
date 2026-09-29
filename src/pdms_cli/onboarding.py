"""Guided setup (``pdms setup``): which parts of the configuration are still missing, and exported files to import.

The interactive steps live in the CLI; this module only holds the decisions, so they can be tested without prompts.
"""

from __future__ import annotations

from pathlib import Path

from . import migrations
from .config import Config

# Steps of the setup that need something, in the order they are offered (stacks are optional, so not listed).
STEPS = ("repo", "migrations", "dbs", "passwords", "users")


def pending(cfg: Config) -> list[str]:
    """The setup steps that still have something to configure."""
    repo = cfg.repo
    has_repo = bool(repo and repo.root.is_dir())
    missing = {
        "repo": not has_repo,
        "migrations": has_repo and not (repo.migrations and migrations.is_migrations_repo(Path(repo.migrations))),
        "dbs": not cfg.dbs,
        "passwords": any(not db.password for db in cfg.dbs.values()),
        "users": not cfg.users,
    }
    return [step for step in STEPS if missing[step]]


def export_dirs() -> list[Path]:
    """Folders where a configuration shared by the team usually ends up."""
    home = Path.home()
    return [Path.cwd(), home / "Downloads", home / "Descargas", home / "Desktop", home]


def find_exports(dirs: list[Path] | None = None, limit: int = 10) -> list[Path]:
    """``pdms-config*.toml`` files in ``dirs`` (not recursive), newest first."""
    found: dict[Path, float] = {}
    for folder in dirs if dirs is not None else export_dirs():
        try:
            for path in folder.glob("pdms-config*.toml"):
                if path.is_file():
                    found[path.resolve()] = path.stat().st_mtime
        except OSError:
            continue
    return sorted(found, key=lambda p: found[p], reverse=True)[:limit]
