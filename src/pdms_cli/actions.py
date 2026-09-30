"""What pdms does with services, without prompting or printing: shared by the CLI and ``pdms ui``.

An action never asks. When it needs a decision it raises a :class:`Decision` (a busy port, a protected database,
the local ElasticMQ not running); each front end answers it its own way (a questionary prompt, a dialog) and calls
the action again with the answer. Problems no answer can fix raise :class:`ActionError` with a message for the user.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import events, installer, instances, repos, runner
from .config import Config, Database, DevUser
from .i18n import _


class ActionError(Exception):
    """A problem the user has to fix (unknown user, bad option...); ``message`` is ready to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


def consumer_of(cfg: Config, service: Path) -> tuple[events.Queue, events.Consumer] | None:
    """The queue and handler when ``service`` is an SQS consumer (event Lambda) of its repo."""
    root = repos.find_repo_root(service) or repos.active_root(cfg)
    if not root:
        return None
    try:
        relative = service.resolve().relative_to((root / "backend").resolve()).as_posix()
    except ValueError:
        return None
    event_map = events.load_event_map(root)
    found = event_map.queue_of_service(relative)
    if not found:
        return None
    queue_name, consumer = found
    return event_map.queues.get(queue_name) or events.Queue(queue_name), consumer


# --------------------------------------------------------------------------- services


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
) -> ServiceLaunch:
    """Decide how ``service`` runs. Raises :class:`PortBusy` or :class:`LocalEventsDown` for the user to answer."""
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
    setup = events_setup(cfg, service, events_mode)
    if consumer:
        queue, handler = consumer
        cmd = events.poller_command(runner.poetry(), queue, handler, cfg.defaults.events_port)
    else:
        cmd = runner.uvicorn_command(host, port, reload)
    return ServiceLaunch(
        service, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], host, port, reload, setup, cmd,
        queue=consumer[0].name if consumer else "",
    )


def installed_parts(service: Path) -> dict[str, str] | None:
    """What is installed in the service's virtualenv, when pdms knows it matches the code (else unknown)."""
    return installer.dependency_fingerprints(service) if installer.is_up_to_date(service) else None


def service_env(cfg: Config, launch: ServiceLaunch) -> dict[str, str]:
    return runner.build_env(cfg.defaults, launch.user, launch.db, launch.events.env)


def start_service(
    cfg: Config, launch: ServiceLaunch, install: Callable[[Path], None] | None = None
) -> instances.Instance:
    """Start the service in the background. ``install`` (the front end's, since it shows progress) runs first."""
    if install:
        install(launch.service)
    return instances.start(
        launch.service, launch.cmd, service_env(cfg, launch), host=launch.host, port=launch.port,
        user=launch.user_name, db=launch.db_name, reload=launch.reload, deps=installed_parts(launch.service),
        events=launch.events.kind, queue=launch.queue,
    )


def exec_service(cfg: Config, launch: ServiceLaunch) -> None:
    """Run the service in the foreground, replacing pdms (see :func:`runner.exec_server`)."""
    runner.exec_server(launch.service, launch.cmd, service_env(cfg, launch))


def stop_service(instance: instances.Instance) -> None:
    instances.stop(instance)


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
    )
    return start_service(cfg, launch, install)
