"""Background instances: ``pdms ps``, ``urls``, ``adopt``, ``logs``, ``open``, ``stop`` and ``restart``."""

from __future__ import annotations

import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, emails, events, frontend, installer, instances, logview, prompts, proxy, repos
from ..config import Config
from ..i18n import _
from .common import app, console, fail, pick, print_endpoints, print_restored, settle, show_menu
from .run import Profile, confirm_protected, do_run
from .services import backend_root, stack_paths


def status_text(state: str) -> str:
    return {
        "ok": "[green]● ok[/]",
        "busy": f"[green]◌ {_('busy')}[/]",
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
    cfg = Config.load()
    outside = actions.strays(cfg)
    if not items and not running_proxy and not running_frontend:
        if outside:
            print_strays(outside)
        else:
            console.print(_("No background services."))
        return
    healths = instances.health_all(list(items.values()))
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
    if outside:
        console.print()
        print_strays(outside)


def stray_table(found: list[actions.Stray]) -> Table:
    table = Table(_("Instance"), "PID", "URL", "Repo", _("User"), "DB", _("Uptime"))
    table.columns[0].no_wrap = table.columns[2].no_wrap = True
    for stray in found:
        proc = stray.process
        table.add_row(proc.key, str(proc.pid), f"sqs ← {proc.queue}" if proc.queue else f"http://localhost:{proc.port}",
                      stray.repo or "-", stray.user or "?", stray.db or "?", uptime(proc.started_at))
    return table


def print_strays(found: list[actions.Stray]) -> None:
    console.print("[yellow]⚠[/] " + _("Running outside pdms ({count}): pdms started them, then lost track of them.",
                                      count=len(found)), highlight=False)
    console.print(stray_table(found))
    console.print(_("[dim]pdms adopt to manage them again (logs, stop, restart), or pdms adopt --stop to stop them.[/]"))


@app.command(help=_("Manage again the services that run outside pdms (pdms ps lists them), or stop them."))
def adopt(
    keys: Optional[list[str]] = typer.Argument(None, help=_("Which ones (by default it asks; every one with --all).")),
    all_: bool = typer.Option(False, "--all", "-a", help=_("Every service running outside pdms.")),
    stop_them: bool = typer.Option(False, "--stop", help=_("Stop them instead, with their reloader and workers.")),
) -> None:
    cfg = Config.load()
    found = actions.strays(cfg)
    if not found:
        console.print(_("Nothing runs outside pdms."))
        return
    if keys:
        chosen = keys
    elif all_ or len(found) == 1:
        chosen = [stray.key for stray in found]
    else:
        prompts.require_tty()
        console.print(stray_table(found))
        question = _("Which ones do you want to stop? (space to select)") if stop_them \
            else _("Which ones do you want to adopt? (space to select)")
        chosen = questionary.checkbox(question, choices=[questionary.Choice(s.key, s.key, checked=True) for s in found]
                                      ).unsafe_ask()
        if not chosen:
            console.print(_("Nothing to do."))
            return
    try:
        if stop_them:
            with console.status(_("Stopping {key}...", key=", ".join(chosen))):
                stopped = actions.stop_strays(cfg, chosen)
            for key in stopped:
                console.print("[green]✓[/] " + _("{key} stopped.", key=key))
            return
        adopted = actions.adopt_strays(cfg, chosen)
    except actions.ActionError as exc:
        fail(exc.message)
    for inst in adopted:
        who = f"{inst.user or '?'} · {inst.db or '?'}"
        console.print("[green]✓[/] " + _("{key} adopted ({who}).", key=inst.key, who=who), highlight=False)
    if any(not inst.user or not inst.db for inst in adopted):
        console.print(_("[dim]? = no user or database of the config matches; pdms restart -c <instance> picks them.[/]"))


@app.command(help=_("Show the console of background services (Ctrl+C to exit)."))
def logs(
    keys: Optional[list[str]] = typer.Argument(
        None, help=_("Instances (or parts of the service name), proxy, frontend, sns (what was published to SNS locally), emails (what services sent through SES) or ui."),
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
    if keys and keys[0] == emails.KEY:
        return email_logs(follow, lines, previous)
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
    from ..ui import instance as ui_instance

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


def email_logs(follow: bool, lines: Optional[int], previous: bool) -> None:
    """What the services sent through SES (kept here, or sent to defaults.email_to only)."""
    log = emails.log_path()
    if previous:
        log = instances.previous_log_path(log)
    if not follow or previous:
        if not log.exists():
            fail(_("No service has sent an email yet."))
        console.print(instances.tail(str(log), lines or 100), markup=False, highlight=False, end="")
        return
    console.rule(_("{names} · Ctrl+C to exit", names=_("emails")))
    kept = instances.Instance(key=emails.KEY, pid=0, service="", host="", port=0, user="", db="", reload=False,
                              log=str(log), started_at="")
    logview.follow(console, [kept], lines or 100, None)


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


@app.command(help=_("Restart background services (same ports; same user and DB by default)."))
def restart(
    keys: Optional[list[str]] = typer.Argument(
        None, help=_("Instances (or parts of the service names). With --stack, which of its services."),
        autocompletion=completion.instance_keys,
    ),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("Switch to this user."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Switch to this database."), autocompletion=completion.dbs),
    change: bool = typer.Option(False, "--change", "-c", help=_("Ask which user and DB to use.")),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    stack: Optional[str] = typer.Option(
        None, "--stack", "-s", help=_("The running services of this stack (all, or the ones named)."), autocompletion=completion.stacks,
    ),
    remember: bool = typer.Option(
        False, "--remember", help=_("With --stack and -u/-d: the stack starts these services that way from now on."),
    ),
) -> None:
    cfg = Config.load()
    chosen = restart_targets(cfg, keys or [], stack)
    if remember and not (stack and (user or db or change)):
        fail(_("--remember needs --stack and a user or a database (-u, -d or --change)."))
    if change:
        prompts.require_tty()
        user = user or prompts.select_name(_("User:"), list(cfg.users), chosen[0].user)
        db = db or prompts.select_name(_("Database:"), list(cfg.dbs), chosen[0].db)
    if user:
        pick(cfg.users, _("user"), user)
    if db:
        pick(cfg.dbs, _("database"), db)
    # Confirm before stopping anything: a "no" must leave every instance running.
    changing = next((inst for inst in chosen if db and db != inst.db), None)
    if changing and cfg.dbs[db].protected:
        profile_user = user or changing.user
        confirm_protected(cfg, Profile(Path(changing.service), profile_user, cfg.users[profile_user], db, cfg.dbs[db],
                                       changing.host, changing.port), False)
    if remember:
        root = backend_root(cfg)
        for inst in chosen:
            settle(lambda inst=inst: actions.remember_in_stack(cfg, stack, root, [Path(inst.service)],
                                                               user or inst.user, db or inst.db))
        console.print("[green]✓[/] " + _("The stack {name} starts them that way from now on.", name=stack))
    for inst in chosen:
        if len(chosen) > 1:
            console.rule(inst.key)
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        do_run(
            user=user or inst.user, db=db or inst.db, port=inst.port, host=inst.host, install=install,
            reload=inst.reload, yes=True, path=Path(inst.service), background=True,
            events_mode="local" if inst.events == "local" else None,
        )


def restart_targets(cfg: Config, keys: list[str], stack: Optional[str]) -> list[instances.Instance]:
    """The instances ``pdms restart`` restarts: the ones named, or a stack's running ones (all, or the ones named)."""
    if not stack:
        if not keys:
            return [pick_instance(None, message=_("Which instance do you want to restart?"))]
        return list({inst.key: inst for inst in (pick_instance(key) for key in keys)}.values())
    running = actions.stack_instances(cfg, pick(cfg.stacks, _("stack"), stack), backend_root(cfg))
    if not running:
        fail(_("No service of the stack {name} is running: pdms up {name}", name=stack))
    if not keys:
        return running
    chosen = [inst for inst in running if any(key in inst.key for key in keys)]
    missing = [key for key in keys if not any(key in inst.key for inst in running)]
    if missing:
        fail(_("Not running in the stack {name}: {names}", name=stack, names=", ".join(missing)))
    return chosen


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


def instances_menu() -> None:
    prompts.require_tty()
    show_menu(_("Background services:"), {
        _("List"): lambda: ps(False),
        _("View logs (console)"): lambda: logs(None, False, None, True, None, False),
        _("View all logs together"): lambda: logs(None, True, None, True, None, False),
        _("View the previous run's log"): lambda: logs(None, False, None, False, None, True),
        _("Open in the browser (/docs)"): lambda: open_cmd(None, "/docs"),
        _("Show endpoints (URLs)"): lambda: urls(None, ""),
        _("Stop"): lambda: stop(None, False),
        _("Restart"): lambda: restart(None, None, None, False, None, None, False),
        _("Restart with another user/DB"): lambda: restart(None, None, None, True, None, None, False),
        _("Stop all"): lambda: stop(None, True),
    })
