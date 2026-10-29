"""What pdms ui's page asks for and does, without HTTP: JSON in, JSON out.

:class:`Api` answers a GET with ``(status, data)`` and a POST with ``(status, data)`` too: 202 and the job when it
started one, 409 with a ``decision`` when the user has to answer first, 400 with an ``error`` (and the ``field`` it is
about, for a form). Each route is a method, found in a table: by its whole path, or by the group and verb of
``/api/<group>/<name>/<verb>``. The server (server.py) checks who is asking, reads the body and writes the reply;
the streams (the state, logs) and the files stay there.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

from rich.errors import MarkupError
from rich.text import Text

from .. import actions, browser, bugreport, emails, events, frontend, health, images, instances, proxy, testruns, testviews
from ..config import Config
from ..i18n import _
from . import instance as ui_instance
from . import jobs as ui_jobs
from . import state as ui_state
from . import updates as ui_updates
from .control import Control

ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # a Terraform environment folder, never a path
PEEK_LIMIT = 50

Query = dict[str, list[str]]
Reply = tuple[int, object]


# --------------------------------------------------------------------------- what the page sends


def plain(message: str) -> str:
    """A CLI message without its Rich markup (``[bold]pdms up[/]`` → ``pdms up``)."""
    try:
        return Text.from_markup(message).plain
    except MarkupError:
        return message


def decision_body(decision: actions.Decision) -> dict:
    if isinstance(decision, actions.ProtectedDatabase):
        return {"decision": "protected_database", "name": decision.name}
    if isinstance(decision, actions.LocalEventsDown):
        return {"decision": "local_events_down", "port": decision.port,
                "error": "The local ElasticMQ is not running: start it with pdms events up."}
    if isinstance(decision, actions.PortBusy):
        return {"decision": "port_busy", "port": decision.port, "free": decision.free}
    if isinstance(decision, actions.TestViewsPending):
        return {"decision": "test_views_pending", "views": [item.describe() for item in decision.items],
                "error": _("{names}: changed in your checkout. Start it from Services to choose between your version "
                           "and the real one.", names=", ".join(item.view for item in decision.items))}
    if isinstance(decision, actions.MigrationsPending):
        return {"decision": "migrations_pending", "dbs": [item.describe() for item in decision.items],
                "error": _("{names} lacks migrations of pdms-db-migrations: start it from Services to choose whether "
                           "to apply them.", names=", ".join(item.alias for item in decision.items))}
    if isinstance(decision, actions.PythonMismatch):
        return {"decision": "python_mismatch", "services": [item.describe() for item in decision.items],
                "error": _("The virtualenv of {names} runs a Python its project no longer accepts: start or restart "
                           "it from Services to choose whether to recreate it.",
                           names=", ".join(item.service.name for item in decision.items))}
    if isinstance(decision, actions.PointFrontend):
        return {"decision": "point_frontend", "url": decision.url}
    if isinstance(decision, images.Missing):
        return {"decision": "images_missing", "error": str(decision),
                "images": [{"name": image.name, "download_mb": image.download_mb, "use": image.use}
                           for image in decision.images]}
    if isinstance(decision, ui_jobs.LeftRunning):
        switch = decision.switch
        return {"decision": "repo_switch", "old": switch.old, "proxy": switch.proxy,
                "running": [{"key": inst.key, "movable": target is not None} for inst, target in switch.running]}
    return {"decision": type(decision).__name__, "error": str(decision)}


def _body_text(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise actions.ActionError(f"{key} must be a name")
    return value


def _strings(body: dict, key: str, what: str) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise actions.ActionError(f"{key} must be a list of {what}")
    return value


def _flag(body: dict, key: str, default: bool | None = None) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise actions.ActionError(f"{key} must be true or false")
    return value


def launch_options(body: dict) -> dict:
    """The user, database, install choice and confirmation of a restart or a stack's up."""
    install, python, migrate, views = body.get("install"), body.get("python"), body.get("migrate"), body.get("views")
    if install not in (None, True, False):
        raise actions.ActionError("install must be true, false or null")
    if python not in (None, True, False):
        raise actions.ActionError("python must be true (recreate), false (keep) or null (ask)")
    if migrate not in (None, True, False):
        raise actions.ActionError("migrate must be true (apply them), false (start as it is) or null (ask)")
    if views not in (None, True, False):
        raise actions.ActionError("views must be true (the test copies), false (the real views) or null (ask)")
    return {
        "user": str(body.get("user") or "") or None, "db": str(body.get("db") or "") or None,
        "install": install, "confirmed": body.get("confirmed") is True, "python": python, "migrate": migrate,
        "views": views,
    }


def run_options(body: dict) -> dict:
    """The service, port, user, database, install choice and confirmation of starting one service."""
    service, port = body.get("service"), body.get("port")
    if not isinstance(service, str) or not service:
        raise actions.ActionError("service must be a service path of the current repo")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise actions.ActionError("port must be a number between 1 and 65535, or null for the next free one")
    on_demand = body.get("on_demand")
    if on_demand not in (None, True, False):
        raise actions.ActionError("on_demand must be true, false or null (the on_demand setting)")
    return {"service": service, "port": port, **launch_options(body), "on_demand": on_demand}


def frontend_options(body: dict) -> dict:
    """The mode, port, install choice, rebuild and restart (stop the running one first) of a frontend start."""
    mode, port, install = body.get("mode", "dev"), body.get("port"), body.get("install")
    if mode not in frontend.MODES:
        raise actions.ActionError(f"mode must be one of: {', '.join(frontend.MODES)}")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise actions.ActionError("port must be a number between 1 and 65535, or null for 3000")
    if install not in (None, True, False):
        raise actions.ActionError("install must be true, false or null")
    return {"mode": mode, "port": port, "install": install, "rebuild": body.get("rebuild") is True,
            "restart": body.get("restart") is True}


def env_name(value: object) -> str:
    env = str(value or "dev")
    if not ENV_NAME.match(env):
        raise actions.ActionError(f"not an environment name: {env!r}")
    return env


def proxy_options(body: dict) -> dict:
    """The port, env, remote, user and frontend choice of a proxy start."""
    port, frontend = body.get("port", Config.load().defaults.proxy_port), body.get("frontend")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise actions.ActionError("port must be a number between 1 and 65535")
    if frontend not in (None, True, False):
        raise actions.ActionError("frontend must be true, false or null")
    return {
        "port": port, "env": env_name(body.get("env")), "remote": str(body.get("remote") or "") or None,
        "no_remote": body.get("no_remote") is True, "user": str(body.get("user") or "") or None, "frontend": frontend,
    }


def _arg(query: Query, name: str, default: str = "") -> str:
    return query.get(name, [default])[0]


def _started(job) -> tuple[int, dict]:
    """A job that may not have been needed (nothing to start or stop)."""
    return (202, {"job": job.key}) if job else (200, {"job": None})


def _once(started: bool, busy: str) -> tuple[int, dict]:
    return (202, {}) if started else (409, {"error": busy})


# --------------------------------------------------------------------------- the API


class Api:
    def __init__(self, hub, jobs: ui_jobs.Jobs, control: Control) -> None:
        self.hub, self.jobs, self.control = hub, jobs, control

    # ------------------------------------------------------------------ GET

    def get(self, path: str, query: Query) -> Reply | None:
        """The answer to a GET of the API, or None when ``path`` is not one."""
        route = GETS.get(path)
        if route is None:
            return None
        handler, failed = route
        try:
            return 200, handler(self, query)
        except actions.ActionError as exc:
            return failed, {"error": plain(exc.message)}
        except OSError as exc:  # ElasticMQ went away in the middle
            if path.startswith("/api/events/"):
                return 502, {"error": f"ElasticMQ: {exc}"}
            raise

    def state(self, _query: Query) -> object:
        hub = self.hub
        return json.loads(hub.latest) if hub.latest and hub.watchers else hub.build()

    def services(self, _query: Query) -> object:
        root, services = ui_jobs.repo_services(Config.load())
        return {"root": str(root), "services": services, "consumers": ui_jobs.repo_consumers(root)}

    def stack_from_running(self, _query: Query) -> object:
        return ui_jobs.stack_from_running()

    def config(self, _query: Query) -> object:
        return {**ui_jobs.settings(Config.load()), "pick_folder": self.control.pick_folder is not None}

    def doctor(self, _query: Query) -> object:
        return {"latest": self.jobs.doctor.latest(), "running": self.jobs.doctor.running}

    def frontend_options(self, _query: Query) -> object:
        return ui_jobs.frontend_options(Config.load())

    def proxy_options(self, _query: Query) -> object:
        return ui_jobs.proxy_options(Config.load())

    def proxy_request(self, query: Query) -> object:
        return actions.proxy_request(_arg(query, "id"))

    def proxy_route(self, query: Query) -> object:
        return ui_jobs.proxy_route(Config.load(), _arg(query, "method", "GET"), _arg(query, "path"))

    def proxy_routes(self, query: Query) -> object:
        return ui_jobs.proxy_routes(Config.load(), env_name(env) if (env := _arg(query, "env")) else None)

    def events_queues(self, _query: Query) -> object:
        return ui_jobs.events_queues(Config.load())

    def events_map(self, _query: Query) -> object:
        return ui_jobs.events_map(Config.load())

    def events_ready(self, _query: Query) -> object:
        return ui_jobs.events_ready(Config.load())

    def events_template(self, query: Query) -> object:
        return ui_jobs.events_template(Config.load(), _arg(query, "type"))

    def events_peek(self, query: Query) -> object:
        cfg = Config.load()
        try:
            limit = max(1, min(int(_arg(query, "limit") or PEEK_LIMIT), PEEK_LIMIT))
        except ValueError:
            limit = PEEK_LIMIT
        return {"queue": _arg(query, "queue"), "messages": ui_jobs.events_peek(cfg, _arg(query, "queue"), limit)}

    def versions(self, _query: Query) -> object:
        return {"versions": ui_updates.versions(Config.load())}

    def notes(self, query: Query) -> object:
        return {"notes": ui_updates.notes(_arg(query, "version"))}

    def views(self, _query: Query) -> object:
        return actions.test_views(Config.load())

    def data(self, _query: Query) -> object:
        return ui_jobs.data_info(Config.load())

    def tests(self, _query: Query) -> object:
        return ui_jobs.tests_info(Config.load(), self.jobs.tests)

    # ------------------------------------------------------------------ POST

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        """Do what the page asked; the errors the user can act on come back as 400 (or 409, a question)."""
        try:
            return self._route(path, body)
        except actions.InvalidValue as invalid:
            return 400, {"error": plain(invalid.reason), "field": invalid.field}
        except actions.ActionError as exc:
            return 400, {"error": plain(exc.message)}
        except actions.Decision as decision:
            return 409, decision_body(decision)

    def _route(self, path: str, body: dict) -> tuple[int, dict]:
        if (handler := POSTS.get(path)) is not None:
            return handler(self, body)
        parts = path.split("/")
        if len(parts) == 4 and parts[:2] == ["", "api"] and (verbs := VERBS.get(parts[2])) is not None:
            handler = verbs.get(parts[3])
            if parts[2] == "events":
                try:
                    return handler(self, body) if handler else (404, {"error": "not found"})
                except OSError as exc:  # ElasticMQ went away in the middle
                    return 502, {"error": f"ElasticMQ: {exc}"}
            if parts[2] == "data" and handler is None:
                return 200, ui_jobs.data_action(parts[3], body)
            return handler(self, body) if handler else (404, {"error": "not found"})
        if len(parts) == 5 and parts[:2] == ["", "api"] and (verbs := NAMED.get(parts[2])) is not None:
            handler = verbs.get(parts[4])
            return handler(self, unquote(parts[3]), body) if handler else (404, {"error": "not found"})
        return 404, {"error": "not found"}

    # the page and the code

    def replay(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body.get("id"), str):
            raise actions.ActionError("id must be a string")
        return 200, actions.replay_request(body["id"])

    def open_code(self, body: dict) -> tuple[int, dict]:
        file, line = body.get("path"), body.get("line", 0)
        if not isinstance(file, str) or not isinstance(line, int) or isinstance(line, bool):
            raise actions.ActionError("path must be a string and line a number")
        actions.open_code(Config.load(), file, line)
        return 200, {}

    def clean(self, _body: dict) -> tuple[int, dict]:
        return 200, {"forgotten": ui_jobs.forget_stopped()}

    def fix_frontend_api(self, _body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.fix_frontend_api()

    def adopt_strays(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.adopt_strays(body)

    def stop_strays(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.stop_strays(body)

    def bug_report(self, _body: dict) -> tuple[int, dict]:
        return 200, {"path": str(bugreport.write(Config.load()))}

    def seen(self, body: dict) -> tuple[int, dict]:
        try:
            return 200, {"new_views": ui_state.mark_seen(_body_text(body, "view"))}
        except ValueError as exc:
            raise actions.ActionError(str(exc)) from exc

    def pick_folder(self, body: dict) -> tuple[int, dict]:
        if self.control.pick_folder is None:
            return 404, {"error": "only in the window"}
        return 200, {"path": self.control.pick_folder(str(body.get("start") or ""))}

    def pull_images(self, body: dict) -> tuple[int, dict]:
        names, then = body.get("names"), body.get("then", "")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or not isinstance(then, str):
            raise actions.ActionError("names must be a list of image names and then a string")
        return 202, {"job": self.jobs.pull_images(names, then, body).key}

    # checks in the background

    def run_doctor(self, body: dict) -> tuple[int, dict]:
        return _once(self.jobs.doctor.run(databases=_flag(body, "databases", False)), "Doctor is running.")

    def run_health(self, body: dict) -> tuple[int, dict]:
        return _once(self.jobs.health.run(body.get("all", False) is True), "Already measuring.")

    def health_line(self, _body: dict) -> tuple[int, dict]:
        ms = health.line()
        return 200, {"ms": None if ms is None else round(ms), "host": health.LINE_HOST[0]}

    def aws_profile(self, body: dict) -> tuple[int, dict]:
        self.jobs.aws.choose(ui_jobs._text(body, "profile", ""))
        return 200, {}

    def aws_read(self, _body: dict) -> tuple[int, dict]:
        return _once(self.jobs.aws.read(), "Already reading.")

    def aws_login(self, _body: dict) -> tuple[int, dict]:
        return _once(self.jobs.aws.login(), "Already logging in.")

    def aws_check(self, _body: dict) -> tuple[int, dict]:
        threading.Thread(target=self.jobs.aws.check, kwargs={"read_if_due": False}, daemon=True).start()
        return 202, {}

    # starting and stopping

    def run(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.start(**run_options(body)).key}

    def start_proxy(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.start_proxy(**proxy_options(body)).key}

    def start_frontend(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.start_frontend(**frontend_options(body)).key}

    def home_start(self, body: dict) -> tuple[int, dict]:
        options = launch_options(body)
        return _started(self.jobs.start_all(install=options["install"], confirmed=options["confirmed"],
                                            python=options["python"], migrate=options["migrate"],
                                            views=options["views"]))

    def home_stop(self, _body: dict) -> tuple[int, dict]:
        return _started(self.jobs.stop_all())

    def restart_many(self, body: dict) -> tuple[int, dict]:
        keys = _strings(body, "keys", "instance keys")
        stack, remember = body.get("stack"), body.get("remember", False)
        if (stack is not None and not isinstance(stack, str)) or not isinstance(remember, bool):
            raise actions.ActionError("stack must be a stack name and remember true or false")
        started = self.jobs.restart_many(keys, **launch_options(body), stack=stack or None, remember=remember)
        return 202, {"jobs": [job.key for job in started]}

    # setups

    def save_setup(self, body: dict) -> tuple[int, dict]:
        return 200, {"setup": ui_jobs.save_setup(body)}

    def save_setup_as(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.save_setup_as(body)

    def remove_setup(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.remove_setup(body)

    def switch_setup(self, body: dict) -> tuple[int, dict]:
        name = body.get("name")
        if not isinstance(name, str):
            raise actions.ActionError("name must be the name of a saved setup")
        return 200, {"stopping": self.jobs.switch_setup(name)}

    # data: the local Postgres, its copy, migrations and test databases

    def postgres_up(self, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.postgres("up").key}

    def postgres_down(self, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.postgres("down").key}

    def migrate_db(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.migrate_db(_body_text(body, "db")).key}

    def restore_snapshot(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.restore_snapshot(_body_text(body, "name")).key}

    def use_copy(self, body: dict) -> tuple[int, dict]:
        return 200, self.jobs.use_copy(_body_text(body, "instead_of"))

    def refresh_copy(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.refresh_copy(body).key}

    def prepare_test_dbs(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.prepare_test_dbs(_strings(body, "names", "test databases")).key}

    def migrate_test_dbs(self, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.migrate_test_dbs(_strings(body, "names", "test databases")).key}

    def migration_status(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.migration_status(body)

    # tests

    def run_tests(self, body: dict) -> tuple[int, dict]:
        return 202, self.jobs.run_tests(body)

    def stop_tests(self, _body: dict) -> tuple[int, dict]:
        return 200, {"stopped": self.jobs.tests.stop()}

    def start_test_db(self, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.start_test_db().key}

    # settings

    def look_at_repo(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.look_at_repo(body)

    def add_repo(self, body: dict) -> tuple[int, dict]:
        return 200, {"name": ui_jobs.add_repo(body)}

    def save_defaults(self, body: dict) -> tuple[int, dict]:
        ui_jobs.save_defaults(body)
        return 200, {}

    def test_db(self, body: dict) -> tuple[int, dict]:
        return 200, {"version": ui_jobs.connect_db(body)}

    def export_config(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.export_config(body)

    def plan_import(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.plan_import(body)

    def apply_import(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.apply_import(body)

    def db_users(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.db_users(body)

    def import_db_users(self, body: dict) -> tuple[int, dict]:
        return 200, ui_jobs.import_db_users(body)

    # the browser extension

    def install_extension(self, body: dict) -> tuple[int, dict]:
        found, folder, opened = actions.install_extension(_body_text(body, "browser"))
        return 200, {"name": found.name, "page": found.page, "folder": browser.shown(folder), "opened": opened}

    def front_redirect(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body.get("on"), bool):
            raise actions.ActionError("on must be true or false")
        actions.set_front_redirect(Config.load(), body["on"])
        return 200, {}

    def front_hosts(self, body: dict) -> tuple[int, dict]:
        return 200, {"hosts": actions.set_front_hosts(Config.load(), _strings(body, "hosts", "host names"))}

    # /api/events/<verb>: local SQS

    def events_up(self, body: dict) -> tuple[int, dict]:
        broker = body.get("broker", True)
        if not isinstance(broker, bool):
            raise actions.ActionError("broker must be true or false")
        return 202, {"job": self.jobs.events_up(broker=broker, **launch_options(body)).key}

    def events_down(self, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.events_down().key}

    def events_send(self, body: dict) -> tuple[int, dict]:
        cfg = Config.load()
        target, message = body.get("target"), body.get("body")
        if not isinstance(target, str) or not target or not isinstance(message, (str, type(None))):
            raise actions.ActionError("target must be an event type or a queue, body a JSON text")
        return 200, ui_jobs.events_send(cfg, target, message, direct=body.get("direct") is True)

    def events_purge(self, body: dict) -> tuple[int, dict]:
        cfg = Config.load()
        queues = body.get("queues")
        if not isinstance(queues, list) or not all(isinstance(name, str) for name in queues):
            raise actions.ActionError("queues must be a list of queue names ([] for every queue)")
        return 200, {"purged": ui_jobs.events_purge(cfg, queues)}

    def events_dismiss(self, _body: dict) -> tuple[int, dict]:
        self.jobs.dismiss(ui_jobs.EVENTS_KEY)
        return 200, {}

    # /api/update/<verb>: new pdms versions

    def update_check(self, _body: dict) -> tuple[int, dict]:
        return 200, ui_updates.check_now(Config.load())

    def update_install(self, body: dict) -> tuple[int, dict]:
        restart_proxy = body.get("restart_proxy", True)
        if not isinstance(restart_proxy, bool):
            raise actions.ActionError("restart_proxy must be true or false")
        version = body.get("version") or ""
        if not isinstance(version, str):
            raise actions.ActionError("version must be a version")
        return 202, {"job": self.jobs.update(self.control, restart_proxy=restart_proxy, version=version).key}

    def update_skip(self, body: dict) -> tuple[int, dict]:
        ui_updates.skip(_body_text(body, "version"))
        self.hub.poke()
        return 200, {}

    def update_restart(self, _body: dict) -> tuple[int, dict]:
        self.control.quit(restart=True)  # updated from the CLI: start again with the version on disk
        return 202, {}

    def update_dismiss(self, _body: dict) -> tuple[int, dict]:
        self.jobs.dismiss(ui_updates.KEY)
        return 200, {}

    # /api/views/<verb>: test copies of materialized views (Data → Test views, see testviews.py)

    def view_create(self, body: dict) -> tuple[int, dict]:
        return 200, actions.create_test_view(Config.load(), _body_text(body, "view"),
                                             confirmed=body.get("confirmed") is True)

    def view_use(self, body: dict) -> tuple[int, dict]:
        cfg = Config.load()
        actions.use_test_view(cfg, _body_text(body, "view"), _body_text(body, "name"))
        return 200, {}

    def view_stop(self, body: dict) -> tuple[int, dict]:
        testviews.stop_using(Config.load(), _body_text(body, "view"))
        return 200, {}

    def view_drop(self, body: dict) -> tuple[int, dict]:
        cfg = Config.load()
        return 200, actions.drop_test_view(cfg, _body_text(body, "view"), _body_text(body, "name"),
                                           confirmed=body.get("confirmed") is True)

    def view_refresh(self, body: dict) -> tuple[int, dict]:
        cfg = Config.load()
        return 200, actions.refresh_test_view(cfg, _body_text(body, "view"), _body_text(body, "name"),
                                              confirmed=body.get("confirmed") is True)

    # /api/instances/<key>/<verb>

    def instance_stop(self, key: str, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.stop(key).key}

    def instance_restart(self, key: str, body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.restart(key, **launch_options(body)).key}

    def instance_on_demand(self, key: str, body: dict) -> tuple[int, dict]:
        if not isinstance(body.get("on"), bool):
            raise actions.ActionError("on must be true or false")
        job = self.jobs.on_demand(key, body["on"])
        return (202, {"job": job.key}) if job else (200, {})

    def instance_sleep(self, key: str, _body: dict) -> tuple[int, dict]:
        return 202, {"job": self.jobs.sleep(key).key}

    def instance_debug(self, key: str, _body: dict) -> tuple[int, dict]:
        return 200, self.jobs.debug(key)

    def instance_forget(self, key: str, _body: dict) -> tuple[int, dict]:
        ui_jobs.forget(key)
        return 200, {}

    def instance_dismiss(self, key: str, _body: dict) -> tuple[int, dict]:
        self.jobs.dismiss(key)
        return 200, {}

    # /api/stacks/<name>/<verb>

    def stack_up(self, name: str, body: dict) -> tuple[int, dict]:
        return _started(self.jobs.up(name, **launch_options(body)))

    def stack_down(self, name: str, _body: dict) -> tuple[int, dict]:
        return _started(self.jobs.down(name))

    def stack_save(self, name: str, body: dict) -> tuple[int, dict]:
        services = _strings(body, "services", "service paths")
        overrides = body.get("overrides")
        if overrides is not None and not (isinstance(overrides, dict) and all(
                isinstance(svc, str) and isinstance(own, dict)
                and all(k in ("user", "db") and isinstance(v, str) for k, v in own.items())
                for svc, own in overrides.items())):
            raise actions.ActionError("overrides must map service paths to {user, db}")
        rename = body.get("rename") or ""
        if not isinstance(rename, str):
            raise actions.ActionError("rename must be the stack's new name")
        if rename.strip() and rename.strip() != name and self.jobs.busy(ui_jobs.stack_key(name)):
            raise actions.ActionError(_("The stack {name} is starting or stopping: rename it afterwards.", name=name))
        on_demand = body.get("on_demand")
        if on_demand not in (None, "", "on", "off"):
            raise actions.ActionError('on_demand must be "on", "off" or "" (as the on_demand setting)')
        saved = ui_jobs.save_stack(name, services, str(body.get("user") or ""), str(body.get("db") or ""),
                                   new=body.get("new") is True, overrides=overrides, rename=rename,
                                   on_demand=on_demand)
        return 200, {"name": saved}

    def stack_remove(self, name: str, _body: dict) -> tuple[int, dict]:
        ui_jobs.remove_stack(name)
        return 200, {}

    def stack_dismiss(self, name: str, _body: dict) -> tuple[int, dict]:
        self.jobs.dismiss(ui_jobs.stack_key(name))
        return 200, {}

    # /api/repos/<name>/<verb>

    def repo_use(self, name: str, body: dict) -> tuple[int, dict]:
        running = body.get("running")
        if running is not None and not isinstance(running, str):
            raise actions.ActionError("running must be keep, stop or move")
        return 200, self.jobs.use_repo(name, running)

    def repo_save(self, name: str, body: dict) -> tuple[int, dict]:
        return 200, {"name": ui_jobs.save_repo(name, body)}

    def repo_remove(self, name: str, _body: dict) -> tuple[int, dict]:
        return 200, {"current": ui_jobs.remove_repo(name)}

    # /api/dbs/<name>/<verb> and /api/users/<name>/<verb>

    def db_save(self, name: str, body: dict) -> tuple[int, dict]:
        return 200, {"name": ui_jobs.save_db(name, body, new=body.get("new") is True)}

    def db_remove(self, name: str, _body: dict) -> tuple[int, dict]:
        return 200, {"stacks": ui_jobs.remove_db(name)}

    def db_password(self, name: str, _body: dict) -> tuple[int, dict]:
        """A database's password, for the eye of its form: never in the state nor in /api/config."""
        return 200, {"password": ui_jobs.db_password(name)}

    def user_save(self, name: str, body: dict) -> tuple[int, dict]:
        return 200, {"name": ui_jobs.save_user(name, body, new=body.get("new") is True)}

    def user_remove(self, name: str, _body: dict) -> tuple[int, dict]:
        return 200, {"stacks": ui_jobs.remove_user(name)}

    def user_rename(self, name: str, body: dict) -> tuple[int, dict]:
        return 200, {"name": ui_jobs.rename_user(name, body)}

    # ------------------------------------------------------------------ logs

    def log_file(self, key: str, which: str) -> Path | None:
        """The log ``which`` (current, previous or install) of an instance, the proxy or a job's instance."""
        if key in (ui_updates.KEY, ui_instance.KEY):
            path = ui_updates.log_path() if key == ui_updates.KEY else ui_instance.log_path()
            return {"current": path, "previous": instances.previous_log_path(path)}.get(which)
        if key.startswith("test:"):
            try:
                root = ui_jobs.backend(Config.load())
            except actions.ActionError:
                return None
            project = key.removeprefix("test:")
            return testruns.files(root, project)["log"] if which == "current" and project in \
                testruns.projects(root) else None
        if frontend.is_key(key):
            return {"current": frontend.log_path(), "previous": instances.previous_log_path(frontend.log_path()),
                    "install": frontend.install_log_path(), "build": frontend.build_log_path()}.get(which)
        if proxy.is_key(key):
            base = proxy.log_path()
        elif key == events.SNS_KEY:
            base = events.sns_log_path()
        elif key == emails.KEY:
            base = emails.log_path()
        elif (inst := instances.load().get(key)) is not None:
            base = Path(inst.log)
        elif key in (current := self.jobs.snapshot()) or any(job["log_key"] == key for job in current.values()):
            base = instances.log_path(key)  # being restarted (forgotten for a moment) or not started yet
        else:
            return None
        if which == "previous":
            return instances.previous_log_path(base)
        if which == "install":
            return None if proxy.is_key(key) or key in (events.SNS_KEY, emails.KEY) else ui_jobs.install_log(key)
        return base if which == "current" else None


# GET: path → (method, the status of an error the user can act on). GitHub's are 502: it did not answer.
GETS: dict[str, tuple[Callable[[Api, Query], object], int]] = {
    "/api/state": (Api.state, 400),
    "/api/services": (Api.services, 400),
    "/api/stacks/from-running": (Api.stack_from_running, 400),
    "/api/config": (Api.config, 400),
    "/api/doctor": (Api.doctor, 400),
    "/api/frontend/options": (Api.frontend_options, 400),
    "/api/proxy/options": (Api.proxy_options, 400),
    "/api/proxy/request": (Api.proxy_request, 400),
    "/api/proxy/route": (Api.proxy_route, 400),
    "/api/proxy/routes": (Api.proxy_routes, 400),
    "/api/events/queues": (Api.events_queues, 400),
    "/api/events/map": (Api.events_map, 400),
    "/api/events/ready": (Api.events_ready, 400),
    "/api/events/template": (Api.events_template, 400),
    "/api/events/peek": (Api.events_peek, 400),
    "/api/update/versions": (Api.versions, 502),
    "/api/update/notes": (Api.notes, 502),
    "/api/views": (Api.views, 400),
    "/api/data": (Api.data, 400),
    "/api/tests": (Api.tests, 400),
}

# POST by its whole path.
POSTS: dict[str, Callable[[Api, dict], tuple[int, dict]]] = {
    "/api/proxy/replay": Api.replay,
    "/api/code/open": Api.open_code,
    "/api/clean": Api.clean,
    "/api/frontend/fix-api": Api.fix_frontend_api,
    "/api/strays/adopt": Api.adopt_strays,
    "/api/strays/stop": Api.stop_strays,
    "/api/bug-report": Api.bug_report,
    "/api/doctor/run": Api.run_doctor,
    "/api/health/run": Api.run_health,
    "/api/health/line": Api.health_line,
    "/api/aws/profile": Api.aws_profile,
    "/api/aws/read": Api.aws_read,
    "/api/aws/login": Api.aws_login,
    "/api/aws/check": Api.aws_check,
    "/api/ui/seen": Api.seen,
    "/api/ui/pick-folder": Api.pick_folder,
    "/api/images/pull": Api.pull_images,
    "/api/data/up": Api.postgres_up,
    "/api/data/down": Api.postgres_down,
    "/api/data/migrate": Api.migrate_db,
    "/api/data/restore": Api.restore_snapshot,
    "/api/data/use-copy": Api.use_copy,
    "/api/data/refresh": Api.refresh_copy,
    "/api/data/test-prepare": Api.prepare_test_dbs,
    "/api/data/test-migrate": Api.migrate_test_dbs,
    "/api/tests/run": Api.run_tests,
    "/api/tests/stop": Api.stop_tests,
    "/api/tests/start-db": Api.start_test_db,
    "/api/migrations/status": Api.migration_status,
    "/api/repos/check": Api.look_at_repo,
    "/api/repos/add": Api.add_repo,
    "/api/proxy/start": Api.start_proxy,
    "/api/run": Api.run,
    "/api/frontend/start": Api.start_frontend,
    "/api/setup/save": Api.save_setup,
    "/api/setups/save": Api.save_setup_as,
    "/api/setups/remove": Api.remove_setup,
    "/api/setups/switch": Api.switch_setup,
    "/api/home/start": Api.home_start,
    "/api/home/stop": Api.home_stop,
    "/api/browser/install": Api.install_extension,
    "/api/browser/redirect": Api.front_redirect,
    "/api/browser/hosts": Api.front_hosts,
    "/api/defaults/save": Api.save_defaults,
    "/api/dbs/test": Api.test_db,
    "/api/config/export": Api.export_config,
    "/api/config/import/plan": Api.plan_import,
    "/api/config/import/apply": Api.apply_import,
    "/api/import-users/search": Api.db_users,
    "/api/import-users/apply": Api.import_db_users,
    "/api/instances/restart": Api.restart_many,
}

# POST /api/<group>/<verb>. Another verb of data is one of Data's own (ui_jobs.data_action).
VERBS: dict[str, dict[str, Callable[[Api, dict], tuple[int, dict]]]] = {
    "events": {"up": Api.events_up, "down": Api.events_down, "send": Api.events_send, "purge": Api.events_purge,
               "dismiss": Api.events_dismiss},
    "update": {"check": Api.update_check, "install": Api.update_install, "skip": Api.update_skip,
               "restart": Api.update_restart, "dismiss": Api.update_dismiss},
    "views": {"create": Api.view_create, "use": Api.view_use, "stop": Api.view_stop, "drop": Api.view_drop,
              "refresh": Api.view_refresh},
    "data": {},
}

# POST /api/<group>/<name>/<verb>.
NAMED: dict[str, dict[str, Callable[[Api, str, dict], tuple[int, dict]]]] = {
    "instances": {"stop": Api.instance_stop, "restart": Api.instance_restart, "on-demand": Api.instance_on_demand,
                  "sleep": Api.instance_sleep, "debug": Api.instance_debug, "forget": Api.instance_forget,
                  "dismiss": Api.instance_dismiss},
    "stacks": {"up": Api.stack_up, "down": Api.stack_down, "save": Api.stack_save, "remove": Api.stack_remove,
               "dismiss": Api.stack_dismiss},
    "repos": {"use": Api.repo_use, "save": Api.repo_save, "remove": Api.repo_remove},
    "dbs": {"save": Api.db_save, "remove": Api.db_remove, "password": Api.db_password},
    "users": {"save": Api.user_save, "remove": Api.user_remove, "rename": Api.user_rename},
}
