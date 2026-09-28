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

from . import completion, i18n, installer, instances, logview, prompts, runner, transfer, vscode
from .config import Config, Database, DevUser, Stack, config_path, write_private
from .i18n import _

console = Console()

app = typer.Typer(help=_("Run PDMS services locally. Without arguments it opens the interactive menu."))
db_app = typer.Typer(help=_("Manage databases."), invoke_without_command=True)
user_app = typer.Typer(help=_("Manage development users (DEV_*)."), invoke_without_command=True)
config_app = typer.Typer(help=_("General settings."), invoke_without_command=True)
stack_app = typer.Typer(help=_("Manage stacks (groups of services started together)."), invoke_without_command=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(config_app, name="config")
app.add_typer(stack_app, name="stack")


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
    if cfg.defaults.backend_path:
        root = Path(cfg.defaults.backend_path).expanduser()
        if root.is_dir():
            return root.resolve()
        console.print(f"[yellow]{_('⚠ The configured backend folder does not exist: {root}', root=root)}[/]")
    return None


def list_services(cfg: Config) -> tuple[Path, list[Path]]:
    root = services_root(cfg) or Path.cwd().resolve()
    candidates = runner.find_services_below(root)
    if not candidates:
        hint = "" if cfg.defaults.backend_path else _(" Set the backend folder with [bold]pdms config[/].")
        fail(_("No services (pyproject.toml + main.py) found in {root}.{hint}", root=root, hint=hint))
    return root, candidates


def label(service: Path, root: Path) -> str:
    try:
        return str(service.relative_to(root))
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
    table = Table(_("Instance"), _("Status"), "URL", "PID", _("User"), "DB", _("Uptime"))
    table.columns[0].no_wrap = table.columns[1].no_wrap = table.columns[2].no_wrap = True
    for inst in items.values():
        state = healths[inst.key].state
        table.add_row(
            inst.key, status_text(state), f"http://localhost:{inst.port}", str(inst.pid),
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
    root = services_root(cfg) or fail(_("Set the backend folder with [bold]pdms config[/] to use stacks."))
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
        doc = transfer.read_document(file.read_text())
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
        subprocess.run([*shlex.split(editor), str(config_path())])
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


def settings_menu() -> None:
    prompts.require_tty()
    _menu(_("Settings:"), {
        _("Defaults"): config_defaults,
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
                questionary.Choice(_("🗄  Databases"), "db"),
                questionary.Choice(_("👤 Users"), "user"),
                questionary.Choice(_("⚙  Settings"), "defaults"),
                questionary.Choice(_("✕  Exit"), "exit"),
            ],
        ).unsafe_ask()
        if choice == "exit":
            return
        actions = {
            "run": do_run, "ps": instances_menu, "stack": stack_menu, "db": db_menu, "user": user_menu,
            "defaults": settings_menu,
        }
        try:
            actions[choice]()  # a foreground do_run replaces the process
        except typer.Exit:
            pass


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        main_menu()


def _entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print(f"\n[dim]{_('Cancelled.')}[/]")
        sys.exit(130)
