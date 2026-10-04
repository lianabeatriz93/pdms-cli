"""The guided setup: ``pdms setup``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import questionary
import typer

from .. import actions, awsenv, i18n, migrations, onboarding, prompts, repos
from ..config import Config, config_path
from ..i18n import _
from .aws import ask_profile, read_now
from .common import add_db, add_user, app, check_db, console, settle
from .doctor import doctor_cmd
from .settings import config_defaults, config_import
from .stacks import stack_add
from .users import user_import


def setup_step(title: str) -> None:
    console.print(f"\n[bold]{title}[/]")


def setup_done(detail: str) -> None:
    console.print(f"  [green]✓[/] {detail}", highlight=False)


def setup_later(command: str) -> None:
    console.print("  [dim]" + _("Skipped; do it later with {command}.", command=command) + "[/]", highlight=False)


def setup_import() -> list[str]:
    """Offer to import an exported configuration; return the imported sections."""
    found = onboarding.find_exports()
    if not questionary.confirm(
        _("Do you have a pdms configuration to import (e.g. exported by a teammate)?"), default=bool(found)
    ).unsafe_ask():
        return []
    file: Optional[Path] = None
    if found:
        choice = questionary.select(
            _("File to import:"),
            choices=[*(questionary.Choice(str(p), str(p)) for p in found), questionary.Choice(_("Another file..."), "")],
        ).unsafe_ask()
        file = Path(choice) if choice else None
    try:
        return config_import(file, None, False, False, False)
    except typer.Exit:
        console.print("  [dim]" + _("Nothing imported; the next steps set it up by hand.") + "[/]")
        return []


def setup_language() -> None:
    cfg = Config.load()
    actions.set_language(cfg, prompts.ask_language(cfg.defaults.language))
    i18n.set_language(cfg.defaults.language)


def setup_repo() -> None:
    setup_step(_("PDMS repo"))
    cfg = Config.load()
    if "repo" not in onboarding.pending(cfg):
        setup_done(f"{cfg.current_repo} · {cfg.repo.root}")
        return
    here = repos.find_repo_root(Path.cwd())
    if here and questionary.confirm(_("Use {path} as the PDMS repo?", path=here), default=True).unsafe_ask():
        root = here
    else:
        def validate(value: str) -> bool | str:
            return not value.strip() or repos.find_repo_root(Path(value.strip()).expanduser()) is not None \
                or _("Not a PDMS repo (no backend/snakesdk folder).")

        answer = questionary.path(
            _("Folder of your PDMS checkout (empty = skip):"), only_directories=True, validate=validate
        ).unsafe_ask().strip()
        if not answer:
            setup_later("pdms repo add <path>")
            return
        root = repos.find_repo_root(Path(answer).expanduser())
    alias = repos.register(cfg, root)
    cfg.current_repo = alias
    cfg.save()
    repos.use_for_this_command(None)
    setup_done(_("Repo '{alias}' registered ({path}).", alias=alias, path=root))


def setup_migrations() -> None:
    cfg = Config.load()
    repo = cfg.repo
    if repo is None or not repo.root.is_dir():
        return
    setup_step(_("Migrations repo (Flyway)"))
    if "migrations" not in onboarding.pending(cfg):
        setup_done(repo.migrations)
        return
    candidates = migrations.siblings(repo.root)
    if len(candidates) == 1:
        path: Optional[Path] = candidates[0]
    elif candidates:
        path = questionary.select(
            _("Which migrations repo goes with '{alias}'?", alias=cfg.current_repo),
            choices=[questionary.Choice(str(c), c) for c in candidates],
            default=migrations.best_match(repo.root, candidates),
        ).unsafe_ask()
    else:
        def validate(value: str) -> bool | str:
            return not value.strip() or migrations.find_upwards(Path(value.strip()).expanduser()) is not None \
                or _("Not a Flyway migrations repo (flyway.toml + migrations/).")

        answer = questionary.path(
            _("Folder of your pdms-db-migrations checkout (empty = skip):"), only_directories=True, validate=validate
        ).unsafe_ask().strip()
        path = migrations.find_upwards(Path(answer).expanduser()) if answer else None
    if path is None:
        setup_later("pdms migrate --migrations <path>")
        return
    repo.migrations = str(path.resolve())
    cfg.save()
    setup_done(repo.migrations)


def setup_dbs() -> None:
    setup_step(_("Databases"))
    cfg = Config.load()
    tested: set[str] = set()
    if cfg.dbs:
        setup_done(", ".join(cfg.dbs))
    else:
        console.print("  " + _("Services need at least one database to run."))
    while questionary.confirm(
        _("Add another database?") if cfg.dbs else _("Add a database now?"), default=not cfg.dbs
    ).unsafe_ask():
        tested.add(add_db(cfg))  # add_db offers its own connection test
    if not cfg.dbs:
        setup_later("pdms db add")
        return

    missing = [name for name, db in cfg.dbs.items() if not db.password]
    for name in missing:
        db = cfg.dbs[name]
        password = questionary.password(
            _("Password of '{name}' ({user}@{host}) (empty = later):", name=name, user=db.user, host=db.host)
        ).unsafe_ask()
        if password:
            db.password = password
            cfg.save()
    if still := [name for name in missing if not cfg.dbs[name].password]:
        setup_later("pdms db edit " + still[0])

    untested = [name for name in cfg.dbs if name not in tested and cfg.dbs[name].password]
    if untested and questionary.confirm(
        _("Test the connection to {names}?", names=", ".join(untested)), default=True
    ).unsafe_ask():
        for name in untested:
            check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)


def setup_users() -> None:
    setup_step(_("Development users"))
    cfg = Config.load()
    if cfg.users:
        setup_done(", ".join(cfg.users))
        return
    choices = [
        questionary.Choice(_("Add one by hand"), "manual"),
        questionary.Choice(_("Later"), "later"),
    ]
    if any(db.password for db in cfg.dbs.values()):
        choices.insert(0, questionary.Choice(_("Import them from the pdms_user table of a database"), "import"))
    choice = questionary.select(_("Services run as a DEV_* user. How do you want to add them?"),
                                choices=choices).unsafe_ask()
    if choice == "import":
        try:
            user_import(None, None, None, False, 200, False)
        except typer.Exit:
            pass
        if not Config.load().users:
            setup_later("pdms user add / pdms user import")
    elif choice == "manual":
        add_user(cfg)
        while questionary.confirm(_("Add another user?"), default=False).unsafe_ask():
            add_user(cfg)
    else:
        setup_later("pdms user add / pdms user import")


def setup_aws() -> None:
    setup_step(_("AWS"))
    cfg = Config.load()
    if cfg.defaults.aws_profile:
        setup_done(_("profile {profile}", profile=cfg.defaults.aws_profile))
        return
    if not awsenv.profiles():
        console.print("  " + _("No AWS profiles on this computer: services use AWS as the terminal has it."))
        setup_later("pdms aws profile")
        return
    console.print("  " + _("Services that use S3 need a profile, and the buckets pdms reads from its Lambdas."))
    name = ask_profile("")
    if not name:
        setup_later("pdms aws profile")
        return
    settle(lambda: actions.set_aws_profile(cfg, name))
    setup_done(_("profile {profile}", profile=name))
    try:
        read_now(cfg)
    except typer.Exit:
        setup_later("pdms aws read")


def setup_stacks() -> None:
    cfg = Config.load()
    if cfg.stacks:
        setup_step(_("Stacks"))
        setup_done(", ".join(cfg.stacks))
        return
    if cfg.repo is None or not cfg.repo.root.is_dir():
        return
    setup_step(_("Stacks"))
    if questionary.confirm(_("Create a stack (services you usually start together) now?"), default=False).unsafe_ask():
        try:
            stack_add(None, False)
        except typer.Exit:
            pass
    else:
        setup_later("pdms stack add")


@app.command("setup", help=_("Guided setup: import a shared configuration, then configure whatever is still missing."))
def setup_cmd() -> None:
    prompts.require_tty()
    first_time = not config_path().exists()
    console.print(_("[bold]pdms setup[/]: first a configuration to import, if you have one, then whatever is missing."))
    imported = setup_import()
    if first_time and "defaults" not in imported:
        setup_language()
    setup_repo()
    setup_migrations()
    setup_dbs()
    setup_users()
    setup_aws()
    setup_stacks()
    if first_time and "defaults" not in imported:
        setup_step(_("Defaults"))
        if questionary.confirm(
            _("Review the other defaults (port, log level, install, events...)?"), default=False
        ).unsafe_ask():
            config_defaults()
        else:
            setup_done(_("Using the standard values (change them with pdms config)."))
    if not config_path().exists():
        Config.load().save()
    console.print()
    if questionary.confirm(_("Check the environment now (pdms doctor)?"), default=True).unsafe_ask():
        try:
            doctor_cmd(False, 5)
        except typer.Exit:
            pass
    left = onboarding.pending(Config.load())
    console.print()
    console.print("[green]✓[/] " + _("Setup finished. Run pdms to open the menu; pdms setup again completes what is left."))
    if left:
        console.print("  [dim]" + _("Still missing: {steps}", steps=", ".join(setup_label(s) for s in left)) + "[/]")


def setup_label(step: str) -> str:
    return {
        "repo": _("PDMS repo"), "migrations": _("migrations repo"), "dbs": _("databases"),
        "passwords": _("database passwords"), "users": _("users"),
    }[step]
