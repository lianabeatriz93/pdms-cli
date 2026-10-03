"""The local API gateway: ``pdms proxy`` and ``pdms proxy routes``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table
from rich.text import Text

from .. import actions, completion, instances, prompts, proxy, repos, routes
from ..config import Config
from ..i18n import _
from .common import (
    METHOD_STYLE,
    console,
    current_repo_root,
    fail,
    interactive_terminal,
    pick,
    print_restored,
    proxy_app,
    settle,
)


def load_repo_routes(root: Path, env: str) -> list[routes.Route]:
    with console.status(_("Reading the API routes from Terraform...")):
        return settle(lambda: actions.proxy_routes(root, env))


def resolve_remote(cfg: Config, root: Path, remote: Optional[str], no_remote: bool) -> Optional[str]:
    url, detected = actions.proxy_remote(cfg, root, remote, no_remote)
    if detected:
        console.print(_("[dim]Remote API taken from frontend/.env and saved for '{alias}': {url}[/]",
                        alias=repos.alias_of(cfg, root), url=url))
    return url


def log_request(method: str, path: str, status: int, target: str, seconds: float, ident: str = "") -> None:
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


@proxy_app.callback()
def proxy_main(
    ctx: typer.Context,
    port: Optional[int] = typer.Option(
        None, "--port", "-p", help=_("Port to listen on (default: the proxy_port setting, 28800)."),
    ),
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
    port = port or cfg.defaults.proxy_port
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
    summary.add_row("[bold]Docs[/]", f"{plan.url}/docs")
    summary.add_row("[bold]Repo[/]", f"{repos.alias_of(cfg, root) or root.name} ({env}, {len(repo_routes)} {_('routes')})")
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
