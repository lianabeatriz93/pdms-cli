"""What pdms does with services, stacks, the proxy, the local events and the configuration (databases, users,
defaults), without prompting or printing: shared by the CLI and ``pdms ui``.

An action never asks. When it needs a decision it raises a :class:`Decision` (a busy port, a protected database,
the local ElasticMQ not running, pointing the frontend to the proxy); each front end answers it its own way (a questionary prompt, a dialog) and calls
the action again with the answer. Problems no answer can fix raise :class:`ActionError` with a message for the user.
"""

from __future__ import annotations

import json
import os
import subprocess
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO

from . import events, installer, instances, proxy, repos, routes, runner, userimport
from .config import EVENTS_MODES, LOG_LEVELS, Config, Database, Defaults, DevUser, Stack
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
    events_ready: EventsSetup | None = None,
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
        cmd = runner.uvicorn_command(host, port, reload)
    return ServiceLaunch(
        service, user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], host, port, reload, setup, cmd,
        queue=consumer[0].name if consumer else "",
    )


def install_wanted(cfg: Config, install: bool | None) -> bool:
    """Whether to install at all: True forces it, False skips it, None follows the settings."""
    return install is True or (install is None and cfg.defaults.install)


def needs_install(cfg: Config, service: Path, install: bool | None) -> bool:
    """Whether to run ``poetry lock && poetry install`` now; by default (smart) only if something changed."""
    if not install_wanted(cfg, install):
        return False
    return not (install is None and cfg.defaults.smart_install and runner.poetry_python(service)
                and installer.is_up_to_date(service))


def install_service(service: Path, output: IO[str] | None = None) -> None:
    """Install the service's dependencies (see :func:`runner.install`) and remember what got installed."""
    try:
        runner.ensure_poetry()
        runner.install(service, output)
    except RuntimeError as exc:
        raise ActionError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise ActionError(_("{cmd} failed (exit code {code}).", cmd=" ".join(map(str, exc.cmd)),
                            code=exc.returncode)) from exc
    installer.remember(service)


def installed_parts(service: Path) -> dict[str, str] | None:
    """What is installed in the service's virtualenv, when pdms knows it matches the code (else unknown)."""
    return installer.dependency_fingerprints(service) if installer.is_up_to_date(service) else None


def service_env(cfg: Config, launch: ServiceLaunch) -> dict[str, str]:
    extra = dict(launch.events.env)
    if launch.events.kind == "local" and (found := repo_event_map(cfg, launch.service)):
        extra.update(events.topic_env(*found))  # its own topics; the rest of the setup may be a whole stack's
    return runner.build_env(cfg.defaults, launch.user, launch.db, extra)


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


def running_by_service() -> dict[str, list[int]]:
    """Ports of the live instances of each service (0 for SQS consumers)."""
    result: dict[str, list[int]] = {}
    for inst in instances.load().values():
        if inst.alive():
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
    launches = []
    for path in pending:
        if not consumers[path]:
            port = runner.next_free_port(host, port, taken)
            taken.add(port)
        launches.append(plan_service(
            cfg, path, user_name=user_name, db_name=db_name, port=port, host=host, events_ready=setup,
        ))
    return StackLaunch(name, user_name, db_name, launches, running, setup)


def start_stack(
    cfg: Config, plan: StackLaunch, install: Callable[[Path], None] | None = None
) -> list[instances.Instance]:
    """Start the planned services in the background; waiting until they respond is up to the front end."""
    return [start_service(cfg, launch, install) for launch in plan.services]


def stack_instances(cfg: Config, name: str, root: Path) -> list[instances.Instance]:
    """Live instances of the stack's services."""
    paths = {str(p) for p in stack_paths(cfg.stacks[require(cfg.stacks, _("stack"), name)], root)}
    return [i for i in instances.load().values() if i.service in paths and i.alive()]


def save_stack(cfg: Config, name: str, stack: Stack) -> None:
    if not stack.services:
        raise ActionError(_("A stack needs at least one service."))
    if stack.user:
        require(cfg.users, _("user"), stack.user)
    if stack.db:
        require(cfg.dbs, _("database"), stack.db)
    cfg.stacks[name] = stack
    cfg.save()


def remove_stack(cfg: Config, name: str) -> None:
    del cfg.stacks[require(cfg.stacks, _("stack"), name)]
    cfg.save()


# --------------------------------------------------------------------------- proxy

PROXY_HOST = "0.0.0.0"


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
) -> ProxyLaunch:
    """Decide how the proxy runs. Raises :class:`PortBusy` or :class:`PointFrontend` for the user to answer."""
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
    )


def point_frontend(plan: ProxyLaunch) -> str | None:
    """Point ``frontend/.env.local`` to the proxy if the plan says so; the changed file (undone when it stops)."""
    if plan.frontend and (change := repos.point_frontend_to(plan.root, plan.url)):
        proxy.remember_frontend_change(change)
        return change["path"]
    return None


def serve_proxy(plan: ProxyLaunch, log: Callable[[str, str, int, str, float], None]) -> None:
    """Run the proxy in this process until it is interrupted; ``log`` gets every request."""
    gateway = proxy.Gateway(
        routes=plan.routes, backend=plan.root / "backend", remote=plan.remote, impersonate=plan.user, log=log,
    )
    proxy.serve(gateway, PROXY_HOST, plan.port, {
        "repo": str(plan.root), "env": plan.env, "remote": plan.remote or "", "as": plan.user_name or "",
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
        plan.root, port=plan.port, env=plan.env, remote=plan.remote, user_name=plan.user_name,
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


def stop_proxy(running: dict | None = None) -> str | None:
    """Stop the running proxy (if any) and put ``frontend/.env.local`` back; the restored file, if one was."""
    running = running or proxy.running_proxy()
    if running:
        proxy.stop(running)
    return proxy.restore_frontend_change()


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


def remove_db(cfg: Config, name: str) -> list[str]:
    """Delete a database; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.dbs[require(cfg.dbs, _("database"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.db == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].db = ""
    if cfg.last_db == name:
        cfg.last_db = ""
    cfg.save()
    return stacks


def check_connection(db: Database, timeout: int) -> str:
    """The server version, e.g. ``PostgreSQL 16.4``; :class:`ActionError` with the driver's message otherwise."""
    try:
        return runner.test_connection(db, timeout).split(",")[0]
    except Exception as exc:  # noqa: BLE001 - any driver error is the answer
        raise ActionError(str(exc).strip() or type(exc).__name__) from exc


def save_user(cfg: Config, name: str, user: DevUser, new: bool = False) -> str:
    """Create (``new``) or replace the development user ``name``; returns its alias."""
    name = _alias(cfg.users, _("user"), name, new)
    cfg.users[name] = DevUser(
        user_id=_required("user_id", user.user_id), username=_required("username", user.username),
        first_name=user.first_name.strip(), last_name=user.last_name.strip(), roles=user.roles.strip(),
    )
    cfg.save()
    return name


def remove_user(cfg: Config, name: str) -> list[str]:
    """Delete a user; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.users[require(cfg.users, _("user"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.user == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].user = ""
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


def save_defaults(cfg: Config, defaults: Defaults) -> None:
    """Replace the defaults, after checking every value. Applying the language is up to the front end."""
    for key in defaults.env:
        if not ENV_NAME.fullmatch(key):
            raise InvalidValue("env", _("'{name}' is not a valid variable name", name=key))
    cfg.defaults = replace(
        defaults,
        language=_one_of("language", defaults.language, LANGUAGES),
        host=_required("host", defaults.host),
        port=_number("port", defaults.port, 1, 65535),
        logging_level=_one_of("logging_level", defaults.logging_level, LOG_LEVELS),
        events=_one_of("events", defaults.events, EVENTS_MODES),
        events_port=_number("events_port", defaults.events_port, 1, 65535),
        db_timeout=_number("db_timeout", defaults.db_timeout, 1, 600),
        env=dict(defaults.env),
    )
    cfg.save()


def set_language(cfg: Config, lang: str) -> None:
    if lang not in LANGUAGES:
        raise ActionError(_("Unknown language '{lang}'. Available: {codes}", lang=lang, codes=", ".join(LANGUAGES)))
    cfg.defaults.language = lang
    cfg.save()
