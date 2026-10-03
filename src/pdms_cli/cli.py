"""``pdms`` command line entrypoint.

The commands live in ``pdms_cli.commands``, one module per group; importing a module registers its commands on the
Typer app. ``COMMAND_ORDER`` then puts them in the order ``pdms --help`` lists them.
"""

from __future__ import annotations

import sys

import typer

from . import __version__
from .commands import (  # noqa: F401 - importing them registers their commands
    dbs,
    doctor,
    events,
    frontend,
    instances,
    menu,
    proxy,
    repos,
    run,
    selfupdate,
    services,
    settings,
    setup,
    stacks,
    testing,
    ui,
    users,
)
from .commands.common import app, console, interactive_terminal
from .commands.menu import main_menu
from .commands.repos import check_repo
from .commands.selfupdate import start_update_check
from .config import Config, config_path
from .i18n import _

# The order of pdms --help (commands of the groups follow their own module's order).
COMMAND_ORDER = [
    "services", "run", "debug", "env", "test", "migrate", "urls", "ps", "adopt", "logs", "open", "stop", "restart",
    "up", "down", "front", "ui", "setup", "doctor", "self-update",
]


def command_name(command: typer.models.CommandInfo) -> str:
    return command.name or typer.main.get_command_name(command.callback.__name__)


app.registered_commands.sort(key=lambda c: COMMAND_ORDER.index(command_name(c)))

# Commands that do not depend on a repo, so they never trigger the "switch repo?" question.
REPO_AGNOSTIC = {"repo", "config", "env", "db", "user", "self-update", "setup"}


def show_version(value: bool) -> None:
    if value:
        print(f"pdms {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", callback=show_version, is_eager=True, help=_("Show the version and exit.")
    ),
) -> None:
    if ctx.resilient_parsing:
        return
    start_update_check(ctx, Config.load())
    # On the very first run the menu offers the guided setup, which registers the repo itself.
    first_run = ctx.invoked_subcommand is None and not config_path().exists() and interactive_terminal()
    if ctx.invoked_subcommand not in REPO_AGNOSTIC and not first_run:
        check_repo(Config.load())
    if ctx.invoked_subcommand is None:
        main_menu(first_run)


def _entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print(f"\n[dim]{_('Cancelled.')}[/]")
        sys.exit(130)
