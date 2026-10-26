"""The browser extension that opens the local frontend from the known front: ``pdms browser ...`` (see browser.py)."""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.markup import escape

from .. import actions, browser
from ..config import Config
from ..i18n import _
from .common import app, console, fail, interactive_terminal, settle

browser_app = typer.Typer(help=_(
    "The browser extension that opens the frontend pdms runs when you go to the known front (dev-pdm.alivi.com...)."
), invoke_without_command=True)
app.add_typer(browser_app, name="browser")


@browser_app.callback()
def browser_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        browser_status()


@browser_app.command("status", help=_("Whether the redirect is on, and where the extension is installed."))
def browser_status() -> None:
    info = browser.status(Config.load())
    state = _("on") if info["redirect"] else _("off")
    console.print(_("Redirect: {state} · {hosts} → the frontend pdms runs", state=state,
                    hosts=", ".join(info["hosts"])), highlight=False)
    if not info["browsers"]:
        console.print("  " + _("No Chrome, Edge, Brave, Chromium or Firefox found on this computer."))
    for row in info["browsers"]:
        if row["outdated"]:
            mark, text = "[yellow]~[/]", _("an older version: pdms browser install {key}", key=row["key"])
        elif row["connected"]:
            mark, text = "[green]✓[/]", _("connected")
        elif row["seen"]:
            mark, text = "[yellow]~[/]", _("installed, last seen {when}", when=row["seen"].replace("T", " "))
        else:
            mark, text = "[dim]·[/]", _("not installed: pdms browser install {key}", key=row["key"])
        console.print(f"  {mark} {row['name']}: {text}", highlight=False)
    console.print("[dim]" + _("The extension asks pdms ui every half a minute: it redirects only while pdms ui runs.")
                  + "[/]")


@browser_app.command("install", help=_("Open the browser's extensions page to load the extension (once)."))
def browser_install(
    name: Optional[str] = typer.Argument(None, help=_("chrome, edge, brave, chromium or firefox (asked if omitted)."),
                                         show_default=False),
) -> None:
    if name is None:
        found = [item for item in browser.BROWSERS if item.key in browser.installed()]
        if not found:
            fail(_("No Chrome, Edge, Brave, Chromium or Firefox found on this computer."))
        if len(found) == 1 or not interactive_terminal():
            name = found[0].key
        else:
            name = questionary.select(_("Install it in:"), choices=[
                questionary.Choice(item.name, item.key) for item in found]).unsafe_ask()
    chosen, folder, opened = settle(lambda: actions.install_extension(str(name)))
    if chosen.key == "firefox":
        console.print("[green]✓[/] " + _("Signed extension: {file}", file=escape(str(folder))), highlight=False)
        if opened:
            console.print("  " + _("Firefox asks whether to add pdms front redirect: click Add."))
        else:
            console.print("[yellow]" + _("Could not open Firefox: open {file} in it.", file=escape(str(folder))) + "[/]",
                          highlight=False)
        console.print("  " + _("Then turn the redirect on (pdms browser redirect on, or the Frontend tile of pdms ui)."))
        return
    console.print("[green]✓[/] " + _("Extension written to {folder}", folder=escape(str(folder))), highlight=False)
    if not opened:
        console.print("[yellow]" + _("Could not open {name}: open {page} in it.", name=chosen.name, page=chosen.page)
                      + "[/]", highlight=False)
    for number, step in enumerate(steps(chosen, str(folder)), 1):
        console.print(f"  {number}. {step}", highlight=False, soft_wrap=True)


def steps(chosen: browser.Browser, folder: str) -> list[str]:
    return [
        _("In {page}, turn on Developer mode.", page=chosen.page),
        _("Click Load unpacked and choose the folder {folder}", folder=escape(folder)),
        _("Turn the redirect on (pdms browser redirect on, or the Frontend tile of pdms ui)."),
    ]


@browser_app.command("redirect", help=_("Turn on or off sending the known front to the frontend pdms runs."))
def browser_redirect(
    state: str = typer.Argument(..., help=_("on or off."), show_default=False),
    hosts: Optional[list[str]] = typer.Option(
        None, "--host", help=_("A known front to redirect (repeat it; replaces the saved ones)."),
    ),
) -> None:
    if state not in ("on", "off"):
        fail(_("Say on or off."))
    cfg = Config.load()
    if hosts:
        try:
            actions.set_front_hosts(cfg, hosts)
        except actions.ActionError as exc:
            fail(escape(exc.message))
    actions.set_front_redirect(cfg, state == "on")
    if state == "on":
        console.print("[green]✓[/] " + _("{hosts} open the frontend pdms runs, while it runs (within half a minute).",
                                          hosts=", ".join(cfg.defaults.front_redirect_hosts)), highlight=False)
    else:
        console.print("[green]✓[/] " + _("The known front opens as always (within half a minute)."))
