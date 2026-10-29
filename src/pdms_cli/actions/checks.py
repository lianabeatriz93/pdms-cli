"""Checks every action uses: names that exist, free ports, protected databases."""

from __future__ import annotations

from .. import instances, runner
from ..config import Config
from ..i18n import _
from .errors import ActionError, PortBusy, ProtectedDatabase

__all__ = ["check_database", "free_port", "port_available", "remember_profile", "require", "suggested_port"]


def require(items: dict, kind: str, name: str) -> str:
    if name not in items:
        raise ActionError(_("'{name}' does not exist ({kind}). Available: {available}",
                            name=name, kind=kind, available=", ".join(items) or _("none")))
    return name


def port_available(host: str, port: int, taken: set[int] | None = None) -> bool:
    """Free on the machine and not held by a background instance (they count as taken while still booting)."""
    taken = instances.running_ports() if taken is None else taken
    return port not in taken and runner.port_is_free(host, port)


def free_port(host: str, port: int) -> int:
    """``port`` if it can be used; otherwise :class:`PortBusy` with the next free one."""
    taken = instances.running_ports()
    if port_available(host, port, taken):
        return port
    raise PortBusy(port, runner.next_free_port(host, port + 1, taken))


def suggested_port(cfg: Config, host: str) -> int:
    return runner.next_free_port(host, cfg.defaults.port, instances.running_ports())


def check_database(cfg: Config, db_name: str, confirmed: bool) -> None:
    """:class:`ProtectedDatabase` unless the database is unprotected or the user already confirmed it."""
    if cfg.dbs[db_name].protected and not confirmed:
        raise ProtectedDatabase(db_name)


def remember_profile(cfg: Config, user_name: str, db_name: str) -> None:
    """The user and database become the defaults next time."""
    cfg.last_user, cfg.last_db = user_name, db_name
    cfg.save()
