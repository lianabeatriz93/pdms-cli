"""PDMS repos (checkouts): ``pdms repo ...`` and the check of the current one."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, instances, prompts, repos
from ..config import Config
from ..i18n import _
from .common import console, fail, interactive_terminal, pick, repo_app, settle, show_menu
from .run import do_run


def switch_repo(cfg: Config, alias: str) -> None:
    switch = settle(lambda: actions.use_repo(cfg, alias))
    console.print("[green]✓[/] " + _("Current repo: {alias} ({path})", alias=alias, path=cfg.repos[alias].root))
    handle_instances_of(switch, alias)
    if switch.proxy:
        console.print(_("[dim]The proxy still routes to '{old}': restart it (pdms proxy) to use '{new}'.[/]",
                        old=switch.old, new=alias))


def handle_instances_of(switch: actions.RepoSwitch, new: str) -> None:
    """Offer to keep, stop or move to the new repo the instances still running from the old one."""
    if not switch.running or not interactive_terminal():
        return
    choice = questionary.select(
        _("{count} instances are running from '{old}': {keys}. What should I do with them?",
          count=len(switch.running), old=switch.old, keys=", ".join(i.key for i, _target in switch.running)),
        choices=[
            questionary.Choice(_("Keep them running (they coexist, each on its port)"), "keep"),
            questionary.Choice(_("Stop them"), "stop"),
            questionary.Choice(_("Restart them from '{new}' (same user, DB and port)", new=new), "move"),
        ],
    ).unsafe_ask()
    if choice == "keep":
        return
    for inst, target in switch.running:
        if choice == "move" and target is None:
            console.print("[yellow]" + _("⚠ {key}: the service does not exist in '{new}'; left running.",
                                         key=inst.key, new=new) + "[/]")
            continue
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
        if choice == "move":
            try:
                do_run(user=inst.user, db=inst.db, port=inst.port, host=inst.host, reload=inst.reload,
                       yes=True, path=target, background=True)
            except typer.Exit:
                pass


def check_repo(cfg: Config) -> None:
    """If the current folder is a PDMS repo other than the current one, offer to switch to it."""
    here = repos.find_repo_root(Path.cwd())
    if here is None:
        return
    alias = repos.alias_of(cfg, here)
    if not cfg.repos or not cfg.repo:
        alias = repos.register(cfg, here)
        cfg.current_repo = alias
        cfg.save()
        console.print(_("[dim]Using {path} as the current repo '{alias}'.[/]", path=here, alias=alias))
        return
    if alias == cfg.current_repo or str(here) in cfg.ignored_repos:
        return
    if not interactive_terminal():
        repos.use_for_this_command(here)
        return
    choice = questionary.select(
        _("You are in {here}, but the current repo is '{current}' ({path}). What should I do?",
          here=here, current=cfg.current_repo, path=cfg.repo.root),
        choices=[
            questionary.Choice(_("Switch to {name} (it becomes the default)", name=alias or here.name), "switch"),
            questionary.Choice(_("Use it only for this command"), "once"),
            questionary.Choice(_("Don't ask again in this repo"), "ignore"),
        ],
    ).unsafe_ask()
    if choice == "switch":
        switch_repo(cfg, repos.register(cfg, here))
    elif choice == "once":
        repos.use_for_this_command(here)
    else:
        cfg.ignored_repos.append(str(here))
        cfg.save()


@repo_app.callback()
def repo_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        repo_menu()


@repo_app.command("list", help=_("List the registered repos."))
def repo_list() -> None:
    cfg = Config.load()
    if not cfg.repos:
        console.print(f"[yellow]{_('No repos registered.')}[/] {_('Use [bold]pdms repo add <path>[/].')}")
        return
    counts: dict[str, int] = {}
    for inst in instances.load().values():
        if inst.alive() and (alias := repos.repo_of(cfg, inst.service)):
            counts[alias] = counts.get(alias, 0) + 1
    table = Table("", _("Name"), _("Path"), _("Migrations"), _("Running"))
    for alias, repo in cfg.repos.items():
        path = str(repo.root) if repo.root.is_dir() else f"[red]{repo.root} ({_('missing')})[/]"
        table.add_row("●" if alias == cfg.current_repo else "", alias, path,
                      Path(repo.migrations).name if repo.migrations else "[dim]-[/]", str(counts.get(alias, "")))
    console.print(table)


@repo_app.command("add", help=_("Register a PDMS repo (defaults to the current folder)."))
def repo_add(
    path: Optional[Path] = typer.Argument(None, help=_("Folder inside the repo.")),
    alias: Optional[str] = typer.Option(None, "--alias", "-a", help=_("Name for the repo.")),
) -> None:
    cfg = Config.load()
    where = (path or Path.cwd()).expanduser()
    try:
        root = actions.repo_root(where)
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    if existing := repos.alias_of(cfg, root):
        console.print(_("{path} is already registered as '{alias}'.", path=root, alias=existing))
        return
    if alias is None and interactive_terminal():
        alias = prompts.ask_name(_("repo"), cfg.repos, repos.suggest_alias(cfg, root))
    was_empty = not cfg.repos
    try:
        alias = actions.add_repo(cfg, root, alias or "")
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    console.print("[green]✓[/] " + _("Repo '{alias}' registered ({path}).", alias=alias, path=root))
    if not was_empty and interactive_terminal() and questionary.confirm(
        _("Make it the current repo?"), default=True
    ).unsafe_ask():
        switch_repo(cfg, alias)


@repo_app.command("use", help=_("Choose the current repo."))
def repo_use(alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos)) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias, cfg.current_repo)
    if alias == cfg.current_repo:
        console.print(_("'{alias}' is already the current repo.", alias=alias))
        return
    switch_repo(cfg, alias)


@repo_app.command("edit", help=_("Change the name, the migrations repo or the proxy's remote API of a repo."))
def repo_edit(
    alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos),
    name: Optional[str] = typer.Option(None, "--alias", "-a", help=_("New name for the repo.")),
    migrations_path: Optional[str] = typer.Option(
        None, "--migrations", help=_("Flyway migrations checkout (pdms-db-migrations); '' to forget it.")),
    remote: Optional[str] = typer.Option(None, "--remote", help=_("Remote API for the proxy; '' to forget it.")),
) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias, cfg.current_repo)
    repo = cfg.repos[alias]
    if name is None and migrations_path is None and remote is None:
        prompts.require_tty()

        def ask(message: str, default: str, check: Callable[[str], object]) -> str:
            def validate(value: str) -> bool | str:
                try:
                    check(value)
                except actions.InvalidValue as invalid:
                    return invalid.reason
                return True

            return questionary.text(message, default=default, validate=validate).unsafe_ask().strip()

        others = [other for other in cfg.repos if other != alias]
        name = ask(_("Alias ({kind}):", kind=_("repo")), alias, lambda value: actions.check_alias(value, others))
        migrations_path = ask(_("Migrations repo (empty = look next to the repo):"), repo.migrations,
                              actions.migrations_repo)
        remote = ask(_("Remote API for the proxy (empty = from frontend/.env):"), repo.remote, actions.remote_api)
    try:
        alias = actions.edit_repo(cfg, alias, new_alias=name, migrations_path=migrations_path, remote=remote)
    except actions.InvalidValue as invalid:
        fail(invalid.reason)
    except actions.ActionError as error:
        fail(error.message)
    console.print("[green]✓[/] " + _("'{name}' saved.", name=alias))


@repo_app.command("remove", help=_("Forget a registered repo (nothing is deleted from disk)."))
def repo_remove(alias: Optional[str] = typer.Argument(None, autocompletion=completion.repos)) -> None:
    cfg = Config.load()
    alias = pick(cfg.repos, _("repo"), alias)
    if interactive_terminal() and not questionary.confirm(_("Delete '{name}'?", name=alias), default=False).unsafe_ask():
        return
    settle(lambda: actions.remove_repo(cfg, alias))
    console.print("[green]✓[/] " + _("'{name}' deleted.", name=alias))


def repo_menu() -> None:
    if not interactive_terminal():
        return repo_list()
    show_menu(_("Repos:"), {
        _("List"): repo_list,
        _("Add"): lambda: repo_add(None, None),
        _("Choose the current one"): lambda: repo_use(None),
        _("Edit"): lambda: repo_edit(None, None, None, None),
        _("Delete"): lambda: repo_remove(None),
    })
