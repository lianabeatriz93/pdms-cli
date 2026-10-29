"""Local API gateway: one port for every PDMS service.

Each request is matched against the repo's Terraform routes and sent to the local instance of that service when
one is running from the current repo; otherwise it goes to the remote API (e.g. dev), unchanged. It also answers
CORS preflights (the services have no CORS middleware; API Gateway does that in AWS), can impersonate a dev user
on local services through the ``X-Dev-*`` headers, and serves a Swagger UI with the live specs of local services.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socketserver
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import captures, instances, repos, trace
from .config import Config, DevUser
from .i18n import _
from .routes import Route, load_routes, match

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "host", "content-length",
}
CORS_RESPONSE_HEADERS = {
    "access-control-allow-origin", "access-control-allow-credentials", "access-control-allow-methods",
    "access-control-allow-headers", "access-control-expose-headers", "access-control-max-age",
}
DOCS_PREFIX = "/_pdms/openapi/"
WAKE_TIMEOUT = 180  # seconds a request waits for its asleep service to start
IDLE_CHECK_EVERY = 30  # seconds between two looks for on-demand services to put to sleep
KEY = "proxy"  # how pdms ps, stop and logs call it (shown as proxy@<port>)


def state_path() -> Path:
    return instances.state_dir() / "proxy.json"


def log_path() -> Path:
    """Where the proxy started with ``--background`` writes its requests."""
    return instances.log_path(KEY)


def frontend_change_path() -> Path:
    """The change made to ``frontend/.env.local`` for the running proxy, to undo it even after a crash."""
    return instances.state_dir() / "proxy-frontend.json"


def remember_frontend_change(change: dict) -> None:
    frontend_change_path().parent.mkdir(parents=True, exist_ok=True)
    frontend_change_path().write_text(json.dumps(change), encoding="utf-8")


def frontend_change() -> str:
    """The ``frontend/.env.local`` pointed to the running proxy, if any."""
    try:
        return str(json.loads(frontend_change_path().read_text(encoding="utf-8"))["path"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return ""


def restore_frontend_change() -> str | None:
    """Undo the recorded ``.env.local`` change, if any; returns the restored file."""
    try:
        change = json.loads(frontend_change_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        frontend_change_path().unlink(missing_ok=True)
        return None
    restored = repos.restore_frontend(change)
    frontend_change_path().unlink(missing_ok=True)
    return change["path"] if restored else None


def running_proxy() -> dict | None:
    """``{"pid", "port", ...}`` of the proxy currently running on this machine, if any."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if instances.process_alive(int(data["pid"]), float(data.get("created", 0))) else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None


def display_key(running: dict) -> str:
    return f"{KEY}@{running['port']}"


def is_key(key: str) -> bool:
    return key == KEY or key.startswith(f"{KEY}@")


def forget(pid: int) -> None:
    """Remove the state file if it still belongs to ``pid``."""
    try:
        if json.loads(state_path().read_text(encoding="utf-8")).get("pid") == pid:
            state_path().unlink()
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def stop(running: dict, timeout: float = 10) -> None:
    """Stop the proxy process (a background one or another terminal's) and forget it."""
    pid = int(running["pid"])
    instances.kill_tree(pid, float(running.get("created", 0)), timeout)
    forget(pid)


def dev_headers(user: DevUser) -> dict[str, str]:
    return {
        "X-Dev-User-Id": user.user_id,
        "X-Dev-Username": user.username,
        "X-Dev-Roles": user.roles,
        "X-Dev-First-Name": user.first_name,
        "X-Dev-Last-Name": user.last_name,
    }


def normalize(path: str) -> str:
    """Drop an API Gateway stage prefix (``/dev/api/v1/...`` -> ``/api/v1/...``)."""
    parts = path.split("/")
    if len(parts) > 2 and parts[1] != "api" and parts[2] == "api":
        return "/" + "/".join(parts[2:])
    return path


@dataclass
class Target:
    kind: str  # local | remote | missing | other-repo
    instance: instances.Instance | None = None  # local: may be asleep, see Gateway.awaken
    note: str = ""


class WakeFailed(Exception):
    """An asleep service could not be started for a request; the message says why."""


def _wake(instance: instances.Instance) -> instances.Instance:
    from . import actions  # actions imports this module

    try:
        return actions.wake_service(Config.load(), instance)
    except actions.LocalEventsDown as exc:
        raise WakeFailed(_("{key} publishes to the local ElasticMQ, which is not running: start it with "
                           "pdms events up", key=instance.key)) from exc
    except actions.ActionError as exc:
        raise WakeFailed(exc.message) from exc


def _idle_seconds() -> float:
    """defaults.idle_minutes, read again each time: changing it in Settings applies to the running proxy."""
    return Config.load().defaults.idle_minutes * 60


@dataclass
class Gateway:
    routes: list[Route]
    backend: Path
    remote: str | None  # e.g. https://<id>.execute-api.us-east-1.amazonaws.com/dev
    impersonate: DevUser | None = None
    timeout: float = 300  # seconds to wait for the answer of a service or the remote API
    log: Callable[..., None] = lambda *args: None  # (method, path, status, target, seconds, capture id, db detail)
    recorder: captures.Recorder | None = None  # the background proxy keeps each request for pdms ui
    # The background proxy also says when a request arrives (method, path, target, capture id), so pdms ui shows it
    # in progress; its line when answered carries the same id.
    log_start: Callable[..., None] = lambda *args: None
    # On-demand services: how one is started for a request, how long it may stay without requests, and where the
    # proxy says it woke or put one to sleep.
    wake: Callable[[instances.Instance], instances.Instance] = _wake
    wake_timeout: float = WAKE_TIMEOUT
    idle_seconds: Callable[[], float] = _idle_seconds
    note: Callable[[str], None] = lambda text: print(f"# {datetime.now():%H:%M:%S} {text}", flush=True)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _waking: dict[str, threading.Lock] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)  # instance key -> time.monotonic()
    _in_progress: Counter = field(default_factory=Counter)  # instance key -> requests it is answering

    # ------------------------------------------------------------------ targets

    def local_instances(self) -> dict[str, list[instances.Instance]]:
        found: dict[str, list[instances.Instance]] = {}
        for inst in instances.load().values():
            if inst.present() and not inst.is_consumer:  # event consumers have no HTTP API
                found.setdefault(str(Path(inst.service).resolve()), []).append(inst)
        return found

    def target(self, route: Route | None, running: dict[str, list[instances.Instance]] | None = None) -> Target:
        running = self.local_instances() if running is None else running
        if route is not None:
            service = str((self.backend / route.service).resolve())
            if service in running:  # a running one before one that would have to wake up
                return Target("local", min(running[service], key=lambda i: (i.asleep, i.port)))
            others = [i for path, items in running.items() if Path(path).name == Path(service).name for i in items]
            if others:
                note = _("{service} is running from another repo ({key}); not mixing versions.",
                         service=route.service, key=others[0].key)
                return Target("remote" if self.remote else "other-repo", others[0], note)
        return Target("remote") if self.remote else Target("missing")

    # ------------------------------------------------------------------ docs

    def local_specs(self) -> list[tuple[str, instances.Instance]]:
        items = []
        backend = str(self.backend.resolve())
        for path, insts in sorted(self.local_instances().items()):
            if path.startswith(backend):
                for inst in insts:
                    if not inst.asleep:  # nothing answers for it to ask for its spec
                        items.append((inst.key, inst))
        return items

    # ------------------------------------------------------------------ on demand

    def awaken(self, inst: instances.Instance) -> instances.Instance:
        """The running instance of an asleep one, started for a request that waits meanwhile; requests that arrive
        while it starts wait for the same start. Raises :class:`WakeFailed`."""
        with self._lock:
            lock = self._waking.setdefault(inst.service, threading.Lock())
        with lock:
            awake = next((i for i in instances.load().values()
                          if i.service == inst.service and i.alive() and not i.is_consumer), None)
            if awake:  # another request woke it while this one waited
                return awake
            self.note(_("waking {key}", key=inst.key))
            started = time.monotonic()
            try:
                awake = self.wake(inst)
            except WakeFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - the request gets why, not a dropped connection
                raise WakeFailed(_("{key} could not start: {error}", key=inst.key, error=exc)) from exc
            self.used(awake.key)  # not idle while it starts
            while time.monotonic() - started < self.wake_timeout:
                health = instances.health(awake)
                if health.state in ("ok", "busy"):
                    self.note(_("{key} awake in {seconds} s", key=awake.key,
                                seconds=f"{time.monotonic() - started:.1f}"))
                    return awake
                if health.state == "stopped":
                    instances.forget(awake.key)
                    instances.register_asleep(  # asleep again, for the next request to try (or for its log)
                        Path(awake.service), host=awake.host, port=awake.port, user=awake.user, db=awake.db,
                        reload=awake.reload, events=awake.events, parallel=awake.parallel,
                    )
                    raise WakeFailed(_("{key} exited while starting; see pdms logs {key}", key=awake.key))
                if health.state == "error":
                    raise WakeFailed(_("{key} failed to load: {detail}", key=awake.key, detail=health.detail))
                time.sleep(0.25)
            raise WakeFailed(_("{key} is not answering {seconds} s after starting", key=awake.key,
                               seconds=f"{self.wake_timeout:g}"))

    def used(self, key: str) -> None:
        with self._lock:
            self._last_request[key] = time.monotonic()

    def sleepy(self, now: float | None = None) -> list[instances.Instance]:
        """The on-demand services of this repo that answer no request and got none for ``idle_seconds``."""
        now = time.monotonic() if now is None else now
        idle = self.idle_seconds()
        backend = self.backend.resolve()
        found = []
        for inst in instances.load().values():
            if not inst.on_demand or inst.is_consumer or not inst.alive():
                continue
            if not Path(inst.service).resolve().is_relative_to(backend):
                continue  # another repo's: this proxy would never wake it
            with self._lock:
                last = self._last_request.setdefault(inst.key, now)  # first seen: counts from now
                if self._in_progress[inst.key] == 0 and now - last >= idle:
                    found.append(inst)
        return found

    def put_idle_to_sleep(self, now: float | None = None) -> list[str]:
        slept = []
        for inst in self.sleepy(now):
            instances.sleep(inst)
            with self._lock:
                self._last_request.pop(inst.key, None)
            self.note(_("{key} asleep after {minutes} min without requests", key=inst.key,
                        minutes=f"{self.idle_seconds() / 60:g}"))
            slept.append(inst.key)
        return slept

    def watch_idle(self, stopped: threading.Event) -> None:
        failed = ""
        while not stopped.wait(IDLE_CHECK_EVERY):
            try:
                self.put_idle_to_sleep()
            except Exception as error:  # noqa: BLE001 - a failed look must not stop the watch
                if str(error) != failed:  # once, not every few seconds
                    failed = str(error)
                    print(f"[pdms] could not put idle services to sleep: {error}", file=sys.stderr, flush=True)

    def spec_for(self, key: str) -> dict | None:
        specs = dict(self.local_specs())
        if key == "all":
            return merged_spec([(k, instances.fetch_openapi(i)) for k, i in specs.items()])
        if key not in specs:
            return None
        spec = instances.fetch_openapi(specs[key])
        if spec is not None:
            spec["servers"] = [{"url": "/"}]
        return spec


def merged_spec(specs: list[tuple[str, dict | None]]) -> dict:
    merged: dict = {
        "openapi": "3.1.0", "info": {"title": "pdms proxy · all local services", "version": "local"},
        "servers": [{"url": "/"}], "paths": {}, "components": {"schemas": {}},
    }
    for key, spec in specs:
        if not spec:
            continue
        for path, operations in (spec.get("paths") or {}).items():
            target = merged["paths"].setdefault(path, {})
            for method, operation in operations.items():
                if isinstance(operation, dict):
                    operation = {**operation, "tags": [key]}
                target[method] = operation
        for name, schema in ((spec.get("components") or {}).get("schemas") or {}).items():
            merged["components"]["schemas"].setdefault(name, schema)
    return merged


def docs_page(gateway: Gateway) -> str:
    specs = [{"name": key, "url": f"{DOCS_PREFIX}{key}.json"} for key, _inst in gateway.local_specs()]
    if len(specs) > 1:
        specs.insert(0, {"name": _("All local services"), "url": f"{DOCS_PREFIX}all.json"})
    remote_docs = f"{gateway.remote}/api/v1/docs" if gateway.remote else ""
    note = _("No local services running. Start one with pdms run -b.") if not specs else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>pdms proxy · docs</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<style>body{{margin:0;font-family:sans-serif}} .pdms{{padding:8px 16px;background:#1b1b1b;color:#eee;font-size:14px}}
.pdms a{{color:#8fd}}</style></head>
<body><div class="pdms">pdms proxy · {_("live specs of the services running locally")}
{f' · <a href="{remote_docs}">{_("remote docs")}</a>' if remote_docs else ''} {note}</div>
<div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
const urls = {json.dumps(specs)};
if (urls.length) SwaggerUIBundle({{dom_id: "#ui", urls, deepLinking: true, layout: "StandaloneLayout",
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset]}});
</script></body></html>"""


# ---------------------------------------------------------------------- HTTP


def make_handler(gateway: Gateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pdms-proxy"

        def log_message(self, *args: object) -> None:  # replaced by gateway.log
            pass

        def do_OPTIONS(self) -> None:  # noqa: N802
            if self.headers.get("Access-Control-Request-Method"):
                self.send_response(204)
                self.cors()
                requested = self.headers.get("Access-Control-Request-Headers")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
                if requested:
                    self.send_header("Access-Control-Allow-Headers", requested)
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.forward()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = lambda self: self.forward()  # noqa: E731

        def cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Expose-Headers", "*")
                self.send_header("Vary", "Origin")

        def reply(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def reply_json(self, status: int, data: object, target: str) -> bytes:
            body = json.dumps(data).encode()
            self.reply(status, body, "application/json", {"X-Pdms-Target": target})
            return body

        def done(self, started: float, path: str, status: int, target: str, sent: bytes | None,
                 answer_headers: list[tuple[str, str]], answer: bytes | None, db: dict | None = None) -> None:
            """Log the request (and keep it, in the background proxy) once it was answered."""
            seconds, ident = time.monotonic() - started, ""
            if gateway.recorder:
                ident = self.capture_id or captures.new_id()
                headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP]
                try:
                    gateway.recorder.record(captures.entry(ident, self.command, path, status, target, seconds,
                                                           headers, sent, answer_headers, answer, db,
                                                           started_at=self.started_at))
                except OSError:
                    ident = ""  # a full disk must not break the proxy
            gateway.log(self.command, path.split("?", 1)[0], status, target, seconds, ident, db)

        capture_id = ""  # given when the request arrives (background proxy): its start and end lines share it
        started_at = 0.0  # when it arrived (epoch): the steps of its events are placed from there

        def forward(self) -> None:
            started, started_at = time.monotonic(), time.time()  # the wall clock places the service's part in time
            self.capture_id = ""
            path = normalize(self.path)
            bare = path.split("?", 1)[0]
            if bare in ("/", "/docs"):
                self.reply(200, docs_page(gateway).encode(), "text/html; charset=utf-8")
                return
            if bare.startswith(DOCS_PREFIX) and bare.endswith(".json"):
                spec = gateway.spec_for(bare[len(DOCS_PREFIX):-5])
                self.reply_json(200 if spec else 404, spec or {"detail": "unknown spec"}, "docs")
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            route = match(gateway.routes, self.command, path)
            target = gateway.target(route)
            label = target.instance.key if target.kind == "local" and target.instance else target.kind
            if target.kind in ("missing", "other-repo"):
                detail = target.note or (
                    _("No route for {method} {path} in the repo's Terraform.", method=self.command, path=bare)
                    if route is None else
                    _("{service} is not running locally. Start it with: pdms run {service} -b", service=route.service)
                )
                answer = self.reply_json(503 if route else 404, {"detail": f"pdms proxy: {detail}"}, label)
                self.done(started, path, 503 if route else 404, label, body, [("Content-Type", "application/json")], answer)
                return

            if gateway.recorder:
                self.capture_id = captures.new_id()
                self.started_at = started_at
                gateway.log_start(self.command, bare, label, self.capture_id)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            if self.capture_id and not any(k.lower() == trace.HEADER for k in headers):
                headers[trace.HEADER_NAME] = self.capture_id  # services log it and pass it on (sqs_patch/pdms_trace.py)
            if target.kind == "local":
                inst = target.instance
                if inst.asleep:
                    try:
                        inst = gateway.awaken(inst)
                    except WakeFailed as exc:
                        answer = self.reply_json(503, {"detail": f"pdms proxy: {exc}"}, label)
                        self.done(started, path, 503, label, body, [("Content-Type", "application/json")], answer)
                        return
                    label = inst.key
                host, port, scheme, base = "127.0.0.1", inst.port, "http", ""
                if gateway.impersonate:
                    headers.update(dev_headers(gateway.impersonate))
            else:
                remote = urlsplit(gateway.remote)
                scheme, host, base = remote.scheme, remote.hostname, remote.path.rstrip("/")
                port = remote.port or (443 if scheme == "https" else 80)
                headers["Host"] = remote.netloc
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn_class = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = conn_class(host, port, timeout=gateway.timeout)
            busy = inst.key if target.kind == "local" else ""
            if busy:
                with gateway._lock:
                    gateway._in_progress[busy] += 1
            try:
                conn.request(self.command, base + path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (OSError, http.client.HTTPException) as exc:
                reason = _("no answer after {seconds} s (raise it with pdms proxy --timeout or the proxy_timeout "
                           "default)", seconds=f"{gateway.timeout:g}") if isinstance(exc, TimeoutError) else exc
                answer = self.reply_json(502, {"detail": f"pdms proxy: {label}: {reason}"}, label)
                self.done(started, path, 502, label, body, [("Content-Type", "application/json")], answer)
                return
            finally:
                conn.close()
                if busy:
                    with gateway._lock:
                        gateway._in_progress[busy] -= 1
                    gateway.used(busy)

            answer_headers = [(k, v) for k, v in response.getheaders() if k.lower() != captures.QUERIES_HEADER]
            db = captures.query_detail(response.getheaders()) if target.kind == "local" else None
            if db is not None:
                db = captures.place_in_time(db, started_at, round((time.monotonic() - started) * 1000))
            self.send_response(response.status)
            for key, value in answer_headers:
                if key.lower() not in HOP_BY_HOP and key.lower() not in CORS_RESPONSE_HEADERS:
                    self.send_header(key, value)
            self.cors()
            self.send_header("X-Pdms-Target", label)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.done(started, path, response.status, label, body, answer_headers, data, db)

    return Handler


def db_text(db: dict | None) -> str:
    """`` db 3623ms 10q ×9``: the request's database time and queries, and the most repeated one when it stands out."""
    if not db:
        return ""
    queries, ms, repeated = captures.db_totals(db)
    return f" db {ms}ms {queries}q" + (f" ×{repeated}" if repeated >= captures.REPEATED else "")


def format_request(method: str, path: str, status: int, target: str, seconds: float, ident: str = "",
                   db: dict | None = None) -> str:
    """One line of the background proxy's log (the terminal shows the same, in colour); ``#id`` names its capture."""
    line = f"{datetime.now():%H:%M:%S} {method:<6} {path} {status} → {target}  {seconds * 1000:.0f}ms{db_text(db)}"
    return f"{line} #{ident}" if ident else line


def format_start(method: str, path: str, target: str, ident: str) -> str:
    """The line of a request that just arrived: ``…`` where the status and the time go once it is answered."""
    return f"{datetime.now():%H:%M:%S} {method:<6} {path} … → {target} #{ident}"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind also looks up the host's FQDN (reverse DNS), which can hang for many seconds on
        # macOS; the proxy never uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request: object, client_address: object) -> None:
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):  # a client that went away
            super().handle_error(request, client_address)


def serve(gateway: Gateway, host: str, port: int, info: dict) -> None:
    server = Server((host, port), make_handler(gateway))
    if threading.current_thread() is threading.main_thread():
        # pdms stop terminates it: clean up (state file, frontend/.env.local) as with Ctrl+C.
        signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({
        "pid": os.getpid(), "created": instances.creation_time(os.getpid()), "port": port,
        "started_at": datetime.now().isoformat(timespec="seconds"), **info,
    }), encoding="utf-8")
    if info.get("background"):
        print(f"# listening on :{port} (pid {os.getpid()})", flush=True)
    stopped = threading.Event()
    threading.Thread(target=gateway.watch_idle, args=(stopped,), daemon=True, name="pdms-idle").start()
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()
        forget(os.getpid())


def background_command(
    root: Path, *, port: int, env: str, remote: str | None, user_name: str | None, timeout: int = 300,
) -> list[str]:
    """How ``pdms proxy --background`` runs the proxy: this module, in a process of its own (see :func:`main`)."""
    cmd = [sys.executable, "-m", "pdms_cli.proxy", "--repo", str(root), "--port", str(port), "--env", env,
           "--timeout", str(timeout)]
    if remote:
        cmd += ["--remote", remote]
    if user_name:
        cmd += ["--as", user_name]
    return cmd


def main(argv: list[str] | None = None) -> None:
    """The background proxy: serves until stopped, logging each request to stdout (its log file)."""
    parser = argparse.ArgumentParser(prog="pdms_cli.proxy")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--remote", default="")
    parser.add_argument("--as", dest="user_name", default="")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    user = Config.load().users.get(args.user_name) if args.user_name else None
    repo_routes = load_routes(args.repo, args.env)
    print(f"# proxy :{args.port} · {args.repo} ({args.env}, {len(repo_routes)} routes) · "
          f"remote {args.remote or '-'} · as {args.user_name or '-'} · timeout {args.timeout}s", flush=True)
    gateway = Gateway(
        routes=repo_routes, backend=args.repo / "backend", remote=args.remote or None, impersonate=user,
        timeout=args.timeout, log=lambda *request: print(format_request(*request), flush=True),
        recorder=captures.Recorder(), log_start=lambda *request: print(format_start(*request), flush=True),
    )
    # frontend/.env.local is put back by whoever stops it (pdms stop), or by the next proxy if it died.
    serve(gateway, "0.0.0.0", args.port, {
        "repo": str(args.repo), "env": args.env, "remote": args.remote, "as": args.user_name, "log": str(log_path()),
        "background": True, "timeout": args.timeout,
    })


if __name__ == "__main__":
    main()
