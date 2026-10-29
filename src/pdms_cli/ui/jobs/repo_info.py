"""What the page needs of the current repo: its root, backend, services and remote API."""

from __future__ import annotations

from pathlib import Path

from ... import actions, events, repos, runner
from ...config import Config
from ...i18n import _

__all__ = ["backend", "repo_consumers", "repo_remote", "repo_root", "repo_services"]


def repo_remote(cfg: Config, root: Path) -> str:
    """The remote API a proxy started now would use: the one saved for the repo, or the one in ``frontend/.env``."""
    alias = repos.alias_of(cfg, root)
    return (cfg.repos[alias].remote if alias in cfg.repos else "") or repos.remote_from_frontend(root) or ""


def repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def backend(cfg: Config) -> Path:
    root = repos.active_backend(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root.resolve()


def repo_services(cfg: Config) -> tuple[Path, list[str]]:
    """The backend folder and every service in it, as the stacks write them (``lead/lead-tp-list``)."""
    root = backend(cfg)
    return root, [path.relative_to(root).as_posix() for path in runner.find_services_below(root)]


def repo_consumers(root: Path) -> list[str]:
    """The services of the backend folder ``root`` that consume an SQS queue (they take no port)."""
    try:
        event_map = events.load_event_map(root.parent)
    except Exception:  # noqa: BLE001 - a Terraform pdms cannot read only loses the hint
        return []
    return sorted({consumer.service for consumer in event_map.consumers.values()})
