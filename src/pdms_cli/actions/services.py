"""Planning, installing, starting, restarting and stopping one service."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING

import psutil

from .. import awsenv, emails, events, installer, instances, migrations, pythons, repos, runner, testviews, trace
from ..config import Config, Database, DevUser
from ..i18n import _
from . import checks, local_events, repo_settings
from .errors import ActionError, MigrationsPending, PortBusy, PythonMismatch

if TYPE_CHECKING:
    from .local_events import EventsSetup

__all__ = [
    "MAX_IDLE_MINUTES", "MAX_WARM_CONNECTIONS", "MIGRATE_TIMEOUT", "SUPPORT_SCHEMAS", "ServiceLaunch", "Unmigrated",
    "apply_migrations", "behind_of", "check_migrations", "check_pythons", "claim_port", "commit_of",
    "ensure_support_schemas", "exec_service", "install_log", "install_service", "install_services",
    "install_wanted", "install_workers", "installed_parts", "needs_install", "plan_service", "quick_install",
    "service_env", "set_on_demand", "sleep_service", "start_service", "stop_service", "unmigrated", "wake_service",
]


MAX_IDLE_MINUTES = 24 * 60
MAX_WARM_CONNECTIONS = 5  # the pool PDMS services keep (DB_PG_CONNECTION_POOL_SIZE); more would be closed again


@dataclass
class ServiceLaunch:
    """Everything decided to start one service; :func:`start_service` or :func:`exec_service` carry it out."""

    service: Path
    user_name: str
    user: DevUser
    db_name: str
    db: Database
    host: str
    port: int  # 0 for SQS consumers, which read a queue instead of listening
    reload: bool
    events: EventsSetup
    cmd: list[str]
    queue: str = ""
    parallel: bool = False  # each request in its own thread (see sqs_patch/pdms_parallel.py)
    on_demand: bool = False  # the proxy starts it with its first request and stops it when idle

    @property
    def is_consumer(self) -> bool:
        return bool(self.queue)


def plan_service(
    cfg: Config,
    service: Path,
    *,
    user_name: str,
    db_name: str,
    port: int | None = None,
    host: str | None = None,
    reload: bool | None = None,
    events_mode: str | None = None,
    events_ready: EventsSetup | None = None,
    parallel: bool | None = None,
    on_demand: bool | None = None,
) -> ServiceLaunch:
    """Decide how ``service`` runs. Raises :class:`PortBusy` or :class:`LocalEventsDown` for the user to answer.

    ``events_ready`` skips deciding where events go (a stack decides it once for all its services).
    """
    checks.require(cfg.users, _("user"), user_name)
    checks.require(cfg.dbs, _("database"), db_name)
    host = host or cfg.defaults.host
    reload = cfg.defaults.reload if reload is None else reload
    consumer = local_events.consumer_of(cfg, service)
    if consumer:
        port = 0
        events_mode = "local"  # a consumer reads its queue from the local ElasticMQ
    else:
        port = checks.free_port(host, port or cfg.defaults.port)
    setup = events_ready or local_events.events_setup(cfg, service, events_mode)
    if consumer:
        queue, handler = consumer
        cmd = events.poller_command(runner.poetry(), queue, handler, cfg.defaults.events_port)
    else:
        cmd = runner.uvicorn_command(host, port, reload, runner.reload_dirs(service) if reload else None)
    return ServiceLaunch(
        service, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], host, port, reload, setup, cmd,
        queue=consumer[0].name if consumer else "",
        parallel=not consumer and (cfg.defaults.parallel_requests if parallel is None else parallel),
        # An SQS consumer reads its queue all the time: no request would ever wake it.
        on_demand=not consumer and (cfg.defaults.on_demand if on_demand is None else on_demand),
    )


def install_wanted(cfg: Config, install: bool | None) -> bool:
    """Whether to install at all: True forces it, False skips it, None follows the settings."""
    return install is True or (install is None and cfg.defaults.install)


@dataclass
class Unmigrated:
    """A local database Flyway manages (the local copy) without some migrations of the checkout: pending ones,
    failed ones, and repeatable ones changed since they ran."""

    alias: str
    db: Database
    repo: Path
    versions: list[str]
    changed: list[str] = field(default_factory=list)  # applied, but their file changed since: repair first
    uncommitted: list[str] = field(default_factory=list)  # of ``versions``: work in progress, not committed

    def describe(self) -> dict:
        return {"db": self.alias, "count": len(self.versions), "first": self.versions[0] if self.versions else "",
                "last": self.versions[-1] if self.versions else "", "changed": self.changed,
                "uncommitted": len(self.uncommitted), "repo": str(self.repo)}


def unmigrated(cfg: Config, alias: str, db: Database | None = None) -> Unmigrated | None:
    """What of pdms-db-migrations the database ``alias`` (or ``db``, a test database) lacks. Only local databases
    Flyway already manages: shared ones are migrated by the pipeline, and tables made another way would get every
    migration run over them."""
    db = db or cfg.dbs.get(alias)
    if db is None or not migrations.is_local(db):
        return None
    try:
        repo = repo_settings.current_migrations_repo(cfg)
        known = migrations.status(repo, db, min(cfg.defaults.db_timeout, 10))
    except Exception:  # noqa: BLE001 - no migrations repo, or the database does not answer: not a reason to stop
        return None
    if not known["flyway"]:
        return None
    return behind_of(alias, db, repo, known)


def behind_of(alias: str, db: Database, repo: Path, known: dict) -> Unmigrated | None:
    """The :class:`Unmigrated` of a :func:`migrations.status` (None when nothing is due)."""
    due = [item for item in known["migrations"] if item["state"] in ("pending", "failed", "outdated")]
    changed = [item["version"] for item in known["migrations"] if item["state"] == "changed"]
    if not due and not changed:
        return None
    return Unmigrated(alias, db, repo, [item.get("version") or item["description"] for item in due], changed,
                      [item.get("version") or item["description"] for item in due if item.get("uncommitted")])


def check_migrations(cfg: Config, aliases: Iterable[str], migrate: bool | None) -> list[Unmigrated]:
    """The databases to migrate before services start on them: :class:`MigrationsPending` for the user to answer
    when ``migrate`` is None, the ones behind when True, none when False."""
    if migrate is False:
        return []
    behind = [item for alias in dict.fromkeys(aliases) if alias and (item := unmigrated(cfg, alias))]
    if behind and migrate is None:
        raise MigrationsPending(behind)
    if behind:
        from .. import images  # images imports this module

        images.require([migrations.IMAGE])  # asked before anything starts
    return behind


MIGRATE_TIMEOUT = 3600
# Schemas the real databases have and migrations write to (backups before a backfill), which no migration creates
# and the local copy does not bring: empty, they let those migrations run locally.
SUPPORT_SCHEMAS = ("backup_data",)


def ensure_support_schemas(db: Database) -> None:
    import psycopg
    from psycopg import sql

    try:
        with psycopg.connect(host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password,
                             connect_timeout=10, autocommit=True) as conn:
            for schema in SUPPORT_SCHEMAS:
                conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
    except Exception as exc:  # noqa: BLE001 - the driver's errors
        raise ActionError(_("Could not prepare {name}: {error}", name=db.database, error=str(exc).strip()[:200])) from exc


def apply_migrations(item: Unmigrated, output: IO[str] | None = None) -> None:
    """``flyway migrate`` on a local database, with the image of the pipeline (output to ``output`` or the
    terminal). Migrations whose file changed since they ran make Flyway refuse: ``flyway repair`` takes the
    checkout's checksums first (the database keeps what those migrations did then)."""
    from .. import images  # images imports this module

    if not migrations.is_local(item.db):
        raise ActionError(_("{name} is not a local database: pdms only migrates local ones.", name=item.alias))
    images.require([migrations.IMAGE])
    ensure_support_schemas(item.db)
    redirect = {"stdout": output, "stderr": subprocess.STDOUT} if output else {}
    for command in (["repair"] if item.changed else []) + (["migrate"] if item.versions else []):
        try:
            code = subprocess.run(migrations.docker_command(item.repo, item.db, command), timeout=MIGRATE_TIMEOUT,
                                  env={**os.environ, "DB_PASSWORD": item.db.password}, stdin=subprocess.DEVNULL,
                                  **redirect).returncode
        except (OSError, subprocess.SubprocessError) as exc:
            raise ActionError(_("Flyway {command} could not run on {name}: {error}", command=command,
                                name=item.alias, error=exc)) from exc
        if code != 0:
            raise ActionError(_("Flyway {command} failed on {name} (exit code {code}); the log says why.",
                                command=command, name=item.alias, code=code))


def check_pythons(cfg: Config, services: Iterable[Path], install: bool | None, python: bool | None) -> dict[Path, str]:
    """The services to install on another Python, ``{service: interpreter}``, when their virtualenv runs one their
    project no longer accepts. ``python`` is the user's answer: None raises :class:`PythonMismatch` for them to
    decide, True recreates (:class:`ActionError` when no accepted Python is installed), False keeps them. Nothing
    happens without an install (``install`` False, or installs off in the settings)."""
    if python is False or not install_wanted(cfg, install):
        return {}
    found = [item for service in dict.fromkeys(services) if (item := pythons.mismatch(service))]
    if not found:
        return {}
    if python is None:
        raise PythonMismatch(found)
    missing = [item for item in found if item.use is None]
    if missing:
        raise ActionError(_("No installed Python meets {spec} ({services}). Install one, e.g.: uv python install {version}",
                            spec=missing[0].wants, services=", ".join(item.service.name for item in missing),
                            version=pythons.suggested(missing[0].wants)))
    return {item.service: item.use[1] for item in found if item.use}


def needs_install(cfg: Config, service: Path, install: bool | None) -> bool:
    """Whether to run ``poetry lock && poetry install`` now; by default (smart) only if something changed."""
    if not install_wanted(cfg, install):
        return False
    return not (install is None and cfg.defaults.smart_install and runner.poetry_python(service)
                and installer.is_up_to_date(service))


def quick_install(cfg: Config, service: Path, install: bool | None) -> list[Path] | None:
    """When following the settings (smart install) and only the code of copied packages changed, those packages:
    reinstalling them is enough (see :func:`installer.code_only_changes`). None for a whole install."""
    if install is not None or not cfg.defaults.smart_install or not runner.poetry_python(service):
        return None
    return installer.code_only_changes(service)


def install_service(service: Path, output: IO[str] | None = None, only: list[Path] | None = None,
                    python: str | None = None) -> None:
    """Install the service's dependencies (see :func:`runner.install`), first on interpreter ``python`` when given,
    and remember what got installed."""
    try:
        runner.ensure_poetry()
        runner.install(service, output, only, python)
    except RuntimeError as exc:
        raise ActionError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise ActionError(_("{cmd} failed (exit code {code}).", cmd=" ".join(map(str, exc.cmd)),
                            code=exc.returncode)) from exc
    installer.remember(service)


def install_log(key: str) -> Path:
    """Where the install of instance ``key`` writes its output (the service's own log is rotated when it starts)."""
    return instances.log_path(key).with_suffix(".install.log")


def install_workers() -> int:
    """How many services install at once: 2 on a modest PC (4 cores or fewer, or under 12 GB of memory), else 3.
    Most of an install is waiting for the package index and the disk, not the CPU."""
    modest = (os.cpu_count() or 1) <= 4 or psutil.virtual_memory().total < 12 * 1024**3
    return 2 if modest else 3


def install_services(
    cfg: Config, services: dict[Path, Path], install: bool | None,
    progress: Callable[[str, Path], None] = lambda event, service: None, recreate: dict[Path, str] | None = None,
) -> None:
    """Install the ``services`` that need it, several at once (:func:`install_workers`), each writing to its log
    (``{service: log}``). ``progress`` gets ``("installing" | "installed" | "retrying", service)``.

    One that fails is tried again on its own once the rest are done: poetry shares its download cache between
    processes without locking it, so two installs downloading the same package can trip over each other. Raises
    :class:`ActionError` naming the ones that still failed, after trying all of them.
    """
    recreate = recreate or {}
    pending = [service for service in services if service in recreate or needs_install(cfg, service, install)]

    def one(service: Path, mode: str = "w") -> None:
        only = None if service in recreate else quick_install(cfg, service, install)
        log = services[service]
        log.parent.mkdir(parents=True, exist_ok=True)
        progress("installing" if mode == "w" else "retrying", service)
        with open(log, mode, encoding="utf-8", errors="replace") as output:
            if mode == "a":
                output.write("\n$ # " + _("tried again on its own") + "\n")
            if service in recreate:  # a new virtualenv on another Python first
                install_service(service, output, None, recreate[service])
            else:
                install_service(service, output, only)
        progress("installed", service)

    failed: list[Path] = []
    if len(pending) > 1:
        with ThreadPoolExecutor(max_workers=min(install_workers(), len(pending))) as pool:
            for service, future in [(s, pool.submit(one, s)) for s in pending]:
                try:
                    future.result()
                except ActionError:
                    failed.append(service)
    elif pending:
        one(pending[0])
    errors = []
    for service in failed:
        try:
            one(service, "a")
        except ActionError as exc:
            errors.append(f"{service.name}: {exc.message} ({services[service]})")
    if errors:
        raise ActionError(_("The install failed for:") + "\n" + "\n".join(errors))


def installed_parts(service: Path) -> dict[str, str] | None:
    """What is installed in the service's virtualenv, when pdms knows it matches the code (else unknown)."""
    return installer.dependency_fingerprints(service) if installer.is_up_to_date(service) else None


def service_env(cfg: Config, launch: ServiceLaunch) -> dict[str, str]:
    extra = dict(launch.events.env)
    if launch.events.kind == "local" and (found := local_events.repo_event_map(cfg, launch.service)):
        extra.update(events.topic_env(*found))  # its own topics; the rest of the setup may be a whole stack's
    extra.update(awsenv.env_for(cfg, launch.service))  # AWS_PROFILE and its Lambda's buckets, when a profile is chosen
    extra.update(awsenv.credentials_env(cfg))  # and its credentials from the AWS CLI (their botocore loses SSO)
    extra.update(emails.env(cfg))  # its SES emails kept for pdms ui, or sent to one address only
    extra.update(testviews.env(cfg, launch.db_name))  # test copies of views it reads instead (pdms_views.py)
    query_stats = cfg.defaults.query_stats and not launch.is_consumer
    # Always: its sitecustomize.py follows the proxy's requests through logs and events (pdms_trace.py) and loads
    # pdms_parallel.py and pdms_queries.py when asked.
    extra["PYTHONPATH"] = str(events.PATCH_DIR)
    extra["PDMS_TRACE_LOG"] = str(trace.path())
    if launch.parallel or cfg.defaults.warm_connections or query_stats:
        if launch.parallel:
            extra["PDMS_PARALLEL_REQUESTS"] = "1"
        if cfg.defaults.warm_connections:
            extra["PDMS_WARM_CONNECTIONS"] = str(cfg.defaults.warm_connections)
        if query_stats:
            extra["PDMS_QUERY_STATS"] = "1"
    return runner.build_env(cfg.defaults, launch.user, launch.db, extra)


def start_service(
    cfg: Config, launch: ServiceLaunch, install: Callable[[Path], None] | None = None, *, awake: bool = False,
) -> instances.Instance:
    """Start the service in the background. ``install`` (the front end's, since it shows progress) runs first.

    An on-demand service is only registered, asleep, for the proxy to start with its first request; ``awake``
    starts it now (a restart, or the proxy waking it), and it still goes back to sleep when idle.
    """
    if install:
        install(launch.service)
    deps, commit = installed_parts(launch.service), commit_of(cfg, launch.service)
    with instances.registry_lock():  # the port is claimed and registered in one step
        launch = claim_port(launch)
        if launch.on_demand and not awake:
            return instances.register_asleep(
                launch.service, host=launch.host, port=launch.port, user=launch.user_name, db=launch.db_name,
                reload=launch.reload, events=launch.events.kind, parallel=launch.parallel,
            )
        cmd, env = runner.direct(launch.service, launch.cmd, service_env(cfg, launch))
        return instances.start(
            launch.service, cmd, env, host=launch.host, port=launch.port,
            user=launch.user_name, db=launch.db_name, reload=launch.reload, deps=deps,
            events=launch.events.kind, queue=launch.queue, parallel=launch.parallel, commit=commit,
            on_demand=launch.on_demand,
        )


def claim_port(launch: ServiceLaunch) -> ServiceLaunch:
    """``launch`` on its port, or on the next free one when another instance took it since it was planned (an
    install in between, or two services starting at once). Called holding :func:`instances.registry_lock`."""
    if not launch.port:  # an SQS consumer listens on its queue
        return launch
    taken = instances.running_ports()
    if launch.port not in taken and runner.port_is_free(launch.host, launch.port):
        return launch
    port = runner.next_free_port(launch.host, launch.port + 1, taken)
    cmd = list(launch.cmd)
    if "--port" in cmd:
        cmd[cmd.index("--port") + 1] = str(port)
    return replace(launch, port=port, cmd=cmd)


def wake_service(cfg: Config, instance: instances.Instance) -> instances.Instance:
    """Start an asleep instance as it was registered (same user, database and port, if still free), without
    installing: the proxy does it for a request that waits meanwhile. Raises :class:`LocalEventsDown` when it
    publishes to the local ElasticMQ and that is down."""
    def plan(port: int) -> ServiceLaunch:
        return plan_service(
            cfg, Path(instance.service), user_name=instance.user, db_name=instance.db, port=port, host=instance.host,
            reload=instance.reload, parallel=instance.parallel, on_demand=True,
            events_mode="local" if instance.events == "local" else "aws",
        )

    # Its port is kept for it while it is registered: freeing it, planning and starting is one step, or another
    # service starting meanwhile (or woken by another request) could take the same port.
    with instances.registry_lock():
        instances.forget(instance.key)
        try:
            try:
                launch = plan(instance.port)
            except PortBusy as busy:  # someone else took it meanwhile
                launch = plan(busy.free)
            return start_service(cfg, launch, awake=True)
        except Exception:
            registry = instances.load()
            registry.setdefault(instance.key, instance)  # asleep again: the next request tries once more
            instances.save(registry)
            raise


def set_on_demand(cfg: Config, instance: instances.Instance, on: bool) -> instances.Instance:
    """Make a service on demand (it keeps running, and sleeps once idle) or always on (an asleep one starts now)."""
    if instance.is_consumer:
        raise ActionError(_("{key} reads a queue all the time: no request would wake it.", key=instance.key))
    if not on and instance.asleep:
        instance = wake_service(cfg, instance)
    return instances.set_on_demand(instance.key, on) or instance


def sleep_service(instance: instances.Instance) -> instances.Instance:
    """Stop a running service now and keep it asleep, on demand, until a request through the proxy wakes it."""
    if instance.is_consumer:
        raise ActionError(_("{key} reads a queue all the time: no request would wake it.", key=instance.key))
    if not instance.alive():
        raise ActionError(_("{key} is not running.", key=instance.key))
    instance.on_demand = True
    return instances.sleep(instance)


def commit_of(cfg: Config, service: Path) -> str:
    """The commit checked out in the service's repo ("" when it is not in a registered git checkout)."""
    alias = repos.repo_of(cfg, service)
    return repos.git_commit(cfg.repos[alias].root) if alias else ""


def exec_service(cfg: Config, launch: ServiceLaunch) -> None:
    """Run the service in the foreground, replacing pdms (see :func:`runner.exec_server`)."""
    runner.exec_server(launch.service, *runner.direct(launch.service, launch.cmd, service_env(cfg, launch)))


def stop_service(instance: instances.Instance) -> None:
    instances.stop(instance)
