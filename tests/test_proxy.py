"""Local API gateway: routes from Terraform, forwarding, CORS and impersonation."""

from __future__ import annotations

import base64
import http.client
import json
import re
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pdms_cli import captures, instances, proxy, repos, routes
from pdms_cli.config import DevUser

TERRAFORM = {
    "api_gw.tf": 'module "api_gw_api_principal" {\n  source = "../../../modules/api_gw"\n}\n',
    "api_rsc_api.tf": 'module "rsc_api" {\n  parent_id = module.api_gw_api_principal.root_resource_id\n  path_part = "api"\n}\n',
    "api_rsc_api_v1.tf": 'module "rsc_v1" {\n  parent_id = module.rsc_api.id\n  path_part = "v1"\n}\n',
    "api_rsc_api_v1_leads_tp.tf": """
module "rsc_leads" {
  parent_id = module.rsc_v1.id
  path_part = "leads"
}
module "rsc_tp" {
  parent_id = module.rsc_leads.id
  path_part = "tp"
}
module "rsc_tp_id" {
  parent_id = module.rsc_tp.id
  path_part = "{entity_id}"
}
module "rsc_tp_export" {
  count     = 1
  parent_id = module.rsc_tp.id
  path_part = "export"
}
module "meth_tp_get" {
  http_method                   = "GET"
  resource_id                   = module.rsc_tp.id
  integration_lambda_invoke_arn = module.lambda_list.invoke_arn
}
module "meth_tp_post" {
  http_method                   = "POST"
  resource_id                   = module.rsc_tp.id
  integration_lambda_invoke_arn = module.lambda_create.invoke_arn
}
module "meth_tp_id_get" {
  http_method                   = "GET"
  resource_id                   = module.rsc_tp_id.id
  integration_lambda_invoke_arn = module.lambda_details.invoke_arn
}
module "meth_tp_export_get" {
  http_method                   = "GET"
  resource_id                   = module.rsc_tp_export[0].id
  integration_lambda_invoke_arn = module.lambda_export[0].invoke_arn
}
module "lambda_list" {
  function_name = "lead-tp-list"
  lambda_path   = "../../../../backend/lead/lead-tp-list/"
}
module "lambda_create" {
  function_name = "lead-tp-create"
  lambda_path   = "../../../../backend/lead/lead-tp-create"
}
module "lambda_details" {
  lambda_path = "../../../../backend/lead/lead-tp-details/"
}
# module "commented_out" {
#   http_method = "DELETE"
# }
module "lambda_export" {
  count       = 1
  lambda_path = "../../../../backend/lead/lead-tp-export/"
}
""",
}


class Echo(BaseHTTPRequestHandler):
    """Answers with what it received; serves a tiny OpenAPI spec at /openapi.json."""

    def log_message(self, *args):
        pass

    def handle_any(self):
        if self.path == "/openapi.json":
            data = {"openapi": "3.1.0", "paths": {"/api/v1/leads/tp": {"get": {"summary": "List"}}},
                    "components": {"schemas": {"Lead": {"type": "object"}}}}
        else:
            length = int(self.headers.get("Content-Length") or 0)
            data = {"server": self.server.name, "method": self.command, "path": self.path,
                    "headers": dict(self.headers.items()), "body": self.rfile.read(length).decode()}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "https://should-be-replaced")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


def start(handler, name: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.name = name
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "pdms"
    (root / "backend" / "snakesdk").mkdir(parents=True)
    for service in ("lead-tp-list", "lead-tp-create"):
        (root / "backend" / "lead" / service).mkdir(parents=True)
    tf = routes.terraform_dir(root, "dev")
    tf.mkdir(parents=True)
    for name, text in TERRAFORM.items():
        (tf / name).write_text(text)
    return root


@pytest.fixture
def gateway_url(repo):
    local = start(Echo, "local")
    remote = start(Echo, "remote")
    instances.save({"lead-tp-list@1": instances.Instance(
        key="lead-tp-list@1", pid=os.getpid(), service=str(repo / "backend" / "lead" / "lead-tp-list"),
        host="127.0.0.1", port=local.server_address[1], user="sup", db="local", reload=True, log="/dev/null",
        started_at=datetime.now().isoformat(timespec="seconds"),
    )})
    gateways = []

    def make(remote_enabled=True, impersonate=None, timeout=300, **extra):
        gw = proxy.Gateway(
            routes=routes.load_routes(repo), backend=repo / "backend",
            remote=f"http://127.0.0.1:{remote.server_address[1]}/dev" if remote_enabled else None,
            impersonate=impersonate, timeout=timeout, **extra,
        )
        server = start(proxy.make_handler(gw), "proxy")
        gateways.append(server)
        return server.server_address[1]

    yield make
    for server in (local, remote, *gateways):
        server.shutdown()


def call(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    try:
        data = json.loads(data)
    except ValueError:
        data = data.decode()
    return response, data


def test_a_service_slower_than_the_timeout_gets_a_clear_502(gateway_url, monkeypatch):
    answer = Echo.handle_any

    def slow(self):
        time.sleep(1)  # a slow database query
        answer(self)

    monkeypatch.setattr(Echo, "do_GET", slow)
    response, data = call(gateway_url(timeout=0.3), "GET", "/dev/api/v1/leads/tp")
    assert response.status == 502
    assert data["detail"] == ("pdms proxy: lead-tp-list@1: no answer after 0.3 s "
                              "(raise it with pdms proxy --timeout or the proxy_timeout default)")
    response, data = call(gateway_url(timeout=5), "GET", "/dev/api/v1/leads/tp")
    assert response.status == 200 and data["server"] == "local"


def test_the_background_proxy_gets_the_timeout(repo):
    cmd = proxy.background_command(repo, port=8000, env="dev", remote=None, user_name=None, timeout=600)
    assert cmd[cmd.index("--timeout") + 1] == "600"


def test_routes_are_read_from_terraform(repo):
    found = {(r.method, r.path, r.service) for r in routes.load_routes(repo)}
    assert found == {
        ("GET", "/api/v1/leads/tp", "lead/lead-tp-list"),
        ("POST", "/api/v1/leads/tp", "lead/lead-tp-create"),
        ("GET", "/api/v1/leads/tp/{entity_id}", "lead/lead-tp-details"),
        ("GET", "/api/v1/leads/tp/export", "lead/lead-tp-export"),
    }


def test_literal_segments_win_over_parameters(repo):
    table = routes.load_routes(repo)
    assert routes.match(table, "GET", "/api/v1/leads/tp/export").service == "lead/lead-tp-export"
    assert routes.match(table, "GET", "/api/v1/leads/tp/42?x=1").service == "lead/lead-tp-details"
    assert routes.match(table, "DELETE", "/api/v1/leads/tp") is None


def test_running_service_gets_the_request_with_the_full_path(gateway_url):
    port = gateway_url()
    response, data = call(port, "GET", "/api/v1/leads/tp?page=2")
    assert data["server"] == "local" and data["path"] == "/api/v1/leads/tp?page=2"
    assert response.getheader("X-Pdms-Target") == "lead-tp-list@1"


def test_stage_prefix_is_removed(gateway_url):
    _, data = call(gateway_url(), "GET", "/dev/api/v1/leads/tp")
    assert data["server"] == "local" and data["path"] == "/api/v1/leads/tp"


def test_other_services_go_to_the_remote_api_unchanged(gateway_url):
    _, data = call(gateway_url(impersonate=DevUser("u-1", "a@x.com", roles="TPR.Agent")), "POST", "/api/v1/leads/tp",
                   headers={"Authorization": "Bearer token", "Content-Type": "application/json"}, body='{"a": 1}')
    assert data["server"] == "remote"
    assert data["path"] == "/dev/api/v1/leads/tp"
    assert data["headers"]["Authorization"] == "Bearer token"
    assert data["body"] == '{"a": 1}'
    assert not any(k.lower().startswith("x-dev-") for k in data["headers"])  # never impersonate on the remote


def test_impersonation_headers_reach_local_services(gateway_url):
    user = DevUser("u-1", "agent@x.com", first_name="Ana", last_name="Agent", roles="TPR.Agent")
    _, data = call(gateway_url(impersonate=user), "GET", "/api/v1/leads/tp")
    assert data["headers"]["X-Dev-Roles"] == "TPR.Agent"
    assert data["headers"]["X-Dev-User-Id"] == "u-1"


def test_cors_preflight_and_response_headers(gateway_url):
    port = gateway_url()
    origin = "http://localhost:5173"
    response, _ = call(port, "OPTIONS", "/api/v1/leads/tp", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization",
    })
    assert response.status == 204
    assert response.getheader("Access-Control-Allow-Origin") == origin
    assert response.getheader("Access-Control-Allow-Headers") == "authorization"
    response, _ = call(port, "GET", "/api/v1/leads/tp", headers={"Origin": origin})
    assert response.getheader("Access-Control-Allow-Origin") == origin  # the service's own value is replaced


def test_without_remote_missing_services_get_a_clear_error(gateway_url):
    port = gateway_url(remote_enabled=False)
    response, data = call(port, "POST", "/api/v1/leads/tp")
    assert response.status == 503 and "pdms run lead/lead-tp-create -b" in data["detail"]
    response, data = call(port, "GET", "/api/v1/unknown")
    assert response.status == 404


def test_docs_list_local_specs_and_merge_them(gateway_url):
    port = gateway_url()
    _, page = call(port, "GET", "/docs")
    assert "lead-tp-list@1" in page and "swagger-ui" in page
    _, spec = call(port, "GET", "/_pdms/openapi/lead-tp-list@1.json")
    assert spec["servers"] == [{"url": "/"}]
    _, merged = call(port, "GET", "/_pdms/openapi/all.json")
    assert merged["paths"]["/api/v1/leads/tp"]["get"]["tags"] == ["lead-tp-list@1"]
    assert "Lead" in merged["components"]["schemas"]


def test_remote_is_read_from_the_frontend_env(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / ".env").write_text(
        'VITE_APP_API_URL="https://abc.execute-api.us-east-1.amazonaws.com"\nVITE_APP_API_URL_VERSION="dev/api/v1"\n'
    )
    assert repos.remote_from_frontend(tmp_path) == "https://abc.execute-api.us-east-1.amazonaws.com/dev"
    (tmp_path / "frontend" / ".env").write_text("VITE_APP_API_URL=http://localhost:8000\n")
    assert repos.remote_from_frontend(tmp_path) is None  # never loop back to the proxy


def test_point_frontend_keeps_other_local_settings(tmp_path):
    (tmp_path / "frontend").mkdir()
    env_local = tmp_path / "frontend" / ".env.local"
    env_local.write_text("VITE_FEATURE_X=1\nVITE_APP_API_URL=https://old\n")
    repos.point_frontend_to(tmp_path, "http://localhost:8000")
    assert env_local.read_text().splitlines() == [
        "VITE_FEATURE_X=1", "VITE_APP_API_URL=http://localhost:8000", "VITE_APP_API_URL_VERSION=api/v1",
    ]
    assert repos.frontend_uses(tmp_path, "http://localhost:8000")


class _Answer:
    def __init__(self, value):
        self.value = value

    def unsafe_ask(self):
        return self.value


def _busy_ports(monkeypatch, busy: set[int], tty: bool) -> None:
    from pdms_cli import runner
    from pdms_cli.commands import proxy as proxy_commands

    monkeypatch.setattr(instances, "running_ports", lambda: set())
    monkeypatch.setattr(runner, "port_is_free", lambda host, port: port not in busy)
    monkeypatch.setattr(proxy_commands, "interactive_terminal", lambda: tty)


def test_proxy_port_keeps_a_free_port(monkeypatch) -> None:
    from pdms_cli.commands import proxy as proxy_commands

    _busy_ports(monkeypatch, set(), tty=False)
    assert proxy_commands.proxy_port(8000) == 8000


def test_proxy_port_offers_the_next_free_one_in_a_terminal(monkeypatch) -> None:
    from pdms_cli.commands import proxy as proxy_commands

    _busy_ports(monkeypatch, {8000, 8001}, tty=True)
    asked = []
    monkeypatch.setattr(proxy_commands.questionary, "confirm", lambda message, **kw: asked.append(message) or _Answer(True))
    assert proxy_commands.proxy_port(8000) == 8002
    assert asked and "8002" in asked[0]


def test_proxy_port_fails_without_a_terminal(monkeypatch) -> None:
    import typer

    from pdms_cli.commands import proxy as proxy_commands

    _busy_ports(monkeypatch, {8000}, tty=False)
    with pytest.raises(typer.Exit):
        proxy_commands.proxy_port(8000)


def test_restore_frontend_removes_a_file_it_created(tmp_path):
    (tmp_path / "frontend").mkdir()
    change = repos.point_frontend_to(tmp_path, "http://localhost:8001")
    assert not change["existed"] and repos.frontend_uses(tmp_path, "http://localhost:8001")
    assert repos.restore_frontend(change)
    assert not (tmp_path / "frontend" / ".env.local").exists()


def test_restore_frontend_puts_back_the_previous_values(tmp_path):
    (tmp_path / "frontend").mkdir()
    env_local = tmp_path / "frontend" / ".env.local"
    env_local.write_text('VITE_FEATURE_X=1\nVITE_APP_API_URL="https://old"\n')
    change = repos.point_frontend_to(tmp_path, "http://localhost:8000")
    assert repos.restore_frontend(change)
    assert env_local.read_text().splitlines() == ["VITE_FEATURE_X=1", 'VITE_APP_API_URL="https://old"']


def test_restore_frontend_keeps_values_edited_meanwhile(tmp_path):
    (tmp_path / "frontend").mkdir()
    env_local = tmp_path / "frontend" / ".env.local"
    change = repos.point_frontend_to(tmp_path, "http://localhost:8000")
    env_local.write_text("VITE_APP_API_URL=https://mine\nVITE_APP_API_URL_VERSION=api/v1\n")
    assert repos.restore_frontend(change)
    assert env_local.read_text().splitlines() == ["VITE_APP_API_URL=https://mine"]


def test_frontend_change_is_undone_from_the_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "frontend").mkdir()
    proxy.remember_frontend_change(repos.point_frontend_to(tmp_path, "http://localhost:8000"))
    assert proxy.restore_frontend_change() == str(tmp_path / "frontend" / ".env.local")
    assert not proxy.frontend_change_path().exists()
    assert not (tmp_path / "frontend" / ".env.local").exists()
    assert proxy.restore_frontend_change() is None  # nothing pending


def test_background_proxy_starts_answers_and_stops(tmp_path, monkeypatch):
    import socket
    import urllib.request

    from pdms_cli import actions
    from pdms_cli.config import Config

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    routes.terraform_dir(root, "dev").mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    plan = actions.plan_proxy(Config(), root, [], port=port, frontend=True)
    assert actions.point_frontend(plan)
    started = actions.start_proxy(plan)
    try:
        assert actions.wait_for_proxy(started) == "ok", instances.tail(str(started.log), 30)
        running = proxy.running_proxy()
        assert running["port"] == port and running["background"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=5) as response:
            assert response.status == 200
        lines = started.log.read_text().splitlines()
        assert lines[1].startswith(f"# proxy :{port}") and lines[2].startswith(f"# listening on :{port}")
    finally:
        restored = actions.stop_proxy()
    assert restored == str(root / "frontend" / ".env.local")
    assert not (root / "frontend" / ".env.local").exists()
    assert not instances.process_alive(int(running["pid"]), running["created"]) and proxy.running_proxy() is None
    assert not proxy.state_path().exists()


def wait_for(condition, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_background_proxy_keeps_each_request_for_its_detail(gateway_url, tmp_path, monkeypatch):
    logged = []
    recorder = captures.Recorder(tmp_path / "requests.jsonl")
    port = gateway_url(recorder=recorder, log=lambda *line: logged.append(line))
    call(port, "POST", "/api/v1/leads/tp?x=1", headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
         body='{"name": "Ana"}')
    call(port, "GET", "/api/v1/nowhere")
    wait_for(lambda: len(logged) == 2)  # logged right after answering
    (method, path, status, target, _seconds, ident, db), other = logged
    assert (method, path, status, target) == ("POST", "/api/v1/leads/tp", 200, "remote") and len(ident) == 8
    assert db is None  # only local services say what they asked the database
    assert other[1] == "/api/v1/nowhere" and other[5] != ident
    assert proxy.format_request(*logged[0]).endswith(f"ms #{ident}")

    kept = captures.find(ident, recorder.file)
    assert kept["path"] == "/api/v1/leads/tp?x=1" and kept["request"]["body"]["text"] == '{"name": "Ana"}'
    assert json.loads(kept["response"]["body"]["text"])["server"] == "remote"
    if os.name != "nt":
        assert recorder.file.stat().st_mode & 0o777 == 0o600
    shown = captures.public(kept)
    assert ["Authorization", "Bearer (hidden)"] in shown["request"]["headers"]
    assert "s3cret" not in json.dumps(shown["request"]["headers"])  # (this echo server repeats it in its body)
    command = captures.curl(kept, 9999)
    assert command.startswith("curl -i -X POST 'http://localhost:9999/api/v1/leads/tp?x=1'")
    assert "s3cret" not in command and "--data-raw '{\"name\": \"Ana\"}'" in command

    status, _took = captures.replay(kept, port)  # through the proxy again, with the real token
    wait_for(lambda: len(logged) == 3)
    assert status == 200
    again = captures.find(logged[2][5], recorder.file)
    assert ["Authorization", "Bearer s3cret"] in again["request"]["headers"]


def test_a_request_is_logged_when_it_arrives_with_the_id_of_its_answer(gateway_url, tmp_path):
    """pdms ui shows a request in progress from its start line, and replaces it with the answer that shares its id."""
    lines = []
    port = gateway_url(recorder=captures.Recorder(tmp_path / "requests.jsonl"),
                       log=lambda *line: lines.append(("end", line)),
                       log_start=lambda *line: lines.append(("start", line)))
    call(port, "GET", "/api/v1/leads/tp?page=2")
    wait_for(lambda: len(lines) == 2)
    (first, start), (second, end) = lines
    assert (first, second) == ("start", "end")
    method, path, target, ident = start
    assert (method, path, target) == ("GET", "/api/v1/leads/tp", "lead-tp-list@1") and end[5] == ident
    line = proxy.format_start(*start)
    assert re.fullmatch(r"\d\d:\d\d:\d\d GET    /api/v1/leads/tp … → lead-tp-list@1 #[0-9a-f]{8}", line)
    quiet = []  # a proxy that keeps nothing (pdms proxy in a terminal) logs only answers
    port = gateway_url(log=lambda *line: quiet.append(line), log_start=lambda *line: quiet.append(("start",)))
    call(port, "GET", "/api/v1/leads/tp")
    wait_for(lambda: len(quiet) == 1)
    assert quiet[0][0] == "GET" and quiet[0][5] == ""


def test_captures_rotate_and_big_or_binary_bodies_are_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(captures, "FILE_LIMIT", 200)
    recorder = captures.Recorder(tmp_path / "requests.jsonl")
    ids = [captures.new_id() for _ in range(3)]
    for ident in ids:
        recorder.record(captures.entry(ident, "GET", "/x", 200, "remote", 0.01, [], None, [], b"{}"))
    assert (tmp_path / "requests.jsonl.1").exists()
    assert captures.find(ids[0], recorder.file) is None or captures.find(ids[-1], recorder.file)
    assert captures.find(ids[-1], recorder.file)["id"] == ids[-1]
    assert captures.body(b"\xff\xfe") == {"size": 2, "binary": True}
    big = captures.body(b"a" * (captures.BODY_LIMIT + 1))
    assert big["truncated"] and len(big["text"]) == captures.BODY_LIMIT
    capture = captures.entry("x", "POST", "/x", 200, "remote", 0, [], b"a" * (captures.BODY_LIMIT + 1), [], None)
    assert "64 KB" in captures.replayable(capture)


def test_what_a_local_service_asked_the_database_is_kept_not_passed_on(gateway_url, tmp_path, monkeypatch):
    detail = {"v": 1, "answered_ms": 3640, "connect_ms": 0, "connections": 0, "transactions": 2, "transaction_ms": 20,
              "statements": [{"sql": "SELECT name FROM pdms_user WHERE id = %(id)s", "count": 9, "ms": 2720,
                              "first_ms": 400, "last_ms": 3300, "caller": "/svc/repo.py:12"},
                             {"sql": "SELECT id FROM lead", "count": 1, "ms": 300, "first_ms": 50, "last_ms": 350,
                              "caller": "/svc/repo.py:20"}], "more": 0}
    encoded = base64.b64encode(json.dumps(detail).encode()).decode()

    def end_headers(self):  # the service's answer, as pdms_queries.py makes it
        if self.path != "/openapi.json":
            self.send_header("x-pdms-db", "queries=10; time=3.040; connect=0.000; transactions=2")
            self.send_header("x-pdms-queries", encoded)
        BaseHTTPRequestHandler.end_headers(self)

    monkeypatch.setattr(Echo, "end_headers", end_headers)
    logged = []
    recorder = captures.Recorder(tmp_path / "requests.jsonl")
    port = gateway_url(recorder=recorder, log=lambda *line: logged.append(line))
    response, data = call(port, "GET", "/api/v1/leads/tp")
    assert data["server"] == "local"
    assert response.getheader("x-pdms-db").startswith("queries=10")  # readable in the browser
    assert response.getheader("x-pdms-queries") is None  # the detail stays in pdms

    wait_for(lambda: len(logged) == 1)
    *_, ident, db = logged[0]
    assert {k: v for k, v in db.items() if k != "service_at_ms"} == detail
    assert proxy.format_request(*logged[0]).endswith(f" db 3040ms 10q ×9 #{ident}")
    kept = captures.find(ident, recorder.file)
    assert kept["db"] == db
    assert all(name.lower() != "x-pdms-queries" for name, _ in kept["response"]["headers"])


def test_a_broken_query_header_is_ignored() -> None:
    assert captures.query_detail([("X-Pdms-Queries", "not base64!")]) is None
    assert captures.query_detail([("x-pdms-queries", base64.b64encode(b"[1, 2]").decode())]) is None
    assert captures.query_detail([("content-type", "x")]) is None
    assert proxy.db_text(None) == ""
    assert proxy.db_text({"statements": [{"count": 2, "ms": 10}], "transaction_ms": 5}) == " db 15ms 2q"


def test_the_service_part_is_placed_in_the_proxy_time_line() -> None:
    detail = {"statements": [], "answered_ms": 15, "received_at": 1000.060}
    assert captures.place_in_time(detail, 1000.000, 81)["service_at_ms"] == 60
    assert captures.place_in_time(detail, 1000.100, 81)["service_at_ms"] == 0  # clocks never put it before the start
    assert captures.place_in_time(detail, 999.000, 81)["service_at_ms"] == 81  # nor after the end
    assert "service_at_ms" not in captures.place_in_time({"statements": []}, 1000.0, 81)  # older services
