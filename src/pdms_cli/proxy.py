"""Local API gateway: one port for every PDMS service.

Each request is matched against the repo's Terraform routes and sent to the local instance of that service when
one is running from the current repo; otherwise it goes to the remote API (e.g. dev), unchanged. It also answers
CORS preflights (the services have no CORS middleware; API Gateway does that in AWS), can impersonate a dev user
on local services through the ``X-Dev-*`` headers, and serves a Swagger UI with the live specs of local services.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import instances
from .config import DevUser
from .i18n import _
from .routes import Route, match

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "host", "content-length",
}
CORS_RESPONSE_HEADERS = {
    "access-control-allow-origin", "access-control-allow-credentials", "access-control-allow-methods",
    "access-control-allow-headers", "access-control-expose-headers", "access-control-max-age",
}
DOCS_PREFIX = "/_pdms/openapi/"


def state_path() -> Path:
    return instances.state_dir() / "proxy.json"


def running_proxy() -> dict | None:
    """``{"pid", "port", ...}`` of the proxy currently running on this machine, if any."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if instances.process_alive(int(data["pid"]), float(data.get("created", 0))) else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None


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
    instance: instances.Instance | None = None
    note: str = ""


@dataclass
class Gateway:
    routes: list[Route]
    backend: Path
    remote: str | None  # e.g. https://<id>.execute-api.us-east-1.amazonaws.com/dev
    impersonate: DevUser | None = None
    log: Callable[[str, str, int, str, float], None] = lambda *args: None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ targets

    def local_instances(self) -> dict[str, list[instances.Instance]]:
        found: dict[str, list[instances.Instance]] = {}
        for inst in instances.load().values():
            if inst.alive() and not inst.is_consumer:  # event consumers have no HTTP API
                found.setdefault(str(Path(inst.service).resolve()), []).append(inst)
        return found

    def target(self, route: Route | None, running: dict[str, list[instances.Instance]] | None = None) -> Target:
        running = self.local_instances() if running is None else running
        if route is not None:
            service = str((self.backend / route.service).resolve())
            if service in running:
                return Target("local", min(running[service], key=lambda i: i.port))
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
                    items.append((inst.key, inst))
        return items

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

        def reply_json(self, status: int, data: object, target: str) -> None:
            self.reply(status, json.dumps(data).encode(), "application/json", {"X-Pdms-Target": target})

        def forward(self) -> None:
            started = time.monotonic()
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
                self.reply_json(503 if route else 404, {"detail": f"pdms proxy: {detail}"}, label)
                gateway.log(self.command, bare, 503 if route else 404, label, time.monotonic() - started)
                return

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            if target.kind == "local":
                inst = target.instance
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
            conn = conn_class(host, port, timeout=120)
            try:
                conn.request(self.command, base + path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (OSError, http.client.HTTPException) as exc:
                self.reply_json(502, {"detail": f"pdms proxy: {label}: {exc}"}, label)
                gateway.log(self.command, bare, 502, label, time.monotonic() - started)
                return
            finally:
                conn.close()

            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP and key.lower() not in CORS_RESPONSE_HEADERS:
                    self.send_header(key, value)
            self.cors()
            self.send_header("X-Pdms-Target", label)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            gateway.log(self.command, bare, response.status, label, time.monotonic() - started)

    return Handler


def serve(gateway: Gateway, host: str, port: int, info: dict) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(gateway))
    server.daemon_threads = True
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({
        "pid": os.getpid(), "created": instances.creation_time(os.getpid()), "port": port,
        "started_at": datetime.now().isoformat(timespec="seconds"), **info,
    }), encoding="utf-8")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if json.loads(state_path().read_text(encoding="utf-8")).get("pid") == os.getpid():
                state_path().unlink()
        except (FileNotFoundError, json.JSONDecodeError):
            pass
