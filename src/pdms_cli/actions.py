"""What pdms does with services, stacks, the proxy, the local events and the configuration (repos, databases, users,
defaults), without prompting or printing: shared by the CLI and ``pdms ui``.

An action never asks. When it needs a decision it raises a :class:`Decision` (a busy port, a protected database,
the local ElasticMQ not running, pointing the frontend to the proxy); each front end answers it its own way (a questionary prompt, a dialog) and calls
the action again with the answer. Problems no answer can fix raise :class:`ActionError` with a message for the user.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import re
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit

import psutil

from . import desktop, events, frontend, installer, instances, migrations, proxy, repos, routes, runner, transfer, userimport
from . import awsenv, browser, captures, codeartifact, emails, health, pythons, testviews, trace, vscode
from .config import EVENTS_MODES, LOG_LEVELS, THEMES, UPDATE_CHANNELS, Config, Database, Defaults, DevUser, Setup, Stack, config_path
from .i18n import LANGUAGES, _


class ActionError(Exception):
    """A problem the user has to fix (unknown user, bad option...); ``message`` is ready to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidValue(ActionError):
    """A value the user gave is not valid; ``field`` is its config key, so a form can point to it."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


class Decision(Exception):
    """The action needs an answer from the user before it can go on."""


class PortBusy(Decision):
    def __init__(self, port: int, free: int) -> None:
        super().__init__(f"port {port} is in use (next free: {free})")
        self.port, self.free = port, free


class ProtectedDatabase(Decision):
    def __init__(self, name: str) -> None:
        super().__init__(f"database {name!r} is protected")
        self.name = name


class LocalEventsDown(Decision):
    """Events must go to the local ElasticMQ, which is not running (answer: start it, then retry)."""

    def __init__(self, port: int) -> None:
        super().__init__(f"the local ElasticMQ is not running on port {port}")
        self.port = port


class PythonMismatch(Decision):
    """Services whose virtualenv runs a Python their project no longer accepts (answer: ``python=True`` to make it
    again on an accepted one before installing, ``python=False`` to keep it as it is)."""

    def __init__(self, items: list[pythons.Mismatch]) -> None:
        super().__init__(", ".join(f"{item.service.name} ({pythons.show(item.have)}, wants {item.wants})"
                                   for item in items))
        self.items = items


class MigrationsPending(Decision):
    """Local databases behind the migrations of pdms-db-migrations (answer: ``migrate=True`` applies them before the
    services start, ``migrate=False`` starts them as they are)."""

    def __init__(self, items: list[Unmigrated]) -> None:
        super().__init__(", ".join(f"{item.alias}: {len(item.versions)}" for item in items))
        self.items = items


class TestViewsPending(Decision):
    """Services about to start read materialized views you changed in your checkout (answer: ``views=True`` makes
    their test copies, or checks you ran their SQL, and has the services read them; ``views=False`` the real ones)."""

    def __init__(self, items: list[TestViewNeed]) -> None:
        super().__init__(", ".join(item.view for item in items))
        self.items = items


class PointFrontend(Decision):
    """Whether ``frontend/.env.local`` should point to the proxy while it runs (answer: ``frontend=True/False``)."""

    def __init__(self, url: str) -> None:
        super().__init__(f"point the frontend to {url}?")
        self.url = url


# --------------------------------------------------------------------------- checks


def require(items: dict, kind: str, name: str) -> str:
    if name not in items:
        raise ActionError(_("'{name}' does not exist ({kind}). Available: {available}",
                            name=name, kind=kind, available=", ".join(items) or _("none")))
    return name


def port_available(host: str, port: int, taken: set[int] | None = None) -> bool:
    """Free on the machine and not held by a background instance (they count as taken while still booting)."""
    taken = instances.running_ports() if taken is None else taken
    return port not in taken and runner.port_is_free(host, port)


def free_port(host: str, port: int) -> int:
    """``port`` if it can be used; otherwise :class:`PortBusy` with the next free one."""
    taken = instances.running_ports()
    if port_available(host, port, taken):
        return port
    raise PortBusy(port, runner.next_free_port(host, port + 1, taken))


def suggested_port(cfg: Config, host: str) -> int:
    return runner.next_free_port(host, cfg.defaults.port, instances.running_ports())


def check_database(cfg: Config, db_name: str, confirmed: bool) -> None:
    """:class:`ProtectedDatabase` unless the database is unprotected or the user already confirmed it."""
    if cfg.dbs[db_name].protected and not confirmed:
        raise ProtectedDatabase(db_name)


def remember_profile(cfg: Config, user_name: str, db_name: str) -> None:
    """The user and database become the defaults next time."""
    cfg.last_user, cfg.last_db = user_name, db_name
    cfg.save()


# --------------------------------------------------------------------------- events


@dataclass
class EventsSetup:
    env: dict[str, str]
    label: str
    kind: str  # "local" or "aws"
    warning: str = ""


def events_setup(cfg: Config, service: Path, mode: str | None) -> EventsSetup:
    """Where the service publishes SQS events: auto, local (pdms events broker) or aws."""
    mode = mode or cfg.defaults.events
    if mode not in events.EVENT_MODES:
        raise ActionError(_("Unknown events mode '{mode}'. Available: {codes}",
                            mode=mode, codes=", ".join(events.EVENT_MODES)))
    if mode == "aws":
        return EventsSetup({}, _("AWS (the service's own configuration)"), "aws")
    port = cfg.defaults.events_port
    if not events.running(port):
        if mode == "auto":
            return EventsSetup({}, _("AWS (run pdms events up to publish locally)"), "aws")
        raise LocalEventsDown(port)
    root = repos.find_repo_root(service) or repos.active_root(cfg)
    event_map = events.load_event_map(root) if root else events.EventMap()
    warning = "" if event_map.broker_queue else _(
        "⚠ No broker queue found in the repo's Terraform; SQS_EVENT_BROKER_URL is left as configured.")
    label = _("local broker · {url}", url=events.queue_url(event_map.broker_queue, port)) \
        if event_map.broker_queue else _("local ElasticMQ · {url}", url=events.endpoint(port))
    return EventsSetup(events.local_env(event_map, port), label, "local", warning)


def repo_event_map(cfg: Config, service: Path) -> tuple[events.EventMap, str] | None:
    """The event map of the service's repo and the service's path in it (``lead/lead-tp-list``)."""
    root = repos.find_repo_root(service) or repos.active_root(cfg)
    if not root:
        return None
    try:
        relative = service.resolve().relative_to((root / "backend").resolve()).as_posix()
    except ValueError:
        return None
    return events.load_event_map(root), relative


def consumer_of(cfg: Config, service: Path) -> tuple[events.Queue, events.Consumer] | None:
    """The queue and handler when ``service`` is an SQS consumer (event Lambda) of its repo."""
    found_map = repo_event_map(cfg, service)
    if not found_map:
        return None
    event_map, relative = found_map
    found = event_map.queue_of_service(relative)
    if not found:
        return None
    queue_name, consumer = found
    return event_map.queues.get(queue_name) or events.Queue(queue_name), consumer


def load_events(cfg: Config, env: str = "dev") -> tuple[Path, events.EventMap]:
    """The current repo and its event map (Terraform and backend/common/event); fails when it has no queues."""
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    event_map = events.load_event_map(root, env)
    if not event_map.queues:
        raise ActionError(_("No SQS queues found in {path}.", path=root))
    return root, event_map


def start_events(cfg: Config, queues: dict[str, events.Queue], wait: float = 30) -> str:
    """Start (or recreate) the local ElasticMQ with ``queues`` and wait until it answers: created | restarted |
    unchanged, as :func:`events.start`."""
    ok, detail = events.docker_available()
    if not ok:
        raise ActionError(_("Docker is not available: {detail}", detail=detail or _("docker not found")))
    port = cfg.defaults.events_port
    state = events.container_state()
    if not (state and state["running"] and state["port"] == port) and not runner.port_is_free("127.0.0.1", port):
        raise ActionError(_("Port {port} is in use by something else (maybe infra/local_sqs's docker compose). Stop "
                            "it or change events_port in pdms config.", port=port))
    try:
        result = events.start(queues, port)
    except RuntimeError as exc:
        raise ActionError(_("Could not start ElasticMQ: {error}", error=exc)) from exc
    deadline = time.monotonic() + wait
    while not events.is_up(port):
        if time.monotonic() > deadline:
            raise ActionError(_("ElasticMQ did not answer on {url}; see: docker logs {name}",
                                url=events.endpoint(port), name=events.CONTAINER))
        time.sleep(0.5)
    return result


def broker_service(root: Path, event_map: events.EventMap) -> Path | None:
    """broker-sqs-event of the repo, which routes published events to their queues as in AWS, if it has one."""
    service = root / "backend" / events.BROKER_SERVICE
    return service if event_map.broker_queue and runner.is_service(service) else None


def event_consumers(queue: str | None = None) -> list[instances.Instance]:
    """The SQS consumers running now (of ``queue`` only, if given)."""
    return [i for i in instances.load().values() if i.is_consumer and i.alive() and queue in (None, i.queue)]


def plan_send(event_map: events.EventMap, target: str, raw: str | None, direct: bool = False) -> tuple[str, str]:
    """``(queue, message)`` to send ``target``: an event type (``raw`` holds its fields; it goes through the broker
    unless ``direct``) or a queue (``raw`` is the message body, JSON)."""
    if target not in event_map.routes and target not in event_map.queues:
        close = [t for t in sorted(event_map.routes) + sorted(event_map.queues) if target in t][:8]
        raise ActionError(_("'{target}' is not an event type nor a queue.", target=target)
                          + (" " + _("Did you mean: {names}", names=", ".join(close)) if close else " pdms events map"))
    raw = "{}" if raw is None else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionError(_("Invalid JSON: {error}", error=exc)) from exc
    if target not in event_map.routes:
        return target, raw
    if not isinstance(data, dict):
        raise ActionError(_("Event fields must be a JSON object."))
    queue = event_map.routes[target] if direct or not event_map.broker_queue else event_map.broker_queue
    return queue, events.event_body(target, data)


def send_message(cfg: Config, event_map: events.EventMap, queue: str, message: str) -> str:
    """Send ``message`` to a local queue; the message id. Fails when the local ElasticMQ is not running."""
    port = cfg.defaults.events_port
    if not events.running(port):
        raise ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    return events.send(port, queue, message, fifo=event_map.queues.get(queue, events.Queue(queue)).fifo)


# --------------------------------------------------------------------------- services

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
    require(cfg.users, _("user"), user_name)
    require(cfg.dbs, _("database"), db_name)
    host = host or cfg.defaults.host
    reload = cfg.defaults.reload if reload is None else reload
    consumer = consumer_of(cfg, service)
    if consumer:
        port = 0
        events_mode = "local"  # a consumer reads its queue from the local ElasticMQ
    else:
        port = free_port(host, port or cfg.defaults.port)
    setup = events_ready or events_setup(cfg, service, events_mode)
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
        repo = current_migrations_repo(cfg)
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
        from . import images  # images imports this module

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
    from . import images  # images imports this module

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
    if launch.events.kind == "local" and (found := repo_event_map(cfg, launch.service)):
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


# --------------------------------------------------------------------------- services outside pdms


@dataclass
class Stray:
    """A service running outside pdms, with the user and database of the config it runs as ("" when none matches)."""

    process: instances.Stray
    user: str
    db: str
    repo: str

    @property
    def key(self) -> str:
        return self.process.key


def _database_id(url: str) -> tuple[str, str, int, str]:
    """``user@host:port/database`` of a connection string, without the password (it may have changed since)."""
    parts = urlsplit(url)
    try:
        port = parts.port or 5432
    except ValueError:
        port = 0
    return unquote(parts.username or ""), parts.hostname or "", port, parts.path.lstrip("/")


def user_of(cfg: Config, env: dict[str, str]) -> str:
    """The user profile whose DEV_USER_ID (and DEV_USERNAME) a service runs with."""
    user_id, username = env.get("DEV_USER_ID", ""), env.get("DEV_USERNAME", "")
    matches = [name for name, user in cfg.users.items() if user_id and user.user_id == user_id]
    return next((name for name in matches if cfg.users[name].username == username), matches[0] if matches else "")


def db_of(cfg: Config, env: dict[str, str]) -> str:
    url = env.get("DB_PG_CONNECTION_STR", "")
    if not url:
        return ""
    wanted = _database_id(url)
    return next((name for name, db in cfg.dbs.items() if _database_id(db.url()) == wanted), "")


def strays(cfg: Config) -> list[Stray]:
    """Services of the registered repos that run but pdms does not know: their pdms exited and lost them (adopt
    them to manage them again, or stop them)."""
    found = instances.strays([repo.root for repo in cfg.repos.values()])
    return [Stray(process, user_of(cfg, process.env), db_of(cfg, process.env),
                  repos.repo_of(cfg, process.service) or "") for process in found]


def _pick_strays(cfg: Config, keys: Iterable[str] | None) -> list[Stray]:
    found = strays(cfg)
    if keys is None:
        return found
    wanted = list(keys)
    unknown = [key for key in wanted if key not in {stray.key for stray in found}]
    if unknown:
        raise ActionError(_("Not running outside pdms: {keys}", keys=", ".join(unknown)))
    return [stray for stray in found if stray.key in wanted]


def adopt_strays(cfg: Config, keys: Iterable[str] | None = None) -> list[instances.Instance]:
    """Register the given services running outside pdms again (every one with ``None``)."""
    return [instances.adopt(stray.process, user=stray.user, db=stray.db) for stray in _pick_strays(cfg, keys)]


def stop_strays(cfg: Config, keys: Iterable[str] | None = None) -> list[str]:
    """Stop the given services running outside pdms (every one with ``None``), with their reloader and workers."""
    chosen = _pick_strays(cfg, keys)
    if chosen:
        with ThreadPoolExecutor(max_workers=min(8, len(chosen))) as pool:
            list(pool.map(lambda stray: instances.kill_tree(stray.process.pid, stray.process.created), chosen))
    return [stray.key for stray in chosen]


def restart_service(
    cfg: Config,
    instance: instances.Instance,
    *,
    user_name: str | None = None,
    db_name: str | None = None,
    confirmed: bool = False,
    install: Callable[[Path], None] | None = None,
) -> instances.Instance:
    """Restart on the same port, with the same user and database unless others are given.

    Everything is checked before stopping, so a refused protected database leaves the instance running.
    """
    user_name, db_name = user_name or instance.user, db_name or instance.db
    require(cfg.users, _("user"), user_name)
    require(cfg.dbs, _("database"), db_name)
    if db_name != instance.db:
        check_database(cfg, db_name, confirmed)
    stop_service(instance)
    launch = plan_service(
        cfg, Path(instance.service), user_name=user_name, db_name=db_name, port=instance.port or None,
        host=instance.host, reload=instance.reload, events_mode="local" if instance.events == "local" else None,
        on_demand=instance.on_demand,
    )
    return start_service(cfg, launch, install, awake=True)  # asked for: it runs now, and sleeps again when idle


# --------------------------------------------------------------------------- debugging


@dataclass
class DebugSetup:
    """A service's VS Code debug configuration, written by :func:`write_debug_config`."""

    launch_json: Path
    name: str  # of the configuration, to pick in Run and Debug
    backup: Path | None  # launch.json had comments: the original, since rewriting it drops them
    env_file: Path
    root: Path  # the folder to open in VS Code


def write_debug_config(cfg: Config, launch: ServiceLaunch) -> DebugSetup:
    """Add or replace the service's configuration in ``.vscode/launch.json`` (without --reload, so breakpoints work).

    The variables, password included, go to an env file outside the repo (see :mod:`vscode`)."""
    python = runner.poetry_python(launch.service)
    if not python:
        raise ActionError(_("{name} has no virtualenv yet: start it once (that installs it) and try again.",
                            name=launch.service.name))
    consumer = consumer_of(cfg, launch.service)
    program = events.poller_command("", consumer[0], consumer[1], cfg.defaults.events_port)[3:] if consumer else None
    env_file = vscode.write_env_file(launch.service, service_env(cfg, launch))
    where = f"sqs {consumer[0].name}" if consumer else f":{launch.port}"
    launch_json, name, backup = vscode.upsert_configuration(
        launch.service, python=python, env_file=env_file, host=launch.host, port=launch.port,
        description=f"{launch.user_name} @ {launch.db_name} {where}", program=program,
    )
    return DebugSetup(launch_json, name, backup, env_file, vscode.workspace_root(launch.service))


def debug_instance(cfg: Config, instance: instances.Instance) -> DebugSetup:
    """Hand a running instance over to VS Code: stop it, write its configuration (same user, database and port) and
    open VS Code on its repo. Pressing F5 there starts it under the debugger."""
    require(cfg.users, _("user"), instance.user)
    require(cfg.dbs, _("database"), instance.db)
    if not runner.poetry_python(Path(instance.service)):
        raise ActionError(_("{name} has no virtualenv yet: start it once (that installs it) and try again.",
                            name=instance.name))
    if not vscode.code_command():
        raise ActionError(_("VS Code's code command was not found. In VS Code: Ctrl+Shift+P → "
                            "Shell Command: Install 'code' command in PATH."))
    if (instance.events == "local" or instance.is_consumer) and not events.running(cfg.defaults.events_port):
        raise LocalEventsDown(cfg.defaults.events_port)  # before stopping it, like a restart
    stop_service(instance)
    launch = plan_service(
        cfg, Path(instance.service), user_name=instance.user, db_name=instance.db, port=instance.port or None,
        host=instance.host, reload=False, events_mode="local" if instance.events == "local" else None,
    )
    setup = write_debug_config(cfg, launch)
    try:
        vscode.open_in_code(setup.root)
    except (RuntimeError, OSError) as exc:
        raise ActionError(str(exc)) from exc
    return setup


def open_code(cfg: Config, path: str, line: int = 0) -> None:
    """Open a file of a registered repo in VS Code at ``line`` (a traceback's frame); other files are refused."""
    file = Path(path)
    alias = repos.repo_of(cfg, file)
    if not alias or not file.is_file():
        raise ActionError(_("{path} is not a file of a registered repo.", path=path))
    try:
        vscode.open_in_code(cfg.repos[alias].root, file, line)
    except RuntimeError as exc:
        raise ActionError(str(exc)) from exc


def running_by_service() -> dict[str, list[int]]:
    """Ports of the live (or asleep) instances of each service (0 for SQS consumers)."""
    result: dict[str, list[int]] = {}
    for inst in instances.load().values():
        if inst.present():
            result.setdefault(inst.service, []).append(inst.port)
    return result


# --------------------------------------------------------------------------- stacks


def stack_paths(stack: Stack, root: Path) -> list[Path]:
    """The stack's service folders under the repo's backend ``root``."""
    paths = []
    for svc in stack.services:
        path = root / svc
        if not runner.is_service(path):
            raise ActionError(_("'{svc}' is no longer a service in {root}. Edit the stack with [bold]pdms stack edit[/].",
                                svc=svc, root=root))
        paths.append(path)
    return paths


@dataclass
class StackLaunch:
    """What starting a stack does: the services to start and those already running (skipped)."""

    name: str
    user_name: str
    db_name: str
    services: list[ServiceLaunch]
    running: dict[Path, list[int]]
    events: EventsSetup | None = None  # None when there is nothing to start


def plan_stack(
    cfg: Config, name: str, root: Path, *, user_name: str, db_name: str, events_mode: str | None = None,
    on_demand: bool | None = None,
) -> StackLaunch:
    """Plan every service of the stack that is not running yet, each on its own free port.

    Raises :class:`LocalEventsDown` when events must go to the local ElasticMQ (always, if the stack has a consumer).
    """
    stack = cfg.stacks[require(cfg.stacks, _("stack"), name)]
    require(cfg.users, _("user"), user_name)
    require(cfg.dbs, _("database"), db_name)
    paths = stack_paths(stack, root)
    live = running_by_service()
    running = {p: live[str(p)] for p in paths if str(p) in live}
    pending = [p for p in paths if p not in running]
    if not pending:
        return StackLaunch(name, user_name, db_name, [], running)

    consumers = {p: consumer_of(cfg, p) for p in pending}
    if any(consumers.values()):
        events_mode = "local"  # consumers read from the local ElasticMQ
    setup = events_setup(cfg, pending[0], events_mode)
    host = cfg.defaults.host
    taken = set(instances.running_ports())
    port = cfg.defaults.port
    on_demand = stack.runs_on_demand(cfg.defaults.on_demand) if on_demand is None else on_demand
    launches = []
    for path in pending:
        if not consumers[path]:
            port = runner.next_free_port(host, port, taken)
            taken.add(port)
        service_user, service_db = stack.profile_of(_relative(path, root), user_name, db_name)
        launches.append(plan_service(
            cfg, path, user_name=service_user, db_name=service_db, port=port, host=host, events_ready=setup,
            on_demand=on_demand,
        ))
    return StackLaunch(name, user_name, db_name, launches, running, setup)


def start_stack(
    cfg: Config, plan: StackLaunch, install: Callable[[Path], None] | None = None
) -> list[instances.Instance]:
    """Start the planned services in the background; waiting until they respond is up to the front end."""
    return [start_service(cfg, launch, install) for launch in plan.services]


def _relative(path: Path, root: Path) -> str:
    """A service's path in a stack: relative to the backend folder, with "/"."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ActionError(_("{path} is not a service of the current repo.", path=path)) from None


def remember_in_stack(cfg: Config, name: str, root: Path, services: Iterable[Path], user: str, db: str) -> None:
    """Make ``services`` of the stack start with ``user`` and ``db`` from now on: an exception where they differ from
    the stack's own, none where they are the same."""
    stack = cfg.stacks[require(cfg.stacks, _("stack"), name)]
    require(cfg.users, _("user"), user)
    require(cfg.dbs, _("database"), db)
    for path in services:
        svc = _relative(path, root)
        if svc not in stack.services:
            raise ActionError(_("{service} is not in the stack {name}.", service=svc, name=name))
        own = {"user": user} if user != stack.user else {}
        if db != stack.db:
            own["db"] = db
        if own:
            stack.overrides[svc] = own
        else:
            stack.overrides.pop(svc, None)
    cfg.save()


# --------------------------------------------------------------------------- setups (Home)


def save_setup_as(cfg: Config, name: str) -> str:
    """Keep the current setup under ``name`` (a new one, or replacing it); it becomes the current one's name."""
    name = check_alias(name, cfg.setups) if name.strip() not in cfg.setups else name.strip()
    cfg.setups[name] = replace(cfg.setup)
    cfg.setup_name = name
    cfg.save()
    return name


def use_setup(cfg: Config, name: str) -> Setup:
    """Make the saved setup ``name`` the current one (what Start everything starts)."""
    cfg.setup = replace(cfg.setups[require(cfg.setups, _("setup"), name)])
    cfg.setup_name = name
    cfg.save()
    return cfg.setup


def remove_setup(cfg: Config, name: str) -> None:
    del cfg.setups[require(cfg.setups, _("setup"), name)]
    if cfg.setup_name == name:
        cfg.setup_name = ""  # the current setup stays as it is, without a name
    cfg.save()


def stack_instances(cfg: Config, name: str, root: Path) -> list[instances.Instance]:
    """Live (or asleep) instances of the stack's services."""
    paths = {str(p) for p in stack_paths(cfg.stacks[require(cfg.stacks, _("stack"), name)], root)}
    return [i for i in instances.load().values() if i.service in paths and i.present()]


def save_stack(cfg: Config, name: str, stack: Stack) -> None:
    if not stack.services:
        raise ActionError(_("A stack needs at least one service."))
    stack.overrides = {svc: own for svc, own in stack.overrides.items() if svc in stack.services}  # gone with them
    for own in stack.overrides.values():
        if own.get("user"):
            require(cfg.users, _("user"), own["user"])
        if own.get("db"):
            require(cfg.dbs, _("database"), own["db"])
    if stack.user:
        require(cfg.users, _("user"), stack.user)
    if stack.db:
        require(cfg.dbs, _("database"), stack.db)
    cfg.stacks[name] = stack
    cfg.save()


def rename_stack(cfg: Config, name: str, new_name: str) -> str:
    """Give the stack another name, in the same place of the list; the setups that start it follow. The new name."""
    stack_name = require(cfg.stacks, _("stack"), name)
    new_name = new_name.strip()
    if new_name == stack_name:
        return stack_name
    new_name = check_alias(new_name, cfg.stacks)
    cfg.stacks = {(new_name if key == stack_name else key): stack for key, stack in cfg.stacks.items()}
    for setup in (cfg.setup, *cfg.setups.values()):
        if setup.stack == stack_name:
            setup.stack = new_name
    cfg.save()
    return new_name


def _most_used(values: Iterable[str]) -> str:
    """The value most of them have ("" when none has one); a tie goes to the first in alphabetical order."""
    counts = Counter(value for value in values if value)
    return max(sorted(counts), key=lambda value: counts[value]) if counts else ""


def stack_from_running(cfg: Config, root: Path) -> Stack:
    """A stack of the services pdms runs from ``root`` (the current repo's backend folder), as they run: with the user
    and database most of them use, and the others' own as exceptions. A service running twice counts as it started
    first; a user or database no longer in the configuration is left for the stack to ask."""
    found: dict[str, tuple[str, str]] = {}
    for inst in sorted(instances.load().values(), key=lambda item: item.started_at):
        if not inst.alive():
            continue
        try:
            path = _relative(Path(inst.service), root)
        except ActionError:
            continue
        found.setdefault(path, (inst.user if inst.user in cfg.users else "", inst.db if inst.db in cfg.dbs else ""))
    if not found:
        raise ActionError(_("No service of the current repo is running."))
    user = _most_used(own[0] for own in found.values())
    db = _most_used(own[1] for own in found.values())
    overrides = {}
    for path, (own_user, own_db) in found.items():
        own = {key: value for key, value, common in (("user", own_user, user), ("db", own_db, db))
               if value and value != common}
        if own:
            overrides[path] = own
    return Stack(services=sorted(found), user=user, db=db, overrides=overrides)


def remove_stack(cfg: Config, name: str) -> None:
    del cfg.stacks[require(cfg.stacks, _("stack"), name)]
    if cfg.setup.stack == name:
        cfg.setup.stack = ""
    for saved in cfg.setups.values():
        if saved.stack == name:
            saved.stack = ""
    cfg.save()


# --------------------------------------------------------------------------- proxy

PROXY_HOST = "0.0.0.0"
MAX_PROXY_TIMEOUT = 3600


def proxy_remote(cfg: Config, root: Path, remote: str | None, no_remote: bool) -> tuple[str | None, bool]:
    """The remote API for what is not running locally, and whether it was just detected from ``frontend/.env``.

    A given or detected URL is saved for the repo, so the next proxy uses it without asking.
    """
    if no_remote:
        return None, False
    alias = repos.alias_of(cfg, root)
    repo = cfg.repos.get(alias) if alias else None
    if remote:
        remote = remote.rstrip("/")
        if repo and repo.remote != remote:
            repo.remote = remote
            cfg.save()
        return remote, False
    if repo and repo.remote:
        return repo.remote, False
    detected = repos.remote_from_frontend(root)
    if detected and repo:
        repo.remote = detected
        cfg.save()
    return detected, bool(detected and repo)


def proxy_routes(root: Path, env: str) -> list[routes.Route]:
    if not routes.terraform_dir(root, env).is_dir():
        raise ActionError(_("No Terraform for '{env}' in {path}.", env=env, path=routes.terraform_dir(root, env)))
    return routes.load_routes(root, env)


@dataclass
class ProxyLaunch:
    """Everything decided to start the proxy; :func:`serve_proxy` or :func:`start_proxy` carry it out."""

    root: Path
    env: str
    routes: list[routes.Route]
    port: int
    remote: str | None
    user_name: str | None
    user: DevUser | None
    frontend: bool  # point frontend/.env.local to the proxy while it runs
    timeout: int = 300  # seconds to wait for each answer

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"


def clear_proxy_leftovers() -> str | None:
    """Fail if a proxy is running; otherwise undo what a proxy that did not stop cleanly left (the restored file)."""
    if running := proxy.running_proxy():
        raise ActionError(_("The proxy is already running on port {port} (pid {pid}).",
                            port=running["port"], pid=running["pid"]))
    return proxy.restore_frontend_change()


def plan_proxy(
    cfg: Config,
    root: Path,
    repo_routes: list[routes.Route],
    *,
    port: int,
    env: str = "dev",
    remote: str | None = None,
    user_name: str | None = None,
    frontend: bool | None = None,
    timeout: int | None = None,
) -> ProxyLaunch:
    """Decide how the proxy runs. Raises :class:`PortBusy` or :class:`PointFrontend` for the user to answer.

    ``timeout`` defaults to the ``proxy_timeout`` setting."""
    timeout = _number("timeout", cfg.defaults.proxy_timeout if timeout is None else timeout, 1, MAX_PROXY_TIMEOUT)
    if user_name:
        require(cfg.users, _("user"), user_name)
    port = free_port(PROXY_HOST, port)
    url = f"http://localhost:{port}"
    if not (root / "frontend").is_dir() or repos.frontend_uses(root, url):
        frontend = False
    elif frontend is None:
        raise PointFrontend(url)
    return ProxyLaunch(
        root, env, repo_routes, port, remote, user_name, cfg.users[user_name] if user_name else None, frontend,
        timeout,
    )


def point_frontend(plan: ProxyLaunch) -> str | None:
    """Point ``frontend/.env.local`` to the proxy if the plan says so; the changed file (undone when it stops)."""
    if plan.frontend and (change := repos.point_frontend_to(plan.root, plan.url)):
        proxy.remember_frontend_change(change)
        return change["path"]
    return None


def serve_proxy(plan: ProxyLaunch, log: Callable[..., None]) -> None:
    """Run the proxy in this process until it is interrupted; ``log`` gets every request."""
    gateway = proxy.Gateway(
        routes=plan.routes, backend=plan.root / "backend", remote=plan.remote, impersonate=plan.user,
        timeout=plan.timeout, log=log,
    )
    proxy.serve(gateway, PROXY_HOST, plan.port, {
        "repo": str(plan.root), "env": plan.env, "remote": plan.remote or "", "as": plan.user_name or "",
        "timeout": plan.timeout,
    })


@dataclass
class ProxyStarted:
    # The process pdms started. On Windows (venv python.exe) and with macOS framework builds it is a launcher that
    # runs the real interpreter as its child, so the proxy's own pid is the one in its state file, not this one.
    pid: int
    port: int
    log: Path


def start_proxy(plan: ProxyLaunch) -> ProxyStarted:
    """Start the proxy in the background; waiting until it responds is up to the front end (:func:`proxy_state`)."""
    cmd = proxy.background_command(
        plan.root, port=plan.port, env=plan.env, remote=plan.remote, user_name=plan.user_name, timeout=plan.timeout,
    )
    proc = instances.spawn(cmd, plan.root, dict(os.environ), proxy.log_path())
    return ProxyStarted(proc.pid, plan.port, proxy.log_path())


def proxy_state(started: ProxyStarted) -> str:
    """``ok`` once the background proxy answers, ``starting`` before, ``stopped`` if it exited."""
    running = proxy.running_proxy()
    if running and running["port"] == started.port:
        return "ok" if instances.responds("127.0.0.1", started.port) else "starting"
    return "starting" if instances.process_alive(started.pid) else "stopped"


def wait_for_proxy(started: ProxyStarted, timeout: float = 30) -> str:
    """The proxy's state once it answers, exits or ``timeout`` runs out."""
    deadline = time.monotonic() + timeout
    while (state := proxy_state(started)) == "starting" and time.monotonic() < deadline:
        time.sleep(0.2)
    return state


def proxy_request(ident: str) -> dict:
    """A request the background proxy kept: what it sent and got (secret headers hidden), as curl, and whether it
    can be sent again (``replay`` says why not)."""
    capture = captures.find(ident) if re.fullmatch(r"[0-9a-f]{8}", ident) else None
    if not capture:
        raise ActionError(_("The proxy no longer keeps that request."))
    running = proxy.running_proxy()
    why = captures.replayable(capture) or ("" if running else _("the proxy is not running"))
    return {"request": captures.public(capture), "curl": captures.curl(capture, running["port"]) if running else "",
            "replay": why, "steps": trace.steps(ident)}


def replay_request(ident: str) -> dict:
    """Send a kept request through the running proxy again, as it came (secret headers included)."""
    data = proxy_request(ident)
    if data["replay"]:
        raise ActionError(_("This request cannot be sent again: {why}.", why=data["replay"]))
    running = proxy.running_proxy()
    try:
        status, seconds = captures.replay(captures.find(ident) or {}, running["port"] if running else 0)
    except (OSError, http.client.HTTPException) as exc:
        raise ActionError(_("The proxy did not answer: {error}", error=exc)) from exc
    return {"status": status, "ms": round(seconds * 1000)}


def stop_proxy(running: dict | None = None) -> str | None:
    """Stop the running proxy (if any) and put ``frontend/.env.local`` back; the restored file, if one was."""
    running = running or proxy.running_proxy()
    if running:
        proxy.stop(running)
    return proxy.restore_frontend_change()


# --------------------------------------------------------------------------- frontend


@dataclass
class FrontendLaunch:
    """Everything decided to start the web app; :func:`prepare_frontend` and :func:`start_frontend` carry it out."""

    root: Path
    mode: str  # dev | build
    port: int
    install: bool  # run yarn install first
    build: str  # why it is built first ("" to serve the last build)

    @property
    def url(self) -> str:
        return frontend.url(self.port)


def frontend_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    if not frontend.exists(root):
        raise ActionError(_("{path} has no frontend (frontend/package.json).", path=root))
    return root


def check_frontend_tools() -> None:
    """Node 22–24 and yarn, as the frontend's package.json asks."""
    version = frontend.node_version()
    if version is None:
        raise ActionError(_("Node.js was not found. Install Node 22 or 24 (for example with nvm install 22)."))
    if not frontend.node_supported(version):
        raise ActionError(_("The frontend needs Node 22 to 24 and this is Node {version} (nvm use 22).",
                            version=version))
    if frontend.yarn() is None:
        raise ActionError(_("yarn was not found. Install it with: npm install -g yarn"))


def plan_frontend(
    cfg: Config, *, mode: str = "dev", port: int | None = None, install: bool | None = None, rebuild: bool = False,
) -> FrontendLaunch:
    """Decide how the web app runs. Raises :class:`PortBusy` for the user to answer.

    ``install``: True forces ``yarn install``, False skips it, None runs it when node_modules does not match the
    lockfile. A build is made again only when something changed since the last one, or with ``rebuild``."""
    mode = _one_of("mode", mode, frontend.MODES)
    root = frontend_root(cfg)
    if current := frontend.running():
        raise ActionError(_("The frontend is already running ({mode}) at {url}. Stop it first: pdms stop frontend",
                            mode=current.get("mode", "dev"), url=frontend.url(current["port"])))
    check_frontend_tools()
    port = free_port("127.0.0.1", port or frontend.PORT)
    wanted = install is True or (install is None and not frontend.dependencies_ok(root))
    build = ""
    if mode == "build":
        build = "rebuild asked" if rebuild else frontend.build_needed(root)
    return FrontendLaunch(root, mode, port, wanted, build)


def _yarn(cmd: list[str], cwd: Path, output: IO[str] | None) -> None:
    redirect = {"stdout": output, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL} if output else {}
    if output:
        output.write(f"$ {' '.join(cmd)}\n")
        output.flush()
    subprocess.run(cmd, cwd=cwd, env=frontend.environment(), check=True, **redirect)


def codeartifact_ready(
    folder: Path, notes: IO[str] | None = None, sso: Callable[[str], bool] | None = None, force: bool = False,
) -> list[str]:
    """Log in to the AWS CodeArtifact repositories of the web app whose token npm no longer accepts (all of them with
    ``force``), as ``frontend/codeartifact-login.sh`` does; the profiles used. An expired SSO session is given to
    ``sso`` (the terminal runs ``aws sso login``), else it says how to log in."""
    cfg = Config.load()
    used = []
    for domain in codeartifact.needed(folder):
        if not force and codeartifact.authorized(folder, domain) is not False:
            continue
        if notes:
            notes.write(_("The {namespace} packages need a new AWS CodeArtifact token; logging in...",
                          namespace=domain.namespace) + "\n")
            notes.flush()
        try:
            try:
                used.append(codeartifact.login(cfg, domain, notes))
            except codeartifact.LoginError as exc:
                if not (exc.expired and sso and sso(exc.profile)):
                    raise
                used.append(codeartifact.login(cfg, domain, notes))
        except codeartifact.LoginError as exc:
            if exc.expired:
                raise ActionError(_("The AWS session of {profile} is over: log in with aws sso login --profile "
                                    "{profile} (or from Data in pdms ui) and install the frontend again.",
                                    profile=exc.profile)) from exc
            raise ActionError(_("Could not log in to AWS CodeArtifact: {error}", error=exc.message)) from exc
    return used


def install_frontend(
    root: Path, output: IO[str] | None = None, log: Path | None = None, sso: Callable[[str], bool] | None = None,
    notes: IO[str] | None = None,
) -> None:
    """``yarn install`` in ``frontend/``, logged in to AWS CodeArtifact first when its token is over (see
    :func:`codeartifact_ready`). A failure that still looks like CodeArtifact logs in again and tries once more.
    ``notes`` gets what pdms says (``output`` when it is given, the terminal's otherwise)."""
    folder, notes = frontend.folder(root), output or notes
    codeartifact_ready(folder, notes, sso)
    try:
        _yarn(frontend.install_command(), folder, output)
        return
    except (OSError, subprocess.CalledProcessError) as exc:
        if not (log and frontend.needs_codeartifact(log)):
            raise ActionError(_("yarn install failed: {error}", error=exc)) from exc
    codeartifact_ready(folder, notes, sso, force=True)
    try:
        _yarn(frontend.install_command(), folder, output)
    except (OSError, subprocess.CalledProcessError) as exc:
        if log and frontend.needs_codeartifact(log):
            raise ActionError(_("yarn install could not download the {packages} packages from AWS CodeArtifact, "
                                "even after logging in: check the profile has the {role} role.",
                                packages="@alivi", role=codeartifact.ROLE)) from exc
        raise ActionError(_("yarn install failed: {error}", error=exc)) from exc


def build_frontend(root: Path, output: IO[str] | None = None) -> dict:
    """``yarn build`` (tsc + vite build into frontend/dist) and remember what it was built from."""
    try:
        _yarn(frontend.build_command(), frontend.folder(root), output)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ActionError(_("yarn build failed: {error}", error=exc)) from exc
    return frontend.remember_build(root)


def start_frontend(plan: FrontendLaunch) -> dict:
    """Start the dev server or the preview of the build in the background; waiting is :func:`wait_for_frontend`."""
    return frontend.start(plan.root, plan.mode, plan.port)


def wait_for_frontend(started: dict, timeout: float = 90) -> str:
    """``ok`` once it answers, ``stopped`` if it exited, ``starting`` when ``timeout`` runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not frontend.running():
            return "stopped"
        if frontend.responds(int(started["port"])):
            return "ok"
        time.sleep(0.5)
    return "starting"


def stop_frontend(current: dict | None = None) -> bool:
    """Stop the frontend pdms started; False when it was not running."""
    current = current or frontend.running()
    if not current:
        return False
    frontend.stop(current)
    return True


@dataclass
class FrontendApi:
    """The web app calls a local API nobody answers on: a proxy that stopped, or one on another port."""

    url: str  # VITE_APP_API_URL (+ version) it calls
    port: int
    proxy_port: int | None  # the proxy running now, if any
    leftover: bool  # frontend/.env.local still has the change a proxy that did not stop cleanly made


def frontend_api(cfg: Config) -> FrontendApi | None:
    """What is wrong with where the frontend of the current repo sends its API calls, or None when nothing is."""
    root = repos.active_root(cfg)
    if not frontend.exists(root):
        return None
    url = frontend.api_url(root, "dev")
    if not frontend.is_local(url):
        return None
    try:
        port = urlsplit(url).port or 80
    except ValueError:
        return None
    running = proxy.running_proxy()
    if running and running["port"] == port:
        return None
    if instances.responds("127.0.0.1", port, timeout=0.5):  # a service it calls directly, on purpose
        return None
    return FrontendApi(url, port, running["port"] if running else None, bool(proxy.frontend_change()))


def fix_frontend_api(cfg: Config) -> str:
    """Point the frontend to the running proxy ("pointed"), or put ``.env.local`` back as it was ("restored")."""
    problem = frontend_api(cfg)
    if problem is None:
        return ""
    root = repos.active_root(cfg)
    if problem.proxy_port:
        proxy.restore_frontend_change()  # the old proxy's change goes first, so this one undoes to the original
        change = repos.point_frontend_to(root, f"http://localhost:{problem.proxy_port}")
        if change:
            proxy.remember_frontend_change(change)
        return "pointed"
    if problem.leftover and proxy.restore_frontend_change():
        return "restored"
    raise ActionError(_("Nothing answers on port {port}: start the proxy (pdms proxy -b), or change VITE_APP_API_URL "
                        "in frontend/.env.local.", port=problem.port))


# --------------------------------------------------------------------------- repos


def repo_root(path: str | Path) -> Path:
    """The PDMS checkout ``path`` is in (it may be a folder inside it); :class:`InvalidValue` otherwise."""
    text = str(path).strip()
    root = repos.find_repo_root(Path(text).expanduser()) if text else None
    if root is None:
        raise InvalidValue("path", _("{path} is not inside a PDMS repo (no backend/snakesdk folder).", path=text or "''"))
    return root


def add_repo(cfg: Config, path: str | Path, alias: str = "") -> str:
    """Register the PDMS checkout ``path`` is in; returns its alias. The first repo becomes the current one."""
    root = repo_root(path)
    if existing := repos.alias_of(cfg, root):
        raise InvalidValue("path", _("{path} is already registered as '{alias}'.", path=root, alias=existing))
    alias = check_alias(alias, cfg.repos) if alias.strip() else repos.suggest_alias(cfg, root)
    alias = repos.register(cfg, root, alias)
    cfg.save()
    return alias


def migrations_repo(path: str) -> str:
    """The Flyway checkout ``path`` is in, as saved for a repo ("" for none); :class:`InvalidValue` otherwise."""
    if not path.strip():
        return ""
    found = migrations.find_upwards(Path(path.strip()).expanduser())
    if found is None:
        raise InvalidValue("migrations", _("{path} is not a Flyway migrations repo (flyway.toml + migrations/).",
                                           path=path.strip()))
    return str(found)


def current_migrations_repo(cfg: Config) -> Path:
    """The Flyway checkout of the current repo: the one saved for it, or the only (or matching) one next to it."""
    root = repos.active_root(cfg)
    alias = repos.alias_of(cfg, root) if root else None
    repo = cfg.repos.get(alias) if alias else None
    if repo and repo.migrations and migrations.is_migrations_repo(Path(repo.migrations)):
        return Path(repo.migrations)
    near = migrations.siblings(root) if root else []
    found = near[0] if len(near) == 1 else migrations.best_match(root, near) if root else None
    if found is None:
        raise ActionError(_("No migrations repo (pdms-db-migrations) for '{alias}': set it in Settings → Repos.",
                            alias=alias or "-"))
    return found


def remote_api(url: str) -> str:
    """``url`` without the trailing slash, if it can be the proxy's remote API ("" for none)."""
    url = url.strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", url):
        raise InvalidValue("remote", _("Must be a URL starting with http:// or https://"))
    return url


def edit_repo(
    cfg: Config, alias: str, *, new_alias: str | None = None, migrations_path: str | None = None,
    remote: str | None = None,
) -> str:
    """Change the alias, the migrations repo or the proxy's remote API of a repo (``None`` keeps it); returns its alias.

    Every value is checked before anything changes."""
    alias = require(cfg.repos, _("repo"), alias)
    repo = cfg.repos[alias]
    renamed = alias
    if new_alias is not None and new_alias.strip() != alias:
        renamed = check_alias(new_alias, cfg.repos)
    saved_migrations = repo.migrations if migrations_path is None else migrations_repo(migrations_path)
    saved_remote = repo.remote if remote is None else remote_api(remote)
    repo.migrations, repo.remote = saved_migrations, saved_remote
    if renamed != alias:
        cfg.repos = {renamed if name == alias else name: item for name, item in cfg.repos.items()}
        if cfg.current_repo == alias:
            cfg.current_repo = renamed
    cfg.save()
    return renamed


@dataclass
class RepoSwitch:
    """What was left behind when the current repo changed: the front end offers to keep, stop or move them."""

    old: str
    # Live instances of services from the old repo, with the same service in the new one (None: not there).
    running: list[tuple[instances.Instance, Path | None]]
    # The proxy is running for the old repo: it keeps routing there until it is restarted.
    proxy: bool


def use_repo(cfg: Config, alias: str) -> RepoSwitch:
    """Make ``alias`` the current repo; what was left running from the previous one."""
    switch = plan_repo_switch(cfg, alias)
    cfg.current_repo = require(cfg.repos, _("repo"), alias)
    cfg.save()
    repos.use_for_this_command(None)
    return switch


def plan_repo_switch(cfg: Config, alias: str) -> RepoSwitch:
    """What making ``alias`` the current repo would leave running from the current one (nothing changes)."""
    alias = require(cfg.repos, _("repo"), alias)
    old = cfg.current_repo
    if not old or old == alias or old not in cfg.repos:
        return RepoSwitch(old, [], False)
    old_root, new_root = cfg.repos[old].root, cfg.repos[alias].root
    running = [
        (inst, repos.translate(Path(inst.service), old_root, new_root))
        for inst in instances.load().values() if inst.alive() and repos.repo_of(cfg, inst.service) == old
    ]
    serving = proxy.running_proxy()
    on_old = bool(serving and serving.get("repo") and repos.repo_of(cfg, serving["repo"]) == old)
    return RepoSwitch(old, running, on_old)


def remove_repo(cfg: Config, alias: str) -> str:
    """Forget a repo (nothing is deleted from disk); returns the current repo afterwards ("" if none is left)."""
    alias = require(cfg.repos, _("repo"), alias)
    root = str(cfg.repos.pop(alias).root)
    cfg.ignored_repos = [r for r in cfg.ignored_repos if r != root]
    if cfg.current_repo == alias:
        cfg.current_repo = next(iter(cfg.repos), "")
    cfg.save()
    return cfg.current_repo


# --------------------------------------------------------------------------- databases, users and defaults

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_alias(name: str, taken: Iterable[str]) -> str:
    """``name`` without surrounding spaces, if it can be the alias of a new database, user or stack."""
    name = name.strip()
    if not name:
        raise InvalidValue("name", _("Required field"))
    if name in set(taken):
        raise InvalidValue("name", _("That name already exists"))
    if not all(c.isalnum() or c in "-_" for c in name):
        raise InvalidValue("name", _("Use only letters, numbers, '-' or '_'"))
    return name


def _alias(items: dict, kind: str, name: str, new: bool) -> str:
    return check_alias(name, items) if new else require(items, kind, name)


def _required(field: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise InvalidValue(field, _("Required field"))
    return value


def _number(field: str, value: int | str, low: int, high: int) -> int:
    try:
        number = None if isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        number = None
    if number is None or not low <= number <= high:
        raise InvalidValue(field, _("Must be a number between {low} and {high}", low=low, high=high))
    return number


def check_email_to(value: str) -> str:
    """The address emails go to (empty: nowhere), trimmed, or :class:`InvalidValue`."""
    value = value.strip()
    if value and not emails.valid_address(value):
        raise InvalidValue("email_to", _("'{value}' is not an email address", value=value))
    return value


def check_front_hosts(hosts: Iterable[str]) -> list[str]:
    """The known fronts the browser extension redirects (``https://dev-pdm.alivi.com/`` → ``dev-pdm.alivi.com``)."""
    found: list[str] = []
    for given in hosts:
        host = given.strip().lower()
        host = (urlsplit(host).hostname or "") if "://" in host else host.split("/", 1)[0]
        if not browser.HOST.fullmatch(host):
            raise InvalidValue("front_redirect_hosts", _(
                "'{host}' is not a host of alivi.com (the extension only redirects those)", host=given.strip()))
        if host not in found:
            found.append(host)
    if not found:
        raise InvalidValue("front_redirect_hosts", _("Give at least one host"))
    return found


def set_front_redirect(cfg: Config, on: bool) -> None:
    """Turn on or off sending the known front to the frontend pdms runs (the browser extension follows in 30 s)."""
    cfg.defaults.front_redirect = on
    cfg.save()


def set_front_hosts(cfg: Config, hosts: Iterable[str]) -> list[str]:
    cfg.defaults.front_redirect_hosts = check_front_hosts(hosts)
    cfg.save()
    return cfg.defaults.front_redirect_hosts


def install_extension(key: str) -> tuple[browser.Browser, Path, bool]:
    """Write the extension's folder and open the extensions page of browser ``key`` (Firefox: open the signed file,
    and it asks whether to add it): the browser, the folder or file, and whether the browser opened."""
    found = browser.BY_KEY.get(key)
    if not found:
        raise ActionError(_("Unknown browser '{key}'. Available: {keys}", key=key, keys=", ".join(browser.BY_KEY)))
    if key not in browser.installed() and not browser.executable(found):
        raise ActionError(_("{name} is not installed on this computer.", name=found.name))
    if key == "firefox":
        try:
            xpi = browser.firefox_xpi()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ActionError(_("Could not get the signed extension for Firefox: {error}", error=exc)) from exc
        return found, xpi, browser.open_page(found, str(xpi))
    folder = browser.write_extension()
    return found, folder, browser.open_page(found)


def _one_of(field: str, value: str, choices: Iterable[str]) -> str:
    if value not in choices:
        raise InvalidValue(field, _("Must be one of: {choices}", choices=", ".join(choices)))
    return value


def valid_db(db: Database) -> Database:
    """``db`` with its fields trimmed, or :class:`InvalidValue` for the first wrong one. The password stays as given."""
    return replace(
        db, host=_required("host", db.host), port=_number("port", db.port, 1, 65535),
        database=_required("database", db.database), user=_required("user", db.user),
    )


def save_db(cfg: Config, name: str, db: Database, new: bool = False) -> str:
    """Create (``new``) or replace the database ``name``; returns its alias."""
    name = _alias(cfg.dbs, _("database"), name, new)
    cfg.dbs[name] = valid_db(db)
    cfg.save()
    return name


def _forget_in_overrides(cfg: Config, kind: str, name: str) -> None:
    """A deleted user or database leaves the stacks' exceptions: those services start like the rest again."""
    for stack in cfg.stacks.values():
        for svc, own in list(stack.overrides.items()):
            if own.get(kind) == name:
                own.pop(kind)
                if not own:
                    del stack.overrides[svc]


def remove_db(cfg: Config, name: str) -> list[str]:
    """Delete a database; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.dbs[require(cfg.dbs, _("database"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.db == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].db = ""
    _forget_in_overrides(cfg, "db", name)
    if cfg.last_db == name:
        cfg.last_db = ""
    cfg.save()
    return stacks


def check_connection(db: Database, timeout: int) -> str:
    """The server version and what a round trip costs, e.g. ``PostgreSQL 16.4 · 370 ms per round trip``;
    :class:`ActionError` with the driver's message otherwise."""
    try:
        version, ms = health.probe(db, timeout)
    except Exception as exc:  # noqa: BLE001 - any driver error is the answer
        raise ActionError(str(exc).strip() or type(exc).__name__) from exc
    return _("{version} · {time} per round trip", version=version, time=health.describe_ms(ms))


def known_roles(cfg: Config) -> list[str]:
    """The ``DEV_ROLES`` the services of the current repo understand (its ``MAP_INTERNAL_ROLES``)."""
    mapping, _source = userimport.role_mapping(repos.active_root(cfg))
    return list(dict.fromkeys(mapping.values()))


def split_roles(roles: str) -> list[str]:
    return list(dict.fromkeys(role.strip() for role in roles.split(",") if role.strip()))


def save_user(cfg: Config, name: str, user: DevUser, new: bool = False, roles: Iterable[str] | None = None) -> str:
    """Create (``new``) or replace the development user ``name``; returns its alias.

    With ``roles``, every role must be one of them, except the ones the user already had (an import keeps the roles
    the repo does not know, and editing the user must not fail because of them)."""
    name = _alias(cfg.users, _("user"), name, new)
    picked = split_roles(user.roles)
    if roles is not None:
        allowed = set(roles) | set(split_roles(cfg.users[name].roles) if name in cfg.users else [])
        if unknown := [role for role in picked if role not in allowed]:
            raise InvalidValue("roles", _("Unknown roles: {roles}. Available: {available}",
                                          roles=", ".join(unknown), available=", ".join(roles)))
    cfg.users[name] = DevUser(
        user_id=_required("user_id", user.user_id), username=_required("username", user.username),
        first_name=user.first_name.strip(), last_name=user.last_name.strip(), roles=",".join(picked),
    )
    cfg.save()
    return name


def rename_user(cfg: Config, name: str, new_name: str) -> str:
    """Give a user another alias, also in the stacks that use it and as the last one used; returns the new alias."""
    name = require(cfg.users, _("user"), name)
    if new_name.strip() == name:
        return name
    renamed = check_alias(new_name, cfg.users)
    cfg.users = {renamed if alias == name else alias: user for alias, user in cfg.users.items()}
    for stack in cfg.stacks.values():
        if stack.user == name:
            stack.user = renamed
        for own in stack.overrides.values():
            if own.get("user") == name:
                own["user"] = renamed
    if cfg.last_user == name:
        cfg.last_user = renamed
    cfg.save()
    return renamed


def remove_user(cfg: Config, name: str) -> list[str]:
    """Delete a user; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.users[require(cfg.users, _("user"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.user == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].user = ""
    _forget_in_overrides(cfg, "user", name)
    if cfg.last_user == name:
        cfg.last_user = ""
    cfg.save()
    return stacks


def read_db_users(
    cfg: Config, db_name: str, mapping: dict[str, str], search: str = "", role: str = "", inactive: bool = False,
    limit: int = 200,
) -> list[userimport.DbUser]:
    """The users of the ``pdms_user`` table of a database, to choose which ones to import."""
    database = cfg.dbs[require(cfg.dbs, _("database"), db_name)]
    try:
        return userimport.fetch_users(
            database, search=search, role=role, include_inactive=inactive, limit=limit,
            timeout=cfg.defaults.db_timeout, mapping=mapping,
        )
    except Exception as exc:  # noqa: BLE001 - show any driver error to the user
        raise ActionError(_("Could not read the users from {name}: {error}", name=db_name,
                            error=str(exc).strip())) from exc


def import_users(
    cfg: Config, picked: list[userimport.DbUser], mapping: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Add the picked users (updating the ones already imported); returns the added and the updated aliases."""
    cfg.users, added, updated = userimport.merge_users(cfg.users, picked, mapping)
    cfg.save()
    return added, updated


def export_config(cfg: Config, sections: list[str], secrets: bool) -> str:
    """The TOML of ``pdms config export`` with those sections; database passwords only with ``secrets``."""
    if unknown := [section for section in sections if section not in transfer.SECTIONS]:
        raise ActionError(_("Unknown sections: {unknown}. Available: {codes}",
                            unknown=", ".join(unknown), codes=", ".join(transfer.SECTIONS)))
    if not sections:
        raise ActionError(_("Nothing selected."))
    return transfer.export_document(cfg, [section for section in transfer.SECTIONS if section in sections], secrets)


def read_export(text: str) -> transfer.Document:
    try:
        return transfer.read_document(text)
    except transfer.TransferError as exc:
        raise ActionError(str(exc)) from exc


def first_setup() -> bool:
    """No configuration file yet: an import has nothing of the user's own to keep."""
    return not config_path().exists()


def import_config(result: Config) -> Path | None:
    """Save an imported configuration (``transfer.apply_import``), keeping a copy of the previous file; returns it."""
    backup = None
    if config_path().exists():
        backup = config_path().with_name(f"{config_path().name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path(), backup)
    result.save()
    return backup


def save_defaults(cfg: Config, defaults: Defaults) -> None:
    """Replace the defaults, after checking every value. Applying the language is up to the front end; opening
    pdms ui at login is set up (or removed) here."""
    for key in defaults.env:
        if not ENV_NAME.fullmatch(key):
            raise InvalidValue("env", _("'{name}' is not a valid variable name", name=key))
    if defaults.ui_at_login != cfg.defaults.ui_at_login:
        set_ui_at_login(defaults.ui_at_login)
    cfg.defaults = replace(
        defaults,
        language=_one_of("language", defaults.language, LANGUAGES),
        host=_required("host", defaults.host),
        port=_number("port", defaults.port, 1, 65535),
        logging_level=_one_of("logging_level", defaults.logging_level, LOG_LEVELS),
        events=_one_of("events", defaults.events, EVENTS_MODES),
        events_port=_number("events_port", defaults.events_port, 1, 65535),
        db_timeout=_number("db_timeout", defaults.db_timeout, 1, 600),
        proxy_timeout=_number("proxy_timeout", defaults.proxy_timeout, 1, MAX_PROXY_TIMEOUT),
        warm_connections=_number("warm_connections", defaults.warm_connections, 0, MAX_WARM_CONNECTIONS),
        proxy_port=_number("proxy_port", defaults.proxy_port, 1, 65535),
        idle_minutes=_number("idle_minutes", defaults.idle_minutes, 1, MAX_IDLE_MINUTES),
        theme=_one_of("theme", defaults.theme, THEMES),
        update_channel=_one_of("update_channel", defaults.update_channel, UPDATE_CHANNELS),
        email_to=check_email_to(defaults.email_to),
        test_views=dict(defaults.test_views), test_views_db=defaults.test_views_db,
        front_redirect_hosts=check_front_hosts(defaults.front_redirect_hosts),
        env=dict(defaults.env),
    )
    cfg.save()


def set_ui_at_login(enabled: bool) -> None:
    """Add or remove the system's entry that opens pdms ui at login; :class:`InvalidValue` if the system refuses."""
    try:
        desktop.set_autostart(enabled)
    except OSError as exc:
        raise InvalidValue("ui_at_login", _("Could not change the login items: {error}", error=exc)) from exc


def set_language(cfg: Config, lang: str) -> None:
    if lang not in LANGUAGES:
        raise ActionError(_("Unknown language '{lang}'. Available: {codes}", lang=lang, codes=", ".join(LANGUAGES)))
    cfg.defaults.language = lang
    cfg.save()


def set_email_to(cfg: Config, address: str) -> None:
    """Where the emails of the services go from their next start (empty: nowhere, pdms ui shows them)."""
    cfg.defaults.email_to = check_email_to(address)
    cfg.save()


# --------------------------------------------------------------------------- AWS (awsenv.py)


def set_aws_profile(cfg: Config, profile: str) -> None:
    """Use ``profile`` (one of the AWS config's; "" = none: pdms leaves AWS as the terminal has it)."""
    if profile and profile not in (known := awsenv.profiles()):
        raise ActionError(_("Unknown AWS profile '{name}'. In {path}: {names}", name=profile, path=awsenv.config_file(),
                            names=", ".join(known) or "-"))
    cfg.defaults.aws_profile = profile
    cfg.save()


def read_aws(cfg: Config) -> list[awsenv.Change]:
    """Read the Lambdas' buckets with the chosen profile now (see :func:`awsenv.refresh`)."""
    if not cfg.defaults.aws_profile:
        raise ActionError(_("Choose an AWS profile first (pdms aws profile)."))
    try:
        return awsenv.refresh(cfg.defaults.aws_profile)
    except awsenv.AwsError as exc:
        if exc.expired:
            raise ActionError(_("The AWS session of {profile} is over: log in again (pdms aws login).",
                                profile=cfg.defaults.aws_profile)) from exc
        raise ActionError(exc.message) from exc


# --------------------------------------------------------------------------- test copies of materialized views


def _test_view_call(work: Callable):
    try:
        return work()
    except (Decision, ActionError):
        raise  # the user answers (a protected database), or it says what is wrong already
    except testviews.TestViewError as exc:
        raise ActionError(exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - the driver's errors (cannot connect...)
        raise ActionError(str(exc).strip()[:300]) from exc


def test_views(cfg: Config, db_alias: str = "", suffix: str = "") -> dict:
    """The materialized views of the checkout and their test copies (see :mod:`testviews`)."""
    db_alias = require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    repo = current_migrations_repo(cfg)
    return _test_view_call(lambda: testviews.overview(cfg, repo, db_alias, suffix, cfg.defaults.db_timeout))


def create_test_view(cfg: Config, view: str, db_alias: str = "", suffix: str = "", confirmed: bool = False) -> dict:
    """Make the test copy of ``view`` in ``db_alias``; when its user cannot create there, the SQL to run in a database
    client instead (``{"created": False, "sql", "file"}``). A protected database asks first."""
    db_alias = require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    repo = current_migrations_repo(cfg)

    def work() -> dict:
        found = suffix or testviews.ticket_suffix(testviews.branch(repo))
        if not found:
            raise testviews.TestViewError(_("The branch of {repo} names no ticket (PDMP-123): give a suffix.",
                                            repo=repo.name))
        db = cfg.dbs[db_alias]
        made, sql = testviews.test_sql(repo, view, testviews.check_suffix(found), db.user)
        if not testviews.can_create(db, cfg.defaults.db_timeout):
            return {"created": False, "name": made.name, "sql": sql, "file": str(testviews.sql_file(made, sql))}
        check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        return {"created": True, "name": made.name}

    return _test_view_call(work)


def use_test_view(cfg: Config, view: str, name: str, db_alias: str = "") -> None:
    """Services on ``db_alias`` read ``name`` instead of ``view`` from their next start; it must be there already."""
    db_alias = require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))

    def work() -> None:
        db = cfg.dbs[db_alias]
        if name not in testviews.existing(db, [name], cfg.defaults.db_timeout):
            raise testviews.TestViewError(_("{name} is not in {db} yet: create it first (or run its SQL).", name=name,
                                            db=db_alias))
        if not testviews.readable(db, name, cfg.defaults.db_timeout):
            raise testviews.TestViewError(_("{user} cannot read {name}: run in your database client: {sql}",
                                            user=db.user, name=name, sql=testviews.grant_sql(name, db.user)))
        testviews.use(cfg, view, name, db_alias)

    _test_view_call(work)


def drop_test_view(cfg: Config, view: str, name: str, db_alias: str = "", confirmed: bool = False) -> dict:
    """Services read the real ``view`` again, and its test copy goes; when the user cannot drop it, the SQL to run
    (``{"dropped": False, "sql"}``)."""
    db_alias = require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    if not name.startswith(view + "_") or name == view:
        raise ActionError(_("{name} is not a test copy of {view}.", name=name, view=view))
    testviews.stop_using(cfg, view)

    def work() -> dict:
        db = cfg.dbs[db_alias]
        sql = testviews.drop_sql(name)
        there = testviews.existing(db, [name], cfg.defaults.db_timeout)
        if name not in there:
            testviews.forget(name)
            return {"dropped": True, "name": name}
        if there[name] != db.user:  # only its owner drops it (made from a database client, as root...)
            return {"dropped": False, "name": name, "sql": sql}
        check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        testviews.forget(name)
        return {"dropped": True, "name": name}

    return _test_view_call(work)


def refresh_test_view(cfg: Config, view: str, name: str, db_alias: str = "", confirmed: bool = False) -> dict:
    """Recompute the data of a test copy (a snapshot of when it was made; nothing else refreshes it). Only its owner
    can: anyone else gets the SQL to run (``{"refreshed": False, "sql"}``)."""
    db_alias = require(cfg.dbs, _("database"), db_alias or testviews.database_of(cfg))
    if not name.startswith(view + "_") or name == view:
        raise ActionError(_("{name} is not a test copy of {view}.", name=name, view=view))

    def work() -> dict:
        db = cfg.dbs[db_alias]
        sql = testviews.refresh_sql(name)
        there = testviews.existing(db, [name], cfg.defaults.db_timeout)
        if name not in there:
            raise testviews.TestViewError(_("{name} is not in {db}.", name=name, db=db_alias))
        if there[name] != db.user:
            return {"refreshed": False, "name": name, "sql": sql}
        check_database(cfg, db_alias, confirmed)
        testviews.run(db, sql, cfg.defaults.db_timeout)
        return {"refreshed": True, "name": name}

    return _test_view_call(work)



@dataclass
class TestViewNeed:
    """A view a service about to start reads, changed in the checkout: its test copy in ``db_alias`` and what it
    takes (``exists``: there already; ``current``: made with the SQL your file has now)."""

    view: str
    name: str
    db_alias: str
    services: list[str]
    exists: bool
    current: bool
    can_create: bool
    uncommitted: bool
    sql: str

    @property
    def ready(self) -> bool:
        return self.exists and self.current

    def describe(self) -> dict:
        return {"view": self.view, "name": self.name, "db": self.db_alias, "services": self.services,
                "exists": self.exists, "current": self.current, "can_create": self.can_create,
                "uncommitted": self.uncommitted, "sql": "" if self.ready or self.can_create else self.sql}


def test_view_needs(cfg: Config, launches: Iterable[tuple[Path, str]]) -> list[TestViewNeed]:
    """The views the services of ``launches`` (``(service, db alias)``) read that your checkout changed, on shared
    databases (a local one is migrated instead). Quiet when it cannot tell (no migrations repo, a database down)."""
    by_db: dict[str, list[Path]] = {}
    for service, alias in launches:
        db = cfg.dbs.get(alias)
        if db is not None and not migrations.is_local(db):
            by_db.setdefault(alias, []).append(service)
    if not by_db:
        return []
    try:
        repo = current_migrations_repo(cfg)
    except ActionError:
        return []
    suffix = testviews.ticket_suffix(testviews.branch(repo))
    needs = []
    for alias, services in by_db.items():
        db = cfg.dbs[alias]
        try:
            changed = testviews.modified(repo, db, cfg.defaults.db_timeout)
            if not changed or not suffix:
                continue
            backend = repos.active_backend(cfg)
            readers: dict[str, list[str]] = {}
            for service in dict.fromkeys(services):
                read = testviews.reads(service, backend) if backend else set()
                for view in testviews.narrowed(service.name, read):
                    if view in changed:
                        readers.setdefault(view, []).append(service.name)
            if not readers:
                continue
            copies = {view: testviews.test_sql(repo, view, suffix, db.user) for view in readers}
            there = testviews.existing(db, [made.name for made, _sql in copies.values()], cfg.defaults.db_timeout)
            can = testviews.can_create(db, cfg.defaults.db_timeout)
        except Exception:  # noqa: BLE001 - a database down, an odd file: the services start on the real views
            continue
        for view, (made, sql) in sorted(copies.items()):
            needs.append(TestViewNeed(view, made.name, alias, readers[view], made.name in there,
                                      testviews.made_with(made.name, sql), can, changed[view], sql))
    return needs


def check_test_views(cfg: Config, launches: Iterable[tuple[Path, str]], views: bool | None) -> list[TestViewNeed]:
    """What to prepare before the services start: :class:`TestViewsPending` for the user to answer when ``views``
    is None, the needs when True (a copy they cannot create must be there by now: they ran its SQL), none when False
    (and the services read the real views)."""
    needs = test_view_needs(cfg, launches)
    if not needs:
        return []
    if views is None:
        raise TestViewsPending(needs)
    if views is False:
        for need in needs:
            testviews.stop_using(cfg, need.view)
        return []
    missing = [need.name for need in needs if not need.can_create and not need.exists]
    if missing:
        raise ActionError(_("{names} is not in the database yet: run its SQL first.", names=", ".join(missing)))
    for need in needs:
        db = cfg.dbs[need.db_alias]
        if need.exists and not _test_view_call(lambda need=need, db=db: testviews.readable(db, need.name)):
            raise ActionError(_("{user} cannot read {name}: run in your database client: {sql}", user=db.user,
                                name=need.name, sql=testviews.grant_sql(need.name, db.user)))
    return needs


def prepare_test_views(cfg: Config, needs: list[TestViewNeed], output: IO[str] | None = None) -> None:
    """Make the copies that are missing or older than your SQL (when the user can), remember with which SQL, and
    have the services read them from their start."""
    for need in needs:
        if not need.ready and need.can_create:
            if output:
                output.write(_("Making {name} in {db}...", name=need.name, db=need.db_alias) + "\n")
                output.flush()
            _test_view_call(lambda need=need: testviews.run(cfg.dbs[need.db_alias], need.sql, 3600))
        testviews.remember(need.name, need.db_alias, need.sql)  # made now, or the user ran this SQL
        _test_view_call(lambda need=need: testviews.use(cfg, need.view, need.name, need.db_alias))
