"""Services running outside pdms: finding them, adopting or stopping them."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import instances, repos
from ..config import Config
from ..i18n import _
from . import checks, services
from .errors import ActionError

__all__ = [
    "Stray", "_database_id", "_pick_strays", "adopt_strays", "db_of", "restart_service", "stop_strays", "strays",
    "user_of",
]


@dataclass
class Stray:
    """A service running outside pdms, with the user and database of the config it runs as ("" when none matches)."""

    process: instances.Stray
    user: str
    db: str
    repo: str

    @property
    def key(self) -> str:
        return self.process.key


def _database_id(url: str) -> tuple[str, str, int, str]:
    """``user@host:port/database`` of a connection string, without the password (it may have changed since)."""
    parts = urlsplit(url)
    try:
        port = parts.port or 5432
    except ValueError:
        port = 0
    return unquote(parts.username or ""), parts.hostname or "", port, parts.path.lstrip("/")


def user_of(cfg: Config, env: dict[str, str]) -> str:
    """The user profile whose DEV_USER_ID (and DEV_USERNAME) a service runs with."""
    user_id, username = env.get("DEV_USER_ID", ""), env.get("DEV_USERNAME", "")
    matches = [name for name, user in cfg.users.items() if user_id and user.user_id == user_id]
    return next((name for name in matches if cfg.users[name].username == username), matches[0] if matches else "")


def db_of(cfg: Config, env: dict[str, str]) -> str:
    url = env.get("DB_PG_CONNECTION_STR", "")
    if not url:
        return ""
    wanted = _database_id(url)
    return next((name for name, db in cfg.dbs.items() if _database_id(db.url()) == wanted), "")


def strays(cfg: Config) -> list[Stray]:
    """Services of the registered repos that run but pdms does not know: their pdms exited and lost them (adopt
    them to manage them again, or stop them)."""
    found = instances.strays([repo.root for repo in cfg.repos.values()])
    return [Stray(process, user_of(cfg, process.env), db_of(cfg, process.env),
                  repos.repo_of(cfg, process.service) or "") for process in found]


def _pick_strays(cfg: Config, keys: Iterable[str] | None) -> list[Stray]:
    found = strays(cfg)
    if keys is None:
        return found
    wanted = list(keys)
    unknown = [key for key in wanted if key not in {stray.key for stray in found}]
    if unknown:
        raise ActionError(_("Not running outside pdms: {keys}", keys=", ".join(unknown)))
    return [stray for stray in found if stray.key in wanted]


def adopt_strays(cfg: Config, keys: Iterable[str] | None = None) -> list[instances.Instance]:
    """Register the given services running outside pdms again (every one with ``None``)."""
    return [instances.adopt(stray.process, user=stray.user, db=stray.db) for stray in _pick_strays(cfg, keys)]


def stop_strays(cfg: Config, keys: Iterable[str] | None = None) -> list[str]:
    """Stop the given services running outside pdms (every one with ``None``), with their reloader and workers."""
    chosen = _pick_strays(cfg, keys)
    if chosen:
        with ThreadPoolExecutor(max_workers=min(8, len(chosen))) as pool:
            list(pool.map(lambda stray: instances.kill_tree(stray.process.pid, stray.process.created), chosen))
    return [stray.key for stray in chosen]


def restart_service(
    cfg: Config,
    instance: instances.Instance,
    *,
    user_name: str | None = None,
    db_name: str | None = None,
    confirmed: bool = False,
    install: Callable[[Path], None] | None = None,
) -> instances.Instance:
    """Restart on the same port, with the same user and database unless others are given.

    Everything is checked before stopping, so a refused protected database leaves the instance running.
    """
    user_name, db_name = user_name or instance.user, db_name or instance.db
    checks.require(cfg.users, _("user"), user_name)
    checks.require(cfg.dbs, _("database"), db_name)
    if db_name != instance.db:
        checks.check_database(cfg, db_name, confirmed)
    services.stop_service(instance)
    launch = services.plan_service(
        cfg, Path(instance.service), user_name=user_name, db_name=db_name, port=instance.port or None,
        host=instance.host, reload=instance.reload, events_mode="local" if instance.events == "local" else None,
        on_demand=instance.on_demand,
    )
    return services.start_service(cfg, launch, install, awake=True)  # asked for: it runs now, and sleeps again when idle
