"""Databases: ``pdms db ...``."""

from __future__ import annotations

from typing import Optional

import questionary
import typer

from .. import actions, completion, prompts
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
