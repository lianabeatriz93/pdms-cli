"""The ``pdms ui`` web server: static files, ``/api/state`` and a live ``/api/stream``, only for this machine.

It binds to 127.0.0.1 and trusts nobody else who can reach that port, such as other web pages open in the
browser. The link ``pdms ui`` prints carries a random token for this session; the first request swaps it for an
HttpOnly, SameSite=Strict cookie. Every request must also name this server in ``Host`` (against DNS rebinding) and,
when the browser sends ``Origin``, come from this server's own page.

The state stays in the CLI's files: a hub thread rebuilds it every couple of seconds while someone is watching and
pushes it as server-sent events when it changes. Actions are JSON POSTs, which must also come from this server's page
(``Origin``). What each route does is api.py's; this module checks who asks, reads the body, writes the reply, and
streams the state and the logs.

Database passwords never go in the state nor in ``/api/config``: the page asks for one, with a POST, only when the
user clicks to see it.
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
from urllib.parse import parse_qs, urlsplit

from .. import actions, browser, emails, i18n, instances, proxy
from ..config import Config
from ..logview import LogFollower
from . import instance as ui_instance
from .api import Api
from . import recent as ui_recent
from . import jobs as ui_jobs
from .control import Control
from .state import build_state

STATIC = resources.files("pdms_cli.ui") / "static"
# A file of the static folder or of one folder in it (js/main.js): no "..", no hidden files.
STATIC_NAME = re.compile(r"^(?:[a-z0-9][a-z0-9_-]*/)?[a-z0-9][a-z0-9_.-]*$")
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
# An email's HTML (the Emails tab shows it in a sandboxed frame): its inline styles, pictures and fonts, no scripts,
# no forms, and only inside pdms ui.
EMAIL_CSP = ("default-src 'none'; style-src 'unsafe-inline' https:; img-src https: data:; font-src https: data:; "
             "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
MAX_BODY = 1024 * 1024  # an imported configuration travels in the body
LOG_LINES = 200
MAX_LOG_LINES = 20000  # the local SNS log: a publish takes a line per line of its JSON
LOG_POLL = 0.25
MAX_LOG_SOURCES = 40  # logs on one stream of the log dock


class Hub:
    """The latest state, rebuilt every ``interval`` seconds while at least one stream is open, or always while
    ``always()`` says so (the desktop notifications need it with no page open)."""

    def __init__(self, build: Callable[[], dict] = build_state, interval: float = 2.0,
                 always: Callable[[], bool] = lambda: False) -> None:
        self.build, self.interval, self.always = build, interval, always
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
            if self.watchers or self.always():
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


def make_handler(
    token: str, hub: Hub, jobs: ui_jobs.Jobs | None = None, ping: float = 15.0, control: Control | None = None,
) -> type[BaseHTTPRequestHandler]:
    jobs = jobs or ui_jobs.Jobs(hub.poke)
    control = control or Control("")
    api = Api(hub, jobs, control)

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

        def email_html(self, ident: str) -> None:
            """An email's HTML for the frame of the Emails tab: its own styles and pictures, never a script."""
            html = emails.html_of(ident)
            if html is None:
                self.reply(404, "text/plain", b"pdms ui: no such email\n")
                return
            self.reply(200, "text/html; charset=utf-8", html.encode("utf-8"), {"Content-Security-Policy": EMAIL_CSP})

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
            file = STATIC.joinpath(*name.split("/"))
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
            query = parse_qs(url.query)
            if url.path == "/":
                self.static("index.html")
            elif url.path.startswith("/static/"):
                self.static(url.path[len("/static/"):])
            elif url.path == "/api/stream":
                self.stream()
            elif url.path == "/api/emails/html":
                self.email_html(query.get("id", [""])[0])
            elif url.path in ("/api/logs", "/api/logs/stream"):
                self.logs(query, live=url.path.endswith("/stream"))
            elif url.path == "/api/logs/streams":
                self.follow_many(query)
            elif (answer := api.get(url.path, query)) is not None:
                self.reply_json(*answer)
            else:
                self.reply_json(404, {"detail": "not found"})

        def show_request(self) -> bool:
            """A second ``pdms ui`` asking this one to show itself, with the token it read from ui.json. It sends a
            header that no other page can send without a preflight (never answered), so it needs no cookie."""
            given = self.headers.get(ui_instance.TOKEN_HEADER)
            if given is None or urlsplit(self.path).path != ui_instance.SHOW_PATH:
                return False
            if not self.allowed():
                return True
            self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_BODY))
            if not secrets.compare_digest(given, token):
                self.reply_json(403, {"error": "wrong token"})
                return True
            control.show()
            self.reply_json(200, {})
            return True

        def extension_request(self) -> bool:
            """The browser extension asking what to redirect, or its panel turning the redirect on or off
            (browser.py). Only an extension sends its ``Origin``, so it needs no cookie; it learns no more than the
            hosts and the local frontend's URL."""
            path = urlsplit(self.path).path
            if path not in (browser.STATE_PATH, browser.REDIRECT_PATH):
                return False
            if self.headers.get("Host", "") not in self.own_hosts() or not browser.is_extension(
                    self.headers.get("Origin", "")):
                self.reply(403, "text/plain", b"pdms ui: only the pdms browser extension asks this\n")
                return True
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}")
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if path == browser.REDIRECT_PATH:
                if not isinstance(body.get("on"), bool):
                    self.reply_json(400, {"error": "on must be true or false"})
                    return True
                actions.set_front_redirect(Config.load(), body["on"])
                hub.poke()
            browser.record_seen(body.get("browser"), body.get("version"))
            self.reply_json(200, browser.answer(Config.load()))
            return True

        def do_POST(self) -> None:  # noqa: N802
            if self.show_request() or self.extension_request():
                return
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
            status, data = api.post(urlsplit(self.path).path, body)
            hub.poke()
            self.reply_json(status, data)

        # ------------------------------------------------------------------ logs

        def logs(self, query: dict[str, list[str]], live: bool) -> None:
            key = query.get("key", [""])[0]
            which = query.get("which", ["current"])[0]
            try:
                lines = max(1, min(int(query.get("lines", [LOG_LINES])[0]), MAX_LOG_LINES))
            except ValueError:
                lines = LOG_LINES
            path = api.log_file(key, which)
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

        def follow_many(self, query: dict[str, list[str]]) -> None:
            """Several logs on one connection (the log dock: a browser keeps only about six open per server).
            Server-sent events ``lines`` with ``{"key", "lines"}`` (first each log's tail, then its new lines) and
            ``reset`` with ``{"key"}`` when one was replaced. ``which`` applies when there is a single key."""
            keys = [key for key in query.get("keys", [""])[0].split(",") if key][:MAX_LOG_SOURCES]
            which = query.get("which", ["current"])[0] if len(keys) == 1 else "current"
            try:  # 0: only what comes from now on (All logs)
                lines = max(0, min(int(query.get("lines", [LOG_LINES])[0]), MAX_LOG_LINES))
            except ValueError:
                lines = LOG_LINES
            found = [(key, path) for key in dict.fromkeys(keys) if (path := api.log_file(key, which)) is not None]
            if not found:
                self.reply_json(404, {"error": "no log for those keys"})
                return
            self.send_events_headers()
            followers = [(key, LogFollower(str(path))) for key, path in found]
            idle = 0.0
            try:
                for key, follower in followers:
                    self.event("lines", {"key": key, "lines": follower.skip_to_tail(lines)})
                while not hub.stopped:
                    sent = False
                    for key, follower in followers:
                        inode = follower.inode
                        new = follower.read_new()
                        if inode is not None and follower.inode != inode:
                            self.event("reset", {"key": key})
                        if new:
                            self.event("lines", {"key": key, "lines": new})
                            sent = True
                    if sent:
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
    recent = ui_recent.Recent()
    notify = {"on": Config.load().defaults.notify}

    def build() -> dict:
        state = build_state(jobs=jobs.snapshot(), doctor=jobs.doctor.summary(), health=jobs.health.summary(),
                            changes=jobs.changes.summary(), tests=jobs.tests.summary(), aws=jobs.aws.summary())
        i18n.set_language(state["language"])  # the server's own messages follow a change made in the CLI too
        notify["on"] = state["notify"]
        recent.observe(state, notify=state["notify"])
        state["recent"] = recent.items()
        return state

    hub = Hub(build=build, always=lambda: notify["on"])
    return hub, jobs


def make_server(
    host: str, port: int, token: str, hub: Hub, jobs: ui_jobs.Jobs | None = None, control: Control | None = None,
) -> proxy.Server:
    """The UI server, bound (``port`` 0 picks a free one); :func:`serve` runs it."""
    return proxy.Server((host, port), make_handler(token, hub, jobs, control=control))


def new_token() -> str:
    return secrets.token_urlsafe(24)


def serve(server: proxy.Server, hub: Hub) -> None:
    hub.start()
    try:
        server.serve_forever()
    finally:
        hub.stop()
        server.server_close()
