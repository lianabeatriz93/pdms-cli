"""What ``pdms ui`` does to services: the same actions as the CLI, run in a thread so the page stays live.

A job belongs to one instance (or the proxy) and shows in the state while it runs, with its phase; a failed one
stays there with its error until the next action on that instance or until it is dismissed. Everything that needs
an answer (a protected database, the local ElasticMQ) is checked before the job starts, so the page can ask first.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import actions, events, instances, proxy, repos, routes, runner
from ..config import Config, Stack
from ..i18n import _

PROXY_PORT = 8000  # pdms proxy's --port default


def install_log(key: str) -> Path:
    """Where the install a restart runs writes its output (the service's own log is rotated when it starts)."""
    return instances.log_path(key).with_suffix(".install.log")


@dataclass
class Job:
    key: str
    action: str  # stop | restart | up | down
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


def save_stack(name: str, services: list[str], user: str, db: str, new: bool) -> None:
    """Create (``new``) or replace a stack; the services must exist in the current repo."""
    cfg = Config.load()
    name = name.strip()
    if new:
        if not name:
            raise actions.ActionError(_("Required field"))
        if name in cfg.stacks:
            raise actions.ActionError(_("That name already exists"))
        if not all(c.isalnum() or c in "-_" for c in name):
            raise actions.ActionError(_("Use only letters, numbers, '-' or '_'"))
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
