"""Shared pieces of the commands: the Typer apps, the console, failing, picking from the config and menus."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional, TypeVar

import questionary
import typer
from rich.console import Console
from rich.table import Table

from .. import actions, instances, prompts, proxy, repos
from ..config import Config, Database
from ..i18n import _

console = Console()


T = TypeVar("T")


app = typer.Typer(help=_("Run PDMS services locally. Without arguments it opens the interactive menu."))


db_app = typer.Typer(help=_("Manage databases."), invoke_without_command=True)


user_app = typer.Typer(help=_("Manage development users (DEV_*)."), invoke_without_command=True)


config_app = typer.Typer(help=_("General settings."), invoke_without_command=True)


stack_app = typer.Typer(help=_("Manage stacks (groups of services started together)."), invoke_without_command=True)


repo_app = typer.Typer(help=_("Manage PDMS repos (checkouts) and choose the current one."), invoke_without_command=True)


events_app = typer.Typer(
    help=_("Local SQS events: the repo's event map and a local ElasticMQ with all its queues."),
    invoke_without_command=True,
)


proxy_app = typer.Typer(
    help=_("Local API gateway: one port for every service, local instances first, the remote API otherwise."),
    invoke_without_command=True,
)


app.add_typer(db_app, name="db")


app.add_typer(user_app, name="user")


app.add_typer(config_app, name="config")


app.add_typer(stack_app, name="stack")


app.add_typer(repo_app, name="repo")


app.add_typer(proxy_app, name="proxy")


app.add_typer(events_app, name="events")


def fail(message: str) -> None:
    console.print(f"[red]✗[/] {message}")
    raise typer.Exit(1)


def yes_no(value: bool) -> str:
    return _("yes") if value else _("no")


def print_dbs(cfg: Config) -> None:
    if not cfg.dbs:
        console.print(f"[yellow]{_('No databases configured.')}[/] {_('Use [bold]pdms db add[/].')}")
        return
    table = Table(_("Name"), "Host", _("Port"), "DB", _("User"), _("Password"), _("Protected"))
    for name, db in cfg.dbs.items():
        table.add_row(
            name, db.host, str(db.port), db.database, db.user,
            "****" if db.password else f"[red]{_('not set')}[/]", _("yes") if db.protected else "",
        )
    console.print(table)


def print_users(cfg: Config) -> None:
    if not cfg.users:
        console.print(f"[yellow]{_('No users configured.')}[/] {_('Use [bold]pdms user add[/].')}")
        return
    table = Table(_("Name"), "DEV_USERNAME", _("Full name"), "DEV_ROLES", "DEV_USER_ID")
    for name, u in cfg.users.items():
        table.add_row(name, u.username, f"{u.first_name} {u.last_name}".strip(), u.roles, u.user_id)
    console.print(table)


def print_removed(name: str, stacks: list[str]) -> None:
    console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))
    if stacks:
        console.print("[dim]" + _("These stacks will ask for it again when they start: {names}",
                                  names=", ".join(stacks)) + "[/]")


def pick(cfg_items: dict, kind: str, name: Optional[str], default: str = "") -> str:
    if name:
        if name not in cfg_items:
            fail(_(
                "'{name}' does not exist ({kind}). Available: {available}",
                name=name, kind=kind, available=", ".join(cfg_items) or _("none"),
            ))
        return name
    if not cfg_items:
        fail(_("Nothing configured ({kind}).", kind=kind))
    if len(cfg_items) == 1:
        return next(iter(cfg_items))
    prompts.require_tty()
    return prompts.select_name(_("Choose {kind}:", kind=kind), list(cfg_items), default)


def add_db(cfg: Config) -> str:
    prompts.require_tty()
    name = prompts.ask_name(_("database"), cfg.dbs)
    database = prompts.ask_database()
    settle(lambda: actions.save_db(cfg, name, database, new=True))
    console.print("[green]✓[/] " + _("Database '{name}' saved.", name=name))
    if questionary.confirm(_("Test the connection now?"), default=True).unsafe_ask():
        check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)
    return name


def add_user(cfg: Config) -> str:
    prompts.require_tty()
    name = prompts.ask_name(_("user"), cfg.users)
    roles = actions.known_roles(cfg)
    user = prompts.ask_user(roles=roles)
    settle(lambda: actions.save_user(cfg, name, user, new=True, roles=roles))
    console.print("[green]✓[/] " + _("User '{name}' saved.", name=name))
    return name


def check_db(name: str, db: Database, timeout: int) -> bool:
    with console.status(_(
        "Connecting to {name} ({host}:{port}, timeout {timeout}s)...", name=name, host=db.host, port=db.port,
        timeout=timeout,
    )):
        try:
            version = actions.check_connection(db, timeout)
        except actions.ActionError as exc:
            console.print(f"[red]✗[/] {name}: {exc.message}")
            return False
    console.print(f"[green]✓[/] {name}: {version}")
    return True


EVENTS_HELP = _("Where the service publishes SQS events: auto, local (pdms events broker) or aws.")


def settle(action: Callable[[], T]) -> T:
    """Run an action, answering in the terminal what it asks (the local ElasticMQ) and failing on its errors."""
    from .events import events_up  # here: the events commands use settle themselves

    while True:
        try:
            return action()
        except actions.ActionError as exc:
            fail(exc.message)
        except actions.LocalEventsDown:
            if not interactive_terminal() or not questionary.confirm(
                _("The local ElasticMQ is not running. Start it now?"), default=True
            ).unsafe_ask():
                fail(_("--events local needs the local ElasticMQ: pdms events up"))
            events_up("dev", True)
        except actions.PortBusy as busy:
            fail(_("Port {port} is in use (the next free one is {free}). Use --port.", port=busy.port, free=busy.free))


PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


METHOD_STYLE = {"GET": "green", "POST": "yellow", "PUT": "blue", "PATCH": "cyan", "DELETE": "red"}


def print_endpoints(inst: instances.Instance, limit: Optional[int] = None, contains: str = "") -> None:
    spec = instances.fetch_openapi(inst)
    if spec is None:
        console.print("  [yellow]" + _("Could not read {url}/openapi.json.", url=f"http://localhost:{inst.port}") + "[/]")
        return
    found = [e for e in instances.endpoints(spec) if contains in e.path]
    shown = found[:limit] if limit else found
    grid = Table.grid(padding=(0, 2))
    for e in shown:
        grid.add_row(
            f"  [{METHOD_STYLE.get(e.method, 'white')}]{e.method}[/]",
            f"http://localhost:{inst.port}{e.path}", f"[dim]{e.summary}[/]",
        )
    if shown:
        console.print(grid, highlight=False)
    elif contains:
        console.print("  [dim]" + _("No endpoint contains '{text}'.", text=contains) + "[/]")
    if limit and len(found) > limit:
        console.print("  [dim]" + _("+{count} more: pdms urls {key}", count=len(found) - limit, key=inst.key) + "[/]")
    console.print("  " + _("Docs: {url}", url=f"http://localhost:{inst.port}/docs"), highlight=False)
    if running := proxy.running_proxy():
        console.print("  " + _("Through the proxy: {url} + the same paths", url=f"http://localhost:{running['port']}"),
                      highlight=False)


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def current_repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def print_restored(restored: Optional[str]) -> None:
    if restored:
        console.print("[green]✓[/] " + _("{path} restored; restart yarn dev to apply it.", path=restored))


def show_menu(title: str, options: dict[str, Callable[[], None]]) -> None:
    back = _("← Back")
    while True:
        choice = questionary.select(title, choices=[*options, back]).unsafe_ask()
        if choice == back:
            return
        try:
            options[choice]()
        except typer.Exit:
            pass
        console.print()
