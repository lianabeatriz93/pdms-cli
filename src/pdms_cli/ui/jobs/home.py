"""Home: starting and stopping everything, and the saved setups."""

from __future__ import annotations

from dataclasses import asdict, replace

from ... import actions, events, frontend, instances, proxy
from ...config import Config, Setup
from ...i18n import _
from . import repo_info, settings_screen
from .core import HOME_KEY, Job
from .local_frontend import FrontendJobs
from .stacks import StackJobs

__all__ = ["HomeJobs", "remove_setup", "save_setup", "save_setup_as"]


def save_setup(body: dict) -> dict:
    """Save what Start everything starts; the stack must exist (empty for none)."""
    cfg = Config.load()
    stack = settings_screen._text(body, "stack")
    if stack:
        actions.require(cfg.stacks, _("stack"), stack)
    mode = settings_screen._text(body, "frontend_mode", "dev")
    if mode not in frontend.MODES:
        raise actions.InvalidValue("frontend_mode", _("Must be one of: {choices}", choices=", ".join(frontend.MODES)))
    cfg.setup = Setup(stack=stack, proxy=settings_screen._bool(body, "proxy"), frontend=settings_screen._bool(body, "frontend"), frontend_mode=mode,
                      events=settings_screen._bool(body, "events"))
    if cfg.setup_name in cfg.setups:
        cfg.setups[cfg.setup_name] = replace(cfg.setup)  # a saved setup follows its edits, like the form says
    cfg.save()
    return asdict(cfg.setup)


def save_setup_as(body: dict) -> dict:
    return {"name": actions.save_setup_as(Config.load(), settings_screen._text(body, "name"))}


def remove_setup(body: dict) -> dict:
    actions.remove_setup(Config.load(), settings_screen._text(body, "name"))
    return {}


class HomeJobs(StackJobs, FrontendJobs):
    def start_all(self, *, install: bool | None = None, confirmed: bool = False, python: bool | None = None,
                  migrate: bool | None = None, views: bool | None = None) -> Job | None:
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
            root = repo_info.backend(cfg)
            live = actions.running_by_service()
            if [path for path in actions.stack_paths(stack, root) if str(path) not in live]:
                stack_name = setup.stack
                user, db = stack.user or cfg.last_user, stack.db or cfg.last_db
                actions.require(cfg.users, _("user"), user)
                actions.require(cfg.dbs, _("database"), db)
                actions.check_database(cfg, db, confirmed)

        repo = repo_info.repo_root(cfg)
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
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=cfg.defaults.proxy_port, **plan_args)
            except actions.PortBusy as busy:
                proxy_plan = actions.plan_proxy(cfg, repo, actions.proxy_routes(repo, env), port=busy.free, **plan_args)
        frontend_plan = None
        if want_frontend and not self.busy(frontend.KEY):
            frontend_plan = actions.plan_frontend(cfg, mode=setup.frontend_mode, install=install)
        if event_map is None and not stack_name and proxy_plan is None and frontend_plan is None:
            return None
        starting = [path for path in actions.stack_paths(cfg.stacks[stack_name], repo_info.backend(cfg))
                    if str(path) not in actions.running_by_service()] if stack_name else []
        recreate = actions.check_pythons(cfg, [*starting, *([broker] if broker else [])], install, python)
        stack_dbs = [cfg.stacks[stack_name].profile_of(path.relative_to(repo_info.backend(cfg)).as_posix(), user, db)[1]
                     for path in starting] if stack_name else []
        behind = actions.check_migrations(cfg, [*stack_dbs, *([db] if broker else [])], migrate)
        needs = actions.check_test_views(cfg, list(zip(starting, stack_dbs)), views)
        if stack_name or broker:
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.migrate_first(job, behind)
            self.views_first(cfg, job, needs)
            if event_map is not None:
                self.phase(job, "starting ElasticMQ")
                actions.start_events(cfg, event_map.queues)
            if broker is not None:
                launch = actions.plan_service(cfg, broker, user_name=user, db_name=db, events_mode="local")
                self.install(cfg, job, broker, instances.make_key(launch.service, launch.port), install, broker.name,
                             recreate)
                self.phase(job, f"starting {broker.name}")
                actions.start_service(cfg, launch)
            if stack_name:
                plan = actions.plan_stack(cfg, stack_name, repo_info.backend(cfg), user_name=user, db_name=db)
                for launch in plan.services:
                    key = instances.make_key(launch.service, launch.port)
                    self.install(cfg, job, launch.service, key, install, launch.service.name, recreate)
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

    def switch_setup(self, name: str) -> list[str]:
        """Make the saved setup ``name`` the current one, stopping what the current one runs and the new one does
        not use: its stack (when another), the proxy and the frontend (when the new one goes without them). The local
        events stay: other services may publish there. Returns the keys of the stop jobs; the page starts the new
        setup (Start everything) once they are done."""
        cfg = Config.load()
        actions.require(cfg.setups, _("setup"), name)
        old, new = cfg.setup, cfg.setups[name]
        stops = []
        if old.stack and old.stack != new.stack and old.stack in cfg.stacks:
            job = self.down(old.stack)
            if job:
                stops.append(job.key)
        running_proxy = proxy.running_proxy()
        if old.proxy and not new.proxy and running_proxy and not self.busy(proxy.display_key(running_proxy)):
            stops.append(self.stop(proxy.display_key(running_proxy)).key)
        if old.frontend and not new.frontend and frontend.running() and not self.busy(frontend.KEY):
            stops.append(self.stop(frontend.KEY).key)
        actions.use_setup(cfg, name)
        self.on_change()
        return stops

    def stop_all(self) -> Job | None:
        """Stop the frontend, the proxy, every background service and the local ElasticMQ; None when nothing runs."""
        current_frontend, running_proxy = frontend.running(), proxy.running_proxy()
        live = [inst for inst in instances.load().values() if inst.present()]  # asleep: else the proxy wakes them
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
