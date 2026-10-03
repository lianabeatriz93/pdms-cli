"""A service's tests and the Flyway migrations: ``pdms test`` and ``pdms migrate``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import completion, events, migrations, repos, runner
from ..config import Config
from ..i18n import _
from .common import PASSTHROUGH, app, console, fail, interactive_terminal, pick
from .run import Profile, confirm_protected, ensure_installed
from .services import resolve_service


def run_in_service(service: Path, cmd: list[str], env: dict[str, str]) -> None:
    console.rule(" ".join([Path(cmd[0]).stem, *cmd[1:]]))
    try:
        result = subprocess.run(cmd, cwd=service, env=env)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    raise typer.Exit(result.returncode)


@app.command(context_settings=PASSTHROUGH, help=_(
    "Run the service's tests (poetry run pytest). Extra arguments go to pytest, e.g. pdms test -- -k name -x."
))
def test(
    ctx: typer.Context,
    service: Optional[str] = typer.Argument(
        None, help=_("Service (name or path relative to the backend folder)."), autocompletion=completion.services
    ),
    user: Optional[str] = typer.Option(
        None, "--user", "-u", help=_("Inject this user's DEV_* variables."), autocompletion=completion.users
    ),
    db: Optional[str] = typer.Option(
        None, "--db", "-d", help=_("Inject this database's DB_PG_CONNECTION_STR."), autocompletion=completion.dbs
    ),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n",
        help=_("Force (-i) or skip (-n) the install; by default only if something changed."),
    ),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help=_("Service folder (defaults to the current one).")),
) -> None:
    cfg = Config.load()
    target = resolve_service(cfg, service, path)
    env = runner.poetry_environ()
    if user or db:
        user_name = pick(cfg.users, _("user"), user, cfg.last_user)
        db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
        prof = Profile(target, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], cfg.defaults.host, 0)
        confirm_protected(cfg, prof, False)
        env.update(runner.service_env(cfg.defaults, prof.user, prof.db))
        console.print(_("Profile: {user} @ {db}", user=user_name, db=db_name))
    ensure_installed(cfg, target, install)
    run_in_service(target, [runner.poetry(), "run", "pytest", *ctx.args], env)


def resolve_migrations(cfg: Config, given: Optional[Path]) -> Path:
    """The pdms-db-migrations checkout: --migrations, the current folder, the one saved for the repo, or a sibling."""
    root = repos.active_root(cfg)
    alias = repos.alias_of(cfg, root) if root else None
    repo = cfg.repos.get(alias) if alias else None

    def remember(path: Path) -> Path:
        path = path.resolve()
        if repo and repo.migrations != str(path):
            repo.migrations = str(path)
            cfg.save()
            console.print(_("[dim]Migrations repo saved for '{alias}': {path}[/]", alias=alias, path=path))
        return path

    if given:
        found = migrations.find_upwards(given.expanduser())
        if not found:
            fail(_("{path} is not a Flyway migrations repo (flyway.toml + migrations/).", path=given))
        return remember(found)
    if here := migrations.find_upwards(Path.cwd()):
        return here
    if repo and repo.migrations and migrations.is_migrations_repo(Path(repo.migrations)):
        return Path(repo.migrations)
    candidates = migrations.siblings(root) if root else []
    if len(candidates) == 1:
        return remember(candidates[0])
    if candidates:
        suggested = migrations.best_match(root, candidates)
        if not interactive_terminal():
            if suggested:
                return remember(suggested)
            fail(_("Several migrations repos found ({names}); choose one with --migrations PATH.",
                   names=", ".join(c.name for c in candidates)))
        choice = questionary.select(
            _("Which migrations repo goes with '{alias}'?", alias=alias or root.name),
            choices=[questionary.Choice(str(c), c) for c in candidates], default=suggested,
        ).unsafe_ask()
        return remember(choice)
    fail(_("No Flyway migrations repo found. Clone pdms-db-migrations next to the PDMS repo, or use --migrations PATH."))


@app.command(help=_(
    "Flyway (pdms-db-migrations): info and validate against any database; migrate only against a local one."
))
def migrate(
    command: str = typer.Argument("info", help=_("info (default), validate or migrate."),
                                  autocompletion=completion.flyway_commands),
    db: Optional[str] = typer.Option(None, "--db", "-d", help=_("Database alias."), autocompletion=completion.dbs),
    migrations_path: Optional[Path] = typer.Option(
        None, "--migrations", "-m", help=_("pdms-db-migrations folder (remembered for the repo).")
    ),
) -> None:
    if command not in migrations.COMMANDS:
        fail(_("'{command}' is not allowed from pdms. Available: {codes}.", command=command,
               codes=", ".join(migrations.COMMANDS)))
    cfg = Config.load()
    db_name = pick(cfg.dbs, _("database"), db, cfg.last_db)
    database = cfg.dbs[db_name]
    if command == "migrate" and not migrations.is_local(database):
        fail(_("migrate only runs against a local database (localhost, not protected). '{name}' ({host}) is shared: "
               "it is migrated by the pdms-db-migrations pipeline.", name=db_name, host=database.host))
    repo = resolve_migrations(cfg, migrations_path)
    ok, detail = events.docker_available()
    if not ok:
        fail(_("Docker is not available: {detail}", detail=detail or _("docker not found")))
    summary = Table.grid(padding=(0, 2))
    summary.add_row(f"[bold]{_('Migrations')}[/]", str(repo))
    summary.add_row("[bold]DB[/]", f"{db_name} → {database.url(mask=True)}")
    summary.add_row("[bold]Flyway[/]", f"{command} · {migrations.IMAGE.rsplit('/', 1)[-1]}")
    console.print(summary)
    if not migrations.image_present():
        with console.status(_("Downloading the Flyway image (about 360 MB, only the first time)...")):
            pulled, error = migrations.pull_image()
        if not pulled:
            fail(_("Could not download {image}: {error}. Check the connection (public ECR also limits anonymous "
                   "downloads; try again in a few minutes).", image=migrations.IMAGE, error=error))
    console.rule(f"flyway {command}")
    try:
        result = subprocess.run(migrations.docker_command(repo, database, command),
                                env={**os.environ, "DB_PASSWORD": database.password})
    except KeyboardInterrupt:
        raise typer.Exit(130)
    raise typer.Exit(result.returncode)
