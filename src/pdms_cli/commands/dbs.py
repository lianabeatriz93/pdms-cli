"""Databases: ``pdms db ...``."""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, localcopy, localdb, prompts, testruns
from ..config import Config
from ..i18n import _
from .common import add_db, check_db, console, db_app, pick, print_dbs, print_removed, settle, show_menu, with_images


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
    def start() -> list[str]:
        with console.status(_("Starting pdms's Postgres and creating the test databases...")):
            return _settle_local(lambda: testruns.prepare_test_dbs(tests))

    names = with_images(start)
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
        names = with_images(lambda: _settle_local(lambda: testruns.prepare_test_dbs(count)))
        for extra in names[count:]:
            _settle_local(lambda name=extra: localdb.remove_test_database(name))
        names = names[:count]
        console.print("[green]✓[/] " + _("Test databases: {names}", names=", ".join(names) or "-"))


def _migrations_repo(cfg: Config):
    """The pdms-db-migrations checkout to migrate the copy with, or None (it is then copied as it is)."""
    from .testing import resolve_migrations

    try:
        return resolve_migrations(cfg, None)
    except (typer.Exit, SystemExit):
        return None


@local_app.command("refresh", help=_(
    "Copy a database into pdms's Postgres (the local copy, alias pdms-local), replacing it, and apply the repo's "
    "migrations. By default pdm_template_dev on the server of web-dev, which is only read."
))
def local_refresh(
    source_alias: str = typer.Option(localcopy.SOURCE_ALIAS, "--from", help=_("Alias of the server to copy from."),
                                     autocompletion=completion.dbs),
    database: str = typer.Option(localcopy.SOURCE_DATABASE, "--database", help=_("Database to copy.")),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help=_("Apply the repo's Flyway migrations after.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask, even when services use the copy.")),
) -> None:
    import sys

    cfg = Config.load()
    using = localcopy.using_copy(cfg) if localdb.state()["running"] else []
    if using and not yes and not questionary.confirm(
        _("{n} running services use the local copy ({keys}): their connections end while it is replaced. Go on?",
          n=len(using), keys=", ".join(using)), default=False).unsafe_ask():
        raise typer.Exit(1)
    repo = _migrations_repo(cfg) if migrate else None
    if migrate and not repo:
        console.print(_("[dim]No pdms-db-migrations repo found: the copy stays as the source has it.[/]"))
    result = with_images(lambda: _settle_local(lambda: localcopy.refresh(
        cfg, sys.stdout, alias=source_alias, database=database, migrations_repo=repo)))
    console.print("[green]✓[/] " + _("Local copy ready in {seconds} s: alias {alias}, {size:.1f} MB.",
                                      seconds=result["seconds"], alias=result["alias"],
                                      size=result["size"] / 1048576))


snapshot_app = typer.Typer(help=_("Snapshots of the local copy: keep it as it is and come back to it in seconds."),
                           no_args_is_help=True)
local_app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("save", help=_("Keep the local copy as it is now."))
def snapshot_save(name: str = typer.Argument(..., help=_("Name (lowercase letters, digits, _)."))) -> None:
    snap = _settle_local(lambda: localcopy.save_snapshot(name))
    console.print("[green]✓[/] " + _("Snapshot '{name}' saved ({size:.1f} MB).", name=name,
                                      size=snap["size"] / 1048576))


@snapshot_app.command("list", help=_("The snapshots of the local copy."))
def snapshot_list() -> None:
    snaps = _settle_local(localcopy.snapshots)
    if not snaps:
        console.print(_("No snapshots yet: pdms db local snapshot save NAME."))
        return
    table = Table(_("Snapshot"), _("Size"), _("Taken"))
    for snap in snaps:
        table.add_row(snap["name"], f"{snap['size'] / 1048576:.1f} MB", snap["at"].replace("T", " ")[:16] or "-")
    console.print(table)


@snapshot_app.command("restore", help=_("Make the local copy what a snapshot keeps (the snapshot stays)."))
def snapshot_restore(name: str = typer.Argument(...)) -> None:
    _settle_local(lambda: localcopy.restore_snapshot(name))
    console.print("[green]✓[/] " + _("Local copy restored from '{name}'.", name=name))


@snapshot_app.command("delete", help=_("Delete a snapshot."))
def snapshot_delete(name: str = typer.Argument(...)) -> None:
    _settle_local(lambda: localcopy.delete_snapshot(name))
    console.print("[green]✓[/] " + _("Snapshot '{name}' deleted.", name=name))
