"""The PDMS web app: ``pdms front``."""

from __future__ import annotations

from typing import Optional

import questionary
import typer

from .. import actions, frontend, instances, prompts, runner
from ..config import Config
from ..i18n import _
from .common import app, console, fail, interactive_terminal, settle


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
