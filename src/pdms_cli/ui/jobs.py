"""What ``pdms ui`` does to services: the same actions as the CLI, run in a thread so the page stays live.

A job belongs to one instance (or the proxy) and shows in the state while it runs, with its phase; a failed one
stays there with its error until the next action on that instance or until it is dismissed. Everything that needs
an answer (a protected database, the local ElasticMQ) is checked before the job starts, so the page can ask first.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from .. import actions, events, frontend, i18n, instances, proxy, repos, routes, runner, transfer, userimport
from ..config import EVENTS_MODES, LOG_LEVELS, Config, Database, Defaults, DevUser, Setup, Stack, config_path
from ..i18n import _

PROXY_PORT = 8000  # pdms proxy's --port default
EVENTS_KEY = "events:elasticmq"  # the job of pdms events up/down (not an instance: no row of its own in Services)
HOME_KEY = "home"  # the job of Home's Start everything / Stop everything


def install_log(key: str) -> Path:
    """Where the install a restart runs writes its output (the service's own log is rotated when it starts)."""
    return instances.log_path(key).with_suffix(".install.log")


@dataclass
class Job:
    key: str
    action: str  # start | stop | restart | up | down
    phase: str  # stopping | installing | starting, with the service for a stack ("installing lead-tp-list")
    started: float
    error: str = ""
    installed: bool = False  # the install log belongs to this job
    log_key: str = ""  # the instance whose install log it wrote last (a stack installs several)

    @property
    def running(self) -> bool:
        return not self.error


class Jobs:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {key: asdict(job) for key, job in self._jobs.items()}

    def busy(self, key: str) -> bool:
        with self._lock:
            job = self._jobs.get(key)
            return bool(job and job.running)

    def dismiss(self, key: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job and not job.running:
                del self._jobs[key]
        self.on_change()

    def phase(self, job: Job, phase: str, **changes: object) -> None:
        with self._lock:
            job.phase = phase
            for name, value in changes.items():
                setattr(job, name, value)
        self.on_change()

    def run(self, key: str, action: str, phase: str, work: Callable[[Job], object]) -> Job:
        """Start ``work`` in a thread; :class:`actions.ActionError` if something is already running on ``key``."""
        with self._lock:
            current = self._jobs.get(key)
            if current and current.running:
                raise actions.ActionError(_("{key} is busy ({phase}).", key=key, phase=current.phase))
            job = self._jobs[key] = Job(key, action, phase, time.time())
        threading.Thread(target=self._work, args=(job, work), name=f"pdms-ui-{action}", daemon=True).start()
        self.on_change()
        return job

    def _work(self, job: Job, work: Callable[[Job], object]) -> None:
        try:
            work(job)
        except actions.ActionError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 - shown on the page instead of lost in a thread
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = ""
        with self._lock:
            if error:
                job.error = error
            elif self._jobs.get(job.key) is job:
                del self._jobs[job.key]  # done: the instance's own health takes over
        self.on_change()

    # ------------------------------------------------------------------ services

    def stop(self, key: str) -> Job:
        if frontend.is_key(key):
            current = frontend.running()
            if not current:
                raise actions.ActionError(_("The frontend is not running."))
            return self.run(frontend.KEY, "stop", "stopping", lambda _job: actions.stop_frontend(current))
        if proxy.is_key(key):
            running = proxy.running_proxy()
            if not running:
                raise actions.ActionError(_("The proxy is not running."))
            return self.run(proxy.display_key(running), "stop", "stopping", lambda _job: actions.stop_proxy(running))
        inst = find(key)
        return self.run(inst.key, "stop", "stopping", lambda _job: actions.stop_service(inst))

    def restart(
        self, key: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job:
        """Restart on the same port; raises the decisions before stopping anything, like ``pdms restart``."""
        inst = find(key)
        cfg = Config.load()
        user, db = user or inst.user, db or inst.db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        if db != inst.db:
            actions.check_database(cfg, db, confirmed)
        if (inst.events == "local" or inst.is_consumer) and not events.running(cfg.defaults.events_port):
            raise actions.LocalEventsDown(cfg.defaults.events_port)
        if self.busy(inst.key):
            raise actions.ActionError(_("{key} is busy.", key=inst.key))
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            def install_step(service: Path) -> None:
                self.install(cfg, job, service, inst.key, install)
                self.phase(job, "starting")

            actions.restart_service(cfg, inst, user_name=user, db_name=db, confirmed=True, install=install_step)

        return self.run(inst.key, "restart", "stopping", work)

    def start(
        self, service: str, *, port: int | None = None, user: str | None = None, db: str | None = None,
        install: bool | None = None, confirmed: bool = False,
    ) -> Job:
        """Start one service of the current repo in the background, like ``pdms run -b``. Without ``port`` it takes
        the next free one; a busy ``port``, a protected database or a stopped local ElasticMQ (for a consumer) are
        raised before anything starts."""
        cfg = Config.load()
        root, known = repo_services(cfg)
        if service not in known:
            raise actions.ActionError(_("Not services of the current repo: {names}", names=service))
        path = root / service
        user, db = user or cfg.last_user, db or cfg.last_db
        actions.require(cfg.users, _("user"), user)
        actions.require(cfg.dbs, _("database"), db)
        launch = actions.plan_service(
            cfg, path, user_name=user, db_name=db, port=port or actions.suggested_port(cfg, cfg.defaults.host),
        )
        actions.check_database(cfg, db, confirmed)
        key = instances.make_key(launch.service, launch.port)
        if self.busy(key):
            raise actions.ActionError(_("{key} is busy.", key=key))
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.install(cfg, job, launch.service, key, install)
            self.phase(job, "starting")
            actions.start_service(cfg, launch)

        return self.run(key, "start", "starting", work)

    def install(self, cfg: Config, job: Job, service: Path, key: str, install: bool | None, label: str = "") -> None:
        """Install ``service`` if needed, writing the output to the install log of instance ``key``."""
        if not actions.needs_install(cfg, service, install):
            return
        log = install_log(key)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.phase(job, f"installing {label}".strip(), installed=True, log_key=key)
        with open(log, "w", encoding="utf-8", errors="replace") as output:
            actions.install_service(service, output)

    # ------------------------------------------------------------------ stacks

    def up(
        self, name: str, *, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job | None:
        """Start what is not running of the stack, like ``pdms up``; None when all of it already runs."""
        cfg = Config.load()
        stack = cfg.stacks[actions.require(cfg.stacks, _("stack"), name)]
        root = backend(cfg)
        actions.stack_paths(stack, root)  # a stale stack fails before anything else
        user, db = user or stack.user or cfg.last_user, db or stack.db or cfg.last_db
        plan = actions.plan_stack(cfg, name, root, user_name=user, db_name=db)
        if not plan.services:
            return None
        actions.check_database(cfg, db, confirmed)
        actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            for launch in plan.services:
                key = instances.make_key(launch.service, launch.port)
                self.install(cfg, job, launch.service, key, install, launch.service.name)
                self.phase(job, f"starting {launch.service.name}")
                actions.start_service(cfg, launch)

        return self.run(stack_key(name), "up", "starting", work)

    def down(self, name: str) -> Job | None:
        """Stop the stack's running services, like ``pdms down``; None when nothing of it runs."""
        cfg = Config.load()
        targets = actions.stack_instances(cfg, name, backend(cfg))
        if not targets:
            return None

        def work(job: Job) -> None:
            for inst in targets:
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)

        return self.run(stack_key(name), "down", "stopping", work)

    # ------------------------------------------------------------------ proxy

    def start_proxy(
        self, *, port: int, env: str = "dev", remote: str | None = None, no_remote: bool = False,
        user: str | None = None, frontend: bool | None = None,
    ) -> Job:
        """Start the proxy in the background, like ``pdms proxy -b``; raises the decisions (busy port, pointing the
        frontend to it) before anything starts. The job ends once the proxy answers."""
        if self.busy(proxy.KEY):
            raise actions.ActionError(_("{key} is busy.", key=proxy.KEY))
        actions.clear_proxy_leftovers()
        cfg = Config.load()
        root = repo_root(cfg)
        repo_routes = actions.proxy_routes(root, env)
        target, _detected = actions.proxy_remote(cfg, root, remote, no_remote)
        plan = actions.plan_proxy(
            cfg, root, repo_routes, port=port, env=env, remote=target, user_name=user, frontend=frontend,
        )

        def work(_job: Job) -> None:
            actions.point_frontend(plan)
            started = actions.start_proxy(plan)
            if actions.wait_for_proxy(started) == "stopped":
                actions.stop_proxy()
                last = instances.tail(str(started.log), 1).strip()
                raise actions.ActionError(_("The proxy exited while starting: {line}", line=last or "?"))

        return self.run(proxy.KEY, "start", "starting", work)

    # ------------------------------------------------------------------ frontend

    def start_frontend(
        self, *, mode: str = "dev", port: int | None = None, install: bool | None = None, rebuild: bool = False,
        restart: bool = False,
    ) -> Job:
        """Start the web app like ``pdms front -b``: yarn install when needed, the build in build mode, then the
        server; a busy port is raised before anything starts. The job ends once it answers. With ``restart`` the
        running one stops first, on the same port (Rebuild)."""
        if self.busy(frontend.KEY):
            raise actions.ActionError(_("{key} is busy.", key=frontend.KEY))
        if restart and (current := frontend.running()):
            port = port or int(current["port"])
            actions.stop_frontend(current)
        plan = actions.plan_frontend(Config.load(), mode=mode, port=port, install=install, rebuild=rebuild)
        return self.run(frontend.KEY, "start", "starting", lambda job: self.frontend_steps(job, plan))

    def frontend_steps(self, job: Job, plan: actions.FrontendLaunch) -> None:
        """Install, build and start the frontend, writing the install and build output to their own logs."""
        if plan.install:
            self.phase(job, "installing frontend", installed=True, log_key=frontend.KEY)
            log = frontend.install_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.install_frontend(plan.root, output, log)
        if plan.mode == "build" and (plan.build or frontend.build_needed(plan.root)):
            self.phase(job, "building frontend", log_key=frontend.KEY)
            log = frontend.build_log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w", encoding="utf-8", errors="replace") as output:
                actions.build_frontend(plan.root, output)
        self.phase(job, "starting frontend")
        started = actions.start_frontend(plan)
        if actions.wait_for_frontend(started) == "stopped":
            raise actions.ActionError(_("The frontend exited while starting: {line}",
                                        line=frontend.last_line(frontend.log_path()) or "?"))

    # ------------------------------------------------------------------ Home

    def start_all(self, *, install: bool | None = None, confirmed: bool = False) -> Job | None:
        """Start what is not running of the saved setup, in order: the local events, the stack, the proxy (pointing
        the frontend to it) and the frontend. A protected database or a busy frontend port are raised first; the
        proxy takes the next free port by itself. None when everything already runs."""
        if self.busy(HOME_KEY):
            raise actions.ActionError(_("{key} is busy.", key=HOME_KEY))
        cfg = Config.load()
        setup = cfg.setup
        want_events = setup.events and not events.is_up(cfg.defaults.events_port)
        event_map = actions.load_events(cfg)[1] if want_events else None

        stack_name = ""
        user, db = cfg.last_user, cfg.last_db
        if setup.stack:
            stack = cfg.stacks[actions.require(cfg.stacks, _("stack"), setup.stack)]
            root = backend(cfg)
            live = actions.running_by_service()
            if [path for path in actions.stack_paths(stack, root) if str(path) not in live]:
                stack_name = setup.stack
                user, db = stack.user or cfg.last_user, stack.db or cfg.last_db
                actions.require(cfg.users, _("user"), user)
                actions.require(cfg.dbs, _("database"), db)
                actions.check_database(cfg, db, confirmed)

        repo = repo_root(cfg)
        broker = None
        if event_map is not None and (service := actions.broker_service(repo, event_map)) and user and db \
                and not actions.event_consumers(event_map.broker_queue):
            actions.require(cfg.users, _("user"), user)
            actions.require(cfg.dbs, _("database"), db)
            actions.check_database(cfg, db, confirmed)
            broker = service
        want_frontend = setup.frontend and frontend.exists(repo) and not frontend.running()
        proxy_plan = None
        if setup.proxy and not proxy.running_proxy() and not self.busy(proxy.KEY):
            actions.clear_proxy_leftovers()
            env = "dev"
            target, _detected = actions.proxy_remote(cfg, repo, None, False)
            plan_args = dict(env=env, remote=target, frontend=frontend.exists(repo) or None)
            try:
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=PROXY_PORT, **plan_args)
            except actions.PortBusy as busy:
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=busy.free, **plan_args)
        frontend_plan = None
        if want_frontend and not self.busy(frontend.KEY):
            frontend_plan = actions.plan_frontend(cfg, mode=setup.frontend_mode, install=install)
        if event_map is None and not stack_name and proxy_plan is None and frontend_plan is None:
            return None
        if stack_name or broker:
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            if event_map is not None:
                self.phase(job, "starting ElasticMQ")
                actions.start_events(cfg, event_map.queues)
            if broker is not None:
                launch = actions.plan_service(cfg, broker, user_name=user, db_name=db, events_mode="local")
                self.install(cfg, job, broker, instances.make_key(launch.service, launch.port), install, broker.name)
                self.phase(job, f"starting {broker.name}")
                actions.start_service(cfg, launch)
            if stack_name:
                plan = actions.plan_stack(cfg, stack_name, backend(cfg), user_name=user, db_name=db)
                for launch in plan.services:
                    key = instances.make_key(launch.service, launch.port)
                    self.install(cfg, job, launch.service, key, install, launch.service.name)
                    self.phase(job, f"starting {launch.service.name}")
                    actions.start_service(cfg, launch)
            if proxy_plan is not None:
                self.phase(job, "starting proxy")
                actions.point_frontend(proxy_plan)
                started = actions.start_proxy(proxy_plan)
                if actions.wait_for_proxy(started) == "stopped":
                    actions.stop_proxy()
                    raise actions.ActionError(_("The proxy exited while starting: {line}",
                                                line=instances.tail(str(started.log), 1).strip() or "?"))
            if frontend_plan is not None:
                self.frontend_steps(job, frontend_plan)

        return self.run(HOME_KEY, "up", "starting", work)

    def stop_all(self) -> Job | None:
        """Stop the frontend, the proxy, every background service and the local ElasticMQ; None when nothing runs."""
        current_frontend, running_proxy = frontend.running(), proxy.running_proxy()
        live = [inst for inst in instances.load().values() if inst.alive()]
        container = events.container_state()
        if not (current_frontend or running_proxy or live or (container and container["running"])):
            return None

        def work(job: Job) -> None:
            if current_frontend:
                self.phase(job, "stopping frontend")
                actions.stop_frontend(current_frontend)
            if running_proxy:
                self.phase(job, "stopping proxy")
                actions.stop_proxy(running_proxy)
            for inst in live:
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
            if container and container["running"]:
                self.phase(job, "stopping ElasticMQ")
                events.stop()

        return self.run(HOME_KEY, "down", "stopping", work)

    # ------------------------------------------------------------------ local events

    def events_up(
        self, *, broker: bool = True, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False,
    ) -> Job:
        """Start the local ElasticMQ with every queue of the repo and, with ``broker``, the broker, like
        ``pdms events up``; a protected database for the broker is asked before anything starts."""
        cfg = Config.load()
        root, event_map = actions.load_events(cfg)
        service = actions.broker_service(root, event_map) if broker else None
        if service and actions.event_consumers(event_map.broker_queue):
            service = None  # already running
        if service:
            user, db = user or cfg.last_user, db or cfg.last_db
            if not user or not db:
                raise actions.ActionError(_("Configure a user and a database to run the broker (pdms user add, "
                                            "pdms db add), or start the events without it."))
            actions.require(cfg.users, _("user"), user)
            actions.require(cfg.dbs, _("database"), db)
            actions.check_database(cfg, db, confirmed)
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            actions.start_events(cfg, event_map.queues)
            if service and user and db:
                launch = actions.plan_service(cfg, service, user_name=user, db_name=db, events_mode="local")
                key = instances.make_key(launch.service, launch.port)
                self.install(cfg, job, service, key, install, service.name)
                self.phase(job, f"starting {service.name}")
                actions.start_service(cfg, launch)

        return self.run(EVENTS_KEY, "up", "starting ElasticMQ", work)

    def events_down(self) -> Job:
        """Stop the SQS consumers and the local ElasticMQ (its messages are lost), like ``pdms events down``."""

        def work(job: Job) -> None:
            for inst in actions.event_consumers():
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
            self.phase(job, "stopping ElasticMQ")
            events.stop()

        return self.run(EVENTS_KEY, "down", "stopping", work)


def events_queues(cfg: Config) -> dict:
    """Every queue of the repo (and any other ElasticMQ has) with its messages, consumer and the event types the
    broker routes to it, like ``pdms events status --all``."""
    root, event_map = actions.load_events(cfg)
    port = cfg.defaults.events_port
    up = events.is_up(port)
    counts = events.queue_counts(port) if up else {}
    running = {i.queue: i.key for i in actions.event_consumers()}
    types: dict[str, int] = {}
    for queue in event_map.routes.values():
        types[queue] = types.get(queue, 0) + 1
    names = sorted(set(event_map.queues) | set(counts))
    queues = []
    for name in names:
        queue = event_map.queues.get(name) or events.Queue(name, fifo=name.endswith(".fifo"), source="elasticmq")
        if name == events.SNS_QUEUE:
            queue = events.Queue(name, fifo=False, source="pdms")
        consumer = event_map.consumers.get(name)
        count = counts.get(name)
        queues.append({
            "name": name, "fifo": queue.fifo, "source": queue.source, "types": types.get(name, 0),
            "visible": count["visible"] if count else None, "in_flight": count["in_flight"] if count else None,
            "consumer": consumer.service if consumer else "", "running": running.get(name, ""),
            "broker": name == event_map.broker_queue, "sns": name == events.SNS_QUEUE,
        })
    publishers = [i.key for i in instances.load().values() if i.alive() and i.events == "local"]
    return {"up": up, "endpoint": events.endpoint(port), "queues": queues, "publishers": publishers,
            "broker": event_map.broker_queue, "broker_service": bool(actions.broker_service(root, event_map))}


def events_map(cfg: Config) -> dict:
    """Every event type, the queue the broker sends it to and its consumer, like ``pdms events map``."""
    _root, event_map = actions.load_events(cfg)
    return {
        "broker": event_map.broker_queue,
        "types": [
            {"type": event_type, "queue": queue,
             "consumer": consumer.service if (consumer := event_map.consumers.get(queue)) else ""}
            for event_type, queue in sorted(event_map.routes.items())
        ],
    }


def events_template(cfg: Config, event_type: str) -> dict:
    root, event_map = actions.load_events(cfg)
    if event_type not in event_map.routes:
        raise actions.ActionError(_("'{target}' is not an event type nor a queue.", target=event_type))
    fields = events.event_template(root, event_type)
    if fields is None:
        raise actions.ActionError(_("No event class found for '{target}' in backend/common/event.", target=event_type))
    return fields


def events_peek(cfg: Config, queue: str, limit: int) -> list[dict]:
    """The messages waiting in ``queue``, without consuming them (``pdms events peek``)."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    if queue not in events.queue_counts(port):
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=queue))
    found = []
    for message in events.peek(port, queue, limit):
        attributes = message.get("Attributes", {})
        sent = attributes.get("SentTimestamp", "")
        found.append({
            "id": message.get("MessageId", ""), "body": message.get("Body", ""),
            "receives": int(attributes.get("ApproximateReceiveCount", 0) or 0),
            "sent": int(sent) if sent.isdigit() else None, "group": attributes.get("MessageGroupId", ""),
        })
    return found


def events_send(cfg: Config, target: str, body: str | None, direct: bool) -> dict:
    """Send an event (through the broker unless ``direct``) or a message to a queue, like ``pdms events send``."""
    _root, event_map = actions.load_events(cfg)
    queue, message = actions.plan_send(event_map, target, body, direct)
    message_id = actions.send_message(cfg, event_map, queue, message)
    routed = event_map.routes.get(target, "") if queue == event_map.broker_queue and target != queue else ""
    consumer = event_map.consumers.get(routed or queue)
    return {"id": message_id, "queue": queue, "routed_to": routed, "consumer": consumer.service if consumer else "",
            "consumed": bool(actions.event_consumers(queue))}


def events_purge(cfg: Config, queues: list[str]) -> list[str]:
    """Delete every message of ``queues`` (all of them that have messages when empty), like ``pdms events purge``."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    counts = events.queue_counts(port)
    unknown = [name for name in queues if name not in counts]
    if unknown:
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=unknown[0]))
    targets = queues or [name for name, count in counts.items() if count["visible"] or count["in_flight"]]
    for name in targets:
        events.purge(port, name)
    return targets


def proxy_options(cfg: Config) -> dict:
    """What the start form offers: the environments with Terraform, the remote it would use, whether there is a
    frontend to point to it. Reads only (the remote is saved when the proxy starts, as in the CLI)."""
    root = repo_root(cfg)
    environments = routes.terraform_dir(root, "dev").parent
    envs = sorted(p.name for p in environments.iterdir() if p.is_dir()) if environments.is_dir() else []
    return {
        "port": PROXY_PORT, "env": "dev" if "dev" in envs or not envs else envs[0], "envs": envs,
        "remote": repo_remote(cfg, root), "frontend": (root / "frontend").is_dir(),
    }


def proxy_routes(cfg: Config, env: str | None = None) -> dict:
    """Where each route goes now, like ``pdms proxy routes``: the running proxy's repo, env and remote, or what a
    proxy started now would use."""
    running = proxy.running_proxy()
    if running and running.get("repo"):
        root, env, remote = Path(running["repo"]), env or running.get("env") or "dev", running.get("remote") or None
    else:
        root = repo_root(cfg)
        env, remote = env or "dev", repo_remote(cfg, root) or None
    gateway = proxy.Gateway(routes=actions.proxy_routes(root, env), backend=root / "backend", remote=remote)
    live = gateway.local_instances()
    items = []
    for route in gateway.routes:
        target = gateway.target(route, live)
        items.append({
            "method": route.method, "path": route.path, "service": route.service, "target": target.kind,
            "key": target.instance.key if target.instance else "", "note": target.note,
        })
    return {"env": env, "remote": remote or "", "routes": items}


def repo_remote(cfg: Config, root: Path) -> str:
    """The remote API a proxy started now would use: the one saved for the repo, or the one in ``frontend/.env``."""
    alias = repos.alias_of(cfg, root)
    return (cfg.repos[alias].remote if alias in cfg.repos else "") or repos.remote_from_frontend(root) or ""


def repo_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root


def stack_key(name: str) -> str:
    return f"stack:{name}"


def backend(cfg: Config) -> Path:
    root = repos.active_backend(cfg)
    if root is None or not root.is_dir():
        raise actions.ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    return root.resolve()


def repo_services(cfg: Config) -> tuple[Path, list[str]]:
    """The backend folder and every service in it, as the stacks write them (``lead/lead-tp-list``)."""
    root = backend(cfg)
    return root, [path.relative_to(root).as_posix() for path in runner.find_services_below(root)]


def repo_consumers(root: Path) -> list[str]:
    """The services of the backend folder ``root`` that consume an SQS queue (they take no port)."""
    try:
        event_map = events.load_event_map(root.parent)
    except Exception:  # noqa: BLE001 - a Terraform pdms cannot read only loses the hint
        return []
    return sorted({consumer.service for consumer in event_map.consumers.values()})


def save_stack(name: str, services: list[str], user: str, db: str, new: bool) -> None:
    """Create (``new``) or replace a stack; the services must exist in the current repo."""
    cfg = Config.load()
    name = name.strip()
    if new:
        actions.check_alias(name, cfg.stacks)
    else:
        actions.require(cfg.stacks, _("stack"), name)
    _root, known = repo_services(cfg)
    unknown = [svc for svc in services if svc not in known]
    if unknown:
        raise actions.ActionError(_("Not services of the current repo: {names}", names=", ".join(unknown)))
    actions.save_stack(cfg, name, Stack(services=list(dict.fromkeys(services)), user=user, db=db))


def remove_stack(name: str) -> None:
    actions.remove_stack(Config.load(), name)


def find(key: str) -> instances.Instance:
    inst = instances.load().get(key)
    if not inst:
        raise actions.ActionError(_("There is no instance {key}.", key=key))
    return inst


def forget(key: str) -> None:
    """Forget a stopped instance (its log stays, like ``pdms ps --clean``)."""
    inst = find(key)
    if inst.alive():
        raise actions.ActionError(_("{key} is running; stop it first.", key=key))
    instances.forget(key)


def forget_stopped() -> list[str]:
    stopped = [i.key for i in instances.load().values() if not i.alive()]
    for key in stopped:
        instances.forget(key)
    return stopped


# --------------------------------------------------------------------------- settings: databases, users, defaults


def settings(cfg: Config) -> dict:
    """What the Settings tab shows: every database without its password, the users, the defaults and the stacks
    that use each database or user (they ask again when it is deleted)."""
    def used_by(kind: str, name: str) -> list[str]:
        return [stack_name for stack_name, stack in cfg.stacks.items() if getattr(stack, kind) == name]

    return {
        "path": str(config_path()),
        "dbs": [
            {"name": name, **{k: v for k, v in asdict(db).items() if k != "password"},
             "has_password": bool(db.password), "stacks": used_by("db", name)}
            for name, db in cfg.dbs.items()
        ],
        "users": [{"name": name, **asdict(user), "stacks": used_by("user", name)} for name, user in cfg.users.items()],
        "defaults": asdict(cfg.defaults),
        "roles": actions.known_roles(cfg),
        "sections": list(transfer.SECTIONS),
        "choices": {"language": i18n.LANGUAGES, "logging_level": list(LOG_LEVELS), "events": list(EVENTS_MODES)},
    }


def _text(body: dict, key: str, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise actions.ActionError(f"{key} must be a text")
    return value


def database_from(body: dict, current: Database | None) -> Database:
    """The database a form sends; a ``password`` left out or null keeps the current one."""
    password = body.get("password")
    if password is None:
        password = current.password if current else ""
    elif not isinstance(password, str):
        raise actions.ActionError("password must be a text, or null to keep the current one")
    return Database(
        host=_text(body, "host"), port=body.get("port", 5432), database=_text(body, "database"),
        user=_text(body, "user"), password=password, driver=current.driver if current else Database.driver,
        protected=body.get("protected") is True,
    )


def save_db(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    current = None if new else cfg.dbs[actions.require(cfg.dbs, _("database"), name)]
    return actions.save_db(cfg, name, database_from(body, current), new=new)


def remove_db(name: str) -> list[str]:
    return actions.remove_db(Config.load(), name)


def db_password(name: str) -> str:
    cfg = Config.load()
    return cfg.dbs[actions.require(cfg.dbs, _("database"), name)].password


def connect_db(body: dict) -> str:
    """Connect to a saved database (only its ``name``) or to the one in a form (its fields; the password of ``name``
    when the form leaves it out); returns the server version."""
    cfg = Config.load()
    name = _text(body, "name")
    if "host" not in body:
        return actions.check_connection(cfg.dbs[actions.require(cfg.dbs, _("database"), name)], cfg.defaults.db_timeout)
    current = cfg.dbs[actions.require(cfg.dbs, _("database"), name)] if name else None
    return actions.check_connection(actions.valid_db(database_from(body, current)), cfg.defaults.db_timeout)


def user_from(body: dict) -> DevUser:
    return DevUser(**{key: _text(body, key) for key in ("user_id", "username", "first_name", "last_name", "roles")})


def save_user(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    return actions.save_user(cfg, name, user_from(body), new=new, roles=actions.known_roles(cfg))


def remove_user(name: str) -> list[str]:
    return actions.remove_user(Config.load(), name)


def rename_user(name: str, body: dict) -> str:
    return actions.rename_user(Config.load(), name, _text(body, "new_name"))


def frontend_options(cfg: Config) -> dict:
    """What the frontend's start form shows: node and yarn, whether node_modules is up to date, where the API goes
    in each mode and whether a build would be made (and why)."""
    root = repo_root(cfg)
    if not frontend.exists(root):
        return {"available": False}
    tool = frontend.yarn()
    return {
        "available": True, "node": frontend.node_version() or "", "yarn": bool(tool), "port": frontend.PORT,
        "dependencies_ok": frontend.dependencies_ok(root) if tool else False,
        "api": {mode: frontend.api_url(root, mode) for mode in frontend.MODES},
        "build_needed": frontend.build_needed(root), "last_build": frontend.last_build(),
    }


def save_setup(body: dict) -> dict:
    """Save what Start everything starts; the stack must exist (empty for none)."""
    cfg = Config.load()
    stack = _text(body, "stack")
    if stack:
        actions.require(cfg.stacks, _("stack"), stack)
    mode = _text(body, "frontend_mode", "dev")
    if mode not in frontend.MODES:
        raise actions.InvalidValue("frontend_mode", _("Must be one of: {choices}", choices=", ".join(frontend.MODES)))
    cfg.setup = Setup(stack=stack, proxy=_bool(body, "proxy"), frontend=_bool(body, "frontend"), frontend_mode=mode,
                      events=_bool(body, "events"))
    cfg.save()
    return asdict(cfg.setup)


def defaults_from(body: dict, current: Defaults) -> Defaults:
    """The current defaults with the values the form sends, each of the right kind (the action checks the rest)."""
    values = asdict(current)
    for item in fields(Defaults):
        if item.name not in body:
            continue
        value, kind = body[item.name], type(values[item.name])
        if kind is bool:
            ok, expected = isinstance(value, bool), "true or false"
        elif kind is int:
            ok, expected = isinstance(value, (int, str)) and not isinstance(value, bool), "a number"
        elif kind is dict:
            ok, expected = isinstance(value, dict) and all(isinstance(v, str) for v in value.values()), "texts by name"
        else:
            ok, expected = isinstance(value, str), "a text"
        if not ok:
            raise actions.InvalidValue(item.name, f"must be {expected}")
        values[item.name] = value
    return Defaults(**values)


def save_defaults(body: dict) -> None:
    """Save the defaults; the language also applies to this server's messages from now on."""
    cfg = Config.load()
    actions.save_defaults(cfg, defaults_from(body, cfg.defaults))
    i18n.set_language(cfg.defaults.language)


def _bool(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise actions.ActionError(f"{key} must be true or false")
    return value


def _sections(body: dict) -> list[str]:
    sections = body.get("sections")
    if not isinstance(sections, list) or not all(isinstance(section, str) for section in sections):
        raise actions.ActionError("sections must be a list of section names")
    return sections


def export_config(body: dict) -> dict:
    """The file of ``pdms config export`` for the page to download."""
    text = actions.export_config(Config.load(), _sections(body), _bool(body, "secrets"))
    return {"filename": f"pdms-config-{datetime.now():%Y-%m-%d}.toml", "text": text}


def _plans(current: Config, doc: transfer.Document, sections: list[str]) -> list[dict]:
    return [asdict(plan) for plan in transfer.plan_import(current, doc.config, sections)]


def plan_import(body: dict) -> dict:
    """What importing the file ``text`` would do to each of its sections (``pdms config import``'s table)."""
    doc = actions.read_export(_text(body, "text"))
    if not doc.sections:
        raise actions.ActionError(_("Nothing to import."))
    meta = {key: doc.meta.get(key) for key in ("exported_at", "cli_version", "secrets")}
    return {"meta": meta, "sections": doc.sections, "plans": _plans(Config.load(), doc, doc.sections),
            "first_setup": actions.first_setup()}


def apply_import(body: dict) -> dict:
    """Import the chosen sections of ``text``: new entries always, the ``overwrite`` ones (``[section, name]``) over
    the user's own, or exactly the file's content with ``replace``. Keeps a copy of the previous configuration."""
    doc = actions.read_export(_text(body, "text"))
    sections = [section for section in _sections(body) if section in doc.sections]
    if not sections:
        raise actions.ActionError(_("Nothing to import."))
    overwrite = body.get("overwrite", [])
    if not isinstance(overwrite, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in overwrite
    ):
        raise actions.ActionError("overwrite must be a list of [section, name] pairs")
    current = Config.load()
    chosen = {tuple(pair) for pair in overwrite}
    if actions.first_setup():
        chosen = {(plan["section"], name) for plan in _plans(current, doc, sections) for name in plan["changed"]}
    result = transfer.apply_import(current, doc.config, sections, chosen, replace=_bool(body, "replace"))
    if result.to_dict() == current.to_dict():
        return {"changed": False, "backup": None, "no_password": []}
    backup = actions.import_config(result)
    i18n.set_language(result.defaults.language)
    return {"changed": True, "backup": str(backup) if backup else None,
            "no_password": [name for name, db in result.dbs.items() if not db.password]}


def _limit(body: dict) -> int:
    limit = body.get("limit", 200)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise actions.ActionError("limit must be a number between 1 and 1000")
    return limit


def db_users(body: dict) -> dict:
    """The users of a database's ``pdms_user`` table, with the ``DEV_ROLES`` each would get (``pdms user import``)."""
    cfg = Config.load()
    mapping, source = userimport.role_mapping(repos.active_root(cfg))
    limit = _limit(body)
    found = actions.read_db_users(
        cfg, _text(body, "db"), mapping, search=_text(body, "search").strip(),
        role=userimport.internal_role(_text(body, "role"), mapping), inactive=_bool(body, "inactive"), limit=limit,
    )
    known = {user.user_id: name for name, user in cfg.users.items()}
    return {
        "source": "built-in" if source == "built-in" else "repo", "limited": len(found) == limit,
        "users": [
            {**asdict(user), "dev_roles": user.dev_roles(mapping), "unknown_roles": user.unknown_roles(mapping),
             "imported_as": known.get(user.user_id, "")}
            for user in found
        ],
    }


def import_db_users(body: dict) -> dict:
    """Add the picked users (as :func:`db_users` listed them), updating the ones already imported."""
    picked = body.get("users")
    if not isinstance(picked, list) or not all(isinstance(user, dict) for user in picked):
        raise actions.ActionError("users must be a list of users")
    users = []
    for user in picked:
        roles = user.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise actions.ActionError("roles must be a list of role names")
        users.append(userimport.DbUser(
            user_id=_text(user, "user_id"), username=_text(user, "username"), first_name=_text(user, "first_name"),
            last_name=_text(user, "last_name"), roles=roles, is_active=user.get("is_active") is True,
        ))
    if not users or not all(user.user_id and user.username for user in users):
        raise actions.ActionError(_("Nothing selected."))
    cfg = Config.load()
    mapping, _source = userimport.role_mapping(repos.active_root(cfg))
    added, updated = actions.import_users(cfg, users, mapping)
    return {"added": added, "updated": updated}
