"""What pdms does with services, stacks, the proxy, the local events and the configuration (repos, databases, users,
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
import shutil
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit

from . import desktop, events, frontend, installer, instances, migrations, proxy, repos, routes, runner, transfer, userimport
from .config import EVENTS_MODES, LOG_LEVELS, Config, Database, Defaults, DevUser, Stack, config_path
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
    if cfg.setup.stack == name:
        cfg.setup.stack = ""
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


def serve_proxy(plan: ProxyLaunch, log: Callable[[str, str, int, str, float], None]) -> None:
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


def install_frontend(root: Path, output: IO[str] | None = None, log: Path | None = None) -> None:
    """``yarn install`` in ``frontend/``; a failure that looks like CodeArtifact says how to log in."""
    try:
        _yarn(frontend.install_command(), frontend.folder(root), output)
    except (OSError, subprocess.CalledProcessError) as exc:
        if log and frontend.needs_codeartifact(log):
            raise ActionError(_("yarn install could not download the @alivi packages from AWS CodeArtifact. Log in "
                                "with ./codeartifact-login.sh (in frontend/) and try again.")) from exc
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
        proxy_port=_number("proxy_port", defaults.proxy_port, 1, 65535),
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
