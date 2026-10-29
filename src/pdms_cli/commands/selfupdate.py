"""``pdms self-update`` and the daily check for a new release."""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
from typing import Optional

import typer

from .. import __version__, proxy, update
from ..config import Config
from ..i18n import _
from .common import app, console, fail, interactive_terminal


@app.command("self-update", help=_("Update pdms to the latest release (or to --version, also an earlier one)."))
def self_update(
    version: Optional[str] = typer.Option(
        None, "--version", help=_("Install this version instead of the latest: an earlier one goes back to it."),
    ),
    check: bool = typer.Option(False, "--check", help=_("Only tell whether there is a newer version.")),
    pre: bool = typer.Option(
        False, "--pre", help=_("Include beta pre-releases (automatic on the beta channel, or if you run one)."),
    ),
    list_versions: bool = typer.Option(False, "--list", help=_("List the latest releases, to pick one for --version.")),
) -> None:
    kind = update.install_kind()
    if kind == "editable":
        console.print(_("pdms {version} runs from a local checkout (editable install): update it with git pull.",
                        version=__version__))
        return
    pre = pre or update.wants_pre(Config.load().defaults.update_channel)
    if list_versions:
        show_versions(pre)
        return
    if version is not None:
        version = version.strip().lstrip("v")
        if not update.VERSION.match(version):
            fail(_("'{version}' is not a pdms version (like 0.4.11): pdms self-update --list shows them.",
                   version=version))
        if version == __version__:
            console.print("[green]✓[/] " + _("pdms {version} is the version you have.", version=version))
            return
    try:
        with console.status(_("Looking for the latest release...")):
            target = version or update.latest_version(pre=pre)
    except Exception as exc:  # noqa: BLE001 - network errors of any kind
        fail(_("Could not reach GitHub: {error}", error=exc))
    if not version and not update.is_newer(target):
        console.print("[green]✓[/] " + _("pdms {version} is the latest version.", version=__version__))
        return
    going_back = update.is_newer(__version__, target)
    if going_back:
        console.print(_("Current version: {current} · going back to: {target}", current=__version__, target=target))
    else:
        console.print(_("Current version: {current} · available: {target}", current=__version__, target=target))
    if check:
        return
    cmd = update.upgrade_command(target)
    if not update.updates_itself():
        console.print(_("Run this to update:"))
        console.print(f"  {subprocess.list2cmdline(cmd) if sys.platform == 'win32' else shlex.join(cmd)}",
                      highlight=False, markup=False)
        return
    from ..ui import instance as ui_instance

    ui_running = ui_instance.running()
    if going_back:
        console.print(_("[dim]pdms will not offer {current} again; a newer version, yes.[/]", current=__version__))
    if sys.platform == "win32":
        # The running pdms.exe cannot replace itself: a PowerShell window updates it once this pdms exits. pdms ui
        # and the proxy run from the same files, so they must not be running.
        if ui_running:
            fail(_("pdms ui is open: update from it (the ⬆ in its title bar) or close it first."))
        if proxy.running_proxy():
            fail(_("The proxy runs from pdms's own files, which the update replaces: stop it first (pdms stop proxy)."))
        update.remember_choice(target)
        update.spawn_windows_update(cmd)
        console.print(_("Updating to {version} in a new window, once this pdms exits.", version=target))
        return
    update.remember_choice(target)
    result = subprocess.run(cmd)
    if result.returncode:
        fail(_("The update failed (exit code {code}).", code=result.returncode))
    console.print("[green]✓[/] " + _("pdms updated to {version}.", version=target))
    if proxy.running_proxy():
        console.print(_("[dim]The proxy still runs the previous version until it restarts.[/]"))
    if ui_running:
        console.print(_("[dim]pdms ui still runs the previous version: it offers to restart itself.[/]"))


def show_versions(pre: bool) -> None:
    try:
        with console.status(_("Looking for the latest release...")):
            found = update.releases()
    except Exception as exc:  # noqa: BLE001 - network errors of any kind
        fail(_("Could not reach GitHub: {error}", error=exc))
    skipped = update.skipped()
    for release in found[:15]:
        if release["prerelease"] and not pre and release["version"] != __version__:
            continue
        marks = []
        if release["version"] == __version__:
            marks.append("[green]" + _("you have it") + "[/]")
        if release["version"] == skipped:
            marks.append("[yellow]" + _("skipped") + "[/]")
        if release["prerelease"]:
            marks.append("[cyan]beta[/]")
        console.print(f"  {release['version']:<12} [dim]{release['date']}[/]  " + " · ".join(marks), highlight=False)
    console.print(_("[dim]pdms self-update --version <version> installs one, also an earlier one.[/]"))


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
    pre = update.wants_pre(cfg.defaults.update_channel)
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
