"""What the UI shows, read from the same files the CLI uses. Never includes secrets (database passwords)."""

from __future__ import annotations

from datetime import datetime

from .. import __version__, events, instances, proxy, repos
from ..config import Config


def instance_state(cfg: Config, inst: instances.Instance, health: instances.Health) -> dict:
    return {
        "key": inst.key, "name": inst.name, "service": inst.service, "repo": repos.repo_of(cfg, inst.service) or "",
        "host": inst.host, "port": inst.port, "queue": inst.queue, "user": inst.user, "db": inst.db,
        "events": inst.events, "reload": inst.reload, "started_at": inst.started_at,
        "status": health.state, "detail": health.detail,
    }


def stacks_state(cfg: Config, live: dict[str, list[str]]) -> list[dict]:
    """Each stack with its services and the keys of their running instances (``live``: service path → keys)."""
    backend = repos.active_backend(cfg)
    root = backend.resolve() if backend and backend.is_dir() else None
    return [
        {
            "name": name, "user": stack.user, "db": stack.db,
            "services": [
                {"path": svc, "running": live.get(str(root / svc), []) if root else []} for svc in stack.services
            ],
        }
        for name, stack in cfg.stacks.items()
    ]


def sns_state(cfg: Config, events_up: bool) -> dict:
    """The local SNS: up with the local ElasticMQ, and when something was last published to it."""
    log = events.sns_log_path()
    try:
        last = datetime.fromtimestamp(log.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        last = ""
    return {"key": events.SNS_KEY, "queue": events.SNS_QUEUE, "status": "ok" if events_up else "stopped",
            "last_publish": last}


def proxy_state(cfg: Config) -> dict | None:
    running = proxy.running_proxy()
    if not running:
        return None
    repo = running.get("repo", "")
    return {
        "key": proxy.display_key(running), "pid": running["pid"], "port": running["port"], "repo": repo,
        "repo_alias": (repos.repo_of(cfg, repo) or "") if repo else "",
        "env": running.get("env", ""), "remote": running.get("remote", ""), "as": running.get("as", ""),
        "frontend": proxy.frontend_change(),
        "started_at": running.get("started_at", ""), "background": bool(running.get("background")),
        "status": "ok" if instances.responds("127.0.0.1", running["port"]) else "starting",
    }


def build_state(cfg: Config | None = None, jobs: dict[str, dict] | None = None) -> dict:
    """Everything the page shows: repo, users and DBs (names only), instances, proxy, local events and jobs."""
    cfg = cfg or Config.load()
    items = list(instances.load().values())
    healths = instances.health_all(items)
    root = repos.active_root(cfg)
    live: dict[str, list[str]] = {}
    for inst in items:
        if healths[inst.key].state != "stopped":
            live.setdefault(inst.service, []).append(inst.key)
    events_up = events.is_up(cfg.defaults.events_port)
    return {
        "version": __version__,
        "repo": {"alias": cfg.current_repo, "root": str(root)} if root else None,
        "user": cfg.last_user,
        "db": cfg.last_db,
        "users": list(cfg.users),
        "dbs": [{"name": name, "protected": db.protected} for name, db in cfg.dbs.items()],
        "instances": [instance_state(cfg, inst, healths[inst.key]) for inst in items],
        "stacks": stacks_state(cfg, live),
        "proxy": proxy_state(cfg),
        "events": {"port": cfg.defaults.events_port, "up": events_up},
        "sns": sns_state(cfg, events_up),
        "jobs": jobs or {},
    }
