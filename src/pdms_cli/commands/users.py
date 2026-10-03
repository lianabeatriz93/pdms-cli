"""Development users: ``pdms user ...``."""

from __future__ import annotations

import sys
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, prompts, repos, userimport
from ..config import Config
from ..i18n import _
from .common import add_user, console, fail, pick, print_removed, print_users, settle, show_menu, user_app


@user_app.callback()
def user_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        user_menu()


@user_app.command("list", help=_("List users."))
def user_list() -> None:
    print_users(Config.load())


@user_app.command("add", help=_("Add a user (wizard)."))
def user_add() -> None:
    add_user(Config.load())


@user_app.command("edit", help=_("Edit a user."))
def user_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.users)) -> None:
    cfg = Config.load()
    prompts.require_tty()
    name = pick(cfg.users, _("user"), name)
    roles = actions.known_roles(cfg)
    user = prompts.ask_user(cfg.users[name], roles)
    settle(lambda: actions.save_user(cfg, name, user, roles=roles))
    console.print("[green]✓[/] " + _("User '{name}' updated.", name=name))


@user_app.command("rename", help=_("Give a user another name (also in the stacks that use it)."))
def user_rename(
    name: Optional[str] = typer.Argument(None, autocompletion=completion.users),
    new_name: Optional[str] = typer.Argument(None, help=_("The new name.")),
) -> None:
    cfg = Config.load()
    name = pick(cfg.users, _("user"), name)
    if not new_name:
        prompts.require_tty()
        new_name = questionary.text(_("New name for '{name}':", name=name), default=name).unsafe_ask()
    renamed = settle(lambda: actions.rename_user(cfg, name, new_name or name))
    console.print("[green]✓[/] " + _("User '{name}' is now '{new}'.", name=name, new=renamed))


@user_app.command("remove", help=_("Delete a user."))
def user_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.users)) -> None:
    cfg = Config.load()
    name = pick(cfg.users, _("user"), name)
    if questionary.confirm(_("Delete '{name}'?", name=name), default=False).unsafe_ask():
        print_removed(name, actions.remove_user(cfg, name))


@user_app.command("import", help=_("Create user profiles from the pdms_user table of a database."))
def user_import(
    db: Optional[str] = typer.Option(
        None, "--db", "-d", help=_("Database to read the users from."), autocompletion=completion.dbs
    ),
    search: Optional[str] = typer.Option(None, "--search", "-s", help=_("Filter by email or name.")),
    role: Optional[str] = typer.Option(
        None, "--role", "-r", help=_("Filter by role (e.g. TPR.Supervisor)."), autocompletion=completion.roles
    ),
    inactive: bool = typer.Option(False, "--inactive", help=_("Include inactive users.")),
    limit: int = typer.Option(200, "--limit", help=_("Maximum number of users to read.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Import every match without asking.")),
) -> None:
    cfg = Config.load()
    interactive = sys.stdin.isatty()
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    mapping, source = userimport.role_mapping(repos.active_root(cfg))
    console.print(_("[dim]Roles mapped with MAP_INTERNAL_ROLES from {source}.[/]", source=source)
                  if source != "built-in" else
                  _("[yellow]⚠ Could not read the roles of the current repo; using the built-in copy.[/]"))
    if search is None and interactive and not yes:
        search = questionary.text(_("Search by email or name (empty = all):")).unsafe_ask().strip()
    with console.status(_("Reading users from {name}...", name=db_name)):
        found = settle(lambda: actions.read_db_users(
            cfg, db_name, mapping, search=search or "", role=role or "", inactive=inactive, limit=limit,
        ))
    if not found:
        fail(_("No users match."))
    if len(found) == limit:
        console.print(_("[dim]Showing the first {limit}; narrow it down with --search or --role.[/]", limit=limit))

    known = {u.user_id for u in cfg.users.values()}
    if yes:
        picked = found
    else:
        prompts.require_tty()

        def title(u: userimport.DbUser) -> str:
            tags = [_("already imported")] if u.user_id in known else []
            if u.is_active is False:
                tags.append(_("inactive"))
            suffix = f"  ({', '.join(tags)})" if tags else ""
            return f"{u.first_name} {u.last_name} <{u.username}>  {u.dev_roles(mapping) or '-'}{suffix}"

        picked = questionary.checkbox(
            _("Which users do you want to import? (space to select)"),
            choices=[questionary.Choice(title(u), u) for u in found],
        ).unsafe_ask()
    if not picked:
        console.print(_("Nothing selected."))
        return

    added, updated = actions.import_users(cfg, picked, mapping)
    table = Table(_("Name"), "DEV_USERNAME", "DEV_ROLES", "")
    for alias in added + updated:
        u = cfg.users[alias]
        table.add_row(alias, u.username, u.roles, _("new") if alias in added else _("updated"))
    if added or updated:
        console.print(table)
    unchanged = len(picked) - len(added) - len(updated)
    console.print("[green]✓[/] " + _(
        "{added} added, {updated} updated, {unchanged} unchanged.", added=len(added), updated=len(updated),
        unchanged=unchanged,
    ))
    unknown = sorted({r for u in picked for r in u.unknown_roles(mapping)})
    if unknown:
        console.print("[yellow]" + _(
            "⚠ Unknown roles kept as they are (the services will ignore them): {roles}", roles=", ".join(unknown)
        ) + "[/]")


def user_menu() -> None:
    prompts.require_tty()
    show_menu(_("Users:"), {
        _("List"): user_list,
        _("Add"): user_add,
        _("Import from a database"): lambda: user_import(None, None, None, False, 200, False),
        _("Edit"): lambda: user_edit(None),
        _("Delete"): lambda: user_remove(None),
    })
