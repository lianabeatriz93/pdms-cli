"""A service's tests and the Flyway migrations: ``pdms test`` and ``pdms migrate``."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, events, localdb, migrations, repos, runner, testruns
from ..config import Config
from ..i18n import _
from .common import PASSTHROUGH, app, console, fail, interactive_terminal, pick
from .run import ensure_installed
from .services import resolve_service


def run_in_service(service: Path, cmd: list[str], env: dict[str, str]) -> int:
    console.rule(" ".join([Path(cmd[0]).stem, *cmd[1:]]))
    try:
        return subprocess.run(cmd, cwd=service, env=env).returncode
    except KeyboardInterrupt:
        raise typer.Exit(130)


def resolve_project(cfg: Config, name: Optional[str], path: Optional[Path]) -> Path:
    """A service as ``pdms run`` finds it, or a package with tests (``common/core``) by its path or folder name."""
    backend = repos.active_backend(cfg)
    if backend and backend.is_dir() and not path:
        if name:
            found = [backend / p for p in testruns.projects(backend) if name in (p, Path(p).name)]
            if len(found) == 1 and not runner.is_service(found[0]):
                return found[0]
        else:
            here = Path.cwd().resolve()
            for folder in (here, *here.parents):
                if not folder.is_relative_to(backend.resolve()):
                    break
                if (folder / "pyproject.toml").is_file() and (folder / "tests").is_dir() \
                        and not runner.find_service_upwards(here):
                    return folder
    return resolve_service(cfg, name, path)


def test_db(db: Optional[str]) -> str:
    """The test database the tests use: --db (only pdms_test_N), else the first one. Without any, pdms's Postgres is
    started and they are created (asked first in a terminal)."""
    names = testruns.test_dbs()
    if not names:
        if not interactive_terminal() or not questionary.confirm(
            _("Tests only run on pdms's test databases (they drop and create every table). Start pdms's Postgres "
              "(Docker, port {port}) and create {n} of them?", port=localdb.PORT, n=localdb.TEST_COUNT), default=True,
        ).unsafe_ask():
            fail(testruns.no_test_db_hint())
        with console.status(_("Starting pdms's Postgres and creating the test databases...")):
            try:
                names = testruns.prepare_test_dbs()
            except actions.ActionError as exc:
                fail(exc.message)
    if db:
        try:
            testruns.require_test_db(db)
        except actions.ActionError as exc:
            fail(exc.message)
        return db
    return names[0]


def ask_dev_mode(target: Path, given: Optional[bool]) -> bool:
    """--dev-mode / --no-dev-mode, else off; when the service's .env turns it on, ask (some tests may expect it)."""
    if given is not None:
        return given
    if not testruns.env_dev_mode(target):
        return False
    if not interactive_terminal():
        console.print(_("[dim]{name}'s .env turns DEVELOPMENT_MODE on; the tests run with it off "
                        "(--dev-mode to keep it).[/]", name=target.name))
        return False
    return questionary.confirm(
        _("{name}'s .env turns DEVELOPMENT_MODE on: no token check, requests act as the DEV_* user. Run the tests "
          "with it on? (By default off, as deployed.)", name=target.name), default=False,
    ).unsafe_ask()


@app.command(context_settings=PASSTHROUGH, help=_(
    "Run the tests of a service or package (poetry run pytest) against a local database, never a shared one. "
    "Extra arguments go to pytest, e.g. pdms test -- -k name -x. The result shows in pdms ui → Tests."
))
def test(
    ctx: typer.Context,
    service: Optional[str] = typer.Argument(
        None, help=_("Service or package (name or path relative to the backend folder)."),
        autocompletion=completion.services,
    ),
    user: Optional[str] = typer.Option(
        None, "--user", "-u", help=_("Inject this user's DEV_* variables."), autocompletion=completion.users
    ),
    db: Optional[str] = typer.Option(
        None, "--db", "-d", help=_("Test database for DB_PG_CONNECTION_STR (pdms_test_1 by default; only pdms's test "
                                   "databases: the tests drop every table)."),
        autocompletion=completion.test_dbs,
    ),
    install: Optional[bool] = typer.Option(
        None, "--install/--no-install", "-i/-n",
        help=_("Force (-i) or skip (-n) the install; by default only if something changed."),
    ),
    dev_mode: Optional[bool] = typer.Option(
        None, "--dev-mode/--no-dev-mode",
        help=_("DEVELOPMENT_MODE on (no token check, the DEV_* user) or off. Off by default; asked when the "
               "service's .env turns it on."),
    ),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help=_("Service folder (defaults to the current one).")),
) -> None:
    cfg = Config.load()
    target = resolve_project(cfg, service, path)
    db_name = test_db(db)
    database = testruns.require_test_db(db_name)
    dev_user = cfg.users[pick(cfg.users, _("user"), user)] if user else None
    dev_mode = ask_dev_mode(target, dev_mode)
    env = testruns.test_env(cfg, database, dev_user, dev_mode)
    console.print(_("Database: {db} ({url})", db=db_name, url=database.url(mask=True))
                  + (" · " + _("user {user}", user=user) if user else "")
                  + " · " + (_("development mode on") if dev_mode else _("development mode off")))
    ensure_installed(cfg, target, install)
    backend = repos.active_backend(cfg)
    project = target.resolve().relative_to(backend.resolve()).as_posix() \
        if backend and target.resolve().is_relative_to(backend.resolve()) else ""
    if not project:
        raise typer.Exit(run_in_service(target, [runner.poetry(), "run", "pytest", *ctx.args], env))
    paths = testruns.files(backend, project)
    paths["xml"].parent.mkdir(parents=True, exist_ok=True)
    paths["xml"].unlink(missing_ok=True)
    began = time.time()
    code = run_in_service(target, testruns.pytest_command(paths["xml"], ctx.args), env)
    if not any(a.startswith(("--junitxml", "--junit-xml")) for a in ctx.args):
        testruns.record(backend, project, db=db_name, code=code, started=began, seconds=time.time() - began,
                        commit=repos.git_commit(backend.parent), origin="cli", dev_mode=dev_mode)
    raise typer.Exit(code)


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
