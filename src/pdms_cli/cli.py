"""``pdms`` command line entrypoint."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

from prompt_toolkit.keys import Keys
import questionary
import typer
from rich.markup import escape
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import (
    __version__, actions, banner, completion, desktop, events, frontend, i18n, installer, instances, logview, migrations, prompts, proxy, repos, routes,
    onboarding, runner, transfer,
    update, userimport, vscode,
)
from . import doctor as diagnostics
from .config import Config, Database, DevUser, Stack, config_path, write_private
from .i18n import _

if TYPE_CHECKING:
    from .ui.control import Control

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


# --------------------------------------------------------------------------- tables


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


# --------------------------------------------------------------------------- CRUD helpers


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


# --------------------------------------------------------------------------- services


def services_root(cfg: Config) -> Optional[Path]:
    backend = repos.active_backend(cfg)
    if backend is None:
        return None
    if backend.is_dir():
        return backend.resolve()
    console.print(f"[yellow]{_('⚠ The configured backend folder does not exist: {root}', root=backend)}[/]")
    return None


def list_services(cfg: Config) -> tuple[Path, list[Path]]:
    root = services_root(cfg) or Path.cwd().resolve()
    candidates = runner.find_services_below(root)
    if not candidates:
        hint = "" if cfg.repos else _(" Register a repo with [bold]pdms repo add <path>[/].")
        fail(_("No services (pyproject.toml + main.py) found in {root}.{hint}", root=root, hint=hint))
    return root, candidates


def label(service: Path, root: Path) -> str:
    try:
        return service.relative_to(root).as_posix()
    except ValueError:
        return str(service)


def running_by_service() -> dict[str, list[int]]:
    return actions.running_by_service()


def choose_service(candidates: list[Path], root: Path, message: Optional[str] = None) -> Path:
    prompts.require_tty()
    running = running_by_service()
    labels = {label(c, root): c for c in candidates}
    meta = {
        text: _("running on :{ports}", ports=", :".join(map(str, running[str(path)])))
        for text, path in labels.items() if str(path) in running
    }

    def matching(text: str) -> list[str]:
        text = text.strip()
        return [text] if text in labels else [t for t in labels if text and text in t]

    def validate(text: str) -> bool | str:
        found = matching(text)
        if len(found) == 1:
            return True
        return _("{count} services match, narrow it down", count=len(found)) if found else _("No service matches")

    choice = questionary.autocomplete(
        _("{message} (type to filter, Tab to see the list):", message=message or _("Service to run")),
        choices=list(labels),
        meta_information=meta,
        validate=validate,
        match_middle=True,
    ).unsafe_ask()
    return labels[matching(choice)[0]]


def resolve_service(cfg: Config, name: Optional[str], path: Optional[Path]) -> Path:
    if path:
        if service := runner.find_service_upwards(path.expanduser().resolve()):
            return service
        fail(_("{path} is not (and is not inside) a service.", path=path))
    if name:
        root, candidates = list_services(cfg)
        exact = [c for c in candidates if name in (c.name, label(c, root))]
        matches = exact or [c for c in candidates if name in label(c, root)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            fail(_("No service matches '{name}' in {root}.", name=name, root=root))
        return choose_service(matches, root)
    if service := runner.find_service_upwards(Path.cwd().resolve()):
        return service
    root, candidates = list_services(cfg)
    return choose_service(candidates, root)


@app.command("services", help=_("List the services available in the backend folder."))
def services_cmd(filter: Optional[str] = typer.Argument(None, help=_("Filter by text."))) -> None:
    cfg = Config.load()
    root, candidates = list_services(cfg)
    running = running_by_service()
    table = Table(_("Service"), _("Running"), title=str(root), title_justify="left")
    for c in candidates:
        text = label(c, root)
        if filter and filter not in text:
            continue
        ports = running.get(str(c), [])
        table.add_row(text, "[green]" + ", ".join(f":{p}" for p in ports) + "[/]" if ports else "")
    console.print(table)


# --------------------------------------------------------------------------- run


@dataclass
class Profile:
    service: Path
    user_name: str
    user: DevUser
    db_name: str
    db: Database
    host: str
    port: int


def choose_profile(
    cfg: Config,
    service_name: Optional[str],
    path: Optional[Path],
    user: Optional[str],
    db: Optional[str],
    port: Optional[int],
    host: Optional[str],
    yes: bool,
    needs_port: bool = True,
) -> Profile:
    """Resolve service, dev user, database and a free port, asking for whatever was not given."""
    service = resolve_service(cfg, service_name, path)
    interactive = sys.stdin.isatty()

    if not cfg.users:
        console.print(_("[yellow]No users configured, let's create one.[/]"))
        add_user(cfg)
    if not cfg.dbs:
        console.print(_("[yellow]No databases configured, let's create one.[/]"))
        add_db(cfg)

    user_name = pick(cfg.users, _("user"), user, cfg.last_user)
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    dev_user, database = cfg.users[user_name], cfg.dbs[db_name]
    if not database.password:
        console.print("[yellow]" + _("⚠ Database '{name}' has no password configured.", name=db_name) + "[/]")

    host = host or cfg.defaults.host
    if not needs_port:  # SQS consumers listen on a queue, not on a port
        return Profile(service, user_name, dev_user, db_name, database, host, 0)
    if port is None and interactive:
        port = prompts.ask_port(actions.suggested_port(cfg, host), lambda p: actions.port_available(host, p))
    try:
        port = actions.free_port(host, port or cfg.defaults.port)
    except actions.PortBusy as busy:
        if yes or not interactive:
            fail(_("Port {port} is in use (the next free one is {free}). Use --port.", port=busy.port, free=busy.free))
        if not questionary.confirm(_("Port {port} is in use. Use {free} instead?", port=busy.port, free=busy.free),
                                   default=True).unsafe_ask():
            raise typer.Exit(1)
        port = busy.free
    return Profile(service, user_name, dev_user, db_name, database, host, port)


def print_summary(cfg: Config, prof: Profile, extra: dict[str, str], show_service: bool = True) -> None:
    summary = Table.grid(padding=(0, 2))
    if show_service:
        summary.add_row(f"[bold]{_('Service')}[/]", str(prof.service))
    summary.add_row(f"[bold]{_('User')}[/]", f"{prof.user_name} → {prof.user.username} [dim]({prof.user.roles})[/]")
    summary.add_row("[bold]DB[/]", f"{prof.db_name} → {prof.db.url(mask=True)}")
    for key, value in extra.items():
        summary.add_row(f"[bold]{key}[/]", value)
    console.print(summary)


def confirm_protected(cfg: Config, prof: Profile, yes: bool) -> None:
    try:
        actions.check_database(cfg, prof.db_name, confirmed=yes)
    except actions.ProtectedDatabase:
        prompts.require_tty()
        if not questionary.confirm(_("'{name}' is a protected DB. Continue?", name=prof.db_name),
                                   default=False).unsafe_ask():
            raise typer.Exit(1)
    actions.remember_profile(cfg, prof.user_name, prof.db_name)


def poetry_install(service: Path) -> None:
    console.rule("poetry lock && poetry install")
    try:
        actions.install_service(service)
    except actions.ActionError as exc:
        fail(exc.message)


def install_label(cfg: Config, install: Optional[bool], each: bool = False) -> str:
    if install is False or (install is None and not cfg.defaults.install):
        return _("no")
    if install is None and cfg.defaults.smart_install:
        return _("only if something changed (-i to force)")
    return _("poetry lock && poetry install (each)") if each else "poetry lock && poetry install"


def ensure_installed(cfg: Config, service: Path, install: Optional[bool]) -> None:
    """Install according to the flag: True forces it, False skips it, None follows the settings (smart by default)."""
    if actions.needs_install(cfg, service, install):
        poetry_install(service)
    elif actions.install_wanted(cfg, install):
        console.print("[green]✓[/] " + _(
            "{name}: dependencies up to date (nothing changed since the last install), skipping.", name=service.name
        ))
    else:
        try:
            runner.ensure_poetry()
        except RuntimeError as exc:
            fail(str(exc))


EVENTS_HELP = _("Where the service publishes SQS events: auto, local (pdms events broker) or aws.")


def settle(action: Callable[[], T]) -> T:
    """Run an action, answering in the terminal what it asks (the local ElasticMQ) and failing on its errors."""
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


def warn_events(setup: actions.EventsSetup) -> None:
    if setup.warning:
        console.print(f"[yellow]{setup.warning}[/]")


def events_for(cfg: Config, service: Path, mode: Optional[str]) -> tuple[dict[str, str], str, str]:
    """``(extra environment, summary label, kind)`` for publishing events, kind being ``local`` or ``aws``."""
    setup = settle(lambda: actions.events_setup(cfg, service, mode))
    warn_events(setup)
    return setup.env, setup.label, setup.kind


def do_run(
    service_name: Optional[str] = None,
    user: Optional[str] = None,
    db: Optional[str] = None,
    port: Optional[int] = None,
    host: Optional[str] = None,
    install: Optional[bool] = None,
    reload: Optional[bool] = None,
    yes: bool = False,
    path: Optional[Path] = None,
    background: Optional[bool] = None,
    events_mode: Optional[str] = None,
) -> None:
    cfg = Config.load()
    service = resolve_service(cfg, service_name, path)
    consumer = actions.consumer_of(cfg, service)
    prof = choose_profile(cfg, None, service, user, db, port, host, yes, needs_port=not consumer)
    if background is None:
        background = sys.stdin.isatty() and prompts.ask_background()
    launch = settle(lambda: actions.plan_service(
        cfg, prof.service, user_name=prof.user_name, db_name=prof.db_name, port=prof.port or None, host=prof.host,
        reload=reload, events_mode=events_mode,
    ))
    warn_events(launch.events)

    if launch.is_consumer:
        what = {_("Consumer"): f"{launch.queue} → {consumer[1].handler}"}
    else:
        what = {_("Server"): f"http://{launch.host}:{launch.port}  reload={yes_no(launch.reload)}  "
                             f"log={cfg.defaults.logging_level}"}
    print_summary(cfg, prof, {
        **what,
        _("Mode"): _("background") if background else _("foreground"),
        _("Events"): launch.events.label,
        _("Install"): install_label(cfg, install),
    })
    confirm_protected(cfg, prof, yes)
    ensure_installed(cfg, launch.service, install)

    if not background:
        console.rule(launch.queue if launch.is_consumer else f"uvicorn :{launch.port}")
        actions.exec_service(cfg, launch)

    wait_until_ready(actions.start_service(cfg, launch))


def wait_until_ready(inst: instances.Instance, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    with console.status(_("Starting {key}...", key=inst.key)):
        while time.monotonic() < deadline:
            health = instances.health(inst)
            if health.state == "stopped":
                instances.forget(inst.key)
                console.print(instances.tail(inst.log, 30), markup=False, highlight=False)
                fail(_("{key} exited while starting. Full log: {log}", key=inst.key, log=inst.log))
            if health.state == "ok" and inst.is_consumer:
                console.print("[green]✓[/] " + _("{key} is consuming {queue} (pid {pid})",
                                                 key=inst.key, queue=inst.queue, pid=inst.pid))
                break
            if health.state == "ok":
                console.print(f"[green]✓[/] " + _(
                    "{key} is responding at http://localhost:{port} (pid {pid})",
                    key=inst.key, port=inst.port, pid=inst.pid,
                ))
                print_endpoints(inst, limit=6)
                break
            if health.state == "error":
                console.print(instances.tail(inst.log, 30), markup=False, highlight=False)
                console.print(
                    f"[yellow]{_('⚠ {key} started with errors: {detail}', key=inst.key, detail=health.detail)}[/]",
                    highlight=False,
                )
                if inst.reload:
                    console.print(_("  Still alive: once you save the fix, --reload will reload it."))
                break
            time.sleep(0.5)
        else:
            console.print(
                f"[yellow]{_('⚠ {key} is not responding after {timeout}s; check the logs.', key=inst.key, timeout=f'{timeout:.0f}')}[/]"
            )
    console.print(_("  Logs: [bold]pdms logs {key}[/]   Stop: [bold]pdms stop {key}[/]", key=inst.key))


@app.command(help=_("Install dependencies and run the service with uvicorn."))
def run(
    service: Optional[str] = typer.Argument(
        None, help=_("Service (name or path relative to the backend folder)."), autocompletion=completion.services
    ),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("Development user alias."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    port: Optional[int] = typer.Option(None, "--port", "-p"),
    host: Optional[str] = typer.Option(None, "--host"),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    reload: Optional[bool] = typer.Option(None, "--reload/--no-reload"),
    background: Optional[bool] = typer.Option(None, "--background/--foreground", "-b/-f", help=_("Background or foreground.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help=_("Service folder (defaults to the current one).")),
    events_mode: Optional[str] = typer.Option(None, "--events", help=EVENTS_HELP, autocompletion=completion.event_modes),
) -> None:
    do_run(service, user, db, port, host, install, reload, yes, path, background, events_mode)


# --------------------------------------------------------------------------- debugging


@app.command(help=_("Create/update the VS Code configuration (launch.json) to debug the service with breakpoints."))
def debug(
    service: Optional[str] = typer.Argument(
        None, help=_("Service (name or path relative to the backend folder)."), autocompletion=completion.services
    ),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("Development user alias."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    port: Optional[int] = typer.Option(None, "--port", "-p"),
    host: Optional[str] = typer.Option(None, "--host"),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help=_("Service folder (defaults to the current one).")),
    events_mode: Optional[str] = typer.Option(None, "--events", help=EVENTS_HELP, autocompletion=completion.event_modes),
) -> None:
    cfg = Config.load()
    target = resolve_service(cfg, service, path)
    consumer = actions.consumer_of(cfg, target)
    if consumer:
        events_mode = "local"
    prof = choose_profile(cfg, None, target, user, db, port, host, yes, needs_port=not consumer)
    events_env, events_label, _events_kind = events_for(cfg, prof.service, events_mode)
    program = events.poller_command("", consumer[0], consumer[1], cfg.defaults.events_port)[3:] if consumer else None
    print_summary(cfg, prof, {
        _("Debug"): f"{consumer[0].name} -> {consumer[1].handler}" if consumer else
        _("http://{host}:{port} (no --reload, so breakpoints work)", host=prof.host, port=prof.port),
        _("Events"): events_label,
        _("Install"): install_label(cfg, install),
    })
    confirm_protected(cfg, prof, yes)
    ensure_installed(cfg, prof.service, install)
    python = runner.poetry_python(prof.service)
    if not python:
        console.print(_("[yellow]The service has no virtualenv yet; installing dependencies.[/]"))
        poetry_install(prof.service)
        python = runner.poetry_python(prof.service) or fail(
            _("Could not find the virtualenv python (poetry env info -e).")
        )

    env_file = vscode.write_env_file(prof.service, runner.service_env(cfg.defaults, prof.user, prof.db, events_env))
    launch, name, backup = vscode.upsert_configuration(
        prof.service, python=python, env_file=env_file, host=prof.host, port=prof.port,
        description=f"{prof.user_name} @ {prof.db_name} " + (f"sqs {consumer[0].name}" if consumer else f":{prof.port}"),
        program=program,
    )
    console.print("[green]✓[/] " + _("Configuration [bold]{name}[/] saved to {launch}", name=name, launch=launch))
    if backup:
        console.print(f"[yellow]{_('⚠ launch.json had comments and they were lost; original copy at {backup}', backup=backup)}[/]")
    console.print(_(
        "  Variables (including the password) in {env_file} [dim](outside the repo, permissions 600)[/]",
        env_file=env_file,
    ))
    console.print(_("  In VS Code: Run and Debug (Ctrl+Shift+D) → pick the configuration → F5."))
    console.print(_(
        "  [dim]Note: common/ libraries are installed as a copy (develop = false); to stop inside them set the\n"
        "  breakpoint in the copy under .venv/lib/.../site-packages, or step in with F11 from the service.[/]"
    ))


@app.command(help=_("Print the variables of a profile. Usage: eval \"$(pdms env -u supervisor -d local)\"."))
def env(
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("Development user alias."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    dotenv: bool = typer.Option(False, "--dotenv", help=_(".env format (KEY=\"value\") instead of export.")),
) -> None:
    cfg = Config.load()
    if not sys.stdout.isatty():
        # Inside $(...) prompts would end up in the captured output: fall back to the last choice.
        user, db = user or cfg.last_user or None, db or cfg.last_db or None
    user_name = pick(cfg.users, _("user"), user, cfg.last_user)
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    variables = runner.service_env(cfg.defaults, cfg.users[user_name], cfg.dbs[db_name])
    for key, value in variables.items():
        print(f'{key}="{value}"' if dotenv else f"export {key}={shlex.quote(value)}")


# --------------------------------------------------------------------------- tests and migrations

PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


def run_in_service(service: Path, cmd: list[str], env: dict[str, str]) -> None:
    console.rule(" ".join([Path(cmd[0]).stem, *cmd[1:]]))
    try:
        result = subprocess.run(cmd, cwd=service, env=env)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    raise typer.Exit(result.returncode)


@app.command(context_settings=PASSTHROUGH, help=_(
    "Run the service's tests (poetry run pytest). Extra arguments go to pytest, e.g. pdms test -- -k name -x."
))
def test(
    ctx: typer.Context,
    service: Optional[str] = typer.Argument(
        None, help=_("Service (name or path relative to the backend folder)."), autocompletion=completion.services
    ),
    user: Optional[str] = typer.Option(
        None, "--user", "-u", help=_("Inject this user's DEV_* variables."), autocompletion=completion.users
    ),
    db: Optional[str] = typer.Option(
        None, "--db", "-d", help=_("Inject this database's DB_PG_CONNECTION_STR."), autocompletion=completion.dbs
    ),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n",
        help=_("Force (-i) or skip (-n) the install; by default only if something changed."),
    ),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help=_("Service folder (defaults to the current one).")),
) -> None:
    cfg = Config.load()
    target = resolve_service(cfg, service, path)
    env = runner.poetry_environ()
    if user or db:
        user_name = pick(cfg.users, _("user"), user, cfg.last_user)
        db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
        prof = Profile(target, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], cfg.defaults.host, 0)
        confirm_protected(cfg, prof, False)
        env.update(runner.service_env(cfg.defaults, prof.user, prof.db))
        console.print(_("Profile: {user} @ {db}", user=user_name, db=db_name))
    ensure_installed(cfg, target, install)
    run_in_service(target, [runner.poetry(), "run", "pytest", *ctx.args], env)


def resolve_migrations(cfg: Config, given: Optional[Path]) -> Path:
    """The pdms-db-migrations checkout: --migrations, the current folder, the one saved for the repo, or a sibling."""
    root = repos.active_root(cfg)
    alias = repos.alias_of(cfg, root) if root else None
    repo = cfg.repos.get(alias) if alias else None

    def remember(path: Path) -> Path:
        path = path.resolve()
        if repo and repo.migrations != str(path):
            repo.migrations = str(path)
            cfg.save()
            console.print(_("[dim]Migrations repo saved for '{alias}': {path}[/]", alias=alias, path=path))
        return path

    if given:
        found = migrations.find_upwards(given.expanduser())
        if not found:
            fail(_("{path} is not a Flyway migrations repo (flyway.toml + migrations/).", path=given))
        return remember(found)
    if here := migrations.find_upwards(Path.cwd()):
        return here
    if repo and repo.migrations and migrations.is_migrations_repo(Path(repo.migrations)):
        return Path(repo.migrations)
    candidates = migrations.siblings(root) if root else []
    if len(candidates) == 1:
        return remember(candidates[0])
    if candidates:
        suggested = migrations.best_match(root, candidates)
        if not interactive_terminal():
            if suggested:
                return remember(suggested)
            fail(_("Several migrations repos found ({names}); choose one with --migrations PATH.",
                   names=", ".join(c.name for c in candidates)))
        choice = questionary.select(
            _("Which migrations repo goes with '{alias}'?", alias=alias or root.name),
            choices=[questionary.Choice(str(c), c) for c in candidates], default=suggested,
        ).unsafe_ask()
        return remember(choice)
    fail(_("No Flyway migrations repo found. Clone pdms-db-migrations next to the PDMS repo, or use --migrations PATH."))


@app.command(help=_(
    "Flyway (pdms-db-migrations): info and validate against any database; migrate only against a local one."
))
def migrate(
    command: str = typer.Argument("info", help=_("info (default), validate or migrate."),
                                  autocompletion=completion.flyway_commands),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    migrations_path: Optional[Path] = typer.Option(
        None, "--migrations", "-m", help=_("pdms-db-migrations folder (remembered for the repo).")
    ),
) -> None:
    if command not in migrations.COMMANDS:
        fail(_("'{command}' is not allowed from pdms. Available: {codes}.", command=command,
               codes=", ".join(migrations.COMMANDS)))
    cfg = Config.load()
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    database = cfg.dbs[db_name]
    if command == "migrate" and not migrations.is_local(database):
        fail(_("migrate only runs against a local database (localhost, not protected). '{name}' ({host}) is shared: "
               "it is migrated by the pdms-db-migrations pipeline.", name=db_name, host=database.host))
    repo = resolve_migrations(cfg, migrations_path)
    ok, detail = events.docker_available()
    if not ok:
        fail(_("Docker is not available: {detail}", detail=detail or _("docker not found")))
    summary = Table.grid(padding=(0, 2))
    summary.add_row(f"[bold]{_('Migrations')}[/]", str(repo))
    summary.add_row("[bold]DB[/]", f"{db_name} → {database.url(mask=True)}")
    summary.add_row("[bold]Flyway[/]", f"{command} · {migrations.IMAGE.rsplit('/', 1)[-1]}")
    console.print(summary)
    if not migrations.image_present():
        with console.status(_("Downloading the Flyway image (about 360 MB, only the first time)...")):
            pulled, error = migrations.pull_image()
        if not pulled:
            fail(_("Could not download {image}: {error}. Check the connection (public ECR also limits anonymous "
                   "downloads; try again in a few minutes).", image=migrations.IMAGE, error=error))
    console.rule(f"flyway {command}")
    try:
        result = subprocess.run(migrations.docker_command(repo, database, command),
                                env={**os.environ, "DB_PASSWORD": database.password})
    except KeyboardInterrupt:
        raise typer.Exit(130)
    raise typer.Exit(result.returncode)


# --------------------------------------------------------------------------- background instances


def status_text(state: str) -> str:
    return {
        "ok": "[green]● ok[/]",
        "starting": f"[cyan]… {_('starting')}[/]",
        "error": "[yellow]⚠ error[/]",
        "stopped": f"[red]✗ {_('stopped')}[/]",
    }[state]


def stale_dependencies(items: list[instances.Instance]) -> dict[str, list[str]]:
    """Instances whose installed parts (common/ libraries, pyproject, lock) changed since they started."""
    with_deps = [i for i in items if i.deps]
    if not with_deps:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(with_deps))) as pool:
        results = pool.map(lambda i: installer.changed_parts(i.deps, Path(i.service)), with_deps)
    return {inst.key: changed for inst, changed in zip(with_deps, results) if changed}


def uptime(started_at: str) -> str:
    seconds = int((datetime.now() - datetime.fromisoformat(started_at)).total_seconds())
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def pick_instance(key: Optional[str], only_alive: bool = False, message: Optional[str] = None) -> instances.Instance:
    items = [i for i in instances.load().values() if i.alive() or not only_alive]
    if not items:
        fail(_("No background services. Start one with [bold]pdms run -b[/]."))
    if key:
        exact = [i for i in items if i.key == key]
        items = exact or [i for i in items if i.key.startswith(key) or key in i.service]
        if not items:
            fail(_("No instance matches '{key}'. See [bold]pdms ps[/].", key=key))
    if len(items) == 1:
        return items[0]
    prompts.require_tty()
    choices = [questionary.Choice(f"{i.key}  ({_('running') if i.alive() else _('stopped')})", i) for i in items]
    return questionary.select(message or _("Choose an instance:"), choices=choices).unsafe_ask()


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


@app.command(help=_("Show the endpoints (method and full URL) of background services."))
def urls(
    keys: Optional[list[str]] = typer.Argument(
        None, help=_("Instances (or parts of the service name)."), autocompletion=completion.instance_keys
    ),
    contains: str = typer.Option("", "--filter", "-f", help=_("Only endpoints whose path contains this text.")),
) -> None:
    if keys:
        targets = []
        for key in keys:
            inst = pick_instance(key, only_alive=True)
            if inst not in targets:
                targets.append(inst)
    else:
        targets = [i for i in instances.load().values() if i.alive() and not i.is_consumer]
        if not targets:
            fail(_("No background services. Start one with [bold]pdms run -b[/]."))
    for inst in targets:
        console.rule(inst.key)
        print_endpoints(inst, contains=contains)


@app.command(help=_("List background services."))
def ps(clean: bool = typer.Option(False, "--clean", help=_("Forget stopped instances."))) -> None:
    items = instances.load()
    if clean:
        for inst in [i for i in items.values() if not i.alive()]:
            instances.forget(inst.key)
            items.pop(inst.key)
    running_proxy = proxy.running_proxy()
    running_frontend = frontend.running()
    if not items and not running_proxy and not running_frontend:
        console.print(_("No background services."))
        return
    healths = instances.health_all(list(items.values()))
    cfg = Config.load()
    table = Table(_("Instance"), _("Status"), "URL", "Repo", _("User"), "DB", _("Uptime"))
    table.columns[0].no_wrap = table.columns[1].no_wrap = table.columns[2].no_wrap = True
    for inst in items.values():
        state = healths[inst.key].state
        repo = repos.repo_of(cfg, inst.service) or "-"
        table.add_row(
            inst.key, status_text(state), f"sqs ← {inst.queue}" if inst.is_consumer else f"http://localhost:{inst.port}",
            f"[bold]{repo}[/]" if repo == cfg.current_repo else repo,
            inst.user, inst.db, uptime(inst.started_at) if state != "stopped" else "",
        )
    if running_proxy:
        port = running_proxy["port"]
        repo = repos.repo_of(cfg, running_proxy["repo"]) if running_proxy.get("repo") else None
        repo = repo or "-"
        table.add_row(
            proxy.display_key(running_proxy),
            status_text("ok" if instances.responds("127.0.0.1", port) else "starting"), f"http://localhost:{port}",
            f"[bold]{repo}[/]" if repo == cfg.current_repo else repo, running_proxy.get("as") or "-", "-",
            uptime(running_proxy["started_at"]) if running_proxy.get("started_at") else "",
        )
    if running_frontend:
        repo = repos.repo_of(cfg, running_frontend.get("root", "")) or "-"
        health = frontend.health(running_frontend)
        table.add_row(
            f"{frontend.KEY} ({running_frontend.get('mode', 'dev')})", status_text(health.state),
            frontend.url(running_frontend["port"], health.detail if health.state == "ok" else "https"), f"[bold]{repo}[/]" if repo == cfg.current_repo else repo, "-", "-",
            uptime(running_frontend["started_at"]) if running_frontend.get("started_at") else "",
        )
    console.print(table)
    for key, health in healths.items():
        if health.state == "error":
            console.print(f"[yellow]⚠ {key}:[/] {health.detail}  [dim](pdms logs {key})[/]", highlight=False)
    for key, changed in stale_dependencies([i for i in items.values() if healths[i.key].state != "stopped"]).items():
        names = ", ".join(_("the service itself") if n == installer.SERVICE_PART else n for n in changed)
        console.print(
            f"[yellow]⚠ {key}:[/] " + _("installed code changed since it started ({names}); --reload does not pick "
                                        "it up → pdms restart {key}", names=names, key=key),
            highlight=False,
        )
    if any(h.state == "stopped" for h in healths.values()):
        console.print(_("[dim]Stopped ones keep their log (pdms logs <instance>). Remove them with pdms ps --clean.[/]"))


@app.command(help=_("Show the console of background services (Ctrl+C to exit)."))
def logs(
    keys: Optional[list[str]] = typer.Argument(
        None, help=_("Instances (or parts of the service name), proxy, frontend, sns (what was published to SNS locally) or ui."),
        autocompletion=completion.log_keys,
    ),
    all_: bool = typer.Option(False, "--all", "-a", help=_("All running instances, including ones started later.")),
    stack: Optional[str] = typer.Option(None, "--stack", "-s", help=_("All instances of a stack."), autocompletion=completion.stacks),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-F/-N", help=_("Follow the output live.")),
    lines: Optional[int] = typer.Option(
        None, "--lines", "-l", help=_("Previous lines to show per instance (100, or 20 with several).")
    ),
    previous: bool = typer.Option(
        False, "--previous", "-p", help=_("Show the log of the previous run (kept when restarting).")
    ),
) -> None:
    if keys and proxy.is_key(keys[0]):
        return proxy_logs(follow, lines, previous)
    if keys and keys[0] == events.SNS_KEY:
        return sns_logs(follow, lines, previous)
    if keys and frontend.is_key(keys[0]):
        return frontend_logs(follow, lines, previous)
    if keys and keys[0] == "ui":
        return ui_logs(follow, lines, previous)
    if previous:
        inst = pick_instance(keys[0] if keys else None, message=_("Which instance do you want to see the logs of?"))
        old = instances.previous_log_path(inst.log)
        if not old.exists():
            fail(_("{key} has no previous log.", key=inst.key))
        console.rule(_("{key} · previous run", key=inst.key))
        console.print(instances.tail(str(old), lines or 100), markup=False, highlight=False, end="")
        return
    discover = None
    if all_:
        targets = [i for i in instances.load().values() if i.alive()]
        if not targets and not follow:
            fail(_("No background services. Start one with [bold]pdms run -b[/]."))
        discover = lambda: [i for i in instances.load().values() if i.alive()]  # noqa: E731
    elif stack:
        cfg = Config.load()
        name = pick(cfg.stacks, _("stack"), stack)
        paths = {str(p) for p in stack_paths(cfg, cfg.stacks[name])}
        targets = [i for i in instances.load().values() if i.service in paths]
        if not targets:
            fail(_("Nothing from stack '{name}' is running.", name=name))
    elif keys:
        targets = []
        for key in keys:
            inst = pick_instance(key)
            if inst not in targets:
                targets.append(inst)
    else:
        items = list(instances.load().values())
        if len(items) > 1:
            prompts.require_tty()
            everything = "__all__"
            choice = questionary.select(
                _("Which instance do you want to see the logs of?"),
                choices=[
                    questionary.Choice(_("All running instances"), everything),
                    *[questionary.Choice(f"{i.key}  ({_('running') if i.alive() else _('stopped')})", i) for i in items],
                ],
            ).unsafe_ask()
            if choice == everything:
                return logs(None, True, None, follow, lines, False)
            targets = [choice]
        else:
            targets = [pick_instance(None)]

    lines = lines if lines is not None else (100 if len(targets) == 1 and not all_ else 20)
    if not follow:
        for inst in targets:
            if len(targets) > 1:
                console.rule(inst.key)
            console.print(instances.tail(inst.log, lines), markup=False, highlight=False, end="")
        return
    names = ", ".join(i.key for i in targets) or _("(waiting for instances)")
    console.rule(_("{names} · Ctrl+C to exit", names=names))
    logview.follow(console, targets, lines, discover)


def ui_logs(follow: bool, lines: Optional[int], previous: bool) -> None:
    """What pdms ui wrote when opened from the app menu (no terminal), and its updates."""
    from .ui import instance as ui_instance

    log = ui_instance.log_path()
    if previous:
        log = instances.previous_log_path(log)
    if not log.exists():
        fail(_("pdms ui has no log: it only writes one when opened from the app menu."))
    if previous or not follow:
        console.print(instances.tail(str(log), lines or 100), markup=False, highlight=False, end="")
        return
    console.rule(_("{names} · Ctrl+C to exit", names="pdms ui"))
    inst = instances.Instance(key=ui_instance.KEY, pid=0, service="", host="", port=0, user="", db="", reload=False,
                              log=str(log), started_at="")
    logview.follow(console, [inst], lines or 100, None)


def sns_logs(follow: bool, lines: Optional[int], previous: bool) -> None:
    """What the services published to SNS locally (see pdms events peek pdms-sns)."""
    log = events.sns_log_path()
    if previous:
        log = instances.previous_log_path(log)
    if not follow or previous:
        if not log.exists():
            fail(_("Nothing was published to the local SNS yet."))
        console.print(instances.tail(str(log), lines or 100), markup=False, highlight=False, end="")
        return
    console.rule(_("{names} · Ctrl+C to exit", names=_("local SNS")))
    sns = instances.Instance(key=events.SNS_KEY, pid=0, service="", host="", port=0, user="", db="", reload=False,
                             log=str(log), started_at="")
    logview.follow(console, [sns], lines or 100, None)


def proxy_logs(follow: bool, lines: Optional[int], previous: bool) -> None:
    """The requests of the background proxy (a foreground one shows them in its own terminal)."""
    log = proxy.log_path()
    if previous:
        log = instances.previous_log_path(log)
    if not log.exists():
        fail(_("The proxy has no log. Start it in the background with [bold]pdms proxy -b[/]."))
    running = proxy.running_proxy()
    key = proxy.display_key(running) if running else proxy.KEY
    if previous or not follow or not running:
        console.print(instances.tail(str(log), lines or 100), markup=False, highlight=False, end="")
        return
    console.rule(_("{names} · Ctrl+C to exit", names=key))
    inst = instances.Instance(
        key=key, pid=int(running["pid"]), service=running.get("repo", ""), host=actions.PROXY_HOST,
        port=running["port"], user=running.get("as", ""), db="", reload=False, log=str(log),
        started_at=running.get("started_at", ""), created=float(running.get("created", 0)),
    )
    logview.follow(console, [inst], lines or 100, None)


@app.command("open", help=_("Open a background service in the browser (Swagger /docs by default)."))
def open_cmd(
    key: Optional[str] = typer.Argument(None, help=_("Instance (or part of the service name)."), autocompletion=completion.instance_keys),
    path: str = typer.Option("/docs", "--path", "-P", help=_("Path to open, e.g. /redoc or /.")),
) -> None:
    inst = pick_instance(key, only_alive=True, message=_("Which instance do you want to open?"))
    if inst.is_consumer:
        fail(_("{key} is an event consumer: it has no web page. See its logs: pdms logs {key}", key=inst.key))
    url = f"http://localhost:{inst.port}/{path.lstrip('/')}"
    console.print(_("Opening {url}", url=url))
    if not webbrowser.open(url):
        console.print(_("[yellow]Could not open a browser; open the URL manually.[/]"))


@app.command(help=_("Stop background services."))
def stop(
    key: Optional[str] = typer.Argument(
        None, help=_("Instance (or part of the service name), proxy or frontend."),
        autocompletion=completion.instance_or_proxy_keys
    ),
    all_: bool = typer.Option(False, "--all", "-a", help=_("Stop all, the proxy and the frontend included.")),
) -> None:
    running = [i for i in instances.load().values() if i.alive()]
    running_proxy = proxy.running_proxy()
    running_frontend = frontend.running()
    stop_proxy = stop_frontend = False
    if all_:
        targets, stop_proxy, stop_frontend = running, bool(running_proxy), bool(running_frontend)
    elif key and frontend.is_key(key):
        if not running_frontend:
            fail(_("The frontend is not running."))
        targets, stop_frontend = [], True
    elif key and proxy.is_key(key):
        if not running_proxy:
            fail(_("The proxy is not running."))
        targets, stop_proxy = [], True
    elif key:
        targets = [pick_instance(key)]
    elif not running_proxy and not running_frontend and len(running) <= 1:
        targets = [pick_instance(None, only_alive=True)]
    elif not running and not (running_proxy and running_frontend):
        targets, stop_proxy, stop_frontend = [], bool(running_proxy), bool(running_frontend)
    else:
        prompts.require_tty()
        chosen = questionary.checkbox(
            _("Which instances do you want to stop? (space to select)"),
            choices=[
                *[questionary.Choice(i.key, i) for i in running],
                *([questionary.Choice(proxy.display_key(running_proxy), proxy.KEY)] if running_proxy else []),
                *([questionary.Choice(frontend.KEY, frontend.KEY)] if running_frontend else []),
            ],
        ).unsafe_ask()
        targets = [c for c in chosen if c not in (proxy.KEY, frontend.KEY)]
        stop_proxy, stop_frontend = proxy.KEY in chosen, frontend.KEY in chosen
    if not targets and not stop_proxy and not stop_frontend:
        console.print(_("Nothing to stop."))
    for inst in targets:
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
    if stop_proxy:
        key = proxy.display_key(running_proxy)
        with console.status(_("Stopping {key}...", key=key)):
            restored = actions.stop_proxy(running_proxy)
        console.print("[green]✓[/] " + _("{key} stopped.", key=key))
        print_restored(restored)
    if stop_frontend:
        with console.status(_("Stopping {key}...", key=frontend.KEY)):
            actions.stop_frontend(running_frontend)
        console.print("[green]✓[/] " + _("{key} stopped.", key=frontend.KEY))


@app.command(help=_("Restart a background service (same port; same user and DB by default)."))
def restart(
    key: Optional[str] = typer.Argument(None, help=_("Instance (or part of the service name)."), autocompletion=completion.instance_keys),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("Switch to this user."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Switch to this database."), autocompletion=completion.dbs),
    change: bool = typer.Option(False, "--change", "-c", help=_("Ask which user and DB to use.")),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
) -> None:
    inst = pick_instance(key, message=_("Which instance do you want to restart?"))
    cfg = Config.load()
    if change:
        prompts.require_tty()
        user = user or prompts.select_name(_("User:"), list(cfg.users), inst.user)
        db = db or prompts.select_name(_("Database:"), list(cfg.dbs), inst.db)
    user, db = user or inst.user, db or inst.db
    pick(cfg.users, _("user"), user)
    pick(cfg.dbs, _("database"), db)
    # Confirm before stopping: a "no" must leave the instance running.
    database = cfg.dbs[db]
    if database.protected and db != inst.db:
        confirm_protected(cfg, Profile(Path(inst.service), user, cfg.users[user], db, database, inst.host, inst.port), False)
    with console.status(_("Stopping {key}...", key=inst.key)):
        actions.stop_service(inst)
    do_run(
        user=user, db=db, port=inst.port, host=inst.host, install=install, reload=inst.reload,
        yes=True, path=Path(inst.service), background=True,
        events_mode="local" if inst.events == "local" else None,
    )


# --------------------------------------------------------------------------- stacks


def print_stacks(cfg: Config) -> None:
    if not cfg.stacks:
        console.print(f"[yellow]{_('No stacks.')}[/] {_('Create one with [bold]pdms stack add[/].')}")
        return
    running = running_by_service()
    root = services_root(cfg)
    ask = f"[dim]{_('ask')}[/]"
    table = Table("Stack", _("Services"), _("User"), "DB")
    for name, stack in cfg.stacks.items():
        lines = []
        for svc in stack.services:
            ports = running.get(str(root / svc), []) if root else []
            lines.append(f"{svc} [green]{' '.join(f':{p}' for p in ports)}[/]" if ports else svc)
        table.add_row(name, "\n".join(lines), stack.user or ask, stack.db or ask)
    console.print(table)


def ask_stack_services(root: Path, candidates: list[Path], current: list[str]) -> list[str]:
    """Every service of the repo in one list: the stack's first (checked), then the running ones, then the rest."""
    running = running_by_service()
    labels = {label(c, root): c for c in candidates}
    busy = sorted(t for t, path in labels.items() if str(path) in running and t not in current)
    rest = sorted(t for t in labels if t not in current and t not in busy)

    def title(text: str) -> str:
        if text not in busy:
            return text
        ports = [p for p in running[str(labels[text])] if p]  # SQS consumers have no port
        return f"{text}  " + (_("(running on :{ports})", ports=", :".join(map(str, ports))) if ports
                              else _("(running)"))

    order = [*current, *busy, *rest]
    question = questionary.checkbox(
        _("Stack services ({count} in the repo):", count=len(labels)),
        choices=[questionary.Choice(title(t), t, checked=t in current) for t in order],
        instruction=_("(type to filter, ↑↓ to move, space to check or uncheck, Enter to save)"),
        use_search_filter=True,
        use_jk_keys=False,
        validate=lambda picked: bool(picked) or _("A stack needs at least one service."),
    )
    # Tab (Ctrl+I) inverts and Ctrl+A checks every service of the repo, not only the filtered ones
    for key in (Keys.ControlI, Keys.ControlA):
        question.application.key_bindings.remove(key)
    picked = set(question.unsafe_ask())
    return [t for t in order if t in picked]


def ask_stack(cfg: Config, current: Optional[Stack] = None) -> Stack:
    prompts.require_tty()
    root, candidates = list_services(cfg)
    services = ask_stack_services(root, candidates, list(current.services) if current else [])
    ask = _("(ask when starting)")
    user = questionary.select(
        _("Stack user:"), choices=[ask, *cfg.users], default=(current.user if current and current.user else ask)
    ).unsafe_ask()
    db = questionary.select(
        _("Stack database:"), choices=[ask, *cfg.dbs], default=(current.db if current and current.db else ask)
    ).unsafe_ask()
    return Stack(services=services, user="" if user == ask else user, db="" if db == ask else db)


@stack_app.callback()
def stack_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        stack_menu()


@stack_app.command("list", help=_("List stacks."))
def stack_list() -> None:
    print_stacks(Config.load())


@stack_app.command("add", help=_("Create a stack (wizard)."))
def stack_add() -> None:
    cfg = Config.load()
    prompts.require_tty()
    name = prompts.ask_name(_("stack"), cfg.stacks)
    settle(lambda: actions.save_stack(cfg, name, ask_stack(cfg)))
    console.print("[green]✓[/] " + _("Stack '{name}' saved. Start it with [bold]pdms up {name}[/].", name=name))


@stack_app.command("edit", help=_("Edit a stack."))
def stack_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    stack = ask_stack(cfg, cfg.stacks[name])
    settle(lambda: actions.save_stack(cfg, name, stack))
    console.print("[green]✓[/] " + _("Stack '{name}' updated.", name=name))


@stack_app.command("remove", help=_("Delete a stack."))
def stack_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    if questionary.confirm(_("Delete stack '{name}'?", name=name), default=False).unsafe_ask():
        actions.remove_stack(cfg, name)
        console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))


def backend_root(cfg: Config) -> Path:
    return services_root(cfg) or fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))


def stack_paths(cfg: Config, stack: Stack) -> list[Path]:
    root = backend_root(cfg)
    return settle(lambda: actions.stack_paths(stack, root))


@app.command(help=_("Start all services of a stack in the background, each on a free port."))
def up(
    name: Optional[str] = typer.Argument(None, help=_("Stack to start."), autocompletion=completion.stacks),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("User (defaults to the stack's)."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database (defaults to the stack's)."), autocompletion=completion.dbs),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
    events_mode: Optional[str] = typer.Option(None, "--events", help=EVENTS_HELP, autocompletion=completion.event_modes),
) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    stack = cfg.stacks[name]
    root = backend_root(cfg)
    settle(lambda: actions.stack_paths(stack, root))  # a stale stack fails before asking for the user and DB
    user_name = pick(cfg.users, _("user"), user or stack.user or None, cfg.last_user)
    db_name = pick(cfg.dbs, _("database"), db or stack.db or None, cfg.last_db)
    plan = settle(lambda: actions.plan_stack(cfg, name, root, user_name=user_name, db_name=db_name,
                                             events_mode=events_mode))

    for path, ports in plan.running.items():
        console.print(_("[dim]· {name} already running on :{ports}, skipping.[/]",
                        name=path.name, ports=", :".join(map(str, ports))))
    if not plan.services or plan.events is None:
        console.print("[green]✓[/] " + _("The whole stack '{name}' is running.", name=name))
        return
    warn_events(plan.events)

    first = plan.services[0]
    profile = Profile(first.service, user_name, first.user, db_name, first.db, first.host, first.port)
    print_summary(cfg, profile, {
        _("Services"): "\n".join(f"{s.service.name} → " + (f"sqs ← {s.queue}" if s.is_consumer else f":{s.port}")
                                 for s in plan.services),
        _("Events"): plan.events.label,
        _("Install"): install_label(cfg, install, each=True),
    }, show_service=False)
    confirm_protected(cfg, profile, yes)

    started = actions.start_stack(cfg, plan, install=lambda path: ensure_installed(cfg, path, install))
    failed = 0
    for inst in started:
        try:
            wait_until_ready(inst)
        except typer.Exit:
            failed += 1
    if failed:
        fail(_("{failed} of {total} services of stack '{name}' did not start.",
               failed=failed, total=len(started), name=name))


@app.command(help=_("Stop all services of a stack."))
def down(name: Optional[str] = typer.Argument(None, help=_("Stack to stop."), autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    root = backend_root(cfg)
    targets = settle(lambda: actions.stack_instances(cfg, name, root))
    if not targets:
        console.print(_("Nothing from stack '{name}' is running.", name=name))
    for inst in targets:
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))


# --------------------------------------------------------------------------- db commands


@db_app.callback()
def db_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        db_menu()


@db_app.command("list", help=_("List databases."))
def db_list() -> None:
    print_dbs(Config.load())


@db_app.command("add", help=_("Add a database (wizard)."))
def db_add() -> None:
    add_db(Config.load())


@db_app.command("edit", help=_("Edit a database."))
def db_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.dbs)) -> None:
    cfg = Config.load()
    prompts.require_tty()
    name = pick(cfg.dbs, _("database"), name)
    database = prompts.ask_database(cfg.dbs[name])
    settle(lambda: actions.save_db(cfg, name, database))
    console.print("[green]✓[/] " + _("Database '{name}' updated.", name=name))


@db_app.command("remove", help=_("Delete a database."))
def db_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.dbs)) -> None:
    cfg = Config.load()
    name = pick(cfg.dbs, _("database"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        print_removed(name, actions.remove_db(cfg, name))


@db_app.command("test", help=_("Test the connection to one or all databases."))
def db_test(
    name: Optional[str] = typer.Argument(None, help=_("Empty = test all."), autocompletion=completion.dbs),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", "-t", help=_("Seconds to wait (defaults to the one in pdms config).")
    ),
) -> None:
    cfg = Config.load()
    targets = [pick(cfg.dbs, _("database"), name)] if name else list(cfg.dbs)
    if not targets:
        print_dbs(cfg)
    ok = all([check_db(n, cfg.dbs[n], timeout or cfg.defaults.db_timeout) for n in targets])
    raise typer.Exit(0 if ok else 1)


# --------------------------------------------------------------------------- user commands


@user_app.callback()
def user_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        user_menu()


@user_app.command("list", help=_("List users."))
def user_list() -> None:
    print_users(Config.load())


@user_app.command("add", help=_("Add a user (wizard)."))
def user_add() -> None:
    add_user(Config.load())


@user_app.command("edit", help=_("Edit a user."))
def user_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.users)) -> None:
    cfg = Config.load()
    prompts.require_tty()
    name = pick(cfg.users, _("user"), name)
    roles = actions.known_roles(cfg)
    user = prompts.ask_user(cfg.users[name], roles)
    settle(lambda: actions.save_user(cfg, name, user, roles=roles))
    console.print("[green]✓[/] " + _("User '{name}' updated.", name=name))


@user_app.command("rename", help=_("Give a user another name (also in the stacks that use it)."))
def user_rename(
    name: Optional[str] = typer.Argument(None, autocompletion=completion.users),
    new_name: Optional[str] = typer.Argument(None, help=_("The new name.")),
) -> None:
    cfg = Config.load()
    name = pick(cfg.users, _("user"), name)
    if not new_name:
        prompts.require_tty()
        new_name = questionary.text(_("New name for '{name}':", name=name), default=name).unsafe_ask()
    renamed = settle(lambda: actions.rename_user(cfg, name, new_name or name))
    console.print("[green]✓[/] " + _("User '{name}' is now '{new}'.", name=name, new=renamed))


@user_app.command("remove", help=_("Delete a user."))
def user_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.users)) -> None:
    cfg = Config.load()
    name = pick(cfg.users, _("user"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        print_removed(name, actions.remove_user(cfg, name))


@user_app.command("import", help=_("Create user profiles from the pdms_user table of a database."))
def user_import(
    db: Optional[str] = typer.Option(
        None, "--db", "-d", help=_("Database to read the users from."), autocompletion=completion.dbs
    ),
    search: Optional[str] = typer.Option(None, "--search", "-s", help=_("Filter by email or name.")),
    role: Optional[str] = typer.Option(
        None, "--role", "-r", help=_("Filter by role (e.g. TPR.Supervisor)."), autocompletion=completion.roles
    ),
    inactive: bool = typer.Option(False, "--inactive", help=_("Include inactive users.")),
    limit: int = typer.Option(200, "--limit", help=_("Maximum number of users to read.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Import every match without asking.")),
) -> None:
    cfg = Config.load()
    interactive = sys.stdin.isatty()
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    mapping, source = userimport.role_mapping(repos.active_root(cfg))
    console.print(_("[dim]Roles mapped with MAP_INTERNAL_ROLES from {source}.[/]", source=source)
                  if source != "built-in" else
                  _("[yellow]⚠ Could not read the roles of the current repo; using the built-in copy.[/]"))
    if search is None and interactive and not yes:
        search = questionary.text(_("Search by email or name (empty = all):")).unsafe_ask().strip()
    with console.status(_("Reading users from {name}...", name=db_name)):
        found = settle(lambda: actions.read_db_users(
            cfg, db_name, mapping, search=search or "", role=role or "", inactive=inactive, limit=limit,
        ))
    if not found:
        fail(_("No users match."))
    if len(found) == limit:
        console.print(_("[dim]Showing the first {limit}; narrow it down with --search or --role.[/]", limit=limit))

    known = {u.user_id for u in cfg.users.values()}
    if yes:
        picked = found
    else:
        prompts.require_tty()

        def title(u: userimport.DbUser) -> str:
            tags = [_("already imported")] if u.user_id in known else []
            if u.is_active is False:
                tags.append(_("inactive"))
            suffix = f"  ({', '.join(tags)})" if tags else ""
            return f"{u.first_name} {u.last_name} <{u.username}>  {u.dev_roles(mapping) or '-'}{suffix}"

        picked = questionary.checkbox(
            _("Which users do you want to import? (space to select)"),
            choices=[questionary.Choice(title(u), u) for u in found],
        ).unsafe_ask()
    if not picked:
        console.print(_("Nothing selected."))
        return

    added, updated = actions.import_users(cfg, picked, mapping)
    table = Table(_("Name"), "DEV_USERNAME", "DEV_ROLES", "")
    for alias in added + updated:
        u = cfg.users[alias]
        table.add_row(alias, u.username, u.roles, _("new") if alias in added else _("updated"))
    if added or updated:
        console.print(table)
    unchanged = len(picked) - len(added) - len(updated)
    console.print("[green]✓[/] " + _(
        "{added} added, {updated} updated, {unchanged} unchanged.", added=len(added), updated=len(updated),
        unchanged=unchanged,
    ))
    unknown = sorted({r for u in picked for r in u.unknown_roles(mapping)})
    if unknown:
        console.print("[yellow]" + _(
            "⚠ Unknown roles kept as they are (the services will ignore them): {roles}", roles=", ".join(unknown)
        ) + "[/]")


# --------------------------------------------------------------------------- repos


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def switch_repo(cfg: Config, alias: str) -> None:
    switch = settle(lambda: actions.use_repo(cfg, alias))
    console.print("[green]✓[/] " + _("Current repo: {alias} ({path})", alias=alias, path=cfg.repos[alias].root))
    handle_instances_of(switch, alias)
    if switch.proxy:
        console.print(_("[dim]The proxy still routes to '{old}': restart it (pdms proxy) to use '{new}'.[/]",
                        old=switch.old, new=alias))


def handle_instances_of(switch: actions.RepoSwitch, new: str) -> None:
    """Offer to keep, stop or move to the new repo the instances still running from the old one."""
    if not switch.running or not interactive_terminal():
        return
    choice = questionary.select(
        _("{count} instances are running from '{old}': {keys}. What should I do with them?",
          count=len(switch.running), old=switch.old, keys=", ".join(i.key for i, _target in switch.running)),
        choices=[
            questionary.Choice(_("Keep them running (they coexist, each on its port)"), "keep"),
            questionary.Choice(_("Stop them"), "stop"),
            questionary.Choice(_("Restart them from '{new}' (same user, DB and port)", new=new), "move"),
        ],
    ).unsafe_ask()
    if choice == "keep":
        return
    for inst, target in switch.running:
        if choice == "move" and target is None:
            console.print("[yellow]" + _("⚠ {key}: the service does not exist in '{new}'; left running.",
                                         key=inst.key, new=new) + "[/]")
            continue
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
        if choice == "move":
            try:
                do_run(user=inst.user, db=inst.db, port=inst.port, host=inst.host, reload=inst.reload,
                       yes=True, path=target, background=True)
            except typer.Exit:
                pass


def check_repo(cfg: Config) -> None:
    """If the current folder is a PDMS repo other than the current one, offer to switch to it."""
    here = repos.find_repo_root(Path.cwd())
    if here is None:
        return
    alias = repos.alias_of(cfg, here)
    if not cfg.repos or not cfg.repo:
        alias = repos.register(cfg, here)
        cfg.current_repo = alias
        cfg.save()
        console.print(_("[dim]Using {path} as the current repo '{alias}'.[/]", path=here, alias=alias))
        return
    if alias == cfg.current_repo or str(here) in cfg.ignored_repos:
        return
    if not interactive_terminal():
        repos.use_for_this_command(here)
        return
    choice = questionary.select(
        _("You are in {here}, but the current repo is '{current}' ({path}). What should I do?",
          here=here, current=cfg.current_repo, path=cfg.repo.root),
        choices=[
            questionary.Choice(_("Switch to {name} (it becomes the default)", name=alias or here.name), "switch"),
            questionary.Choice(_("Use it only for this command"), "once"),
            questionary.Choice(_("Don't ask again in this repo"), "ignore"),
        ],
    ).unsafe_ask()
    if choice == "switch":
        switch_repo(cfg, repos.register(cfg, here))
    elif choice == "once":
        repos.use_for_this_command(here)
    else:
        cfg.ignored_repos.append(str(here))
        cfg.save()


@repo_app.callback()
def repo_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        repo_menu()


@repo_app.command("list", help=_("List the registered repos."))
def repo_list() -> None:
    cfg = Config.load()
    if not cfg.repos:
        console.print(f"[yellow]{_('No repos registered.')}[/] {_('Use [bold]pdms repo add <path>[/].')}")
        return
    counts: dict[str, int] = {}
    for inst in instances.load().values():
        if inst.alive() and (alias := repos.repo_of(cfg, inst.service)):
            counts[alias] = counts.get(alias, 0) + 1
    table = Table("", _("Name"), _("Path"), _("Migrations"), _("Running"))
    for alias, repo in cfg.repos.items():
        path = str(repo.root) if repo.root.is_dir() else f"[red]{repo.root} ({_('missing')})[/]"
        table.add_row("●" if alias == cfg.current_repo else "", alias, path,
                      Path(repo.migrations).name if repo.migrations else "[dim]-[/]", str(counts.get(alias, "")))
    console.print(table)


@repo_app.command("add", help=_("Register a PDMS repo (defaults to the current folder)."))
def repo_add(
    path: Optional[Path] = typer.Argument(None, help=_("Folder inside the repo.")),
    alias: Optional[str] = typer.Option(None, "--alias", "-a", help=_("Name for the repo.")),
) -> None:
    cfg = Config.load()
    where = (path or Path.cwd()).expanduser()
    try:
        root = actions.repo_root(where)
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    if existing := repos.alias_of(cfg, root):
        console.print(_("{path} is already registered as '{alias}'.", path=root, alias=existing))
        return
    if alias is None and interactive_terminal():
        alias = prompts.ask_name(_("repo"), cfg.repos, repos.suggest_alias(cfg, root))
    was_empty = not cfg.repos
    try:
        alias = actions.add_repo(cfg, root, alias or "")
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    console.print("[green]✓[/] " + _("Repo '{alias}' registered ({path}).", alias=alias, path=root))
    if not was_empty and interactive_terminal() and questionary.confirm(
        _("Make it the current repo?"), default=True
    ).unsafe_ask():
        switch_repo(cfg, alias)


@repo_app.command("use", help=_("Choose the current repo."))
def repo_use(alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos)) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias, cfg.current_repo)
    if alias == cfg.current_repo:
        console.print(_("'{alias}' is already the current repo.", alias=alias))
        return
    switch_repo(cfg, alias)


@repo_app.command("edit", help=_("Change the name, the migrations repo or the proxy's remote API of a repo."))
def repo_edit(
    alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos),
    name: Optional[str] = typer.Option(None, "--alias", "-a", help=_("New name for the repo.")),
    migrations_path: Optional[str] = typer.Option(
        None, "--migrations", help=_("Flyway migrations checkout (pdms-db-migrations); '' to forget it.")),
    remote: Optional[str] = typer.Option(None, "--remote", help=_("Remote API for the proxy; '' to forget it.")),
) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias, cfg.current_repo)
    repo = cfg.repos[alias]
    if name is None and migrations_path is None and remote is None:
        prompts.require_tty()

        def ask(message: str, default: str, check: Callable[[str], object]) -> str:
            def validate(value: str) -> bool | str:
                try:
                    check(value)
                except actions.InvalidValue as invalid:
                    return invalid.reason
                return True

            return questionary.text(message, default=default, validate=validate).unsafe_ask().strip()

        others = [other for other in cfg.repos if other != alias]
        name = ask(_("Alias ({kind}):", kind=_("repo")), alias, lambda value: actions.check_alias(value, others))
        migrations_path = ask(_("Migrations repo (empty = look next to the repo):"), repo.migrations,
                              actions.migrations_repo)
        remote = ask(_("Remote API for the proxy (empty = from frontend/.env):"), repo.remote, actions.remote_api)
    try:
        alias = actions.edit_repo(cfg, alias, new_alias=name, migrations_path=migrations_path, remote=remote)
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    except actions.ActionError as error:
        fail(error.message)
    console.print("[green]✓[/] " + _("'{name}' saved.", name=alias))


@repo_app.command("remove", help=_("Forget a registered repo (nothing is deleted from disk)."))
def repo_remove(alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos)) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias)
    if interactive_terminal() and not questionary.confirm(_("Delete '{name}'?", name=alias), default=False).unsafe_ask():
        return
    settle(lambda: actions.remove_repo(cfg, alias))
    console.print("[green]✓[/] " + _("'{name}' deleted.", name=alias))


# --------------------------------------------------------------------------- proxy


def current_repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def load_repo_routes(root: Path, env: str) -> list[routes.Route]:
    with console.status(_("Reading the API routes from Terraform...")):
        return settle(lambda: actions.proxy_routes(root, env))


def resolve_remote(cfg: Config, root: Path, remote: Optional[str], no_remote: bool) -> Optional[str]:
    url, detected = actions.proxy_remote(cfg, root, remote, no_remote)
    if detected:
        console.print(_("[dim]Remote API taken from frontend/.env and saved for '{alias}': {url}[/]",
                        alias=repos.alias_of(cfg, root), url=url))
    return url


def log_request(method: str, path: str, status: int, target: str, seconds: float) -> None:
    color = "green" if status < 400 else "yellow" if status < 500 else "red"
    where = "dim" if target in ("remote", "missing", "other-repo") else "cyan"
    console.print(Text.assemble(
        (f"{datetime.now():%H:%M:%S} ", "dim"), (f"{method:<6} ", "bold"), (f"{path} ", ""),
        (f"{status} ", color), ("→ ", "dim"), (target, where), (f"  {seconds * 1000:.0f}ms", "dim"),
    ), soft_wrap=True)


def proxy_port(port: int) -> int:
    """``port`` if it is free; otherwise the next free one, offered in a terminal (the menu cannot pass --port)."""
    try:
        return actions.free_port(actions.PROXY_HOST, port)
    except actions.PortBusy as busy:
        if not interactive_terminal():
            fail(_("Port {port} is in use (the next free one is {free}). Use --port.", port=busy.port, free=busy.free))
        if not questionary.confirm(_("Port {port} is in use. Use {free} instead?", port=busy.port, free=busy.free),
                                   default=True).unsafe_ask():
            raise typer.Exit(1)
        return busy.free


def plan_proxy(cfg: Config, root: Path, repo_routes: list[routes.Route], port: int, env: str,
               remote: Optional[str], as_user: Optional[str], frontend: Optional[bool],
               timeout: Optional[int] = None) -> actions.ProxyLaunch:
    """Plan the proxy, answering in the terminal what it asks (a busy port, pointing the frontend to it)."""
    port = proxy_port(port)
    while True:
        try:
            return settle(lambda: actions.plan_proxy(
                cfg, root, repo_routes, port=port, env=env, remote=remote, user_name=as_user, frontend=frontend,
                timeout=timeout,
            ))
        except actions.PointFrontend:
            frontend = interactive_terminal() and questionary.confirm(
                _("Point the frontend to the proxy? (writes VITE_APP_API_URL in frontend/.env.local, undone when it stops)"),
                default=True,
            ).unsafe_ask()


def print_restored(restored: Optional[str]) -> None:
    if restored:
        console.print("[green]✓[/] " + _("{path} restored; restart yarn dev to apply it.", path=restored))


@proxy_app.callback()
def proxy_main(
    ctx: typer.Context,
    port: int = typer.Option(8000, "--port", "-p", help=_("Port to listen on.")),
    as_user: Optional[str] = typer.Option(
        None, "--as", help=_("Act as this user on local services (X-Dev-* headers)."), autocompletion=completion.users
    ),
    remote: Optional[str] = typer.Option(
        None, "--remote", help=_("Remote API for what is not running locally (saved for the repo).")
    ),
    no_remote: bool = typer.Option(False, "--no-remote", help=_("Never forward to the remote API.")),
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
    frontend: Optional[bool] = typer.Option(
        None, "--frontend-env/--no-frontend-env", help=_("Point frontend/.env.local to the proxy (asked if omitted).")
    ),
    background: Optional[bool] = typer.Option(
        None, "--background/--foreground", "-b/-f",
        help=_("Background (pdms ps, logs proxy, stop proxy) or foreground (asked if omitted)."),
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", "-t",
        help=_("Seconds to wait for each answer before replying 502 (default: the proxy_timeout setting)."),
    ),
) -> None:
    if ctx is not None and ctx.invoked_subcommand is not None:
        return
    if restored := settle(actions.clear_proxy_leftovers):  # the previous proxy did not stop cleanly
        console.print("[green]✓[/] " + _("{path} restored (left over by the previous proxy).", path=restored))
    cfg = Config.load()
    root = current_repo_root(cfg)
    repo_routes = load_repo_routes(root, env)
    target_remote = resolve_remote(cfg, root, remote, no_remote)
    if as_user:
        as_user = pick(cfg.users, _("user"), as_user)
    if background is None:
        background = interactive_terminal() and prompts.ask_proxy_background()
    plan = plan_proxy(cfg, root, repo_routes, port, env, target_remote, as_user, frontend, timeout)

    if changed := actions.point_frontend(plan):
        console.print("[green]✓[/] " + _("{path} points to the proxy until it stops; restart yarn dev to apply it.",
                                         path=changed))
    summary = Table.grid(padding=(0, 2))
    summary.add_row(f"[bold]{_('Proxy')}[/]", plan.url)
    summary.add_row(f"[bold]Docs[/]", f"{plan.url}/docs")
    summary.add_row(f"[bold]Repo[/]", f"{repos.alias_of(cfg, root) or root.name} ({env}, {len(repo_routes)} {_('routes')})")
    summary.add_row(f"[bold]{_('Remote')}[/]", target_remote or _("none (only local services)"))
    summary.add_row(f"[bold]{_('Timeout')}[/]", _("{seconds} s per request", seconds=plan.timeout))
    summary.add_row(f"[bold]{_('Acting as')}[/]", f"{as_user} ({plan.user.roles})" if plan.user else _("each service's own profile"))
    console.print(summary)

    if background:
        started = actions.start_proxy(plan)
        with console.status(_("Starting the proxy...")):
            state = actions.wait_for_proxy(started)
        if state == "stopped":
            print_restored(actions.stop_proxy())
            console.print(instances.tail(str(started.log), 30), markup=False, highlight=False)
            fail(_("The proxy exited while starting. Full log: {log}", log=started.log))
        if state == "ok" and (running := proxy.running_proxy()):
            console.print("[green]✓[/] " + _("The proxy is responding at {url} (pid {pid})", url=plan.url, pid=running["pid"]))
        else:
            console.print(f"[yellow]{_('⚠ The proxy is not responding yet; check its log.')}[/]")
        console.print(_("  Requests: [bold]pdms logs proxy[/]   Stop: [bold]pdms stop proxy[/]"))
        return

    console.rule(_("Requests · Ctrl+C to stop"))
    try:
        actions.serve_proxy(plan, log_request)
    except KeyboardInterrupt:
        console.print(f"\n[dim]{_('Proxy stopped.')}[/]")
    finally:
        print_restored(proxy.restore_frontend_change())


@proxy_app.command("routes", help=_("Show which service handles each route and where the proxy would send it."))
def proxy_routes(
    contains: str = typer.Option("", "--filter", "-f", help=_("Only routes whose path or service contains this text.")),
    local_only: bool = typer.Option(False, "--local", "-l", help=_("Only routes served by a local instance.")),
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
) -> None:
    cfg = Config.load()
    root = current_repo_root(cfg)
    repo_routes = load_repo_routes(root, env)
    gateway = proxy.Gateway(routes=repo_routes, backend=root / "backend", remote=resolve_remote(cfg, root, None, False))
    running = gateway.local_instances()
    table = Table(_("Method"), _("Path"), _("Service"), _("Target"))
    shown = 0
    for route in repo_routes:
        if contains and contains not in route.path and contains not in route.service:
            continue
        target = gateway.target(route, running)
        if local_only and target.kind != "local":
            continue
        where = (
            f"[cyan]{target.instance.key}[/]" if target.kind == "local"
            else f"[yellow]{_('remote')} ({target.instance.key} {_('in another repo')})[/]" if target.instance
            else f"[dim]{_('remote')}[/]" if target.kind == "remote" else f"[red]{_('not available')}[/]"
        )
        table.add_row(f"[{METHOD_STYLE.get(route.method, 'white')}]{route.method}[/]", route.path, route.service, where)
        shown += 1
    console.print(table)
    console.print(_("{shown} of {total} routes.", shown=shown, total=len(repo_routes)))


# --------------------------------------------------------------------------- frontend


@app.command("front", help=_("Run the PDMS web app (frontend/): the yarn dev server, or a production build with --build."))
def front_cmd(
    build: bool = typer.Option(
        False, "--build/--dev", help=_("Build it as in production and serve the build (yarn build + vite preview)."),
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help=_("Build again even if nothing changed since the last build.")),
    port: int = typer.Option(frontend.PORT, "--port", "-p", help=_("Port to listen on.")),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", help=_("Run yarn install first (by default only when node_modules is out of date)."),
    ),
    background: Optional[bool] = typer.Option(
        None, "--background/--foreground", "-b/-f",
        help=_("Background (pdms ps, logs frontend, stop frontend) or foreground (asked if omitted)."),
    ),
) -> None:
    cfg = Config.load()
    mode = "build" if build or rebuild else "dev"
    while True:
        try:
            with console.status(_("Checking node, yarn and node_modules...")):
                plan = settle(lambda: actions.plan_frontend(cfg, mode=mode, port=port, install=install, rebuild=rebuild))
            break
        except actions.PortBusy as busy:
            prompts.require_tty()
            if not questionary.confirm(
                _("Port {port} is in use. Use {free}? (logging in may only work on {port})", port=busy.port,
                  free=busy.free), default=False,
            ).unsafe_ask():
                raise typer.Exit(1)
            port = busy.free
    if background is None:
        background = interactive_terminal() and questionary.confirm(
            _("Run it in the background? (pdms logs frontend, pdms stop frontend)"), default=True,
        ).unsafe_ask()
    if plan.install:
        console.rule("yarn install")
        settle(lambda: actions.install_frontend(plan.root))
    if plan.build:
        reasons = {"no build yet": _("no build yet"), "the API URL changed": _("the API URL changed"),
                   "the dependencies changed": _("the dependencies changed"), "the code changed": _("the code changed"),
                   "rebuild asked": "--rebuild"}
        console.rule(_("yarn build ({reason})", reason=reasons.get(plan.build, plan.build)))
        settle(lambda: actions.build_frontend(plan.root))
    elif plan.mode == "build":
        console.print("[green]✓[/] " + _("The last build is up to date; serving it (--rebuild to build again)."))
    api = frontend.api_url(plan.root, plan.mode)
    console.print(_("API: {url}", url=api or "-") + (f" [dim]({_('the proxy')})[/]" if frontend.is_local(api) else ""),
                  highlight=False)
    if not background:
        runner.exec_server(frontend.folder(plan.root), frontend.serve_command(plan.mode, plan.port), frontend.environment())
        return
    started = actions.start_frontend(plan)
    with console.status(_("Starting the frontend...")):
        state = actions.wait_for_frontend(started)
    if state == "stopped":
        console.print(instances.tail(str(frontend.log_path()), 30), markup=False, highlight=False)
        fail(_("The frontend exited while starting. Full log: {log}", log=frontend.log_path()))
    if state == "ok":
        url = frontend.url(plan.port, frontend.scheme(plan.port) or "https")
        console.print("[green]✓[/] " + _("The frontend is responding at {url}", url=url), highlight=False)
    else:
        console.print(f"[yellow]{_('⚠ The frontend is not responding yet; check its log.')}[/]")
    console.print(_("  Log: [bold]pdms logs frontend[/]   Stop: [bold]pdms stop frontend[/]"))


def frontend_logs(follow: bool, lines: Optional[int], previous: bool) -> None:
    log = frontend.log_path()
    if previous:
        log = instances.previous_log_path(log)
    if not log.exists():
        fail(_("The frontend has no log. Start it in the background with [bold]pdms front -b[/]."))
    current = frontend.running()
    if previous or not follow or not current:
        console.print(instances.tail(str(log), lines or 100), markup=False, highlight=False, end="")
        return
    console.rule(_("{names} · Ctrl+C to exit", names=frontend.KEY))
    inst = instances.Instance(
        key=frontend.KEY, pid=int(current["pid"]), service=current.get("root", ""), host="127.0.0.1",
        port=current["port"], user="", db="", reload=False, log=str(log), started_at=current.get("started_at", ""),
        created=float(current.get("created", 0)),
    )
    logview.follow(console, [inst], lines or 100, None)


# --------------------------------------------------------------------------- ui


@app.command("ui", help=_("Open the pdms web interface (local only; Ctrl+C to stop it)."))
def ui_cmd(
    port: int = typer.Option(8765, "--port", "-p", help=_("Port to listen on (the next free one if it is in use).")),
    browser: bool = typer.Option(True, "--browser/--no-browser", help=_("Open it in the browser.")),
    window: bool = typer.Option(
        False, "--window", "-w", help=_("Open it in a window of its own instead of the browser (needs the desktop extra)."),
    ),
    install: bool = typer.Option(False, "--install", help=_("Add pdms to the app menu of this computer.")),
    uninstall: bool = typer.Option(False, "--uninstall", help=_("Remove pdms from the app menu (and from login).")),
    at_login: Optional[bool] = typer.Option(
        None, "--at-login/--not-at-login", help=_("Open pdms ui in the tray when you log in, or stop doing it."),
    ),
    detached: bool = typer.Option(False, "--detached", hidden=True, help="No terminal: log to pdms logs ui."),
    hidden: bool = typer.Option(False, "--hidden", hidden=True, help="Start in the tray, without the window."),
) -> None:
    if install or uninstall or at_login is not None:
        ui_setup(install, uninstall, at_login)
        return
    from .ui import control as ui_control
    from .ui import instance as ui_instance
    from .ui import server as ui_server
    from .ui import updates as ui_updates
    from .ui import window as ui_window

    if detached:
        ui_instance.redirect_output()
        # Opened from the menu or at login: the tools of the user's shell (pyenv, nvm, Poetry...) are not on PATH yet.
        from . import shellenv

        if changed := shellenv.adopt_in_background():
            console.print(f"[dim]From the shell's environment: {', '.join(sorted(changed))}[/]", highlight=False)

    def stop_with(message: str) -> None:
        if detached:
            desktop.notify("pdms", Text.from_markup(message).plain)
        fail(message)

    token = os.environ.pop(ui_instance.TOKEN_ENV, "")  # restarting after an update: the same token
    if not token and (existing := ui_instance.running()):
        if ui_instance.show(existing):
            console.print(_("pdms ui is already running at {url}; showing it.", url=ui_instance.url(existing)),
                          highlight=False, soft_wrap=True)
            return
    if window and not ui_window.available() and ui_window.installs_itself():
        console.print(_("The window needs pywebview; installing it (only this once)..."))
        try:
            ui_window.install_desktop()
        except RuntimeError as exc:
            stop_with(_("Could not install pywebview ({error}). Install it with:\n  {command}\n"
                        "or use pdms ui to open it in the browser.", error=escape(str(exc)),
                        command=escape(ui_window.install_command())))
        console.print("[green]✓[/] " + _("pywebview installed."))
    if window and not ui_window.available():
        message = _("The window needs pywebview, which comes with the desktop extra. Install it with:\n  {command}\n"
                    "or use pdms ui to open it in the browser.", command=escape(ui_window.install_command()))
        if not detached:
            fail(message)
        desktop.notify("pdms", _("pdms ui opens in the browser: the window needs the desktop extra (see pdms logs ui)."))
        console.print(message)
        window = False
    try:
        port = actions.free_port("127.0.0.1", port)
    except actions.PortBusy as busy:
        console.print(_("[dim]Port {port} is in use; using {free}.[/]", port=busy.port, free=busy.free))
        port = busy.free
    token = token or ui_server.new_token()
    url = f"http://127.0.0.1:{port}/?token={token}"
    relaunch = [str(desktop.pdms_executable(gui=detached)), "ui", "--port", str(port),
                *(["--window"] if window else ["--no-browser"]), *(["--detached"] if detached else [])]
    control = ui_control.Control(url, token, relaunch)
    hub, jobs = ui_server.make_app()
    server = ui_server.make_server("127.0.0.1", port, token, hub, jobs, control)
    ui_instance.remember(port, token, window)
    jobs.after_update(ui_instance.take_after_update())
    stopped = threading.Event()
    threading.Thread(target=ui_updates.watch, args=(Config.load, hub.poke, stopped), name="pdms-ui-update",
                     daemon=True).start()
    threading.Thread(target=jobs.doctor.watch, args=(stopped,), name="pdms-ui-doctor-watch", daemon=True).start()
    console.print("[green]✓[/] " + _("pdms ui is running at {url}", url=url), highlight=False, soft_wrap=True)
    try:
        if window:
            console.print(_("[dim]Close the window (or Ctrl+C) to stop it; the link also opens it in a browser.[/]"))
            thread = threading.Thread(target=ui_server.serve, args=(server, hub), name="pdms-ui", daemon=True)
            thread.start()
            try:
                ui_window.open_window(url, control, hidden=hidden)
            except KeyboardInterrupt:
                pass
            except Exception as exc:  # noqa: BLE001 - a missing system library of the GUI toolkit, no display...
                server.shutdown()
                stop_with(_("Could not open the window: {error}. pdms ui opens it in the browser.",
                            error=escape(str(exc))))
            server.shutdown()
            thread.join(5)
        else:
            control.attach(None, lambda: threading.Thread(target=server.shutdown, daemon=True).start())
            console.print(_("[dim]Only this machine can open it, and only with this link. Ctrl+C to stop it.[/]"))
            if browser and not webbrowser.open(url):
                console.print(_("[yellow]Could not open a browser; open the URL manually.[/]"))
            try:
                ui_server.serve(server, hub)
            except KeyboardInterrupt:
                console.print()
    finally:
        stopped.set()
        if not control.restart:
            ui_instance.forget()
    if control.restart:
        restart_ui(control)
    console.print(f"[dim]{_('pdms ui stopped.')}[/]")


def restart_ui(control: Control) -> None:
    """Become the pdms ui just installed (same pid, port and token: the page and ui.json stay valid)."""
    from .ui import instance as ui_instance

    console.print(f"[dim]{_('Restarting pdms ui with the new version...')}[/]")
    sys.stdout.flush()
    sys.stderr.flush()
    exe, *args = control.relaunch
    os.execve(exe, [exe, *args], {**os.environ, ui_instance.TOKEN_ENV: control.token})


def ui_setup(install: bool, uninstall: bool, at_login: Optional[bool]) -> None:
    """pdms ui --install / --uninstall / --at-login: the entries of pdms ui in the system."""
    from .ui import window as ui_window

    cfg = Config.load()
    if uninstall:
        removed = desktop.uninstall()
        if cfg.defaults.ui_at_login:
            cfg.defaults.ui_at_login = False
            cfg.save()
        console.print("[green]✓[/] " + (_("pdms is no longer in the app menu.") if removed
                                        else _("pdms was not in the app menu.")))
        return
    if install:
        try:
            entry = desktop.install()
        except OSError as exc:
            fail(_("Could not add pdms to the app menu: {error}", error=escape(str(exc))))
        console.print("[green]✓[/] " + _("pdms is in the app menu ({path}).", path=escape(str(entry))), highlight=False)
        if not ui_window.available() and not ui_window.installs_itself():
            console.print(_("[dim]It opens in the browser until the desktop extra is installed:[/] {command}",
                            command=escape(ui_window.install_command())), highlight=False)
        if cfg.defaults.ui_at_login and at_login is None:
            settle(lambda: actions.set_ui_at_login(True))  # point it to this pdms too
    if at_login is not None:
        settle(lambda: actions.save_defaults(cfg, replace(cfg.defaults, ui_at_login=at_login)))
        console.print("[green]✓[/] " + (_("pdms ui opens in the tray when you log in.") if at_login
                                        else _("pdms ui no longer opens when you log in.")))


# --------------------------------------------------------------------------- events


def load_events(env: str = "dev") -> tuple[Path, events.EventMap]:
    cfg = Config.load()
    current_repo_root(cfg)
    with console.status(_("Reading the event map (Terraform and backend/common/event)...")):
        return settle(lambda: actions.load_events(cfg, env))


@events_app.callback()
def events_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        events_status(False)


@events_app.command("map", help=_("Show every event type, the queue the broker sends it to and its consumer."))
def events_map(
    contains: str = typer.Option("", "--filter", "-f", help=_("Only rows containing this text.")),
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
) -> None:
    _root, event_map = load_events(env)
    table = Table(_("Event type"), _("Queue"), _("Consumer"))
    routed = set()
    for event_type, queue in sorted(event_map.routes.items()):
        consumer = event_map.consumers.get(queue)
        row = (event_type, queue, consumer.service if consumer else f"[red]{_('none')}[/]")
        routed.add(queue)
        if not contains or any(contains in str(v) for v in row):
            table.add_row(*row)
    console.print(table)
    others = Table(_("Queue"), _("Consumer"), _("Source"), title=_("Queues not routed by the broker"), title_justify="left")
    for name, queue in sorted(event_map.queues.items()):
        if name in routed:
            continue
        consumer = event_map.consumers.get(name)
        row = (name, consumer.service if consumer else "-", queue.source)
        if not contains or any(contains in str(v) for v in row):
            others.add_row(*row)
    if others.row_count:
        console.print(others)
    console.print(_("{types} event types · {queues} queues · {consumers} consumers · broker: {broker}",
                    types=len(event_map.routes), queues=len(event_map.queues), consumers=len(event_map.consumers),
                    broker=event_map.broker_queue or _("not found")))


@events_app.command("up", help=_("Start a local ElasticMQ (Docker) with every queue of the repo, and the broker."))
def events_up(
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
    broker: bool = typer.Option(True, "--broker/--no-broker", help=_("Also run the broker (broker-sqs-event) locally.")),
) -> None:
    cfg = Config.load()
    _root, event_map = load_events(env)
    port = cfg.defaults.events_port
    with console.status(_("Starting ElasticMQ...")):
        result = settle(lambda: actions.start_events(cfg, event_map.queues))
    extra = sum(q.source != "terraform" for q in event_map.queues.values())
    message = {
        "created": _("ElasticMQ started"), "restarted": _("ElasticMQ restarted with the updated queues"),
        "unchanged": _("ElasticMQ was already running with these queues"),
    }[result]
    console.print("[green]✓[/] " + message + f" · {events.endpoint(port)}")
    console.print("  " + _("{count} queues ({extra} only in infra/local_sqs/elasticmq.conf) · broker: {broker}",
                           count=len(event_map.queues), extra=extra, broker=event_map.broker_queue or "-"))
    if broker:
        start_broker(cfg, _root, event_map)


def start_broker(cfg: Config, root: Path, event_map: events.EventMap) -> None:
    """Run broker-sqs-event locally, so published events are routed to their queues as in AWS."""
    service = actions.broker_service(root, event_map)
    if service is None:
        console.print("[yellow]" + _("⚠ The broker ({service}) was not found in the repo; events stay in the broker "
                                     "queue.", service=events.BROKER_SERVICE) + "[/]")
        return
    if actions.event_consumers(event_map.broker_queue):
        console.print(_("[dim]The broker is already running.[/]"))
        return
    if not cfg.users or not cfg.dbs:
        console.print("[yellow]" + _("⚠ Configure a user and a database to run the broker (pdms user add, pdms db "
                                     "add), then: pdms run {service} -b", service=events.BROKER_SERVICE) + "[/]")
        return
    console.rule(_("Broker"))
    try:
        do_run(user=cfg.last_user or None, db=cfg.last_db or None, path=service, background=True, yes=True,
               events_mode="local")
    except typer.Exit:
        console.print("[yellow]" + _("⚠ The broker did not start; see pdms logs {name}", name=service.name) + "[/]")


@events_app.command("down", help=_("Stop the local ElasticMQ (its messages are lost)."))
def events_down() -> None:
    for inst in actions.event_consumers():
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
    if events.stop():
        console.print("[green]✓[/] " + _("ElasticMQ stopped."))
    else:
        console.print(_("ElasticMQ is not running."))


@events_app.command("status", help=_("Show whether ElasticMQ is running and the messages waiting in each queue."))
def events_status(
    all_: bool = typer.Option(False, "--all", "-a", help=_("Also list empty queues.")),
) -> None:
    cfg = Config.load()
    port = cfg.defaults.events_port
    state = events.container_state()
    if not state or not state["running"] or not events.is_up(port):
        console.print(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
        return
    root = repos.active_root(cfg)
    event_map = events.load_event_map(root) if root and root.is_dir() else events.EventMap()
    counts = events.queue_counts(port)
    table = Table(_("Queue"), _("Waiting"), _("In flight"), _("Consumer"))
    for name, count in sorted(counts.items()):
        if name == events.SNS_QUEUE:
            continue  # shown last, always: it is the local SNS, not a queue of the repo
        if not all_ and not count["visible"] and not count["in_flight"]:
            continue
        consumer = event_map.consumers.get(name)
        running = next((i.key for i in instances.load().values() if i.is_consumer and i.queue == name and i.alive()), "")
        table.add_row(name, str(count["visible"]), str(count["in_flight"]),
                      f"[green]● {running}[/]" if running else (f"[dim]{consumer.service}[/]" if consumer else "-"))
    repo_rows = table.row_count
    sns = counts.get(events.SNS_QUEUE, {"visible": 0, "in_flight": 0})
    table.add_row(f"{events.SNS_QUEUE} [dim]({_('local SNS')})[/]", str(sns["visible"]), str(sns["in_flight"]),
                  f"[dim]{_('every SNS publish · pdms events peek {queue}', queue=events.SNS_QUEUE)}[/]")
    console.print("[green]●[/] " + _("ElasticMQ running at {url} · {count} queues", url=events.endpoint(port),
                                     count=len(counts)))
    publishers = [i.key for i in instances.load().values() if i.alive() and i.events == "local"]
    console.print("  " + _("Publishing to the local broker: {names}", names=", ".join(publishers) or _("none")))
    console.print(table)
    if not repo_rows:
        console.print("  [dim]" + _("All queues are empty (--all to list them).") + "[/]")


def require_elasticmq(cfg: Config) -> int:
    port = cfg.defaults.events_port
    if not events.running(port):
        fail(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    return port


@events_app.command("send", help=_("Send an event (through the broker, or --direct to its queue) or a message to a queue."))
def events_send(
    target: str = typer.Argument(..., help=_("Event type (e.g. email-notify) or queue name."),
                                 autocompletion=completion.event_targets),
    body: Optional[str] = typer.Option(None, "--body", "-b", help=_("Event fields / message body as JSON.")),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help=_("Read the JSON from a file ('-' = stdin).")),
    direct: bool = typer.Option(False, "--direct", help=_("Skip the broker: send the event straight to its queue.")),
    template: bool = typer.Option(False, "--template", "-t", help=_("Print the fields of the event type and exit.")),
) -> None:
    cfg = Config.load()
    root, event_map = load_events()
    is_event = target in event_map.routes
    if template:
        settle(lambda: actions.plan_send(event_map, target, None))  # an unknown target says so first
        fields = events.event_template(root, target) if is_event else None
        if fields is None:
            fail(_("No event class found for '{target}' in backend/common/event.", target=target))
        print(json.dumps(fields, indent=2))
        return
    if file is not None:
        raw = sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
    else:
        raw = body
    queue, message = settle(lambda: actions.plan_send(event_map, target, raw, direct))
    require_elasticmq(cfg)
    message_id = settle(lambda: actions.send_message(cfg, event_map, queue, message))
    console.print("[green]✓[/] " + _("Sent {id} to {queue}", id=message_id[:8], queue=queue))
    if is_event and queue == event_map.broker_queue:
        consumer = event_map.consumer_of_type(target)
        console.print("  [dim]" + _("The broker routes it to {queue} (consumer: {consumer}).",
                                    queue=event_map.routes[target], consumer=consumer.service if consumer else "-") + "[/]")
    if not actions.event_consumers(queue):
        console.print("  [yellow]" + _("Nothing is consuming {queue} right now; it waits there (pdms events peek {queue}).",
                                       queue=queue) + "[/]")


@events_app.command("peek", help=_("Show the messages waiting in a queue, without consuming them."))
def events_peek(
    queue: str = typer.Argument(..., help=_("Queue name."), autocompletion=completion.queues),
    limit: int = typer.Option(10, "--limit", "-n", help=_("Maximum number of messages.")),
    full: bool = typer.Option(False, "--full", help=_("Print the whole body of each message.")),
) -> None:
    cfg = Config.load()
    port = require_elasticmq(cfg)
    try:
        messages = events.peek(port, queue, limit)
    except Exception as exc:  # noqa: BLE001 - unknown queue, ElasticMQ error
        fail(_("Could not read {queue}: {error}", queue=queue, error=exc))
    if not messages:
        console.print(_("{queue} is empty.", queue=queue))
        return
    for message in messages:
        body = message.get("Body", "")
        try:
            parsed = json.loads(body)
            kind = parsed.get("type", "-") if isinstance(parsed, dict) else "-"
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            kind, pretty = "-", body
        receives = message["Attributes"].get("ApproximateReceiveCount", "?")
        console.rule(f"{message['MessageId'][:8]} · type={kind} · " + _("received {n} times", n=receives), align="left")
        console.print(pretty if full or len(pretty) < 1500 else pretty[:1500] + " …", markup=False, highlight=False)


@events_app.command("purge", help=_("Delete every message of a queue (or of all of them)."))
def events_purge(
    queue: Optional[str] = typer.Argument(None, help=_("Queue name."), autocompletion=completion.queues),
    all_: bool = typer.Option(False, "--all", "-a", help=_("Purge every queue.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation.")),
) -> None:
    cfg = Config.load()
    port = require_elasticmq(cfg)
    counts = events.queue_counts(port)
    if all_:
        targets = [q for q, c in counts.items() if c["visible"] or c["in_flight"]]
    elif queue:
        if queue not in counts:
            fail(_("Unknown queue '{queue}'. See pdms events status --all.", queue=queue))
        targets = [queue]
    else:
        prompts.require_tty()
        targets = [prompts.select_name(_("Queue:"), sorted(counts))]
    if not targets:
        console.print(_("All queues are empty (--all to list them)."))
        return
    total = sum(counts[q]["visible"] + counts[q]["in_flight"] for q in targets)
    if not yes and not (interactive_terminal() and questionary.confirm(
        _("Delete {count} messages from {queues}?", count=total, queues=", ".join(targets)), default=False
    ).unsafe_ask()):
        raise typer.Exit(1)
    for name in targets:
        events.purge(port, name)
    console.print("[green]✓[/] " + _("Purged {queues}.", queues=", ".join(targets)))


def events_menu() -> None:
    prompts.require_tty()
    _menu(_("Events (local SQS):"), {
        _("Status"): lambda: events_status(False),
        _("Start ElasticMQ and the broker"): lambda: events_up("dev", True),
        _("Event map"): lambda: events_map("", "dev"),
        _("Peek a queue"): lambda: events_peek(prompts.select_name(_("Queue:"), sorted(load_events()[1].queues)), 10, False),
        _("Purge a queue"): lambda: events_purge(None, False, False),
        _("Stop everything"): events_down,
    })


# --------------------------------------------------------------------------- config commands


@config_app.callback()
def config_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        config_defaults()


@config_app.command("defaults", help=_("Edit the defaults (language, port, log, reload, extra env...)."))
def config_defaults() -> None:
    prompts.require_tty()
    cfg = Config.load()
    defaults = prompts.ask_defaults(cfg.defaults)
    settle(lambda: actions.save_defaults(cfg, defaults))
    i18n.set_language(cfg.defaults.language)
    console.print("[green]✓[/] " + _("Defaults saved."))


@config_app.command("language", help=_("Change the CLI language."))
def config_language(
    lang: Optional[str] = typer.Argument(
        None, help=_("Language code: {codes}. Empty = ask.", codes=", ".join(i18n.LANGUAGES)), autocompletion=completion.languages
    ),
) -> None:
    cfg = Config.load()
    if lang is None:
        prompts.require_tty()
        lang = prompts.ask_language(cfg.defaults.language)
    settle(lambda: actions.set_language(cfg, lang))
    i18n.set_language(lang)
    console.print("[green]✓[/] " + _("Language set to {name}.", name=i18n.LANGUAGES[lang]))


def section_label(section: str) -> str:
    return {
        "defaults": _("defaults"), "users": _("users"), "dbs": _("databases"), "stacks": _("stacks"),
    }[section]


def parse_sections(only: Optional[str], available: list[str]) -> Optional[list[str]]:
    if not only:
        return None
    sections = [s.strip() for s in only.split(",") if s.strip()]
    unknown = [s for s in sections if s not in transfer.SECTIONS]
    if unknown:
        fail(_("Unknown sections: {unknown}. Available: {codes}",
               unknown=", ".join(unknown), codes=", ".join(transfer.SECTIONS)))
    return [s for s in sections if s in available]


def ask_sections(message: str, available: list[str]) -> list[str]:
    selected = questionary.checkbox(
        message, choices=[questionary.Choice(section_label(s), s, checked=True) for s in available]
    ).unsafe_ask()
    return [s for s in transfer.SECTIONS if s in selected]


SECTIONS_HELP = _("Comma-separated sections: defaults, users, dbs, stacks. Default: all.")


@config_app.command("export", help=_("Export the configuration (users, databases, stacks, defaults) to a TOML file."))
def config_export(
    file: Optional[Path] = typer.Argument(None, help=_("Output file ('-' = stdout). Default: pdms-config-<date>.toml.")),
    only: Optional[str] = typer.Option(None, "--only", help=SECTIONS_HELP),
    secrets: Optional[bool] = typer.Option(
        None, "--secrets/--no-secrets", help=_("Include database passwords (asked if omitted; no by default).")
    ),
    force: bool = typer.Option(False, "--force", help=_("Overwrite the file if it exists.")),
) -> None:
    cfg = Config.load()
    interactive = sys.stdin.isatty() and file != Path("-")
    sections = parse_sections(only, list(transfer.SECTIONS))
    if sections is None:
        sections = ask_sections(_("What do you want to export?"), list(transfer.SECTIONS)) if interactive \
            else list(transfer.SECTIONS)
    if not sections:
        fail(_("Nothing selected."))
    has_passwords = "dbs" in sections and any(db.password for db in cfg.dbs.values())
    if secrets is None:
        secrets = has_passwords and interactive and questionary.confirm(
            _("Include database passwords? (only if the file stays private)"), default=False
        ).unsafe_ask()

    text = settle(lambda: actions.export_config(cfg, sections, secrets))
    if file == Path("-"):
        sys.stdout.write(text)
        return
    file = file or Path(f"pdms-config-{datetime.now():%Y-%m-%d}.toml")
    if file.exists() and not force:
        if not interactive or not questionary.confirm(_("{file} already exists. Overwrite it?", file=file),
                                                      default=False).unsafe_ask():
            fail(_("{file} already exists (use --force).", file=file))
    write_private(file, text)

    counts = {"users": len(cfg.users), "dbs": len(cfg.dbs), "stacks": len(cfg.stacks)}
    parts = [f"{section_label(s)}" if s == "defaults" else f"{counts[s]} {section_label(s)}" for s in sections]
    console.print("[green]✓[/] " + _("Exported {parts} to {file}.", parts=", ".join(parts), file=file))
    if "dbs" in sections:
        console.print(_("  [yellow]The file includes database passwords: do not share it or commit it.[/]") if secrets
                      else _("  [dim]Database passwords were left out.[/]"))


@config_app.command("import", help=_("Import a configuration exported with pdms config export."))
def config_import(
    file: Optional[Path] = typer.Argument(None, help=_("File to import.")),
    only: Optional[str] = typer.Option(None, "--only", help=SECTIONS_HELP),
    replace: bool = typer.Option(
        False, "--replace", help=_("Replace the selected sections entirely instead of merging.")
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help=_("Overwrite existing entries without asking.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Apply without asking for confirmation.")),
) -> list[str]:
    """Returns the imported sections (used by the guided setup)."""
    interactive = sys.stdin.isatty()
    if file is None:
        prompts.require_tty()
        file = Path(questionary.path(_("File to import:"), validate=lambda v: Path(v).expanduser().is_file()
                                     or _("File not found")).unsafe_ask()).expanduser()
    if not file.is_file():
        fail(_("File not found: {file}", file=file))
    try:
        doc = transfer.read_document(file.read_text(encoding="utf-8"))
    except transfer.TransferError as exc:
        fail(str(exc))

    current = Config.load()
    sections = parse_sections(only, doc.sections)
    if sections is None:
        sections = ask_sections(_("What do you want to import?"), doc.sections) if interactive else doc.sections
    if not sections:
        fail(_("Nothing to import."))
    plans = transfer.plan_import(current, doc.config, sections)

    exported = doc.meta.get("exported_at", "?")
    console.print(_("File exported on {date} (pdms {version}).", date=exported, version=doc.meta.get("cli_version", "?")))
    table = Table(_("Section"), _("New"), _("Changed"), _("Unchanged"), _("Only local") if not replace else _("Removed"))
    for plan in plans:
        table.add_row(
            section_label(plan.section), ", ".join(plan.added) or "-", ", ".join(plan.changed) or "-",
            ", ".join(plan.same) or "-", ", ".join(plan.missing) or "-",
        )
    console.print(table)
    if not doc.meta.get("secrets") and "dbs" in sections:
        console.print(_("[dim]The file has no passwords: databases you already have keep their password.[/]"))

    conflicts = [(p.section, name) for p in plans for name in p.changed]
    chosen: set[tuple[str, str]] = set()
    if actions.first_setup():
        chosen = set(conflicts)  # there is no own configuration to keep
    elif replace:
        removed = sum(len(p.missing) for p in plans)
        if removed:
            console.print("[yellow]" + _("⚠ --replace will delete {count} local entries not in the file.",
                                         count=removed) + "[/]")
    elif overwrite:
        chosen = set(conflicts)
    elif conflicts and interactive:
        chosen = set(questionary.checkbox(
            _("These entries differ from yours. Which ones do you want to overwrite? (unchecked = keep yours)"),
            choices=[
                questionary.Choice(section_label(sec) if sec == "defaults" else f"{section_label(sec)}: {name}", (sec, name))
                for sec, name in conflicts
            ],
        ).unsafe_ask())
    elif conflicts:
        console.print(_("[dim]Existing entries are kept (use --overwrite to replace them).[/]"))

    result = transfer.apply_import(current, doc.config, sections, chosen, replace=replace)
    if result.to_dict() == current.to_dict():
        console.print(_("Nothing changes."))
        return sections
    if not yes:
        if not interactive:
            fail(_("Use --yes to import without an interactive terminal."))
        if not questionary.confirm(_("Apply the import?"), default=True).unsafe_ask():
            raise typer.Exit(1)

    backup = actions.import_config(result)
    i18n.set_language(result.defaults.language)
    console.print("[green]✓[/] " + _("Configuration imported."))
    if backup:
        console.print(_("  [dim]Previous configuration saved to {backup}[/]", backup=backup))
    no_password = [name for name, db in result.dbs.items() if not db.password]
    if no_password:
        console.print("[yellow]" + _("⚠ Databases without password: {names}. Set it with pdms db edit <name>.",
                                     names=", ".join(no_password)) + "[/]")
    return sections


@config_app.command("path", help=_("Show the path of the configuration file."))
def config_show_path() -> None:
    console.print(str(config_path()))


@config_app.command("edit", help=_("Open the configuration file in $EDITOR."))
def config_edit() -> None:
    cfg = Config.load()
    if not config_path().exists():
        cfg.save()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(config_path())])
    else:
        typer.launch(str(config_path()))


# --------------------------------------------------------------------------- guided setup


def setup_step(title: str) -> None:
    console.print(f"\n[bold]{title}[/]")


def setup_done(detail: str) -> None:
    console.print(f"  [green]✓[/] {detail}", highlight=False)


def setup_later(command: str) -> None:
    console.print("  [dim]" + _("Skipped; do it later with {command}.", command=command) + "[/]", highlight=False)


def setup_import() -> list[str]:
    """Offer to import an exported configuration; return the imported sections."""
    found = onboarding.find_exports()
    if not questionary.confirm(
        _("Do you have a pdms configuration to import (e.g. exported by a teammate)?"), default=bool(found)
    ).unsafe_ask():
        return []
    file: Optional[Path] = None
    if found:
        choice = questionary.select(
            _("File to import:"),
            choices=[*(questionary.Choice(str(p), str(p)) for p in found), questionary.Choice(_("Another file..."), "")],
        ).unsafe_ask()
        file = Path(choice) if choice else None
    try:
        return config_import(file, None, False, False, False)
    except typer.Exit:
        console.print("  [dim]" + _("Nothing imported; the next steps set it up by hand.") + "[/]")
        return []


def setup_language() -> None:
    cfg = Config.load()
    actions.set_language(cfg, prompts.ask_language(cfg.defaults.language))
    i18n.set_language(cfg.defaults.language)


def setup_repo() -> None:
    setup_step(_("PDMS repo"))
    cfg = Config.load()
    if "repo" not in onboarding.pending(cfg):
        setup_done(f"{cfg.current_repo} · {cfg.repo.root}")
        return
    here = repos.find_repo_root(Path.cwd())
    if here and questionary.confirm(_("Use {path} as the PDMS repo?", path=here), default=True).unsafe_ask():
        root = here
    else:
        def validate(value: str) -> bool | str:
            return not value.strip() or repos.find_repo_root(Path(value.strip()).expanduser()) is not None \
                or _("Not a PDMS repo (no backend/snakesdk folder).")

        answer = questionary.path(
            _("Folder of your PDMS checkout (empty = skip):"), only_directories=True, validate=validate
        ).unsafe_ask().strip()
        if not answer:
            setup_later("pdms repo add <path>")
            return
        root = repos.find_repo_root(Path(answer).expanduser())
    alias = repos.register(cfg, root)
    cfg.current_repo = alias
    cfg.save()
    repos.use_for_this_command(None)
    setup_done(_("Repo '{alias}' registered ({path}).", alias=alias, path=root))


def setup_migrations() -> None:
    cfg = Config.load()
    repo = cfg.repo
    if repo is None or not repo.root.is_dir():
        return
    setup_step(_("Migrations repo (Flyway)"))
    if "migrations" not in onboarding.pending(cfg):
        setup_done(repo.migrations)
        return
    candidates = migrations.siblings(repo.root)
    if len(candidates) == 1:
        path: Optional[Path] = candidates[0]
    elif candidates:
        path = questionary.select(
            _("Which migrations repo goes with '{alias}'?", alias=cfg.current_repo),
            choices=[questionary.Choice(str(c), c) for c in candidates],
            default=migrations.best_match(repo.root, candidates),
        ).unsafe_ask()
    else:
        def validate(value: str) -> bool | str:
            return not value.strip() or migrations.find_upwards(Path(value.strip()).expanduser()) is not None \
                or _("Not a Flyway migrations repo (flyway.toml + migrations/).")

        answer = questionary.path(
            _("Folder of your pdms-db-migrations checkout (empty = skip):"), only_directories=True, validate=validate
        ).unsafe_ask().strip()
        path = migrations.find_upwards(Path(answer).expanduser()) if answer else None
    if path is None:
        setup_later("pdms migrate --migrations <path>")
        return
    repo.migrations = str(path.resolve())
    cfg.save()
    setup_done(repo.migrations)


def setup_dbs() -> None:
    setup_step(_("Databases"))
    cfg = Config.load()
    tested: set[str] = set()
    if cfg.dbs:
        setup_done(", ".join(cfg.dbs))
    else:
        console.print("  " + _("Services need at least one database to run."))
    while questionary.confirm(
        _("Add another database?") if cfg.dbs else _("Add a database now?"), default=not cfg.dbs
    ).unsafe_ask():
        tested.add(add_db(cfg))  # add_db offers its own connection test
    if not cfg.dbs:
        setup_later("pdms db add")
        return

    missing = [name for name, db in cfg.dbs.items() if not db.password]
    for name in missing:
        db = cfg.dbs[name]
        password = questionary.password(
            _("Password of '{name}' ({user}@{host}) (empty = later):", name=name, user=db.user, host=db.host)
        ).unsafe_ask()
        if password:
            db.password = password
            cfg.save()
    if still := [name for name in missing if not cfg.dbs[name].password]:
        setup_later("pdms db edit " + still[0])

    untested = [name for name in cfg.dbs if name not in tested and cfg.dbs[name].password]
    if untested and questionary.confirm(
        _("Test the connection to {names}?", names=", ".join(untested)), default=True
    ).unsafe_ask():
        for name in untested:
            check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)


def setup_users() -> None:
    setup_step(_("Development users"))
    cfg = Config.load()
    if cfg.users:
        setup_done(", ".join(cfg.users))
        return
    choices = [
        questionary.Choice(_("Add one by hand"), "manual"),
        questionary.Choice(_("Later"), "later"),
    ]
    if any(db.password for db in cfg.dbs.values()):
        choices.insert(0, questionary.Choice(_("Import them from the pdms_user table of a database"), "import"))
    choice = questionary.select(_("Services run as a DEV_* user. How do you want to add them?"),
                                choices=choices).unsafe_ask()
    if choice == "import":
        try:
            user_import(None, None, None, False, 200, False)
        except typer.Exit:
            pass
        if not Config.load().users:
            setup_later("pdms user add / pdms user import")
    elif choice == "manual":
        add_user(cfg)
        while questionary.confirm(_("Add another user?"), default=False).unsafe_ask():
            add_user(cfg)
    else:
        setup_later("pdms user add / pdms user import")


def setup_stacks() -> None:
    cfg = Config.load()
    if cfg.stacks:
        setup_step(_("Stacks"))
        setup_done(", ".join(cfg.stacks))
        return
    if cfg.repo is None or not cfg.repo.root.is_dir():
        return
    setup_step(_("Stacks"))
    if questionary.confirm(_("Create a stack (services you usually start together) now?"), default=False).unsafe_ask():
        try:
            stack_add()
        except typer.Exit:
            pass
    else:
        setup_later("pdms stack add")


@app.command("setup", help=_("Guided setup: import a shared configuration, then configure whatever is still missing."))
def setup_cmd() -> None:
    prompts.require_tty()
    first_time = not config_path().exists()
    console.print(_("[bold]pdms setup[/]: first a configuration to import, if you have one, then whatever is missing."))
    imported = setup_import()
    if first_time and "defaults" not in imported:
        setup_language()
    setup_repo()
    setup_migrations()
    setup_dbs()
    setup_users()
    setup_stacks()
    if first_time and "defaults" not in imported:
        setup_step(_("Defaults"))
        if questionary.confirm(
            _("Review the other defaults (port, log level, install, events...)?"), default=False
        ).unsafe_ask():
            config_defaults()
        else:
            setup_done(_("Using the standard values (change them with pdms config)."))
    if not config_path().exists():
        Config.load().save()
    console.print()
    if questionary.confirm(_("Check the environment now (pdms doctor)?"), default=True).unsafe_ask():
        try:
            doctor_cmd(False, 5)
        except typer.Exit:
            pass
    left = onboarding.pending(Config.load())
    console.print()
    console.print("[green]✓[/] " + _("Setup finished. Run pdms to open the menu; pdms setup again completes what is left."))
    if left:
        console.print("  [dim]" + _("Still missing: {steps}", steps=", ".join(setup_label(s) for s in left)) + "[/]")


def setup_label(step: str) -> str:
    return {
        "repo": _("PDMS repo"), "migrations": _("migrations repo"), "dbs": _("databases"),
        "passwords": _("database passwords"), "users": _("users"),
    }[step]


# --------------------------------------------------------------------------- interactive menus


def _menu(title: str, options: dict[str, Callable[[], None]]) -> None:
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


def db_menu() -> None:
    prompts.require_tty()
    _menu(_("Databases:"), {
        _("List"): db_list,
        _("Add"): db_add,
        _("Edit"): lambda: db_edit(None),
        _("Test connection"): lambda: db_test(None, None),
        _("Delete"): lambda: db_remove(None),
    })


def user_menu() -> None:
    prompts.require_tty()
    _menu(_("Users:"), {
        _("List"): user_list,
        _("Add"): user_add,
        _("Import from a database"): lambda: user_import(None, None, None, False, 200, False),
        _("Edit"): lambda: user_edit(None),
        _("Delete"): lambda: user_remove(None),
    })


def instances_menu() -> None:
    prompts.require_tty()
    _menu(_("Background services:"), {
        _("List"): lambda: ps(False),
        _("View logs (console)"): lambda: logs(None, False, None, True, None, False),
        _("View all logs together"): lambda: logs(None, True, None, True, None, False),
        _("View the previous run's log"): lambda: logs(None, False, None, False, None, True),
        _("Open in the browser (/docs)"): lambda: open_cmd(None, "/docs"),
        _("Show endpoints (URLs)"): lambda: urls(None, ""),
        _("Stop"): lambda: stop(None, False),
        _("Restart"): lambda: restart(None, None, None, False, None),
        _("Restart with another user/DB"): lambda: restart(None, None, None, True, None),
        _("Stop all"): lambda: stop(None, True),
    })


def stack_menu() -> None:
    prompts.require_tty()
    _menu(_("Stacks:"), {
        _("Start stack"): lambda: up(None, None, None, None, False, None),
        _("Stop stack"): lambda: down(None),
        _("List"): stack_list,
        _("Create"): stack_add,
        _("Edit"): lambda: stack_edit(None),
        _("Delete"): lambda: stack_remove(None),
    })


def repo_menu() -> None:
    if not interactive_terminal():
        return repo_list()
    _menu(_("Repos:"), {
        _("List"): repo_list,
        _("Add"): lambda: repo_add(None, None),
        _("Choose the current one"): lambda: repo_use(None),
        _("Edit"): lambda: repo_edit(None, None, None, None),
        _("Delete"): lambda: repo_remove(None),
    })


def settings_menu() -> None:
    prompts.require_tty()
    _menu(_("Settings:"), {
        _("Guided setup"): setup_cmd,
        _("Defaults"): config_defaults,
        _("Repos"): repo_menu,
        _("Check the environment (doctor)"): lambda: doctor_cmd(False, 5),
        _("Language"): lambda: config_language(None),
        _("Export configuration"): lambda: config_export(None, None, None, False),
        _("Import configuration"): lambda: config_import(None, None, False, False, False),
        _("Show configuration file path"): config_show_path,
    })


def main_menu(first_run: bool = False) -> None:
    prompts.require_tty()
    cfg = Config.load()
    if cfg.defaults.banner and not os.environ.get("PDMS_NO_BANNER"):
        banner.render(console, __version__, _("Local PDMS services, made easy"))
    if first_run:
        if questionary.confirm(
            _("Welcome! There is no pdms configuration yet. Run the guided setup now?"), default=True
        ).unsafe_ask():
            setup_cmd()
            console.print()
        else:
            check_repo(Config.load())
    if not config_path().exists():
        Config.load().save()
        console.print(_("[dim]Configuration created at {path}[/]", path=config_path()))
    if instances.running_ports():
        ps(False)
        console.print()
    while True:
        running = len(instances.running_ports())
        choice = questionary.select(
            _("What do you want to do?"),
            choices=[
                questionary.Choice(_("▶  Run a service"), "run"),
                questionary.Choice(_("📋 Background services ({count} running)", count=running), "ps"),
                questionary.Choice(_("🧩 Stacks (groups of services)"), "stack"),
                questionary.Choice(_("🌐 Proxy (one port for every service)"), "proxy"),
                questionary.Choice(_("📨 Events (local SQS)"), "events"),
                questionary.Choice(_("🗄  Databases"), "db"),
                questionary.Choice(_("👤 Users"), "user"),
                questionary.Choice(_("⚙  Settings"), "defaults"),
                questionary.Choice(_("✕  Exit"), "exit"),
            ],
        ).unsafe_ask()
        if choice == "exit":
            return
        actions = {
            "run": do_run, "ps": instances_menu, "stack": stack_menu,
            "proxy": lambda: proxy_main(None, 8000, None, None, False, "dev", None, None, None),
            "events": events_menu, "db": db_menu, "user": user_menu,
            "defaults": settings_menu,
        }
        try:
            actions[choice]()  # a foreground do_run replaces the process
        except typer.Exit:
            pass


# Commands that do not depend on a repo, so they never trigger the "switch repo?" question.
REPO_AGNOSTIC = {"repo", "config", "env", "db", "user", "self-update", "setup"}


DOCTOR_ICONS = {diagnostics.OK: "[green]✓[/]", diagnostics.WARN: "[yellow]⚠[/]", diagnostics.FAIL: "[red]✗[/]"}


@app.command("doctor", help=_("Check that everything pdms needs is in place (tools, configuration, databases, repo, ports)."))
def doctor_cmd(
    no_db: bool = typer.Option(False, "--no-db", help=_("Do not test the database connections.")),
    timeout: int = typer.Option(5, "--timeout", "-t", help=_("Seconds to wait for each database.")),
) -> None:
    cfg = Config.load()
    with console.status(_("Checking the environment...")):
        checks = diagnostics.run_all(cfg, databases=not no_db, timeout=timeout)
    section = None
    for check in checks:
        if check.section != section:
            section = check.section
            console.print(f"\n[bold]{section}[/]")
        line = f"  {DOCTOR_ICONS[check.status]} {check.name}: {check.detail}"
        if check.hint and check.status != diagnostics.OK:
            line += f"  [dim]→ {check.hint}[/]"
        console.print(line, highlight=False)
    counts = {status: sum(c.status == status for c in checks) for status in DOCTOR_ICONS}
    console.print()
    console.print(_("{ok} ok · {warn} warnings · {fail} problems", ok=counts[diagnostics.OK],
                    warn=counts[diagnostics.WARN], fail=counts[diagnostics.FAIL]))
    if counts[diagnostics.FAIL]:
        raise typer.Exit(1)


@app.command("self-update", help=_("Update pdms to the latest release (or to --version)."))
def self_update(
    version: Optional[str] = typer.Option(None, "--version", help=_("Install this version instead of the latest.")),
    check: bool = typer.Option(False, "--check", help=_("Only tell whether there is a newer version.")),
    pre: bool = typer.Option(False, "--pre", help=_("Include alpha/beta pre-releases (automatic if you run one).")),
) -> None:
    kind = update.install_kind()
    if kind == "editable":
        console.print(_("pdms {version} runs from a local checkout (editable install): update it with git pull.",
                        version=__version__))
        return
    try:
        with console.status(_("Looking for the latest release...")):
            target = version or update.latest_version(pre=pre or update.is_prerelease(__version__))
    except Exception as exc:  # noqa: BLE001 - network errors of any kind
        fail(_("Could not reach GitHub: {error}", error=exc))
    if not version and not update.is_newer(target):
        console.print("[green]✓[/] " + _("pdms {version} is the latest version.", version=__version__))
        return
    console.print(_("Current version: {current} · available: {target}", current=__version__, target=target))
    if check:
        return
    cmd = update.upgrade_command(target)
    if not update.updates_itself():
        console.print(_("Run this to update:"))
        console.print(f"  {subprocess.list2cmdline(cmd) if sys.platform == 'win32' else shlex.join(cmd)}",
                      highlight=False, markup=False)
        return
    from .ui import instance as ui_instance

    ui_running = ui_instance.running()
    if sys.platform == "win32":
        # The running pdms.exe cannot replace itself: a PowerShell window updates it once this pdms exits. pdms ui
        # and the proxy run from the same files, so they must not be running.
        if ui_running:
            fail(_("pdms ui is open: update from it (the ⬆ in its title bar) or close it first."))
        if proxy.running_proxy():
            fail(_("The proxy runs from pdms's own files, which the update replaces: stop it first (pdms stop proxy)."))
        update.spawn_windows_update(cmd)
        console.print(_("Updating to {version} in a new window, once this pdms exits.", version=target))
        return
    result = subprocess.run(cmd)
    if result.returncode:
        fail(_("The update failed (exit code {code}).", code=result.returncode))
    console.print("[green]✓[/] " + _("pdms updated to {version}.", version=target))
    if proxy.running_proxy():
        console.print(_("[dim]The proxy still runs the previous version until it restarts.[/]"))
    if ui_running:
        console.print(_("[dim]pdms ui still runs the previous version: it offers to restart itself.[/]"))


def update_check_enabled(cfg: Config, subcommand: Optional[str]) -> bool:
    return (
        interactive_terminal() and subcommand not in ("self-update", "ui")
        and update.checks_enabled(cfg.defaults.update_check)
    )


def show_update_notice(pre: bool) -> None:
    latest = update.notice_due(pre)
    if latest:
        console.print(
            "[yellow]⬆ " + _("New pdms version available: {current} → {latest} · update with: pdms self-update",
                             current=__version__, latest=latest) + "[/]",
            highlight=False,
        )


def start_update_check(ctx: typer.Context, cfg: Config) -> None:
    """Refresh the cached latest version in the background (once a day) and announce it when the command ends."""
    if not update_check_enabled(cfg, ctx.invoked_subcommand):
        return
    pre = update.is_prerelease(__version__)
    worker = None
    if update.check_due(pre):
        worker = threading.Thread(target=update.refresh, args=(pre,), daemon=True)
        worker.start()

    def finish() -> None:
        if worker is not None:
            worker.join(timeout=2.5)
        show_update_notice(pre)

    if ctx.invoked_subcommand is None:
        show_update_notice(pre)  # the menu can stay open a long time: announce what is already known up front
    ctx.call_on_close(finish)


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
