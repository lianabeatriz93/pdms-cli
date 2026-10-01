"""pdms ui server: only this machine's own page, with the session token, reaches the API."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from pdms_cli.config import Config, Database, DevUser
from pdms_cli.instances import Health, Instance
from pdms_cli.ui import server as ui_server
from pdms_cli.ui import state as ui_state

TOKEN = "s3cret-token"


@pytest.fixture
def ui():
    """A running UI server on a free port with a fake state; yields ``(port, hub, states)``."""
    states = [{"instances": [], "n": 0}]
    hub = ui_server.Hub(build=lambda: states[-1], interval=0.05)
    server = ui_server.make_server("127.0.0.1", 0, TOKEN, hub)
    thread = threading.Thread(target=ui_server.serve, args=(server, hub), daemon=True)
    thread.start()
    yield server.server_address[1], hub, states
    server.shutdown()
    thread.join(5)


def request(port: int, path: str, headers: dict[str, str] | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    response = conn.getresponse()
    return response, response.read(), conn


def cookie(port: int) -> dict[str, str]:
    return {"Cookie": f"pdms_ui_{port}={TOKEN}"}


def test_the_token_is_swapped_for_a_strict_cookie(ui) -> None:
    port, _hub, _states = ui
    response, _body, _conn = request(port, f"/?token={TOKEN}")
    assert response.status == 303 and response.getheader("Location") == "/"
    set_cookie = response.getheader("Set-Cookie")
    assert set_cookie.startswith(f"pdms_ui_{port}={TOKEN};") and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie

    response, body, _conn = request(port, "/", cookie(port))
    assert response.status == 200 and b"<title>pdms</title>" in body
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")


@pytest.mark.parametrize("headers", [{}, {"Cookie": "pdms_ui_1=other"}, {"Cookie": "garbage;;=="}])
def test_without_the_token_nothing_is_served(ui, headers) -> None:
    port, _hub, _states = ui
    for path in ("/", "/api/state", "/api/stream", "/static/app.js"):
        response, _body, _conn = request(port, path, headers)
        assert response.status == 401, path
    assert request(port, "/?token=wrong")[0].status == 403


def test_other_hosts_and_other_pages_are_rejected(ui) -> None:
    port, _hub, _states = ui
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host.
    assert request(port, "/api/state", {**cookie(port), "Host": f"evil.example:{port}"})[0].status == 421
    assert request(port, "/api/state", {**cookie(port), "Origin": "http://evil.example"})[0].status == 403
    assert request(port, "/api/state", {**cookie(port), "Origin": "null"})[0].status == 403
    assert request(port, "/api/state", {**cookie(port), "Origin": f"http://localhost:{port}"})[0].status == 200


def test_static_files_stay_inside_the_static_folder(ui) -> None:
    port, _hub, _states = ui
    response, _body, _conn = request(port, "/static/app.js", cookie(port))
    assert response.status == 200 and response.getheader("Content-Type") == "text/javascript; charset=utf-8"
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/.hidden", "/static/missing.js"):
        assert request(port, path, cookie(port))[0].status == 404, path


def test_state_and_stream(ui) -> None:
    port, hub, states = ui
    response, body, _conn = request(port, "/api/state", cookie(port))
    assert response.status == 200 and json.loads(body) == {"instances": [], "n": 0}

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{port}", **cookie(port)})
    stream = conn.getresponse()
    assert stream.status == 200 and stream.getheader("Content-Type") == "text/event-stream"

    def next_event() -> dict:
        lines = []
        while (line := stream.fp.readline().decode().rstrip("\n")) or not lines:
            if line.startswith("data: "):
                lines.append(line[len("data: "):])
        return json.loads("".join(lines))

    assert next_event()["n"] == 0
    states.append({"instances": [], "n": 1})  # something changed in the state files
    assert next_event()["n"] == 1
    assert hub.watchers == 1
    conn.close()


def test_the_state_never_carries_database_passwords(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(
        users={"agent": DevUser("u1", "a@x.com")},
        dbs={"shared": Database("db.example.com", user="app", password="hunter2")},
        last_user="agent", last_db="shared",
    )
    inst = Instance(
        key="lead-tp-list@8081", pid=1, service=str(tmp_path / "lead-tp-list"), host="0.0.0.0", port=8081,
        user="agent", db="shared", reload=True, log="x.log", started_at="2026-09-30T10:00:00",
    )
    monkeypatch.setattr(ui_state.instances, "load", lambda: {inst.key: inst})
    monkeypatch.setattr(ui_state.instances, "health_all", lambda items: {inst.key: Health("ok")})
    monkeypatch.setattr(ui_state.events, "is_up", lambda port: False)
    state = ui_state.build_state(cfg)
    assert state["user"] == "agent" and state["db"] == "shared" and state["proxy"] is None
    assert state["instances"][0]["key"] == "lead-tp-list@8081" and state["instances"][0]["status"] == "ok"
    text = json.dumps(state)
    assert "hunter2" not in text and "db.example.com" not in text
