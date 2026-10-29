"""PDMS repos (checkouts) and their migrations repo."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import instances, migrations, proxy, repos
from ..config import Config
from ..i18n import _
from . import checks, settings
from .errors import ActionError, InvalidValue

__all__ = [
    "RepoSwitch", "add_repo", "current_migrations_repo", "edit_repo", "migrations_repo", "plan_repo_switch",
    "remote_api", "remove_repo", "repo_root", "use_repo",
]


def repo_root(path: str | Path) -> Path:
    """The PDMS checkout ``path`` is in (it may be a folder inside it); :class:`InvalidValue` otherwise."""
    text = str(path).strip()
    root = repos.find_repo_root(Path(text).expanduser()) if text else None
    if root is None:
        raise InvalidValue("path", _("{path} is not inside a PDMS repo (no backend/snakesdk folder).", path=text or "''"))
    return root


def add_repo(cfg: Config, path: str | Path, alias: str = "") -> str:
    """Register the PDMS checkout ``path`` is in; returns its alias. The first repo becomes the current one."""
    root = repo_root(path)
    if existing := repos.alias_of(cfg, root):
        raise InvalidValue("path", _("{path} is already registered as '{alias}'.", path=root, alias=existing))
    alias = settings.check_alias(alias, cfg.repos) if alias.strip() else repos.suggest_alias(cfg, root)
    alias = repos.register(cfg, root, alias)
    cfg.save()
    return alias


def migrations_repo(path: str) -> str:
    """The Flyway checkout ``path`` is in, as saved for a repo ("" for none); :class:`InvalidValue` otherwise."""
    if not path.strip():
        return ""
    found = migrations.find_upwards(Path(path.strip()).expanduser())
    if found is None:
        raise InvalidValue("migrations", _("{path} is not a Flyway migrations repo (flyway.toml + migrations/).",
                                           path=path.strip()))
    return str(found)


def current_migrations_repo(cfg: Config) -> Path:
    """The Flyway checkout of the current repo: the one saved for it, or the only (or matching) one next to it."""
    root = repos.active_root(cfg)
    alias = repos.alias_of(cfg, root) if root else None
    repo = cfg.repos.get(alias) if alias else None
    if repo and repo.migrations and migrations.is_migrations_repo(Path(repo.migrations)):
        return Path(repo.migrations)
    near = migrations.siblings(root) if root else []
    found = near[0] if len(near) == 1 else migrations.best_match(root, near) if root else None
    if found is None:
        raise ActionError(_("No migrations repo (pdms-db-migrations) for '{alias}': set it in Settings → Repos.",
                            alias=alias or "-"))
    return found


def remote_api(url: str) -> str:
    """``url`` without the trailing slash, if it can be the proxy's remote API ("" for none)."""
    url = url.strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", url):
        raise InvalidValue("remote", _("Must be a URL starting with http:// or https://"))
    return url


def edit_repo(
    cfg: Config, alias: str, *, new_alias: str | None = None, migrations_path: str | None = None,
    remote: str | None = None,
) -> str:
    """Change the alias, the migrations repo or the proxy's remote API of a repo (``None`` keeps it); returns its alias.

    Every value is checked before anything changes."""
    alias = checks.require(cfg.repos, _("repo"), alias)
    repo = cfg.repos[alias]
    renamed = alias
    if new_alias is not None and new_alias.strip() != alias:
        renamed = settings.check_alias(new_alias, cfg.repos)
    saved_migrations = repo.migrations if migrations_path is None else migrations_repo(migrations_path)
    saved_remote = repo.remote if remote is None else remote_api(remote)
    repo.migrations, repo.remote = saved_migrations, saved_remote
    if renamed != alias:
        cfg.repos = {renamed if name == alias else name: item for name, item in cfg.repos.items()}
        if cfg.current_repo == alias:
            cfg.current_repo = renamed
    cfg.save()
    return renamed


@dataclass
class RepoSwitch:
    """What was left behind when the current repo changed: the front end offers to keep, stop or move them."""

    old: str
    # Live instances of services from the old repo, with the same service in the new one (None: not there).
    running: list[tuple[instances.Instance, Path | None]]
    # The proxy is running for the old repo: it keeps routing there until it is restarted.
    proxy: bool


def use_repo(cfg: Config, alias: str) -> RepoSwitch:
    """Make ``alias`` the current repo; what was left running from the previous one."""
    switch = plan_repo_switch(cfg, alias)
    cfg.current_repo = checks.require(cfg.repos, _("repo"), alias)
    cfg.save()
    repos.use_for_this_command(None)
    return switch


def plan_repo_switch(cfg: Config, alias: str) -> RepoSwitch:
    """What making ``alias`` the current repo would leave running from the current one (nothing changes)."""
    alias = checks.require(cfg.repos, _("repo"), alias)
    old = cfg.current_repo
    if not old or old == alias or old not in cfg.repos:
        return RepoSwitch(old, [], False)
    old_root, new_root = cfg.repos[old].root, cfg.repos[alias].root
    running = [
        (inst, repos.translate(Path(inst.service), old_root, new_root))
        for inst in instances.load().values() if inst.alive() and repos.repo_of(cfg, inst.service) == old
    ]
    serving = proxy.running_proxy()
    on_old = bool(serving and serving.get("repo") and repos.repo_of(cfg, serving["repo"]) == old)
    return RepoSwitch(old, running, on_old)


def remove_repo(cfg: Config, alias: str) -> str:
    """Forget a repo (nothing is deleted from disk); returns the current repo afterwards ("" if none is left)."""
    alias = checks.require(cfg.repos, _("repo"), alias)
    root = str(cfg.repos.pop(alias).root)
    cfg.ignored_repos = [r for r in cfg.ignored_repos if r != root]
    if cfg.current_repo == alias:
        cfg.current_repo = next(iter(cfg.repos), "")
    cfg.save()
    return cfg.current_repo
