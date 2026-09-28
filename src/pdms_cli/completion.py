"""Shell completion callbacks (enable them with ``pdms --install-completion``).

They run on every Tab press, so they must be quiet (no prompts or prints) and never raise.
"""

from __future__ import annotations

from . import i18n, instances, runner
from .config import Config
from .userimport import EXTERNAL_TO_INTERNAL_ROLES


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
    names = sorted({p.name for p in found} | {str(p.relative_to(root)) for p in found})
    return _matching(names, incomplete)


def instance_keys(incomplete: str) -> list[str]:
    try:
        return _matching(sorted(instances.load()), incomplete)
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


def repos(incomplete: str) -> list[str]:
    cfg = _config()
    return _matching(list(cfg.repos), incomplete) if cfg else []


def roles(incomplete: str) -> list[str]:
    return _matching(list(EXTERNAL_TO_INTERNAL_ROLES), incomplete)


def languages(incomplete: str) -> list[str]:
    return _matching(list(i18n.LANGUAGES), incomplete)
