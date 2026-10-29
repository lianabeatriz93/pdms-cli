"""Stacks: their services, starting and stopping them together."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import instances, runner
from ..config import Config, Stack
from ..i18n import _
from . import checks, debugging, local_events, services
from .errors import ActionError

if TYPE_CHECKING:
    from .local_events import EventsSetup
    from .services import ServiceLaunch

__all__ = ["StackLaunch", "_relative", "plan_stack", "remember_in_stack", "stack_paths", "start_stack"]


def stack_paths(stack: Stack, root: Path) -> list[Path]:
    """The stack's service folders under the repo's backend ``root``."""
    paths = []
    for svc in stack.services:
        path = root / svc
        if not runner.is_service(path):
            raise ActionError(_("'{svc}' is no longer a service in {root}. Edit the stack with [bold]pdms stack edit[/].",
                                svc=svc, root=root))
        paths.append(path)
    return paths


@dataclass
class StackLaunch:
    """What starting a stack does: the services to start and those already running (skipped)."""

    name: str
    user_name: str
    db_name: str
    services: list[ServiceLaunch]
    running: dict[Path, list[int]]
    events: EventsSetup | None = None  # None when there is nothing to start


def plan_stack(
    cfg: Config, name: str, root: Path, *, user_name: str, db_name: str, events_mode: str | None = None,
    on_demand: bool | None = None,
) -> StackLaunch:
    """Plan every service of the stack that is not running yet, each on its own free port.

    Raises :class:`LocalEventsDown` when events must go to the local ElasticMQ (always, if the stack has a consumer).
    """
    stack = cfg.stacks[checks.require(cfg.stacks, _("stack"), name)]
    checks.require(cfg.users, _("user"), user_name)
    checks.require(cfg.dbs, _("database"), db_name)
    paths = stack_paths(stack, root)
    live = debugging.running_by_service()
    running = {p: live[str(p)] for p in paths if str(p) in live}
    pending = [p for p in paths if p not in running]
    if not pending:
        return StackLaunch(name, user_name, db_name, [], running)

    consumers = {p: local_events.consumer_of(cfg, p) for p in pending}
    if any(consumers.values()):
        events_mode = "local"  # consumers read from the local ElasticMQ
    setup = local_events.events_setup(cfg, pending[0], events_mode)
    host = cfg.defaults.host
    taken = set(instances.running_ports())
    port = cfg.defaults.port
    on_demand = stack.runs_on_demand(cfg.defaults.on_demand) if on_demand is None else on_demand
    launches = []
    for path in pending:
        if not consumers[path]:
            port = runner.next_free_port(host, port, taken)
            taken.add(port)
        service_user, service_db = stack.profile_of(_relative(path, root), user_name, db_name)
        launches.append(services.plan_service(
            cfg, path, user_name=service_user, db_name=service_db, port=port, host=host, events_ready=setup,
            on_demand=on_demand,
        ))
    return StackLaunch(name, user_name, db_name, launches, running, setup)


def start_stack(
    cfg: Config, plan: StackLaunch, install: Callable[[Path], None] | None = None
) -> list[instances.Instance]:
    """Start the planned services in the background; waiting until they respond is up to the front end."""
    return [services.start_service(cfg, launch, install) for launch in plan.services]


def _relative(path: Path, root: Path) -> str:
    """A service's path in a stack: relative to the backend folder, with "/"."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ActionError(_("{path} is not a service of the current repo.", path=path)) from None


def remember_in_stack(cfg: Config, name: str, root: Path, services: Iterable[Path], user: str, db: str) -> None:
    """Make ``services`` of the stack start with ``user`` and ``db`` from now on: an exception where they differ from
    the stack's own, none where they are the same."""
    stack = cfg.stacks[checks.require(cfg.stacks, _("stack"), name)]
    checks.require(cfg.users, _("user"), user)
    checks.require(cfg.dbs, _("database"), db)
    for path in services:
        svc = _relative(path, root)
        if svc not in stack.services:
            raise ActionError(_("{service} is not in the stack {name}.", service=svc, name=name))
        own = {"user": user} if user != stack.user else {}
        if db != stack.db:
            own["db"] = db
        if own:
            stack.overrides[svc] = own
        else:
            stack.overrides.pop(svc, None)
    cfg.save()
