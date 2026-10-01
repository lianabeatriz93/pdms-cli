"""What the UI shows, read from the same files the CLI uses. Never includes secrets (database passwords)."""

from __future__ import annotations

from .. import __version__, events, instances, proxy, repos
from ..config import Config


def instance_state(cfg: Config, inst: instances.Instance, health: instances.Health) -> dict:
    return {
        "key": inst.key, "name": inst.name, "service": inst.service, "repo": repos.repo_of(cfg, inst.service) or "",
        "host": inst.host, "port": inst.port, "queue": inst.queue, "user": inst.user, "db": inst.db,
        "events": inst.events, "reload": inst.reload, "started_at": inst.started_at,
        "status": health.state, "detail": health.detail,
    }


def proxy_state() -> dict | None:
    running = proxy.running_proxy()
    if not running:
        return None
    return {
        "key": proxy.display_key(running), "port": running["port"], "repo": running.get("repo", ""),
        "remote": running.get("remote", ""), "as": running.get("as", ""),
        "started_at": running.get("started_at", ""), "background": bool(running.get("background")),
        "status": "ok" if instances.responds("127.0.0.1", running["port"]) else "starting",
    }


def build_state(cfg: Config | None = None) -> dict:
    """Everything the first paint needs: repo, default user and DB, instances, proxy and local events."""
    cfg = cfg or Config.load()
    items = list(instances.load().values())
    healths = instances.health_all(items)
    root = repos.active_root(cfg)
    return {
        "version": __version__,
        "repo": {"alias": cfg.current_repo, "root": str(root)} if root else None,
        "user": cfg.last_user,
        "db": cfg.last_db,
        "instances": [instance_state(cfg, inst, healths[inst.key]) for inst in items],
        "proxy": proxy_state(),
        "events": {"port": cfg.defaults.events_port, "up": events.is_up(cfg.defaults.events_port)},
    }
