"""Setups (pdms ui's Home): a stack, the proxy, the frontend and local events, under a name."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .. import instances
from ..config import Config, Setup, Stack
from ..i18n import _
from . import checks, settings, stacks
from .errors import ActionError

__all__ = [
    "_most_used", "remove_setup", "remove_stack", "rename_stack", "save_setup_as", "save_stack",
    "stack_from_running", "stack_instances", "use_setup",
]


def save_setup_as(cfg: Config, name: str) -> str:
    """Keep the current setup under ``name`` (a new one, or replacing it); it becomes the current one's name."""
    name = settings.check_alias(name, cfg.setups) if name.strip() not in cfg.setups else name.strip()
    cfg.setups[name] = replace(cfg.setup)
    cfg.setup_name = name
    cfg.save()
    return name


def use_setup(cfg: Config, name: str) -> Setup:
    """Make the saved setup ``name`` the current one (what Start everything starts)."""
    cfg.setup = replace(cfg.setups[checks.require(cfg.setups, _("setup"), name)])
    cfg.setup_name = name
    cfg.save()
    return cfg.setup


def remove_setup(cfg: Config, name: str) -> None:
    del cfg.setups[checks.require(cfg.setups, _("setup"), name)]
    if cfg.setup_name == name:
        cfg.setup_name = ""  # the current setup stays as it is, without a name
    cfg.save()


def stack_instances(cfg: Config, name: str, root: Path) -> list[instances.Instance]:
    """Live (or asleep) instances of the stack's services."""
    paths = {str(p) for p in stacks.stack_paths(cfg.stacks[checks.require(cfg.stacks, _("stack"), name)], root)}
    return [i for i in instances.load().values() if i.service in paths and i.present()]


def save_stack(cfg: Config, name: str, stack: Stack) -> None:
    if not stack.services:
        raise ActionError(_("A stack needs at least one service."))
    stack.overrides = {svc: own for svc, own in stack.overrides.items() if svc in stack.services}  # gone with them
    for own in stack.overrides.values():
        if own.get("user"):
            checks.require(cfg.users, _("user"), own["user"])
        if own.get("db"):
            checks.require(cfg.dbs, _("database"), own["db"])
    if stack.user:
        checks.require(cfg.users, _("user"), stack.user)
    if stack.db:
        checks.require(cfg.dbs, _("database"), stack.db)
    cfg.stacks[name] = stack
    cfg.save()


def rename_stack(cfg: Config, name: str, new_name: str) -> str:
    """Give the stack another name, in the same place of the list; the setups that start it follow. The new name."""
    stack_name = checks.require(cfg.stacks, _("stack"), name)
    new_name = new_name.strip()
    if new_name == stack_name:
        return stack_name
    new_name = settings.check_alias(new_name, cfg.stacks)
    cfg.stacks = {(new_name if key == stack_name else key): stack for key, stack in cfg.stacks.items()}
    for setup in (cfg.setup, *cfg.setups.values()):
        if setup.stack == stack_name:
            setup.stack = new_name
    cfg.save()
    return new_name


def _most_used(values: Iterable[str]) -> str:
    """The value most of them have ("" when none has one); a tie goes to the first in alphabetical order."""
    counts = Counter(value for value in values if value)
    return max(sorted(counts), key=lambda value: counts[value]) if counts else ""


def stack_from_running(cfg: Config, root: Path) -> Stack:
    """A stack of the services pdms runs from ``root`` (the current repo's backend folder), as they run: with the user
    and database most of them use, and the others' own as exceptions. A service running twice counts as it started
    first; a user or database no longer in the configuration is left for the stack to ask."""
    found: dict[str, tuple[str, str]] = {}
    for inst in sorted(instances.load().values(), key=lambda item: item.started_at):
        if not inst.alive():
            continue
        try:
            path = stacks._relative(Path(inst.service), root)
        except ActionError:
            continue
        found.setdefault(path, (inst.user if inst.user in cfg.users else "", inst.db if inst.db in cfg.dbs else ""))
    if not found:
        raise ActionError(_("No service of the current repo is running."))
    user = _most_used(own[0] for own in found.values())
    db = _most_used(own[1] for own in found.values())
    overrides = {}
    for path, (own_user, own_db) in found.items():
        own = {key: value for key, value, common in (("user", own_user, user), ("db", own_db, db))
               if value and value != common}
        if own:
            overrides[path] = own
    return Stack(services=sorted(found), user=user, db=db, overrides=overrides)


def remove_stack(cfg: Config, name: str) -> None:
    del cfg.stacks[checks.require(cfg.stacks, _("stack"), name)]
    if cfg.setup.stack == name:
        cfg.setup.stack = ""
    for saved in cfg.setups.values():
        if saved.stack == name:
            saved.stack = ""
    cfg.save()
