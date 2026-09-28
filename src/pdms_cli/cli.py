"""``pdms`` command line entrypoint."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import questionary
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import (
    __version__, completion, i18n, installer, instances, logview, prompts, proxy, repos, routes, runner, transfer,
    update, userimport, vscode,
)
from .config import Config, Database, DevUser, Stack, config_path, write_private
from .i18n import _

console = Console()

app = typer.Typer(help=_("Run PDMS services locally. Without arguments it opens the interactive menu."))
db_app = typer.Typer(help=_("Manage databases."), invoke_without_command=True)
user_app = typer.Typer(help=_("Manage development users (DEV_*)."), invoke_without_command=True)
config_app = typer.Typer(help=_("General settings."), invoke_without_command=True)
stack_app = typer.Typer(help=_("Manage stacks (groups of services started together)."), invoke_without_command=True)
repo_app = typer.Typer(help=_("Manage PDMS repos (checkouts) and choose the current one."), invoke_without_command=True)
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
    cfg.dbs[name] = prompts.ask_database()
    cfg.save()
    console.print("[green]✓[/] " + _("Database '{name}' saved.", name=name))
    if questionary.confirm(_("Test the connection now?"), default=True).unsafe_ask():
        check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)
    return name


def add_user(cfg: Config) -> str:
    prompts.require_tty()
    name = prompts.ask_name(_("user"), cfg.users)
    cfg.users[name] = prompts.ask_user()
    cfg.save()
    console.print("[green]✓[/] " + _("User '{name}' saved.", name=name))
    return name


def check_db(name: str, db: Database, timeout: int) -> bool:
    with console.status(_(
        "Connecting to {name} ({host}:{port}, timeout {timeout}s)...", name=name, host=db.host, port=db.port,
        timeout=timeout,
    )):
        try:
            version = runner.test_connection(db, timeout)
        except Exception as exc:  # noqa: BLE001 - show any driver error to the user
            console.print(f"[red]✗[/] {name}: {str(exc).strip()}")
            return False
    console.print(f"[green]✓[/] {name}: {version.split(',')[0]}")
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
    result: dict[str, list[int]] = {}
    for inst in instances.load().values():
        if inst.alive():
            result.setdefault(inst.service, []).append(inst.port)
    return result


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
    # Ports of background instances count as taken even while they are still booting.
    taken = instances.running_ports()
    is_free = lambda p: p not in taken and runner.port_is_free(host, p)  # noqa: E731
    if port is None and interactive:
        port = prompts.ask_port(runner.next_free_port(host, cfg.defaults.port, taken), is_free)
    port = port or cfg.defaults.port
    if not is_free(port):
        free = runner.next_free_port(host, port + 1, taken)
        if yes or not interactive:
            fail(_("Port {port} is in use (the next free one is {free}). Use --port.", port=port, free=free))
        if not questionary.confirm(_("Port {port} is in use. Use {free} instead?", port=port, free=free),
                                   default=True).unsafe_ask():
            raise typer.Exit(1)
        port = free
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
    if prof.db.protected and not yes:
        prompts.require_tty()
        if not questionary.confirm(_("'{name}' is a protected DB. Continue?", name=prof.db_name),
                                   default=False).unsafe_ask():
            raise typer.Exit(1)
    cfg.last_user, cfg.last_db = prof.user_name, prof.db_name
    cfg.save()


def poetry_install(service: Path) -> None:
    try:
        runner.ensure_poetry()
        console.rule("poetry lock && poetry install")
        runner.install(service)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        fail(str(exc))
    installer.remember(service)


def install_label(cfg: Config, install: Optional[bool], each: bool = False) -> str:
    if install is False or (install is None and not cfg.defaults.install):
        return _("no")
    if install is None and cfg.defaults.smart_install:
        return _("only if something changed (-i to force)")
    return _("poetry lock && poetry install (each)") if each else "poetry lock && poetry install"


def ensure_installed(cfg: Config, service: Path, install: Optional[bool]) -> None:
    """Install according to the flag: True forces it, False skips it, None follows the settings (smart by default)."""
    if install is False or (install is None and not cfg.defaults.install):
        try:
            runner.ensure_poetry()
        except RuntimeError as exc:
            fail(str(exc))
        return
    if install is None and cfg.defaults.smart_install and runner.poetry_python(service) \
            and installer.is_up_to_date(service):
        console.print("[green]✓[/] " + _(
            "{name}: dependencies up to date (nothing changed since the last install), skipping.", name=service.name
        ))
        return
    poetry_install(service)


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
) -> None:
    cfg = Config.load()
    prof = choose_profile(cfg, service_name, path, user, db, port, host, yes)
    if background is None:
        background = sys.stdin.isatty() and prompts.ask_background()
    reload = cfg.defaults.reload if reload is None else reload

    print_summary(cfg, prof, {
        _("Server"): f"http://{prof.host}:{prof.port}  reload={yes_no(reload)}  log={cfg.defaults.logging_level}",
        _("Mode"): _("background") if background else _("foreground"),
        _("Install"): install_label(cfg, install),
    })
    confirm_protected(cfg, prof, yes)
    ensure_installed(cfg, prof.service, install)

    cmd = runner.uvicorn_command(prof.host, prof.port, reload)
    env = runner.build_env(cfg.defaults, prof.user, prof.db)
    if not background:
        console.rule(f"uvicorn :{prof.port}")
        runner.exec_server(prof.service, cmd, env)

    inst = instances.start(
        prof.service, cmd, env, host=prof.host, port=prof.port, user=prof.user_name, db=prof.db_name, reload=reload
    )
    wait_until_ready(inst)


def wait_until_ready(inst: instances.Instance, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    with console.status(_("Starting {key}...", key=inst.key)):
        while time.monotonic() < deadline:
            health = instances.health(inst)
            if health.state == "stopped":
                instances.forget(inst.key)
                console.print(instances.tail(inst.log, 30), markup=False, highlight=False)
                fail(_("{key} exited while starting. Full log: {log}", key=inst.key, log=inst.log))
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
) -> None:
    do_run(service, user, db, port, host, install, reload, yes, path, background)


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
) -> None:
    cfg = Config.load()
    prof = choose_profile(cfg, service, path, user, db, port, host, yes)
    print_summary(cfg, prof, {
        _("Debug"): _("http://{host}:{port} (no --reload, so breakpoints work)", host=prof.host, port=prof.port),
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

    env_file = vscode.write_env_file(prof.service, runner.service_env(cfg.defaults, prof.user, prof.db))
    launch, name, backup = vscode.upsert_configuration(
        prof.service, python=python, env_file=env_file, host=prof.host, port=prof.port,
        description=f"{prof.user_name} @ {prof.db_name} :{prof.port}",
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
# Alembic commands that only read the database (revision/merge write files, not the DB).
ALEMBIC_READ_ONLY = {"current", "history", "heads", "branches", "show", "check", "revision", "merge"}


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
    env = dict(os.environ)
    if user or db:
        user_name = pick(cfg.users, _("user"), user, cfg.last_user)
        db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
        prof = Profile(target, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], cfg.defaults.host, 0)
        confirm_protected(cfg, prof, False)
        env.update(runner.service_env(cfg.defaults, prof.user, prof.db))
        console.print(_("Profile: {user} @ {db}", user=user_name, db=db_name))
    ensure_installed(cfg, target, install)
    run_in_service(target, [runner.poetry(), "run", "pytest", *ctx.args], env)


@app.command(context_settings=PASSTHROUGH, help=_(
    "Run Alembic (backend/common/sync-database) against a database, e.g. pdms migrate -d local upgrade head. "
    "Without arguments: current."
))
def migrate(
    ctx: typer.Context,
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    allow_protected: bool = typer.Option(
        False, "--allow-protected", help=_("Allow commands that change a protected (shared) database.")
    ),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n",
        help=_("Force (-i) or skip (-n) the install; by default only if something changed."),
    ),
) -> None:
    cfg = Config.load()
    root = services_root(cfg) or fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    project = root / "common" / "sync-database"
    if not (project / "alembic.ini").is_file():
        fail(_("Alembic project not found at {path}.", path=project))
    args = ctx.args or ["current"]
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    database = cfg.dbs[db_name]
    writes = args[0] not in ALEMBIC_READ_ONLY
    if writes and database.protected:
        console.print("[red]" + _(
            "⚠ '{name}' is a protected (shared) database. Running 'alembic {command}' on it can break the pipeline "
            "and the data of the whole team.", name=db_name, command=args[0],
        ) + "[/]")
        if not allow_protected:
            fail(_("Refused. Use a local database, or --allow-protected if you really have to."))
        prompts.require_tty()
        typed = questionary.text(_("Type the database alias ({name}) to confirm:", name=db_name)).unsafe_ask()
        if typed.strip() != db_name:
            fail(_("Confirmation does not match; nothing was run."))
    console.print(_("Database: {name} → {url}", name=db_name, url=database.url(mask=True)))
    ensure_installed(cfg, project, install)
    env = {**os.environ, "DB_PG_CONNECTION_STR": database.url(), "LOGGING_LEVEL": cfg.defaults.logging_level}
    run_in_service(project, [runner.poetry(), "run", "alembic", *args], env)


# --------------------------------------------------------------------------- background instances


def status_text(state: str) -> str:
    return {
        "ok": "[green]● ok[/]",
        "starting": f"[cyan]… {_('starting')}[/]",
        "error": "[yellow]⚠ error[/]",
        "stopped": f"[red]✗ {_('stopped')}[/]",
    }[state]


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
        targets = [i for i in instances.load().values() if i.alive()]
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
    if not items:
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
            inst.key, status_text(state), f"http://localhost:{inst.port}",
            f"[bold]{repo}[/]" if repo == cfg.current_repo else repo,
            inst.user, inst.db, uptime(inst.started_at) if state != "stopped" else "",
        )
    console.print(table)
    for key, health in healths.items():
        if health.state == "error":
            console.print(f"[yellow]⚠ {key}:[/] {health.detail}  [dim](pdms logs {key})[/]", highlight=False)
    if any(h.state == "stopped" for h in healths.values()):
        console.print(_("[dim]Stopped ones keep their log (pdms logs <instance>). Remove them with pdms ps --clean.[/]"))


@app.command(help=_("Show the console of background services (Ctrl+C to exit)."))
def logs(
    keys: Optional[list[str]] = typer.Argument(None, help=_("Instances (or parts of the service name)."), autocompletion=completion.instance_keys),
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


@app.command("open", help=_("Open a background service in the browser (Swagger /docs by default)."))
def open_cmd(
    key: Optional[str] = typer.Argument(None, help=_("Instance (or part of the service name)."), autocompletion=completion.instance_keys),
    path: str = typer.Option("/docs", "--path", "-P", help=_("Path to open, e.g. /redoc or /.")),
) -> None:
    inst = pick_instance(key, only_alive=True, message=_("Which instance do you want to open?"))
    url = f"http://localhost:{inst.port}/{path.lstrip('/')}"
    console.print(_("Opening {url}", url=url))
    if not webbrowser.open(url):
        console.print(_("[yellow]Could not open a browser; open the URL manually.[/]"))


@app.command(help=_("Stop background services."))
def stop(
    key: Optional[str] = typer.Argument(None, help=_("Instance (or part of the service name)."), autocompletion=completion.instance_keys),
    all_: bool = typer.Option(False, "--all", "-a", help=_("Stop all.")),
) -> None:
    running = [i for i in instances.load().values() if i.alive()]
    if all_:
        targets = running
    elif key:
        targets = [pick_instance(key)]
    elif len(running) <= 1:
        targets = [pick_instance(None, only_alive=True)]
    else:
        prompts.require_tty()
        targets = questionary.checkbox(
            _("Which instances do you want to stop? (space to select)"),
            choices=[questionary.Choice(i.key, i) for i in running],
        ).unsafe_ask()
    if not targets:
        console.print(_("Nothing to stop."))
    for inst in targets:
        with console.status(_("Stopping {key}...", key=inst.key)):
            instances.stop(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))


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
        instances.stop(inst)
    do_run(
        user=user, db=db, port=inst.port, host=inst.host, install=install, reload=inst.reload,
        yes=True, path=Path(inst.service), background=True,
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


def ask_stack(cfg: Config, current: Optional[Stack] = None) -> Stack:
    prompts.require_tty()
    root, candidates = list_services(cfg)
    services = list(current.services) if current else []
    if services:
        services = questionary.checkbox(
            _("Stack services (uncheck to remove):"),
            choices=[questionary.Choice(s, s, checked=True) for s in services],
        ).unsafe_ask()
    while not services or questionary.confirm(
        _("Add another service? (it has {count})", count=len(services)), default=not services
    ).unsafe_ask():
        remaining = [c for c in candidates if label(c, root) not in services]
        services.append(label(choose_service(remaining, root, _("Service to add")), root))
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
    cfg.stacks[name] = ask_stack(cfg)
    cfg.save()
    console.print("[green]✓[/] " + _("Stack '{name}' saved. Start it with [bold]pdms up {name}[/].", name=name))


@stack_app.command("edit", help=_("Edit a stack."))
def stack_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    cfg.stacks[name] = ask_stack(cfg, cfg.stacks[name])
    cfg.save()
    console.print("[green]✓[/] " + _("Stack '{name}' updated.", name=name))


@stack_app.command("remove", help=_("Delete a stack."))
def stack_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    if questionary.confirm(_("Delete stack '{name}'?", name=name), default=False).unsafe_ask():
        del cfg.stacks[name]
        cfg.save()
        console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))


def stack_paths(cfg: Config, stack: Stack) -> list[Path]:
    root = services_root(cfg) or fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    paths = []
    for svc in stack.services:
        path = root / svc
        if not runner.is_service(path):
            fail(_("'{svc}' is no longer a service in {root}. Edit the stack with [bold]pdms stack edit[/].",
                   svc=svc, root=root))
        paths.append(path)
    return paths


@app.command(help=_("Start all services of a stack in the background, each on a free port."))
def up(
    name: Optional[str] = typer.Argument(None, help=_("Stack to start."), autocompletion=completion.stacks),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("User (defaults to the stack's)."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database (defaults to the stack's)."), autocompletion=completion.dbs),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    stack = cfg.stacks[name]
    paths = stack_paths(cfg, stack)
    user_name = pick(cfg.users, _("user"), user or stack.user or None, cfg.last_user)
    db_name = pick(cfg.dbs, _("database"), db or stack.db or None, cfg.last_db)
    host = cfg.defaults.host

    running = running_by_service()
    pending = [p for p in paths if str(p) not in running]
    for path in paths:
        if str(path) in running:
            console.print(_("[dim]· {name} already running on :{ports}, skipping.[/]",
                            name=path.name, ports=", :".join(map(str, running[str(path)]))))
    if not pending:
        console.print("[green]✓[/] " + _("The whole stack '{name}' is running.", name=name))
        return

    taken = set(instances.running_ports())
    plan = []
    port = cfg.defaults.port
    for path in pending:
        port = runner.next_free_port(host, port, taken)
        taken.add(port)
        plan.append((path, port))

    first = Profile(plan[0][0], user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], host, plan[0][1])
    print_summary(cfg, first, {
        _("Services"): "\n".join(f"{p.name} → :{port}" for p, port in plan),
        _("Install"): install_label(cfg, install, each=True),
    }, show_service=False)
    confirm_protected(cfg, first, yes)

    started = []
    for path, port in plan:
        ensure_installed(cfg, path, install)
        env = runner.build_env(cfg.defaults, cfg.users[user_name], cfg.dbs[db_name])
        cmd = runner.uvicorn_command(host, port, cfg.defaults.reload)
        started.append(instances.start(
            path, cmd, env, host=host, port=port, user=user_name, db=db_name, reload=cfg.defaults.reload
        ))
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
    paths = {str(p) for p in stack_paths(cfg, cfg.stacks[name])}
    targets = [i for i in instances.load().values() if i.service in paths and i.alive()]
    if not targets:
        console.print(_("Nothing from stack '{name}' is running.", name=name))
    for inst in targets:
        with console.status(_("Stopping {key}...", key=inst.key)):
            instances.stop(inst)
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
    cfg.dbs[name] = prompts.ask_database(cfg.dbs[name])
    cfg.save()
    console.print("[green]✓[/] " + _("Database '{name}' updated.", name=name))


@db_app.command("remove", help=_("Delete a database."))
def db_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.dbs)) -> None:
    cfg = Config.load()
    name = pick(cfg.dbs, _("database"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        del cfg.dbs[name]
        cfg.save()
        console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))


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
    cfg.users[name] = prompts.ask_user(cfg.users[name])
    cfg.save()
    console.print("[green]✓[/] " + _("User '{name}' updated.", name=name))


@user_app.command("remove", help=_("Delete a user."))
def user_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.users)) -> None:
    cfg = Config.load()
    name = pick(cfg.users, _("user"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        del cfg.users[name]
        cfg.save()
        console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))


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
        try:
            found = userimport.fetch_users(
                cfg.dbs[db_name], search=search or "", role=role or "", include_inactive=inactive, limit=limit,
                timeout=cfg.defaults.db_timeout, mapping=mapping,
            )
        except Exception as exc:  # noqa: BLE001 - show any driver error to the user
            fail(_("Could not read the users from {name}: {error}", name=db_name, error=str(exc).strip()))
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

    cfg.users, added, updated = userimport.merge_users(cfg.users, picked, mapping)
    cfg.save()
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
    old = cfg.current_repo
    cfg.current_repo = alias
    cfg.save()
    repos.use_for_this_command(None)
    console.print("[green]✓[/] " + _("Current repo: {alias} ({path})", alias=alias, path=cfg.repos[alias].root))
    if old and old != alias and old in cfg.repos:
        handle_instances_of(cfg, old, alias)


def handle_instances_of(cfg: Config, old: str, new: str) -> None:
    """Offer to keep, stop or move to the new repo the instances still running from the old one."""
    running = [i for i in instances.load().values() if i.alive() and repos.repo_of(cfg, i.service) == old]
    if not running or not interactive_terminal():
        return
    choice = questionary.select(
        _("{count} instances are running from '{old}': {keys}. What should I do with them?",
          count=len(running), old=old, keys=", ".join(i.key for i in running)),
        choices=[
            questionary.Choice(_("Keep them running (they coexist, each on its port)"), "keep"),
            questionary.Choice(_("Stop them"), "stop"),
            questionary.Choice(_("Restart them from '{new}' (same user, DB and port)", new=new), "move"),
        ],
    ).unsafe_ask()
    if choice == "keep":
        return
    old_root, new_root = cfg.repos[old].root, cfg.repos[new].root
    for inst in running:
        target = repos.translate(Path(inst.service), old_root, new_root) if choice == "move" else None
        if choice == "move" and target is None:
            console.print("[yellow]" + _("⚠ {key}: the service does not exist in '{new}'; left running.",
                                         key=inst.key, new=new) + "[/]")
            continue
        with console.status(_("Stopping {key}...", key=inst.key)):
            instances.stop(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
        if target is not None:
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
    table = Table("", _("Name"), _("Path"), _("Running"))
    for alias, repo in cfg.repos.items():
        path = str(repo.root) if repo.root.is_dir() else f"[red]{repo.root} ({_('missing')})[/]"
        table.add_row("●" if alias == cfg.current_repo else "", alias, path, str(counts.get(alias, "")))
    console.print(table)


@repo_app.command("add", help=_("Register a PDMS repo (defaults to the current folder)."))
def repo_add(
    path: Optional[Path] = typer.Argument(None, help=_("Folder inside the repo.")),
    alias: Optional[str] = typer.Option(None, "--alias", "-a", help=_("Name for the repo.")),
) -> None:
    cfg = Config.load()
    root = repos.find_repo_root((path or Path.cwd()).expanduser())
    if root is None:
        fail(_("{path} is not inside a PDMS repo (no backend/snakesdk folder).", path=path or Path.cwd()))
    if existing := repos.alias_of(cfg, root):
        console.print(_("{path} is already registered as '{alias}'.", path=root, alias=existing))
        return
    if alias is None and interactive_terminal():
        alias = questionary.text(_("Alias ({kind}):", kind=_("repo")), default=repos.suggest_alias(cfg, root),
                                 validate=lambda v: bool(v.strip()) and v.strip() not in cfg.repos
                                 or _("That name already exists")).unsafe_ask().strip()
    was_empty = not cfg.repos
    alias = repos.register(cfg, root, alias)
    cfg.save()
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


@repo_app.command("remove", help=_("Forget a registered repo (nothing is deleted from disk)."))
def repo_remove(alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos)) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias)
    if interactive_terminal() and not questionary.confirm(_("Delete '{name}'?", name=alias), default=False).unsafe_ask():
        return
    root = str(cfg.repos.pop(alias).root)
    cfg.ignored_repos = [r for r in cfg.ignored_repos if r != root]
    if cfg.current_repo == alias:
        cfg.current_repo = next(iter(cfg.repos), "")
    cfg.save()
    console.print("[green]✓[/] " + _("'{name}' deleted.", name=alias))


# --------------------------------------------------------------------------- proxy


def current_repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def load_repo_routes(root: Path, env: str) -> list[routes.Route]:
    if not routes.terraform_dir(root, env).is_dir():
        fail(_("No Terraform for '{env}' in {path}.", env=env, path=routes.terraform_dir(root, env)))
    with console.status(_("Reading the API routes from Terraform...")):
        return routes.load_routes(root, env)


def resolve_remote(cfg: Config, root: Path, remote: Optional[str], no_remote: bool) -> Optional[str]:
    if no_remote:
        return None
    alias = repos.alias_of(cfg, root)
    repo = cfg.repos.get(alias) if alias else None
    if remote:
        remote = remote.rstrip("/")
        if repo and repo.remote != remote:
            repo.remote = remote
            cfg.save()
        return remote
    if repo and repo.remote:
        return repo.remote
    detected = repos.remote_from_frontend(root)
    if detected and repo:
        repo.remote = detected
        cfg.save()
        console.print(_("[dim]Remote API taken from frontend/.env and saved for '{alias}': {url}[/]",
                        alias=alias, url=detected))
    return detected


def log_request(method: str, path: str, status: int, target: str, seconds: float) -> None:
    color = "green" if status < 400 else "yellow" if status < 500 else "red"
    where = "dim" if target in ("remote", "missing", "other-repo") else "cyan"
    console.print(Text.assemble(
        (f"{datetime.now():%H:%M:%S} ", "dim"), (f"{method:<6} ", "bold"), (f"{path} ", ""),
        (f"{status} ", color), ("→ ", "dim"), (target, where), (f"  {seconds * 1000:.0f}ms", "dim"),
    ), soft_wrap=True)


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
) -> None:
    if ctx is not None and ctx.invoked_subcommand is not None:
        return
    if running := proxy.running_proxy():
        fail(_("The proxy is already running on port {port} (pid {pid}).", port=running["port"], pid=running["pid"]))
    cfg = Config.load()
    root = current_repo_root(cfg)
    repo_routes = load_repo_routes(root, env)
    target_remote = resolve_remote(cfg, root, remote, no_remote)
    user = cfg.users[pick(cfg.users, _("user"), as_user)] if as_user else None
    if not runner.port_is_free("0.0.0.0", port):
        fail(_("Port {port} is in use (the next free one is {free}). Use --port.",
               port=port, free=runner.next_free_port("0.0.0.0", port + 1)))

    proxy_url = f"http://localhost:{port}"
    if (root / "frontend").is_dir() and not repos.frontend_uses(root, proxy_url):
        if frontend is None and interactive_terminal():
            frontend = questionary.confirm(
                _("Point the frontend to the proxy? (writes VITE_APP_API_URL in frontend/.env.local, git-ignored)"),
                default=True,
            ).unsafe_ask()
        if frontend:
            written = repos.point_frontend_to(root, proxy_url)
            console.print("[green]✓[/] " + _("{path} updated; restart yarn dev to apply it.", path=written))

    summary = Table.grid(padding=(0, 2))
    summary.add_row(f"[bold]{_('Proxy')}[/]", proxy_url)
    summary.add_row(f"[bold]Docs[/]", f"{proxy_url}/docs")
    summary.add_row(f"[bold]Repo[/]", f"{repos.alias_of(cfg, root) or root.name} ({env}, {len(repo_routes)} {_('routes')})")
    summary.add_row(f"[bold]{_('Remote')}[/]", target_remote or _("none (only local services)"))
    summary.add_row(f"[bold]{_('Acting as')}[/]", f"{as_user} ({user.roles})" if user else _("each service's own profile"))
    console.print(summary)
    console.rule(_("Requests · Ctrl+C to stop"))

    gateway = proxy.Gateway(
        routes=repo_routes, backend=(root / "backend"), remote=target_remote, impersonate=user, log=log_request
    )
    try:
        proxy.serve(gateway, "0.0.0.0", port, {"repo": str(root), "remote": target_remote or "", "as": as_user or ""})
    except KeyboardInterrupt:
        console.print(f"\n[dim]{_('Proxy stopped.')}[/]")


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


# --------------------------------------------------------------------------- config commands


@config_app.callback()
def config_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        config_defaults()


@config_app.command("defaults", help=_("Edit the defaults (language, port, log, reload, extra env...)."))
def config_defaults() -> None:
    prompts.require_tty()
    cfg = Config.load()
    cfg.defaults = prompts.ask_defaults(cfg.defaults)
    cfg.save()
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
    if lang not in i18n.LANGUAGES:
        fail(_("Unknown language '{lang}'. Available: {codes}", lang=lang, codes=", ".join(i18n.LANGUAGES)))
    cfg.defaults.language = lang
    cfg.save()
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

    text = transfer.export_document(cfg, sections, secrets)
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
) -> None:
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
    if not config_path().exists():
        chosen = set(conflicts)  # first setup: there is no own configuration to keep
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
        return
    if not yes:
        if not interactive:
            fail(_("Use --yes to import without an interactive terminal."))
        if not questionary.confirm(_("Apply the import?"), default=True).unsafe_ask():
            raise typer.Exit(1)

    backup = None
    if config_path().exists():
        backup = config_path().with_name(f"{config_path().name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path(), backup)
    result.save()
    i18n.set_language(result.defaults.language)
    console.print("[green]✓[/] " + _("Configuration imported."))
    if backup:
        console.print(_("  [dim]Previous configuration saved to {backup}[/]", backup=backup))
    no_password = [name for name, db in result.dbs.items() if not db.password]
    if no_password:
        console.print("[yellow]" + _("⚠ Databases without password: {names}. Set it with pdms db edit <name>.",
                                     names=", ".join(no_password)) + "[/]")


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
        _("Start stack"): lambda: up(None, None, None, None, False),
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
        _("Delete"): lambda: repo_remove(None),
    })


def settings_menu() -> None:
    prompts.require_tty()
    _menu(_("Settings:"), {
        _("Defaults"): config_defaults,
        _("Repos"): repo_menu,
        _("Language"): lambda: config_language(None),
        _("Export configuration"): lambda: config_export(None, None, None, False),
        _("Import configuration"): lambda: config_import(None, None, False, False, False),
        _("Show configuration file path"): config_show_path,
    })


def main_menu() -> None:
    prompts.require_tty()
    cfg = Config.load()
    if not config_path().exists():
        cfg.save()
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
            "proxy": lambda: proxy_main(None, 8000, None, None, False, "dev", None), "db": db_menu, "user": user_menu,
            "defaults": settings_menu,
        }
        try:
            actions[choice]()  # a foreground do_run replaces the process
        except typer.Exit:
            pass


# Commands that do not depend on a repo, so they never trigger the "switch repo?" question.
REPO_AGNOSTIC = {"repo", "config", "env", "db", "user", "self-update"}


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
    if kind != "uv-tool" or sys.platform == "win32" or not shutil.which("uv"):
        # On Windows the running pdms.exe cannot replace itself, and non-uv installs need their own command.
        console.print(_("Run this to update:"))
        console.print(f"  {subprocess.list2cmdline(cmd) if sys.platform == 'win32' else shlex.join(cmd)}",
                      highlight=False, markup=False)
        return
    result = subprocess.run(cmd)
    if result.returncode:
        fail(_("The update failed (exit code {code}).", code=result.returncode))
    console.print("[green]✓[/] " + _("pdms updated to {version}.", version=target))


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
    if ctx.invoked_subcommand not in REPO_AGNOSTIC:
        check_repo(Config.load())
    if ctx.invoked_subcommand is None:
        main_menu()


def _entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print(f"\n[dim]{_('Cancelled.')}[/]")
        sys.exit(130)
