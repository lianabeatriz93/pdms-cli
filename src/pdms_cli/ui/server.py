"""The ``pdms ui`` web server: static files, ``/api/state`` and a live ``/api/stream``, only for this machine.

It binds to 127.0.0.1 and trusts nobody else who can reach that port, such as other web pages open in the
browser. The link ``pdms ui`` prints carries a random token for this session; the first request swaps it for an
HttpOnly, SameSite=Strict cookie. Every request must also name this server in ``Host`` (against DNS rebinding) and,
when the browser sends ``Origin``, come from this server's own page.

The state stays in the CLI's files: a hub thread rebuilds it every couple of seconds while someone is watching and
pushes it as server-sent events when it changes. Actions are JSON POSTs, which must also come from this server's page
(``Origin``); they answer 202 and the job shows in the state, 409 with a ``decision`` when the user has to answer
first, or 400 with an ``error``.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from collections.abc import Callable
from http import cookies
from http.server import BaseHTTPRequestHandler
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from rich.errors import MarkupError
from rich.text import Text

from .. import actions, events, instances, proxy
from ..config import Config
from ..logview import LogFollower
from . import jobs as ui_jobs
from .state import build_state

STATIC = resources.files("pdms_cli.ui") / "static"
STATIC_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # a Terraform environment folder, never a path
# Fixed, not from mimetypes: on Windows that reads the registry, which may call .js text/plain (blocked by nosniff).
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
MAX_BODY = 64 * 1024
LOG_LINES = 200
MAX_LOG_LINES = 20000  # the local SNS log: a publish takes a line per line of its JSON
LOG_POLL = 0.25
PEEK_LIMIT = 50


class Hub:
    """The latest state, rebuilt every ``interval`` seconds while at least one stream is open."""

    def __init__(self, build: Callable[[], dict] = build_state, interval: float = 2.0) -> None:
        self.build, self.interval = build, interval
        self.latest = ""
        self.version = 0
        self.watchers = 0
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pdms-ui-hub", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._changed:
            self._changed.notify_all()

    def refresh(self) -> None:
        payload = json.dumps(self.build(), sort_keys=True)
        with self._changed:
            if payload != self.latest:
                self.latest, self.version = payload, self.version + 1
                self._changed.notify_all()

    def poke(self) -> None:
        """Rebuild now instead of at the next tick (an action changed something)."""
        self._wake.set()

    def sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.watchers:
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001, S110 - a bad tick (a config being saved) must not kill the hub
                    pass
            self._wake.wait(self.interval)
            self._wake.clear()

    def watch(self) -> None:
        with self._changed:
            self.watchers += 1
        if not self.latest:
            self.refresh()

    def unwatch(self) -> None:
        with self._changed:
            self.watchers -= 1

    def wait(self, seen: int, timeout: float) -> tuple[int, str]:
        """``(version, state)`` once there is a version newer than ``seen``, or the same after ``timeout``."""
        with self._changed:
            self._changed.wait_for(lambda: self.version > seen or self._stop.is_set(), timeout)
            return self.version, self.latest


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
    if isinstance(decision, actions.PointFrontend):
        return {"decision": "point_frontend", "url": decision.url}
    return {"decision": type(decision).__name__, "error": str(decision)}


def launch_options(body: dict) -> dict:
    """The user, database, install choice and confirmation of a restart or a stack's up."""
    install = body.get("install")
    if install not in (None, True, False):
        raise actions.ActionError("install must be true, false or null")
    return {
        "user": str(body.get("user") or "") or None, "db": str(body.get("db") or "") or None,
        "install": install, "confirmed": body.get("confirmed") is True,
    }


def run_options(body: dict) -> dict:
    """The service, port, user, database, install choice and confirmation of starting one service."""
    service, port = body.get("service"), body.get("port")
    if not isinstance(service, str) or not service:
        raise actions.ActionError("service must be a service path of the current repo")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise actions.ActionError("port must be a number between 1 and 65535, or null for the next free one")
    return {"service": service, "port": port, **launch_options(body)}


def env_name(value: object) -> str:
    env = str(value or "dev")
    if not ENV_NAME.match(env):
        raise actions.ActionError(f"not an environment name: {env!r}")
    return env


def proxy_options(body: dict) -> dict:
    """The port, env, remote, user and frontend choice of a proxy start."""
    port, frontend = body.get("port", ui_jobs.PROXY_PORT), body.get("frontend")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise actions.ActionError("port must be a number between 1 and 65535")
    if frontend not in (None, True, False):
        raise actions.ActionError("frontend must be true, false or null")
    return {
        "port": port, "env": env_name(body.get("env")), "remote": str(body.get("remote") or "") or None,
        "no_remote": body.get("no_remote") is True, "user": str(body.get("user") or "") or None, "frontend": frontend,
    }


def make_handler(
    token: str, hub: Hub, jobs: ui_jobs.Jobs | None = None, ping: float = 15.0
) -> type[BaseHTTPRequestHandler]:
    jobs = jobs or ui_jobs.Jobs(hub.poke)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pdms-ui"

        def log_message(self, *args: object) -> None:
            pass

        # ------------------------------------------------------------------ checks

        @property
        def port(self) -> int:
            return int(self.server.server_address[1])  # type: ignore[index]

        def own_hosts(self) -> set[str]:
            return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

        @property
        def cookie_name(self) -> str:
            return f"pdms_ui_{self.port}"  # cookies are not per port: one per server

        def authorized(self) -> bool:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            morsel = jar.get(self.cookie_name)
            return morsel is not None and secrets.compare_digest(morsel.value, token)

        def allowed(self, origin_required: bool = False) -> bool:
            if self.headers.get("Host", "") not in self.own_hosts():
                self.reply(421, "text/plain", b"pdms ui: unknown host\n")
                return False
            origin = self.headers.get("Origin")
            if (origin is None and origin_required) or (
                origin is not None and origin.removeprefix("http://") not in self.own_hosts()
            ):
                self.reply(403, "text/plain", b"pdms ui: requests from other pages are not allowed\n")
                return False
            return True

        # ------------------------------------------------------------------ replies

        def reply(self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def reply_json(self, status: int, data: object) -> None:
            self.reply(status, "application/json", json.dumps(data).encode())

        def send_events_headers(self) -> None:
            self.send_response(200)
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def event(self, name: str, data: object) -> None:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        def static(self, name: str) -> None:
            file = STATIC / name
            content_type = CONTENT_TYPES.get("." + name.rsplit(".", 1)[-1])
            if not STATIC_NAME.match(name) or not content_type or not file.is_file():
                self.reply_json(404, {"detail": "not found"})
                return
            self.reply(200, content_type, file.read_bytes())

        # ------------------------------------------------------------------ routes

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            if not self.allowed():
                return
            url = urlsplit(self.path)
            given = parse_qs(url.query).get("token", [""])[0]
            if url.path == "/" and given:
                if not secrets.compare_digest(given, token):
                    self.reply(403, "text/plain", b"pdms ui: wrong token; open the link pdms ui printed\n")
                    return
                self.reply(303, "text/plain", b"", {
                    "Location": "/",
                    "Set-Cookie": f"{self.cookie_name}={token}; HttpOnly; SameSite=Strict; Path=/",
                })
                return
            if not self.authorized():
                self.reply(401, "text/plain", b"pdms ui: open the link pdms ui printed (it carries the token)\n")
                return
            if url.path == "/":
                self.static("index.html")
            elif url.path.startswith("/static/"):
                self.static(url.path[len("/static/"):])
            elif url.path == "/api/state":
                self.reply_json(200, json.loads(hub.latest) if hub.latest and hub.watchers else hub.build())
            elif url.path == "/api/stream":
                self.stream()
            elif url.path == "/api/services":
                self.services()
            elif url.path in ("/api/proxy/options", "/api/proxy/routes"):
                self.proxy_info(url.path.rsplit("/", 1)[-1], parse_qs(url.query))
            elif url.path in ("/api/events/queues", "/api/events/map", "/api/events/peek", "/api/events/template"):
                self.events_info(url.path.rsplit("/", 1)[-1], parse_qs(url.query))
            elif url.path in ("/api/logs", "/api/logs/stream"):
                self.logs(parse_qs(url.query), live=url.path.endswith("/stream"))
            else:
                self.reply_json(404, {"detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.allowed(origin_required=True):
                return
            if not self.authorized():
                self.reply(401, "text/plain", b"pdms ui: open the link pdms ui printed (it carries the token)\n")
                return
            # A JSON body cannot be sent cross-site without a preflight, which this server never answers.
            if self.headers.get_content_type() != "application/json":
                self.reply_json(415, {"error": "expected application/json"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.reply_json(413, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.reply_json(400, {"error": "invalid JSON"})
                return
            if not isinstance(body, dict):
                self.reply_json(400, {"error": "expected a JSON object"})
                return
            try:
                status, data = self.act(urlsplit(self.path).path, body)
            except actions.ActionError as exc:
                status, data = 400, {"error": plain(exc.message)}
            except actions.Decision as decision:
                status, data = 409, decision_body(decision)
            hub.poke()
            self.reply_json(status, data)

        def services(self) -> None:
            try:
                root, services = ui_jobs.repo_services(Config.load())
            except actions.ActionError as exc:
                self.reply_json(400, {"error": plain(exc.message)})
                return
            self.reply_json(200, {"root": str(root), "services": services, "consumers": ui_jobs.repo_consumers(root)})

        def proxy_info(self, which: str, query: dict[str, list[str]]) -> None:
            try:
                cfg = Config.load()
                if which == "options":
                    data = ui_jobs.proxy_options(cfg)
                else:
                    data = ui_jobs.proxy_routes(cfg, env_name(env) if (env := query.get("env", [""])[0]) else None)
            except actions.ActionError as exc:
                self.reply_json(400, {"error": plain(exc.message)})
                return
            self.reply_json(200, data)

        def events_info(self, which: str, query: dict[str, list[str]]) -> None:
            def arg(name: str) -> str:
                return query.get(name, [""])[0]

            try:
                cfg = Config.load()
                if which == "queues":
                    data: object = ui_jobs.events_queues(cfg)
                elif which == "map":
                    data = ui_jobs.events_map(cfg)
                elif which == "template":
                    data = ui_jobs.events_template(cfg, arg("type"))
                else:
                    try:
                        limit = max(1, min(int(arg("limit") or PEEK_LIMIT), PEEK_LIMIT))
                    except ValueError:
                        limit = PEEK_LIMIT
                    data = {"queue": arg("queue"), "messages": ui_jobs.events_peek(cfg, arg("queue"), limit)}
            except actions.ActionError as exc:
                self.reply_json(400, {"error": plain(exc.message)})
                return
            except OSError as exc:  # ElasticMQ went away in the middle
                self.reply_json(502, {"error": f"ElasticMQ: {exc}"})
                return
            self.reply_json(200, data)

        def act_on_events(self, verb: str, body: dict) -> tuple[int, dict]:
            cfg = Config.load()
            if verb == "up":
                broker = body.get("broker", True)
                if not isinstance(broker, bool):
                    raise actions.ActionError("broker must be true or false")
                return 202, {"job": jobs.events_up(broker=broker, **launch_options(body)).key}
            if verb == "down":
                return 202, {"job": jobs.events_down().key}
            if verb == "send":
                target, message = body.get("target"), body.get("body")
                if not isinstance(target, str) or not target or not isinstance(message, (str, type(None))):
                    raise actions.ActionError("target must be an event type or a queue, body a JSON text")
                return 200, ui_jobs.events_send(cfg, target, message, direct=body.get("direct") is True)
            if verb == "purge":
                queues = body.get("queues")
                if not isinstance(queues, list) or not all(isinstance(name, str) for name in queues):
                    raise actions.ActionError("queues must be a list of queue names ([] for every queue)")
                return 200, {"purged": ui_jobs.events_purge(cfg, queues)}
            if verb == "dismiss":
                jobs.dismiss(ui_jobs.EVENTS_KEY)
                return 200, {}
            return 404, {"error": "not found"}

        def act(self, path: str, body: dict) -> tuple[int, dict]:
            if path.startswith("/api/events/") and path.count("/") == 3:
                try:
                    return self.act_on_events(path.rsplit("/", 1)[-1], body)
                except OSError as exc:  # ElasticMQ went away in the middle
                    return 502, {"error": f"ElasticMQ: {exc}"}
            if path == "/api/clean":
                return 200, {"forgotten": ui_jobs.forget_stopped()}
            if path == "/api/proxy/start":
                return 202, {"job": jobs.start_proxy(**proxy_options(body)).key}
            if path == "/api/run":
                return 202, {"job": jobs.start(**run_options(body)).key}
            parts = path.split("/")
            if len(parts) != 5 or parts[:2] != ["", "api"] or parts[2] not in ("instances", "stacks"):
                return 404, {"error": "not found"}
            if parts[2] == "stacks":
                return self.act_on_stack(unquote(parts[3]), parts[4], body)
            key, verb = unquote(parts[3]), parts[4]
            if verb == "stop":
                return 202, {"job": jobs.stop(key).key}
            if verb == "restart":
                job = jobs.restart(key, **launch_options(body))
                return 202, {"job": job.key}
            if verb == "forget":
                ui_jobs.forget(key)
                return 200, {}
            if verb == "dismiss":
                jobs.dismiss(key)
                return 200, {}
            return 404, {"error": "not found"}

        def act_on_stack(self, name: str, verb: str, body: dict) -> tuple[int, dict]:
            if verb in ("up", "down"):
                job = jobs.up(name, **launch_options(body)) if verb == "up" else jobs.down(name)
                return (202, {"job": job.key}) if job else (200, {"job": None})
            if verb == "save":
                services = body.get("services")
                if not isinstance(services, list) or not all(isinstance(svc, str) for svc in services):
                    raise actions.ActionError("services must be a list of service paths")
                ui_jobs.save_stack(name, services, str(body.get("user") or ""), str(body.get("db") or ""),
                                   new=body.get("new") is True)
                return 200, {}
            if verb == "remove":
                ui_jobs.remove_stack(name)
                return 200, {}
            if verb == "dismiss":
                jobs.dismiss(ui_jobs.stack_key(name))
                return 200, {}
            return 404, {"error": "not found"}

        # ------------------------------------------------------------------ logs

        def log_file(self, key: str, which: str) -> Path | None:
            """The log ``which`` (current, previous or install) of an instance, the proxy or a job's instance."""
            if proxy.is_key(key):
                base = proxy.log_path()
            elif key == events.SNS_KEY:
                base = events.sns_log_path()
            elif (inst := instances.load().get(key)) is not None:
                base = Path(inst.log)
            elif key in (current := jobs.snapshot()) or any(job["log_key"] == key for job in current.values()):
                base = instances.log_path(key)  # being restarted (forgotten for a moment) or not started yet
            else:
                return None
            if which == "previous":
                return instances.previous_log_path(base)
            if which == "install":
                return None if proxy.is_key(key) or key == events.SNS_KEY else ui_jobs.install_log(key)
            return base if which == "current" else None

        def logs(self, query: dict[str, list[str]], live: bool) -> None:
            key = query.get("key", [""])[0]
            which = query.get("which", ["current"])[0]
            try:
                lines = max(1, min(int(query.get("lines", [LOG_LINES])[0]), MAX_LOG_LINES))
            except ValueError:
                lines = LOG_LINES
            path = self.log_file(key, which)
            if path is None:
                self.reply_json(404, {"error": f"no log for {key!r}"})
                return
            if not live:
                text = instances.tail(str(path), lines)
                self.reply_json(200, {"key": key, "which": which, "exists": path.exists(), "lines": text.splitlines()})
                return
            self.follow(path, lines)

        def follow(self, path: Path, lines: int) -> None:
            """Server-sent events: ``lines`` with the tail and then each batch of new lines, ``reset`` when the file
            was replaced (the service restarted and its log rotated)."""
            self.send_events_headers()
            follower = LogFollower(str(path))
            idle = 0.0
            try:
                self.event("lines", follower.skip_to_tail(lines))
                while not hub.stopped:
                    inode = follower.inode
                    new = follower.read_new()
                    if inode is not None and follower.inode != inode:
                        self.event("reset", [])
                    if new:
                        self.event("lines", new)
                        idle = 0.0
                    elif (idle := idle + LOG_POLL) >= ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
                    hub.sleep(LOG_POLL)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def stream(self) -> None:
            """Server-sent events: ``state`` with the whole state on every change, a comment line as keep-alive."""
            self.send_events_headers()
            hub.watch()
            seen = 0
            try:
                while not hub.stopped:
                    version, state = hub.wait(seen, ping)
                    if version > seen:
                        self.wfile.write(f"event: state\ndata: {state}\n\n".encode())
                        seen = version
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unwatch()

    return Handler


def make_app() -> tuple[Hub, ui_jobs.Jobs]:
    """The hub and the jobs of one ``pdms ui``: the state carries the jobs, and a job's change refreshes it."""
    hub: Hub
    jobs = ui_jobs.Jobs(lambda: hub.poke())
    hub = Hub(build=lambda: build_state(jobs=jobs.snapshot()))
    return hub, jobs


def make_server(host: str, port: int, token: str, hub: Hub, jobs: ui_jobs.Jobs | None = None) -> proxy.Server:
    """The UI server, bound (``port`` 0 picks a free one); :func:`serve` runs it."""
    return proxy.Server((host, port), make_handler(token, hub, jobs))


def new_token() -> str:
    return secrets.token_urlsafe(24)


def serve(server: proxy.Server, hub: Hub) -> None:
    hub.start()
    try:
        server.serve_forever()
    finally:
        hub.stop()
        server.server_close()
