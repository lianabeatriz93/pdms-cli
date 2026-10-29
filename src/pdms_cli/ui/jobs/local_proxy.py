"""The local proxy: starting it, its options and routes."""

from __future__ import annotations

from pathlib import Path

from ... import actions, instances, proxy, routes
from ...config import Config
from ...i18n import _
from . import repo_info
from .core import Job, JobsCore

__all__ = ["ProxyJobs", "_proxy_repo", "proxy_options", "proxy_route", "proxy_routes"]


def proxy_options(cfg: Config) -> dict:
    """What the start form offers: the environments with Terraform, the remote it would use, whether there is a
    frontend to point to it. Reads only (the remote is saved when the proxy starts, as in the CLI)."""
    root = repo_info.repo_root(cfg)
    environments = routes.terraform_dir(root, "dev").parent
    envs = sorted(p.name for p in environments.iterdir() if p.is_dir()) if environments.is_dir() else []
    return {
        "port": cfg.defaults.proxy_port, "env": "dev" if "dev" in envs or not envs else envs[0], "envs": envs,
        "remote": repo_info.repo_remote(cfg, root), "frontend": (root / "frontend").is_dir(),
    }


def proxy_route(cfg: Config, method: str, path: str) -> dict:
    """The service of the route a request took (``lead/lead-tp-list``; "" when none matches), to start it here."""
    root, env = _proxy_repo(cfg)
    route = routes.match(actions.proxy_routes(root, env), method, path)
    return {"service": route.service if route else ""}


def _proxy_repo(cfg: Config) -> tuple[Path, str]:
    """The repo and environment of the running proxy, or the ones a proxy started now would use."""
    running = proxy.running_proxy()
    if running and running.get("repo"):
        return Path(running["repo"]), running.get("env") or "dev"
    return repo_info.repo_root(cfg), "dev"


def proxy_routes(cfg: Config, env: str | None = None) -> dict:
    """Where each route goes now, like ``pdms proxy routes``: the running proxy's repo, env and remote, or what a
    proxy started now would use."""
    running = proxy.running_proxy()
    if running and running.get("repo"):
        root, env, remote = Path(running["repo"]), env or running.get("env") or "dev", running.get("remote") or None
    else:
        root = repo_info.repo_root(cfg)
        env, remote = env or "dev", repo_info.repo_remote(cfg, root) or None
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


class ProxyJobs(JobsCore):
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
        root = repo_info.repo_root(cfg)
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
