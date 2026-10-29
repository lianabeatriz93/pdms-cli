"""Debugging a service in VS Code, and what runs from each service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import events, instances, repos, runner, vscode
from ..config import Config
from ..i18n import _
from . import checks, local_events, services
from .errors import ActionError, LocalEventsDown

if TYPE_CHECKING:
    from .services import ServiceLaunch

__all__ = ["DebugSetup", "debug_instance", "open_code", "running_by_service", "write_debug_config"]


@dataclass
class DebugSetup:
    """A service's VS Code debug configuration, written by :func:`write_debug_config`."""

    launch_json: Path
    name: str  # of the configuration, to pick in Run and Debug
    backup: Path | None  # launch.json had comments: the original, since rewriting it drops them
    env_file: Path
    root: Path  # the folder to open in VS Code


def write_debug_config(cfg: Config, launch: ServiceLaunch) -> DebugSetup:
    """Add or replace the service's configuration in ``.vscode/launch.json`` (without --reload, so breakpoints work).

    The variables, password included, go to an env file outside the repo (see :mod:`vscode`)."""
    python = runner.poetry_python(launch.service)
    if not python:
        raise ActionError(_("{name} has no virtualenv yet: start it once (that installs it) and try again.",
                            name=launch.service.name))
    consumer = local_events.consumer_of(cfg, launch.service)
    program = events.poller_command("", consumer[0], consumer[1], cfg.defaults.events_port)[3:] if consumer else None
    env_file = vscode.write_env_file(launch.service, services.service_env(cfg, launch))
    where = f"sqs {consumer[0].name}" if consumer else f":{launch.port}"
    launch_json, name, backup = vscode.upsert_configuration(
        launch.service, python=python, env_file=env_file, host=launch.host, port=launch.port,
        description=f"{launch.user_name} @ {launch.db_name} {where}", program=program,
    )
    return DebugSetup(launch_json, name, backup, env_file, vscode.workspace_root(launch.service))


def debug_instance(cfg: Config, instance: instances.Instance) -> DebugSetup:
    """Hand a running instance over to VS Code: stop it, write its configuration (same user, database and port) and
    open VS Code on its repo. Pressing F5 there starts it under the debugger."""
    checks.require(cfg.users, _("user"), instance.user)
    checks.require(cfg.dbs, _("database"), instance.db)
    if not runner.poetry_python(Path(instance.service)):
        raise ActionError(_("{name} has no virtualenv yet: start it once (that installs it) and try again.",
                            name=instance.name))
    if not vscode.code_command():
        raise ActionError(_("VS Code's code command was not found. In VS Code: Ctrl+Shift+P → "
                            "Shell Command: Install 'code' command in PATH."))
    if (instance.events == "local" or instance.is_consumer) and not events.running(cfg.defaults.events_port):
        raise LocalEventsDown(cfg.defaults.events_port)  # before stopping it, like a restart
    services.stop_service(instance)
    launch = services.plan_service(
        cfg, Path(instance.service), user_name=instance.user, db_name=instance.db, port=instance.port or None,
        host=instance.host, reload=False, events_mode="local" if instance.events == "local" else None,
    )
    setup = write_debug_config(cfg, launch)
    try:
        vscode.open_in_code(setup.root)
    except (RuntimeError, OSError) as exc:
        raise ActionError(str(exc)) from exc
    return setup


def open_code(cfg: Config, path: str, line: int = 0) -> None:
    """Open a file of a registered repo in VS Code at ``line`` (a traceback's frame); other files are refused."""
    file = Path(path)
    alias = repos.repo_of(cfg, file)
    if not alias or not file.is_file():
        raise ActionError(_("{path} is not a file of a registered repo.", path=path))
    try:
        vscode.open_in_code(cfg.repos[alias].root, file, line)
    except RuntimeError as exc:
        raise ActionError(str(exc)) from exc


def running_by_service() -> dict[str, list[int]]:
    """Ports of the live (or asleep) instances of each service (0 for SQS consumers)."""
    result: dict[str, list[int]] = {}
    for inst in instances.load().values():
        if inst.present():
            result.setdefault(inst.service, []).append(inst.port)
    return result
