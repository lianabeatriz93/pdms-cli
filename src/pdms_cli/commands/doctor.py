"""``pdms doctor``."""

from __future__ import annotations

import typer

from .. import doctor as diagnostics
from ..config import Config
from ..i18n import _
from .common import app, console

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
