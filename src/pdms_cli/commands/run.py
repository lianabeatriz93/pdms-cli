"""Running a service: ``pdms run``, ``pdms debug`` and ``pdms env``."""

from __future__ import annotations

import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, instances, prompts, runner
from ..config import Config, Database, DevUser
from ..i18n import _
from .common import EVENTS_HELP, add_db, add_user, app, console, fail, pick, print_endpoints, settle, yes_no
from .services import resolve_service


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


def warn_events(setup: actions.EventsSetup) -> None:
    if setup.warning:
        console.print(f"[yellow]{setup.warning}[/]")


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
    parallel: Optional[bool] = None,
) -> None:
    cfg = Config.load()
    service = resolve_service(cfg, service_name, path)
    consumer = actions.consumer_of(cfg, service)
    prof = choose_profile(cfg, None, service, user, db, port, host, yes, needs_port=not consumer)
    if background is None:
        background = sys.stdin.isatty() and prompts.ask_background()
    launch = settle(lambda: actions.plan_service(
        cfg, prof.service, user_name=prof.user_name, db_name=prof.db_name, port=prof.port or None, host=prof.host,
        reload=reload, events_mode=events_mode, parallel=parallel,
    ))
    warn_events(launch.events)

    if launch.is_consumer:
        what = {_("Consumer"): f"{launch.queue} → {consumer[1].handler}"}
    else:
        what = {_("Server"): f"http://{launch.host}:{launch.port}  reload={yes_no(launch.reload)}  "
                             f"parallel={yes_no(launch.parallel)}  log={cfg.defaults.logging_level}"}
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
            if health.state in ("ok", "busy"):  # busy: already serving a request that keeps it from answering
                console.print("[green]✓[/] " + _(
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
    parallel: Optional[bool] = typer.Option(
        None, "--parallel/--no-parallel",
        help=_("Each request in its own thread, so a slow query only holds up its own (default: the parallel_requests setting)."),
    ),
) -> None:
    do_run(service, user, db, port, host, install, reload, yes, path, background, events_mode, parallel)


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
    setup = settle(lambda: actions.events_setup(cfg, prof.service, events_mode))
    warn_events(setup)
    print_summary(cfg, prof, {
        _("Debug"): f"{consumer[0].name} -> {consumer[1].handler}" if consumer else
        _("http://{host}:{port} (no --reload, so breakpoints work)", host=prof.host, port=prof.port),
        _("Events"): setup.label,
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

    launch = settle(lambda: actions.plan_service(
        cfg, prof.service, user_name=prof.user_name, db_name=prof.db_name, port=prof.port or None, host=prof.host,
        reload=False, events_ready=setup,
    ))
    debug_setup = settle(lambda: actions.write_debug_config(cfg, launch))
    console.print("[green]✓[/] " + _("Configuration [bold]{name}[/] saved to {launch}", name=debug_setup.name,
                                     launch=debug_setup.launch_json))
    if debug_setup.backup:
        console.print(f"[yellow]{_('⚠ launch.json had comments and they were lost; original copy at {backup}', backup=debug_setup.backup)}[/]")
    console.print(_(
        "  Variables (including the password) in {env_file} [dim](outside the repo, permissions 600)[/]",
        env_file=debug_setup.env_file,
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
