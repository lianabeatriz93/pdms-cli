"""The web interface: ``pdms ui``."""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

import typer
from rich.markup import escape
from rich.text import Text

from .. import actions, desktop, instances, repos
from ..config import Config
from ..i18n import _
from .common import app, console, fail, settle

if TYPE_CHECKING:
    from ..ui.control import Control


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
    from ..ui import control as ui_control
    from ..ui import instance as ui_instance
    from ..ui import server as ui_server
    from ..ui import updates as ui_updates
    from ..ui import window as ui_window

    if detached:
        ui_instance.redirect_output()
        # Opened from the menu or at login: the tools of the user's shell (pyenv, nvm, Poetry...) are not on PATH yet.
        from .. import shellenv

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
    threading.Thread(target=jobs.health.watch, args=(stopped,), name="pdms-ui-health-watch", daemon=True).start()
    threading.Thread(target=jobs.changes.watch, args=(stopped,), name="pdms-ui-changes-watch", daemon=True).start()
    threading.Thread(target=jobs.aws.watch, args=(stopped,), name="pdms-ui-aws-watch", daemon=True).start()
    threading.Thread(target=instances.watch_logs, args=(stopped,), name="pdms-ui-logs-watch", daemon=True).start()
    threading.Thread(target=jobs.tests.count_failing, args=(repos.active_backend(Config.load()),),
                     name="pdms-ui-tests-count", daemon=True).start()
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
    from ..ui import instance as ui_instance

    console.print(f"[dim]{_('Restarting pdms ui with the new version...')}[/]")
    sys.stdout.flush()
    sys.stderr.flush()
    exe, *args = control.relaunch
    os.execve(exe, [exe, *args], {**os.environ, ui_instance.TOKEN_ENV: control.token})


def ui_setup(install: bool, uninstall: bool, at_login: Optional[bool]) -> None:
    """pdms ui --install / --uninstall / --at-login: the entries of pdms ui in the system."""
    from ..ui import window as ui_window

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
