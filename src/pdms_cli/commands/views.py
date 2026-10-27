"""Test copies of the materialized views: ``pdms view ...`` (see testviews.py)."""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.markup import escape
from rich.table import Table

from .. import actions, completion, instances, testviews
from ..config import Config
from ..i18n import _
from .common import app, console, fail, interactive_terminal, settle

view_app = typer.Typer(help=_(
    "Test copies of the materialized views: make one from your checkout's SQL in web-dev, with your ticket as a suffix, "
    "and have the services pdms runs read it instead of the real one."
), invoke_without_command=True)
app.add_typer(view_app, name="view")

DB_HELP = _("Database the test copy is in (web-dev by default).")
SUFFIX_HELP = _("Suffix of the copy (by default from the ticket of the migrations repo's branch, e.g. _pdmp627).")


@view_app.callback()
def view_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        view_list(db=None, suffix="")


@view_app.command("list", help=_("The materialized views you changed, their test copies and which ones services read."))
def view_list(
    db: Optional[str] = typer.Option(None, "--db", "-d", help=DB_HELP, autocompletion=completion.dbs),
    suffix: str = typer.Option("", "--suffix", help=SUFFIX_HELP),
) -> None:
    cfg = Config.load()
    with console.status(_("Reading the materialized views...")):
        info = settle(lambda: actions.test_views(cfg, db or "", suffix))
    console.print(_("{db} · branch {branch} · suffix {suffix}", db=info["db"], branch=info["branch"] or "-",
                    suffix=info["suffix"] or _("(none: give --suffix)")), highlight=False)
    if not info["views"]:
        console.print("  " + _("None of the materialized views is changed in your checkout."))
        return
    table = Table(_("View"), _("Test copy"), _("State"))
    for row in info["views"]:
        state = (_("left over: drop it") if not row["changed"] else _("not made yet") if not row["exists"]
                 else _("your SQL changed since it was made") if not row["current"]
                 else _("services read it") if row["in_use"] else _("ready"))
        table.add_row(row["view"] + (" " + _("(uncommitted)") if row["uncommitted"] else ""), row["name"] or "-", state)
    console.print(table)
    if not info["can_create"]:
        console.print("[dim]" + _("Your user of {db} cannot create there: pdms view create gives you the SQL to run in a "
                                  "database client.", db=info["db"]) + "[/]")


@view_app.command("create", help=_("Make the test copy of a view from your checkout's SQL (or give the SQL to run)."))
def view_create(
    view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view.")),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=DB_HELP, autocompletion=completion.dbs),
    suffix: str = typer.Option("", "--suffix", help=SUFFIX_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
) -> None:
    cfg = Config.load()
    try:
        result = settle(lambda: actions.create_test_view(cfg, view, db or "", suffix, confirmed=yes))
    except actions.ProtectedDatabase as protected:
        if not interactive_terminal() or not questionary.confirm(
                _("'{name}' is a protected DB: create {view}'s test copy there?", name=protected.name, view=view),
                default=False).unsafe_ask():
            raise typer.Exit(1)
        result = settle(lambda: actions.create_test_view(cfg, view, db or "", suffix, confirmed=True))
    if result["created"]:
        console.print("[green]✓[/] " + _("{name} created. Have the services read it: pdms view use {view}",
                                          name=result["name"], view=view), highlight=False)
        return
    console.print("[yellow]" + _("Your user cannot create views there. Run this SQL in your database client (it makes "
                                 "{name}):", name=result["name"]) + "[/]", highlight=False)
    console.print(f"  {escape(result['file'])}", highlight=False)
    console.print("[dim]" + _("Or print it: pdms view sql {view}. Then: pdms view use {view}", view=view) + "[/]",
                  highlight=False)


@view_app.command("sql", help=_("Print the SQL that makes a view's test copy (or drops it, with --drop)."))
def view_sql(
    view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view.")),
    suffix: str = typer.Option("", "--suffix", help=SUFFIX_HELP),
    drop: bool = typer.Option(False, "--drop", help=_("The SQL that drops it instead.")),
) -> None:
    cfg = Config.load()
    repo = settle(lambda: actions.current_migrations_repo(cfg))
    try:
        found = testviews.check_suffix(suffix or testviews.ticket_suffix(testviews.branch(repo)) or "x")
        made, sql = testviews.test_sql(repo, view, found)
    except testviews.TestViewError as exc:
        fail(exc.message)
        return
    print(testviews.drop_sql(made.name) if drop else sql)


@view_app.command("use", help=_("Have the services pdms runs on that database read the test copy (from their next start)."))
def view_use(
    view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view.")),
    name: str = typer.Option("", "--name", help=_("The test copy (by default the view with your suffix).")),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=DB_HELP, autocompletion=completion.dbs),
    suffix: str = typer.Option("", "--suffix", help=SUFFIX_HELP),
) -> None:
    cfg = Config.load()
    if not name:
        repo = settle(lambda: actions.current_migrations_repo(cfg))
        found = suffix or testviews.ticket_suffix(testviews.branch(repo))
        if not found:
            fail(_("The branch of {repo} names no ticket (PDMP-123): give --suffix or --name.", repo=repo.name))
        name = view + testviews.check_suffix(found)
    settle(lambda: actions.use_test_view(cfg, view, name, db or ""))
    restart_hint(cfg)
    console.print("[green]✓[/] " + _("Services on {db} read {name} instead of {view}.", db=testviews.database_of(cfg),
                                      name=name, view=view), highlight=False)


@view_app.command("stop", help=_("The services read the real view again (from their next start); the copy stays."))
def view_stop(view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view."))) -> None:
    cfg = Config.load()
    if not testviews.stop_using(cfg, view):
        fail(_("Services do not read a test copy of {view}.", view=view))
    restart_hint(cfg)
    console.print("[green]✓[/] " + _("Services read the real {view} again.", view=view), highlight=False)


@view_app.command("drop", help=_("Drop the test copy of a view (or give the SQL that drops it); services read the real one again."))
def view_drop(
    view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view.")),
    name: str = typer.Option("", "--name", help=_("The test copy (by default the one services read, or the view with your suffix).")),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=DB_HELP, autocompletion=completion.dbs),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
) -> None:
    cfg = Config.load()
    if not name:
        name = testviews.in_use(cfg).get(view, "")
    if not name:
        repo = settle(lambda: actions.current_migrations_repo(cfg))
        found = testviews.ticket_suffix(testviews.branch(repo))
        if not found:
            fail(_("The branch of {repo} names no ticket (PDMP-123): give --name.", repo=repo.name))
        name = view + found
    try:
        result = settle(lambda: actions.drop_test_view(cfg, view, name, db or "", confirmed=yes))
    except actions.ProtectedDatabase as protected:
        if not interactive_terminal() or not questionary.confirm(
                _("'{name}' is a protected DB: drop {view} there?", name=protected.name, view=name),
                default=False).unsafe_ask():
            raise typer.Exit(1)
        result = settle(lambda: actions.drop_test_view(cfg, view, name, db or "", confirmed=True))
    if result["dropped"]:
        console.print("[green]✓[/] " + _("{name} is gone; services read the real {view}.", name=name, view=view),
                      highlight=False)
        return
    console.print("[yellow]" + _("Only its owner can drop it. Run this in your database client:") + "[/]")
    print(result["sql"])


@view_app.command("refresh", help=_("Recompute the data of a view's test copy (it is a snapshot of when it was made)."))
def view_refresh(
    view: str = typer.Argument(..., help=_("The view, e.g. pdms_report_lead_view.")),
    name: str = typer.Option("", "--name", help=_("The test copy (by default the one services read, or the view with your suffix).")),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=DB_HELP, autocompletion=completion.dbs),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
) -> None:
    cfg = Config.load()
    name = name or testviews.in_use(cfg).get(view, "")
    if not name:
        repo = settle(lambda: actions.current_migrations_repo(cfg))
        found = testviews.ticket_suffix(testviews.branch(repo))
        if not found:
            fail(_("The branch of {repo} names no ticket (PDMP-123): give --name.", repo=repo.name))
        name = view + found
    try:
        with console.status(_("Refreshing {name}...", name=name)):
            result = settle(lambda: actions.refresh_test_view(cfg, view, name, db or "", confirmed=yes))
    except actions.ProtectedDatabase as protected:
        if not interactive_terminal() or not questionary.confirm(
                _("'{name}' is a protected DB: refresh {view} there?", name=protected.name, view=name),
                default=False).unsafe_ask():
            raise typer.Exit(1)
        with console.status(_("Refreshing {name}...", name=name)):
            result = settle(lambda: actions.refresh_test_view(cfg, view, name, db or "", confirmed=True))
    if result["refreshed"]:
        console.print("[green]✓[/] " + _("{name} has today's data.", name=name), highlight=False)
        return
    console.print("[yellow]" + _("Only its owner can refresh it. Run this in your database client:") + "[/]")
    print(result["sql"])


def restart_hint(cfg: Config) -> None:
    db = testviews.database_of(cfg)
    running = [inst.key for inst in instances.load().values() if inst.alive() and inst.db == db]
    if running:
        console.print("[dim]" + _("Running on {db}: {keys}. They read it from their next start: pdms restart {first}",
                                  db=db, keys=", ".join(running), first=running[0]) + "[/]", highlight=False)
