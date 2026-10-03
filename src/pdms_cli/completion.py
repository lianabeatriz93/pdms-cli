"""Shell completion callbacks (enable them with ``pdms --install-completion``).

They run on every Tab press, so they must be quiet (no prompts or prints) and never raise.
"""

from __future__ import annotations

from . import frontend, i18n, instances, proxy, runner, userimport
from .config import Config


def _matching(candidates: list[str], incomplete: str) -> list[str]:
    starts = [c for c in candidates if c.startswith(incomplete)]
    return starts or [c for c in candidates if incomplete in c]


def _config() -> Config | None:
    try:
        return Config.load()
    except Exception:  # noqa: BLE001 - a broken config must not break the shell
        return None


def services(incomplete: str) -> list[str]:
    cfg = _config()
    root = cfg.repo.backend_dir if cfg and cfg.repo else None
    if not root or not root.is_dir():
        return []
    found = runner.find_services_below(root)
    # Offer the short name and the path relative to the backend folder (e.g. lead-tp-list, lead/lead-tp-list).
    names = sorted({p.name for p in found} | {p.relative_to(root).as_posix() for p in found})
    return _matching(names, incomplete)


def instance_keys(incomplete: str) -> list[str]:
    try:
        return _matching(sorted(instances.load()), incomplete)
    except Exception:  # noqa: BLE001
        return []


def log_keys(incomplete: str) -> list[str]:
    """What ``pdms logs`` follows: instances, the running proxy, the local SNS and pdms ui."""
    return instance_or_proxy_keys(incomplete) + _matching(["sns", "ui"], incomplete)


def instance_or_proxy_keys(incomplete: str) -> list[str]:
    """Instances plus the running proxy and frontend (``pdms stop`` and ``pdms logs`` also take them)."""
    try:
        keys = sorted(instances.load())
        if running := proxy.running_proxy():
            keys.append(proxy.display_key(running))
        if frontend.running():
            keys.append(frontend.KEY)
        return _matching(keys, incomplete)
    except Exception:  # noqa: BLE001
        return []


def users(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.users), incomplete) if cfg else []


def dbs(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.dbs), incomplete) if cfg else []


def stacks(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.stacks), incomplete) if cfg else []


def setups(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.setups), incomplete) if cfg else []


def repos(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.repos), incomplete) if cfg else []


def roles(incomplete: str) -> list[str]:
    cfg = _config()
    mapping, _source = userimport.role_mapping(cfg.repo.root if cfg and cfg.repo else None)
    return _matching(list(mapping.values()), incomplete)


def _event_map():
    try:
        from . import events

        cfg = _config()
        return events.load_event_map(cfg.repo.root) if cfg and cfg.repo else None
    except Exception:  # noqa: BLE001
        return None


def queues(incomplete: str) -> list[str]:
    event_map = _event_map()
    return _matching(sorted(event_map.queues), incomplete) if event_map else []


def event_targets(incomplete: str) -> list[str]:
    """Event types and queue names (what ``pdms events send`` accepts)."""
    event_map = _event_map()
    return _matching(sorted(event_map.routes) + sorted(event_map.queues), incomplete) if event_map else []


def flyway_commands(incomplete: str) -> list[str]:
    return _matching(["info", "validate", "migrate"], incomplete)


def event_modes(incomplete: str) -> list[str]:
    return _matching(["auto", "local", "aws"], incomplete)


def languages(incomplete: str) -> list[str]:
    return _matching(list(i18n.LANGUAGES), incomplete)
