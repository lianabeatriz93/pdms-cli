"""Stacks: starting and stopping them, saving and removing them."""

from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path

from ... import actions, instances
from ...config import Config, Stack
from ...i18n import _
from . import core, repo_info
from .core import Job
from .services import ServiceJobs

__all__ = ["StackJobs", "remove_stack", "save_stack", "stack_from_running", "stack_key"]


def stack_key(name: str) -> str:
    return f"stack:{name}"


def save_stack(name: str, services: list[str], user: str, db: str, new: bool,
               overrides: dict[str, dict[str, str]] | None = None, rename: str = "",
               on_demand: str | None = None) -> str:
    """Create (``new``) or replace a stack, and give an existing one another name (``rename``); the services must
    exist in the current repo. Without ``overrides`` an existing stack keeps its own (for the services it still has).
    The stack's name afterwards."""
    cfg = Config.load()
    name = name.strip()
    rename = rename.strip()
    if new:
        actions.check_alias(name, cfg.stacks)
    else:
        actions.require(cfg.stacks, _("stack"), name)
        if rename and rename != name:
            actions.check_alias(rename, cfg.stacks)  # before saving anything
    _root, known = repo_info.repo_services(cfg)
    unknown = [svc for svc in services if svc not in known]
    if unknown:
        raise actions.ActionError(_("Not services of the current repo: {names}", names=", ".join(unknown)))
    if overrides is None:
        overrides = {} if new else dict(cfg.stacks[name].overrides)
    if on_demand is None:  # an older page: what the stack had
        on_demand = "" if new else cfg.stacks[name].on_demand
    actions.save_stack(cfg, name, Stack(services=list(dict.fromkeys(services)), user=user, db=db, overrides=overrides,
                                        on_demand=on_demand))
    return actions.rename_stack(cfg, name, rename) if not new and rename else name


def stack_from_running() -> dict:
    """What the editor starts from to keep the running services as a new stack (see actions.stack_from_running)."""
    cfg = Config.load()
    return asdict(actions.stack_from_running(cfg, repo_info.backend(cfg)))


def remove_stack(name: str) -> None:
    actions.remove_stack(Config.load(), name)


class StackJobs(ServiceJobs):
    def up(
        self, name: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, python: bool | None = None, migrate: bool | None = None, views: bool | None = None,
    ) -> Job | None:
        """Start what is not running of the stack, like ``pdms up``; None when all of it already runs."""
        cfg = Config.load()
        stack = cfg.stacks[actions.require(cfg.stacks, _("stack"), name)]
        root = repo_info.backend(cfg)
        actions.stack_paths(stack, root)  # a stale stack fails before anything else
        user, db = user or stack.user or cfg.last_user, db or stack.db or cfg.last_db
        plan = actions.plan_stack(cfg, name, root, user_name=user, db_name=db)
        if not plan.services:
            return None
        actions.check_database(cfg, db, confirmed)
        recreate = actions.check_pythons(cfg, [launch.service for launch in plan.services], install, python)
        behind = actions.check_migrations(cfg, [launch.db_name for launch in plan.services], migrate)
        needs = actions.check_test_views(cfg, [(launch.service, launch.db_name) for launch in plan.services], views)
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.migrate_first(job, behind)
            self.views_first(cfg, job, needs)
            keys = {launch.service: instances.make_key(launch.service, launch.port) for launch in plan.services}
            installing: list[Path] = []  # at once
            lock = threading.Lock()

            def progress(event: str, service: Path) -> None:
                with lock:
                    if event == "installed":
                        installing.remove(service)
                    else:
                        installing.append(service)
                    now = list(installing)
                if now:
                    self.phase(job, f"installing {', '.join(path.name for path in now)}", installed=True,
                               log_key=keys[now[-1]])

            actions.install_services(cfg, {path: core.install_log(key) for path, key in keys.items()}, install, progress,
                                     recreate)
            for launch in plan.services:
                self.phase(job, f"starting {launch.service.name}")
                actions.start_service(cfg, launch)

        return self.run(stack_key(name), "up", "starting", work)

    def down(self, name: str) -> Job | None:
        """Stop the stack's running services, like ``pdms down``; None when nothing of it runs."""
        cfg = Config.load()
        targets = actions.stack_instances(cfg, name, repo_info.backend(cfg))
        if not targets:
            return None

        def work(job: Job) -> None:
            for inst in targets:
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)

        return self.run(stack_key(name), "down", "stopping", work)
