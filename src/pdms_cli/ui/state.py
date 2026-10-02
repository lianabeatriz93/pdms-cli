"""What the UI shows, read from the same files the CLI uses. Never includes secrets (database passwords)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .. import __version__, events, frontend, i18n, instances, proxy, repos
from ..config import Config
from . import updates


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
        "timeout": running.get("timeout", 0),
        "frontend": proxy.frontend_change(),
        "started_at": running.get("started_at", ""), "background": bool(running.get("background")),
        "status": "ok" if instances.responds("127.0.0.1", running["port"]) else "starting",
    }


def frontend_state(cfg: Config) -> dict | None:
    """The web app of the current repo: whether pdms runs it, how, where its API calls go and its last build.

    ``stale`` says why the running build no longer matches (its API URL was fixed when it was built)."""
    root = repos.active_root(cfg)
    current = frontend.running()
    if not frontend.exists(root) and not current:
        return None
    build = frontend.last_build()
    state = {
        "key": frontend.KEY, "running": bool(current), "status": "stopped", "detail": "", "mode": "",
        "port": frontend.PORT, "url": "", "started_at": "", "build": build, "stale": "",
        "api": frontend.api_url(root, "dev") if root and frontend.exists(root) else "",
    }
    if current:
        health = frontend.health(current)
        mode = current.get("mode", "dev")
        answers = health.detail if health.state == "ok" else ""
        state.update(
            status=health.state, detail="" if answers else health.detail, mode=mode, port=current["port"],
            url=frontend.url(current["port"], answers or "https"), started_at=current.get("started_at", ""),
            api=current.get("api", ""), repo=repos.repo_of(cfg, current.get("root", "")) or "",
        )
        if mode == "build" and build and build.get("root") == current.get("root"):
            now = frontend.api_url(Path(current["root"]), "build")
            state["api"] = build.get("api", "")
            if now != build.get("api"):
                state["stale"] = now
    state["api_local"] = frontend.is_local(state["api"])
    return state


def profiles(cfg: Config) -> dict[str, dict]:
    """Who each user is, to tell them apart: full name and roles (no ids nor emails)."""
    return {
        name: {"name": f"{user.first_name} {user.last_name}".strip(), "roles": user.roles}
        for name, user in cfg.users.items()
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
        "language": i18n.configured(cfg.defaults.language),
        "repo": {"alias": cfg.current_repo, "root": str(root)} if root else None,
        "user": cfg.last_user,
        "db": cfg.last_db,
        "users": list(cfg.users),
        "profiles": profiles(cfg),
        "dbs": [{"name": name, "protected": db.protected} for name, db in cfg.dbs.items()],
        "instances": [instance_state(cfg, inst, healths[inst.key]) for inst in items],
        "stacks": stacks_state(cfg, live),
        "proxy": proxy_state(cfg),
        "events": {"port": cfg.defaults.events_port, "up": events_up},
        "sns": sns_state(cfg, events_up),
        "frontend": frontend_state(cfg),
        "setup": asdict(cfg.setup),
        "update": updates.state(cfg),
        "jobs": jobs or {},
    }
