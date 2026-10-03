"""Stacks: ``pdms stack ...``, ``pdms up`` and ``pdms down``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import questionary
import typer
from prompt_toolkit.keys import Keys
from rich.table import Table

from .. import actions, completion, prompts
from ..config import Config, Stack
from ..i18n import _
from .common import EVENTS_HELP, app, console, fail, pick, settle, show_menu, stack_app
from .run import (
    Profile,
    confirm_protected,
    ensure_installed,
    install_label,
    print_summary,
    wait_until_ready,
    warn_events,
)
from .services import backend_root, label, list_services, running_by_service, services_root


def print_stacks(cfg: Config) -> None:
    if not cfg.stacks:
        console.print(f"[yellow]{_('No stacks.')}[/] {_('Create one with [bold]pdms stack add[/].')}")
        return
    running = running_by_service()
    root = services_root(cfg)
    ask = f"[dim]{_('ask')}[/]"
    table = Table("Stack", _("Services"), _("User"), "DB")
    for name, stack in cfg.stacks.items():
        lines = []
        for svc in stack.services:
            ports = running.get(str(root / svc), []) if root else []
            lines.append(f"{svc} [green]{' '.join(f':{p}' for p in ports)}[/]" if ports else svc)
        table.add_row(name, "\n".join(lines), stack.user or ask, stack.db or ask)
    console.print(table)


def ask_stack_services(root: Path, candidates: list[Path], current: list[str]) -> list[str]:
    """Every service of the repo in one list: the stack's first (checked), then the running ones, then the rest."""
    running = running_by_service()
    labels = {label(c, root): c for c in candidates}
    busy = sorted(t for t, path in labels.items() if str(path) in running and t not in current)
    rest = sorted(t for t in labels if t not in current and t not in busy)

    def title(text: str) -> str:
        if text not in busy:
            return text
        ports = [p for p in running[str(labels[text])] if p]  # SQS consumers have no port
        return f"{text}  " + (_("(running on :{ports})", ports=", :".join(map(str, ports))) if ports
                              else _("(running)"))

    order = [*current, *busy, *rest]
    question = questionary.checkbox(
        _("Stack services ({count} in the repo):", count=len(labels)),
        choices=[questionary.Choice(title(t), t, checked=t in current) for t in order],
        instruction=_("(type to filter, ↑↓ to move, space to check or uncheck, Enter to save)"),
        use_search_filter=True,
        use_jk_keys=False,
        validate=lambda picked: bool(picked) or _("A stack needs at least one service."),
    )
    # Tab (Ctrl+I) inverts and Ctrl+A checks every service of the repo, not only the filtered ones
    for key in (Keys.ControlI, Keys.ControlA):
        question.application.key_bindings.remove(key)
    picked = set(question.unsafe_ask())
    return [t for t in order if t in picked]


def ask_stack(cfg: Config, current: Optional[Stack] = None) -> Stack:
    prompts.require_tty()
    root, candidates = list_services(cfg)
    services = ask_stack_services(root, candidates, list(current.services) if current else [])
    ask = _("(ask when starting)")
    user = questionary.select(
        _("Stack user:"), choices=[ask, *cfg.users], default=(current.user if current and current.user else ask)
    ).unsafe_ask()
    db = questionary.select(
        _("Stack database:"), choices=[ask, *cfg.dbs], default=(current.db if current and current.db else ask)
    ).unsafe_ask()
    return Stack(services=services, user="" if user == ask else user, db="" if db == ask else db)


@stack_app.callback()
def stack_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        stack_menu()


@stack_app.command("list", help=_("List stacks."))
def stack_list() -> None:
    print_stacks(Config.load())


@stack_app.command("add", help=_("Create a stack (wizard)."))
def stack_add() -> None:
    cfg = Config.load()
    prompts.require_tty()
    name = prompts.ask_name(_("stack"), cfg.stacks)
    settle(lambda: actions.save_stack(cfg, name, ask_stack(cfg)))
    console.print("[green]✓[/] " + _("Stack '{name}' saved. Start it with [bold]pdms up {name}[/].", name=name))


@stack_app.command("edit", help=_("Edit a stack."))
def stack_edit(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    stack = ask_stack(cfg, cfg.stacks[name])
    settle(lambda: actions.save_stack(cfg, name, stack))
    console.print("[green]✓[/] " + _("Stack '{name}' updated.", name=name))


@stack_app.command("remove", help=_("Delete a stack."))
def stack_remove(name: Optional[str] = typer.Argument(None, autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    if questionary.confirm(_("Delete stack '{name}'?", name=name), default=False).unsafe_ask():
        actions.remove_stack(cfg, name)
        console.print("[green]✓[/] " + _("'{name}' deleted.", name=name))


@app.command(help=_("Start all services of a stack in the background, each on a free port."))
def up(
    name: Optional[str] = typer.Argument(None, help=_("Stack to start."), autocompletion=completion.stacks),
    user: Optional[str] = typer.Option(None, "--user", "-u", help=_("User (defaults to the stack's)."), autocompletion=completion.users),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database (defaults to the stack's)."), autocompletion=completion.dbs),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n", help=_("Force (-i) or skip (-n) the install; by default only if something changed.")
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation on protected DBs.")),
    events_mode: Optional[str] = typer.Option(None, "--events", help=EVENTS_HELP, autocompletion=completion.event_modes),
    setup: Optional[str] = typer.Option(
        None, "--setup", help=_("A saved setup of pdms ui's Home: start its stack and make it the current setup."),
        autocompletion=completion.setups,
    ),
) -> None:
    cfg = Config.load()
    if setup:
        if name:
            fail(_("Give a stack or --setup, not both."))
        saved = cfg.setups[pick(cfg.setups, _("setup"), setup)]
        if not saved.stack:
            fail(_("The setup {name} has no stack.", name=setup))
        actions.use_setup(cfg, setup)
        name = saved.stack
    name = pick(cfg.stacks, _("stack"), name)
    stack = cfg.stacks[name]
    root = backend_root(cfg)
    settle(lambda: actions.stack_paths(stack, root))  # a stale stack fails before asking for the user and DB
    user_name = pick(cfg.users, _("user"), user or stack.user or None, cfg.last_user)
    db_name = pick(cfg.dbs, _("database"), db or stack.db or None, cfg.last_db)
    plan = settle(lambda: actions.plan_stack(cfg, name, root, user_name=user_name, db_name=db_name,
                                             events_mode=events_mode))

    for path, ports in plan.running.items():
        console.print(_("[dim]· {name} already running on :{ports}, skipping.[/]",
                        name=path.name, ports=", :".join(map(str, ports))))
    if not plan.services or plan.events is None:
        console.print("[green]✓[/] " + _("The whole stack '{name}' is running.", name=name))
        return
    warn_events(plan.events)

    first = plan.services[0]
    profile = Profile(first.service, user_name, first.user, db_name, first.db, first.host, first.port)
    print_summary(cfg, profile, {
        _("Services"): "\n".join(f"{s.service.name} → " + (f"sqs ← {s.queue}" if s.is_consumer else f":{s.port}")
                                 for s in plan.services),
        _("Events"): plan.events.label,
        _("Install"): install_label(cfg, install, each=True),
    }, show_service=False)
    confirm_protected(cfg, profile, yes)

    started = actions.start_stack(cfg, plan, install=lambda path: ensure_installed(cfg, path, install))
    failed = 0
    for inst in started:
        try:
            wait_until_ready(inst)
        except typer.Exit:
            failed += 1
    if failed:
        fail(_("{failed} of {total} services of stack '{name}' did not start.",
               failed=failed, total=len(started), name=name))


@app.command(help=_("Stop all services of a stack."))
def down(name: Optional[str] = typer.Argument(None, help=_("Stack to stop."), autocompletion=completion.stacks)) -> None:
    cfg = Config.load()
    name = pick(cfg.stacks, _("stack"), name)
    root = backend_root(cfg)
    targets = settle(lambda: actions.stack_instances(cfg, name, root))
    if not targets:
        console.print(_("Nothing from stack '{name}' is running.", name=name))
    for inst in targets:
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))


def stack_menu() -> None:
    prompts.require_tty()
    show_menu(_("Stacks:"), {
        _("Start stack"): lambda: up(None, None, None, None, False, None, None),
        _("Stop stack"): lambda: down(None),
        _("List"): stack_list,
        _("Create"): stack_add,
        _("Edit"): lambda: stack_edit(None),
        _("Delete"): lambda: stack_remove(None),
    })
