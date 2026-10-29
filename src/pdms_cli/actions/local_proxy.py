"""The local proxy: planning, starting, stopping it, and its requests."""

from __future__ import annotations

import http.client
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import captures, instances, proxy, repos, routes, trace
from ..config import Config, DevUser
from ..i18n import _
from . import checks, settings
from .errors import ActionError, PointFrontend

__all__ = [
    "MAX_PROXY_TIMEOUT", "PROXY_HOST", "ProxyLaunch", "ProxyStarted", "clear_proxy_leftovers", "plan_proxy",
    "point_frontend", "proxy_remote", "proxy_request", "proxy_routes", "proxy_state", "replay_request",
    "serve_proxy", "start_proxy", "stop_proxy", "wait_for_proxy",
]


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
    timeout = settings._number("timeout", cfg.defaults.proxy_timeout if timeout is None else timeout, 1, MAX_PROXY_TIMEOUT)
    if user_name:
        checks.require(cfg.users, _("user"), user_name)
    port = checks.free_port(PROXY_HOST, port)
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
