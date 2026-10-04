"""Databases: ``pdms db ...``."""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, localdb, prompts, testruns
from ..config import Config
from ..i18n import _
from .common import add_db, check_db, console, db_app, pick, print_dbs, print_removed, settle, show_menu


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
    database = prompts.ask_database(cfg.dbs[name])
    settle(lambda: actions.save_db(cfg, name, database))
    console.print("[green]✓[/] " + _("Database '{name}' updated.", name=name))


@db_app.command("remove", help=_("Delete a database."))
def db_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.dbs)) -> None:
    cfg = Config.load()
    name = pick(cfg.dbs, _("database"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        print_removed(name, actions.remove_db(cfg, name))


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


def db_menu() -> None:
    prompts.require_tty()
    show_menu(_("Databases:"), {
        _("List"): db_list,
        _("Add"): db_add,
        _("Edit"): lambda: db_edit(None),
        _("Test connection"): lambda: db_test(None, None),
        _("Delete"): lambda: db_remove(None),
    })


# ---------------------------------------------------------------------------------------------- pdms's own Postgres

local_app = typer.Typer(help=_(
    "pdms's own Postgres (Docker, port 5440, data in a volume): the test databases, where tests run."
), no_args_is_help=True)
db_app.add_typer(local_app, name="local")


def _settle_local(work):
    try:
        return work()
    except actions.ActionError as exc:
        console.print(f"[red]✗[/] {exc.message}")
        raise typer.Exit(1)


@local_app.command("up", help=_("Start it (created the first time) and create the test databases."))
def local_up(
    tests: int = typer.Option(localdb.TEST_COUNT, "--tests", min=0, max=20, help=_("Test databases to have.")),
) -> None:
    with console.status(_("Starting pdms's Postgres and creating the test databases...")):
        names = _settle_local(lambda: testruns.prepare_test_dbs(tests))
    state = localdb.state()
    console.print("[green]✓[/] " + _("{name} runs on localhost:{port} (user {user}, password {password}).",
                                      name=localdb.CONTAINER, port=state["port"], user=localdb.USER,
                                      password=localdb.PASSWORD))
    console.print(_("Test databases: {names}", names=", ".join(names) or "-"))


@local_app.command("down", help=_("Stop it (its data stays in the volume)."))
def local_down() -> None:
    stopped = _settle_local(localdb.down)
    console.print("[green]✓[/] " + (_("{name} stopped.", name=localdb.CONTAINER) if stopped
                                    else _("{name} was not running.", name=localdb.CONTAINER)))


@local_app.command("status", help=_("Whether it runs, and its databases with their size."))
def local_status() -> None:
    state = localdb.state()
    if not state["exists"]:
        console.print(_("{name} does not exist yet: pdms db local up creates it.", name=localdb.CONTAINER))
        return
    if not state["running"]:
        console.print(_("{name} is stopped: pdms db local up starts it.", name=localdb.CONTAINER))
        return
    table = Table(_("Database"), _("Size"), _("Use"), title=f"{localdb.CONTAINER} · localhost:{state['port']}",
                  title_justify="left")
    for name, size in _settle_local(localdb.databases).items():
        use = _("tests (emptied by each run)") if localdb.is_test_database(name) else ""
        table.add_row(name, f"{size / 1048576:.1f} MB", use)
    console.print(table)


@local_app.command("tests", help=_("Have N test databases (creates the missing ones), or recreate one empty."))
def local_tests(
    count: Optional[int] = typer.Argument(None, min=0, max=20, help=_("How many to have.")),
    recreate: Optional[str] = typer.Option(None, "--recreate", help=_("Test database to recreate empty."),
                                           autocompletion=completion.test_dbs),
) -> None:
    if recreate:
        _settle_local(lambda: localdb.recreate_test_database(recreate))
        console.print("[green]✓[/] " + _("{name} recreated, empty.", name=recreate))
    if count is not None:
        names = _settle_local(lambda: testruns.prepare_test_dbs(count))
        for extra in names[count:]:
            _settle_local(lambda name=extra: localdb.remove_test_database(name))
        names = names[:count]
        console.print("[green]✓[/] " + _("Test databases: {names}", names=", ".join(names) or "-"))
