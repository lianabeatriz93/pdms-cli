"""The ``pdms ui`` web server: static files, ``/api/state`` and a live ``/api/stream``, only for this machine.

It binds to 127.0.0.1 and trusts nobody else who can reach that port, such as other web pages open in the
browser. The link ``pdms ui`` prints carries a random token for this session; the first request swaps it for an
HttpOnly, SameSite=Strict cookie. Every request must also name this server in ``Host`` (against DNS rebinding) and,
when the browser sends ``Origin``, come from this server's own page.

The state stays in the CLI's files: a hub thread rebuilds it every couple of seconds while someone is watching and
pushes it as server-sent events when it changes.
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
from urllib.parse import parse_qs, urlsplit

from .. import proxy
from .state import build_state

STATIC = resources.files("pdms_cli.ui") / "static"
STATIC_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
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


class Hub:
    """The latest state, rebuilt every ``interval`` seconds while at least one stream is open."""

    def __init__(self, build: Callable[[], dict] = build_state, interval: float = 2.0) -> None:
        self.build, self.interval = build, interval
        self.latest = ""
        self.version = 0
        self.watchers = 0
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pdms-ui-hub", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        with self._changed:
            self._changed.notify_all()

    def refresh(self) -> None:
        payload = json.dumps(self.build(), sort_keys=True)
        with self._changed:
            if payload != self.latest:
                self.latest, self.version = payload, self.version + 1
                self._changed.notify_all()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.watchers:
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001, S110 - a bad tick (a config being saved) must not kill the hub
                    pass
            self._stop.wait(self.interval)

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


def make_handler(token: str, hub: Hub, ping: float = 15.0) -> type[BaseHTTPRequestHandler]:
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

        def allowed(self) -> bool:
            if self.headers.get("Host", "") not in self.own_hosts():
                self.reply(421, "text/plain", b"pdms ui: unknown host\n")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin.removeprefix("http://") not in self.own_hosts():
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
            else:
                self.reply_json(404, {"detail": "not found"})

        def stream(self) -> None:
            """Server-sent events: ``state`` with the whole state on every change, a comment line as keep-alive."""
            self.send_response(200)
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
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


def make_server(host: str, port: int, token: str, hub: Hub) -> proxy.Server:
    """The UI server, bound (``port`` 0 picks a free one); :func:`serve` runs it."""
    return proxy.Server((host, port), make_handler(token, hub))


def new_token() -> str:
    return secrets.token_urlsafe(24)


def serve(server: proxy.Server, hub: Hub) -> None:
    hub.start()
    try:
        server.serve_forever()
    finally:
        hub.stop()
        server.server_close()
