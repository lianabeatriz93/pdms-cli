"""Shared pieces of the commands: the Typer apps, the console, failing, picking from the config and menus."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Optional, TypeVar

from ..lazy import questionary
import typer
from rich.console import Console
from rich.table import Table

from .. import actions, images, instances, prompts, proxy, repos
from ..config import Config, Database, DevUser
from ..i18n import _

console = Console()
# Errors go to stderr: eval "$(pdms env …)" and scripts reading the output must not get them as output.
err_console = Console(stderr=True)


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
    err_console.print(f"[red]✗[/] {message}")
    raise typer.Exit(1)


YES_HELP = _("Do it without asking (for scripts).")
JSON_HELP = _("Print JSON instead of a table (for scripts).")


ADD_OPTIONS_NOTE = _("With the options it asks nothing (only for the required ones left out, in a terminal).")
EDIT_OPTIONS_NOTE = _("With options it changes only those, without asking.")
PASSWORD_STDIN_HELP = _("Read the password from the first line of stdin (it never goes on the command line).")
PASSWORD_ENV_HELP = _("Read the password from this environment variable (it never goes on the command line).")


def read_secret(from_stdin: bool, env_name: Optional[str]) -> Optional[str]:
    """A password from the first line of stdin or from an environment variable; None when neither is given."""
    if from_stdin and env_name:
        fail(_("Give --password-stdin or --password-env, not both."))
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    if env_name:
        if env_name not in os.environ:
            fail(_("The environment variable {name} is not set.", name=env_name))
        return os.environ[env_name]
    return None


def given(**values: object) -> dict:
    """The options a command received (the ones left as None were not given)."""
    return {key: value for key, value in values.items() if value is not None}


def ask_missing(missing: dict[str, Callable[[], T]]) -> dict[str, T]:
    """Ask in the terminal for the required values the options left out (``option -> asks it``); without a
    terminal, a failure that names the options to pass."""
    if missing and not interactive_terminal():
        fail(_("Missing {options}: pass them, or run it in a terminal to be asked.", options=", ".join(missing)))
    return {option: ask() for option, ask in missing.items()}


def split_list(value: str) -> list[str]:
    """``a, b,,c`` -> ``["a", "b", "c"]`` (no repeats)."""
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def confirm(question: str, yes: bool) -> bool:
    """``yes``, or the user's answer; without a terminal to ask in, a failure that says to pass --yes."""
    if yes:
        return True
    if not interactive_terminal():
        fail(_("{question} There is no terminal to ask in: pass --yes.", question=question))
    return bool(questionary.confirm(question, default=False).unsafe_ask())


def print_json(data: object) -> None:
    """``data`` as JSON on stdout, for scripts (dataclasses as their fields)."""
    print(json.dumps(data, indent=2, ensure_ascii=False, default=lambda value: asdict(value)))


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


def _required_text(label: str) -> Callable[[], str]:
    return lambda: questionary.text(label, validate=lambda value: bool(value.strip()) or _("Required field")).unsafe_ask()


def _new_name(cfg_items: dict, name: Optional[str]) -> Optional[str]:
    return None if name is None else settle(lambda: actions.check_alias(name, cfg_items))


def add_db(cfg: Config, name: Optional[str] = None, values: Optional[dict] = None, ping: Optional[bool] = None) -> str:
    """A new database: the wizard, or without asking the fields ``values`` has (``Database`` fields; the alias and
    the user are asked only when missing, the rest defaults as in the wizard)."""
    name = _new_name(cfg.dbs, name)
    if not values:
        prompts.require_tty()
        name = name or prompts.ask_name(_("database"), cfg.dbs)
        database = prompts.ask_database()
    else:
        asked = ask_missing({option: ask for option, ask, missing in (
            ("NAME", lambda: prompts.ask_name(_("database"), cfg.dbs), name is None),
            ("--user", _required_text(_("User:")), not values.get("user")),
        ) if missing})
        name = name or asked.get("NAME")
        values = {**values, "user": values.get("user") or asked.get("--user", "")}
        host = values.get("host", "localhost")
        database = replace(Database(host=host, protected=host not in ("localhost", "127.0.0.1")), **values)
    settle(lambda: actions.save_db(cfg, name, database, new=True))
    console.print("[green]✓[/] " + _("Database '{name}' saved.", name=name))
    if ping is None:
        ping = not values and questionary.confirm(_("Test the connection now?"), default=True).unsafe_ask()
    if ping:
        check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)
    return name


def add_user(cfg: Config, name: Optional[str] = None, values: Optional[dict] = None) -> str:
    """A new development user: the wizard, or without asking the fields ``values`` has (``DevUser`` fields; the
    alias, the id and the username are asked only when missing)."""
    name = _new_name(cfg.users, name)
    roles = actions.known_roles(cfg)
    if not values:
        prompts.require_tty()
        name = name or prompts.ask_name(_("user"), cfg.users)
        user = prompts.ask_user(roles=roles)
    else:
        asked = ask_missing({option: ask for option, ask, missing in (
            ("NAME", lambda: prompts.ask_name(_("user"), cfg.users), name is None),
            ("--user-id", _required_text("DEV_USER_ID (uuid):"), not values.get("user_id")),
            ("--username", _required_text("DEV_USERNAME (email):"), not values.get("username")),
        ) if missing})
        name = name or asked.get("NAME")
        user = DevUser(**{"user_id": asked.get("--user-id", ""), "username": asked.get("--username", ""),
                          **{key: value for key, value in values.items() if value}})
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
    """A user at a terminal (not pdmsw.exe's no console, where Python may leave the streams as None)."""
    return all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout))


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


def with_images(work: Callable[[], T]) -> T:
    """Run ``work``; when it needs Docker images that are not there yet, ask, download them (their progress on the
    terminal) and run it again. Without a terminal to ask, say what to download and stop."""
    while True:
        try:
            return work()
        except images.Missing as needed:
            commands = "  " + "\n  ".join(f"docker pull {image.name}" for image in needed.images)
            if not interactive_terminal():
                fail(str(needed) + "\n" + _("Download them first (or from pdms ui → Doctor):") + "\n" + commands)
            for image in needed.images:
                console.print(f"  [bold]{image.name}[/] · ~{image.download_mb} MB · {image.use}")
            if not questionary.confirm(str(needed) + " " + _("Download now?"), default=True).unsafe_ask():
                fail(_("Not downloaded. When you want:") + "\n" + commands)
            for image in needed.images:
                console.rule(f"docker pull {image.name}")
                try:
                    images.pull(image.name, sys.stdout)
                except actions.ActionError as exc:
                    fail(exc.message)
