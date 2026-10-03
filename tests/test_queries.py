"""Database time and queries per request: sqs_patch/pdms_queries.py, the module a service loads (tried with stand-ins
for Starlette and SQLAlchemy), and the setting that turns it on."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from pdms_cli import actions, events
from pdms_cli.config import Config, Database, Defaults, DevUser


def load(name: str = "pdms_queries_under_test"):
    spec = importlib.util.spec_from_file_location(name, events.PATCH_DIR / "pdms_queries.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def queries():
    return load()


def decode(header: bytes) -> dict:
    return json.loads(base64.b64decode(header))


# --------------------------------------------------------------------------- one request's numbers


def test_statements_are_grouped_and_timed(queries) -> None:
    request = queries.Request()
    start = request.started
    request.query("SELECT a\n  FROM t WHERE id = %(id)s", start + 0.1, 0.4, "/svc/repo.py:12")
    request.query("SELECT a FROM t WHERE id = %(id)s", start + 0.6, 0.5, "/svc/repo.py:12")
    request.query("SELECT count(*) FROM t", start + 1.2, 0.2, "/svc/list.py:3")
    request.ended(0.3)
    request.opened(1.5)

    assert request.totals() == (3, pytest.approx(1.4))
    assert request.summary() == "queries=3; time=1.400; connect=1.500; transactions=1"
    detail = decode(request.detail().encode())
    first = detail["statements"][0]
    assert first == {"sql": "SELECT a FROM t WHERE id = %(id)s", "count": 2, "ms": 900, "first_ms": 100,
                     "last_ms": 1100, "caller": "/svc/repo.py:12"}
    assert (detail["connect_ms"], detail["connections"], detail["transactions"], detail["transaction_ms"]) == (1500, 1, 1, 300)
    assert abs(detail["received_at"] - time.time()) < 5  # the wall clock, for pdms proxy's time line


def test_the_values_sent_with_a_query_are_never_kept(queries, sqlalchemy) -> None:
    queries.watch_sqlalchemy()
    listeners = sqlalchemy.event.LISTENERS
    request = queries.Request()
    token = queries.current.set(request)
    try:
        conn = Conn()
        args = (conn, None, "SELECT * FROM pdms_user WHERE email = %(email_1)s", {"email_1": "secret@x.com"}, None, False)
        listeners["before_cursor_execute"](*args)
        listeners["after_cursor_execute"](*args)
    finally:
        queries.current.reset(token)
    kept = base64.b64decode(request.detail()).decode() + request.summary() + request.log_line("GET", "/")
    assert "%(email_1)s" in kept and "secret@x.com" not in kept


def test_the_header_stays_small(queries) -> None:
    request = queries.Request()
    for n in range(200):
        request.query(f"SELECT {'x' * 1500} FROM t{n}", request.started, 0.001 * n, "")
    header = request.detail()
    assert len(header) <= queries.HEADER_LIMIT
    detail = decode(header.encode())
    assert len(detail["statements"]) + detail["more"] == 200
    assert detail["statements"][0]["ms"] >= detail["statements"][-1]["ms"]  # the slowest are kept


def test_the_log_line_only_when_something_stands_out(queries) -> None:
    quiet = queries.Request()
    quiet.query("SELECT 1", quiet.started, 0.2, "")
    assert quiet.log_line("GET", "/x") == ""

    slow = queries.Request()
    slow.query("SELECT 1", slow.started, 1.2, "")
    assert slow.log_line("GET", "/x") == "GET /x: 1 queries in 1.20 s"

    loop = queries.Request()
    for _ in range(9):
        loop.query("SELECT * FROM pdms_user WHERE id = %(id)s", loop.started, 0.01,
                   os.path.join(os.getcwd(), "lead", "repo.py") + ":212")
    loop.opened(3.1)
    line = loop.log_line("GET", "/lead/8812")
    assert line == "GET /lead/8812: 9 queries in 0.09 s (+ 3.1 s opening 1 connection(s)) · the same query 9 times, " \
                   f"0.09 s ({os.path.join('lead', 'repo.py')}:212)"


def test_the_caller_is_the_service_line(queries) -> None:
    def sqlalchemy_dispatch():  # caller() skips itself and the event handler that calls it
        return handler()

    def handler():
        return queries.caller()

    where = sqlalchemy_dispatch()
    assert where.startswith(__file__ + ":")


# --------------------------------------------------------------------------- around each request (Starlette)


@pytest.fixture
def starlette(monkeypatch, tmp_path):
    """A stand-in ``starlette`` whose app runs the queries the scope asks for, then answers."""
    fake = tmp_path / "starlette"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "applications.py").write_text(textwrap.dedent("""
        RUN = []  # set by the test: what runs while handling a request

        class Starlette:
            async def __call__(self, scope, receive, send):
                for step in RUN:
                    step(scope)
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"x")]})
                await send({"type": "http.response.body", "body": b"ok"})
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("starlette", "starlette.applications"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import starlette.applications

    return starlette.applications


def call(app, scope) -> list[dict]:
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def test_each_request_gets_its_numbers_in_headers(queries, starlette, capfd) -> None:
    queries.measure_requests()
    seen = []

    def run_queries(scope):
        request = queries.current.get()
        seen.append(request)
        for _ in range(6):
            request.query("SELECT * FROM lead WHERE id = %(id)s", request.started, 0.01, "/svc/a.py:1")

    starlette.RUN[:] = [run_queries]
    sent = call(starlette.Starlette(), {"type": "http", "method": "GET", "path": "/lead/1"})
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"x"
    assert headers[b"x-pdms-db"].startswith(b"queries=6; time=0.060;")
    assert decode(headers[b"x-pdms-queries"])["statements"][0]["count"] == 6
    assert queries.current.get() is None  # nothing left behind for the next request
    assert "[pdms] GET /lead/1: 6 queries in 0.06 s · the same query 6 times" in capfd.readouterr().err

    starlette.RUN[:] = [run_queries]
    call(starlette.Starlette(), {"type": "http", "method": "GET", "path": "/lead/2"})
    assert seen[0] is not seen[1]  # one record per request


def test_a_mounted_app_is_measured_once(queries, starlette) -> None:
    """A Starlette app inside another one (a mount) runs inside the outer request: it adds nothing of its own."""
    queries.measure_requests()
    starlette.RUN[:] = []
    outer = queries.Request()

    async def inside_the_outer_request():
        token = queries.current.set(outer)
        try:
            sent = []

            async def send(message):
                sent.append(message)

            await starlette.Starlette()({"type": "http", "method": "GET", "path": "/"}, None, send)
            return sent
        finally:
            queries.current.reset(token)

    sent = asyncio.run(inside_the_outer_request())
    assert b"x-pdms-db" not in dict(sent[0]["headers"])


def test_lifespan_and_websockets_are_left_alone(queries, starlette) -> None:
    queries.measure_requests()
    starlette.RUN[:] = []
    sent = call(starlette.Starlette(), {"type": "lifespan"})
    assert b"x-pdms-db" not in dict(sent[0]["headers"])


def test_a_broken_request_still_reaches_uvicorn(queries, starlette) -> None:
    queries.measure_requests()

    def boom(scope):
        raise RuntimeError("the endpoint failed")

    starlette.RUN[:] = [boom]
    with pytest.raises(RuntimeError, match="the endpoint failed"):
        call(starlette.Starlette(), {"type": "http", "method": "GET", "path": "/"})
    assert queries.current.get() is None


# --------------------------------------------------------------------------- SQLAlchemy's events


@pytest.fixture
def sqlalchemy(monkeypatch, tmp_path):
    """A stand-in ``sqlalchemy`` with the event registry and the dialect methods the module wraps."""
    root = tmp_path / "sqlalchemy"
    (root / "engine").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "event.py").write_text("LISTENERS = {}\n\ndef listen(target, name, fn):\n    LISTENERS[name] = fn\n")
    (root / "engine" / "__init__.py").write_text("class Engine:\n    pass\n")
    (root / "engine" / "default.py").write_text(textwrap.dedent("""
        import time

        class DefaultDialect:
            def connect(self, *args, **kwargs):
                time.sleep(0.05)
                return "dbapi connection"

            def do_commit(self, connection):
                time.sleep(0.02)

            def do_rollback(self, connection):
                pass
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m == "sqlalchemy" or m.startswith("sqlalchemy.")]:
        monkeypatch.delitem(sys.modules, name)
    import sqlalchemy.engine.default
    import sqlalchemy.event

    return sqlalchemy


class Conn:
    def __init__(self) -> None:
        self.info: dict = {}


def test_queries_connections_and_commits_reach_the_current_request(queries, sqlalchemy) -> None:
    queries.watch_sqlalchemy()
    listeners = sqlalchemy.event.LISTENERS
    dialect = sqlalchemy.engine.default.DefaultDialect()
    request = queries.Request()
    token = queries.current.set(request)
    try:
        assert dialect.connect() == "dbapi connection"
        conn = Conn()
        listeners["before_cursor_execute"](conn, None, "SELECT 1", {"id": 1}, None, False)
        listeners["after_cursor_execute"](conn, None, "SELECT 1", {"id": 1}, None, False)
        dialect.do_commit(None)
        dialect.do_rollback(None)
    finally:
        queries.current.reset(token)

    assert request.totals()[0] == 1
    # Windows' clock moves in steps of about 15 ms: the 50 ms and 20 ms sleeps may measure a little less.
    assert request.connections == 1 and request.connect >= 0.03
    assert request.transactions == 2 and request.transaction_seconds >= 0.005
    assert conn.info == {}  # the start time does not stay on the connection


def test_outside_a_request_nothing_is_recorded(queries, sqlalchemy) -> None:
    queries.watch_sqlalchemy()
    listeners = sqlalchemy.event.LISTENERS
    conn = Conn()
    listeners["before_cursor_execute"](conn, None, "SELECT 1", {}, None, False)
    listeners["after_cursor_execute"](conn, None, "SELECT 1", {}, None, False)
    assert conn.info == {}
    assert sqlalchemy.engine.default.DefaultDialect().connect() == "dbapi connection"


def test_watching_twice_wraps_once(queries, sqlalchemy) -> None:
    queries.watch_sqlalchemy()
    wrapped = sqlalchemy.engine.default.DefaultDialect.connect
    load("pdms_queries_again").watch_sqlalchemy()
    assert sqlalchemy.engine.default.DefaultDialect.connect is wrapped


# --------------------------------------------------------------------------- turned on by the setting


def test_install_does_nothing_without_the_variable(queries, starlette, monkeypatch) -> None:
    monkeypatch.delenv("PDMS_QUERY_STATS", raising=False)
    queries.install()
    assert not getattr(starlette.Starlette.__call__, "_pdms_queries", False)
    monkeypatch.setenv("PDMS_QUERY_STATS", "1")
    queries.install()  # no SQLAlchemy here: the requests are measured all the same, without queries
    assert getattr(starlette.Starlette.__call__, "_pdms_queries", False)


def test_a_service_python_loads_it_through_sitecustomize(tmp_path) -> None:
    fake = tmp_path / "starlette"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "applications.py").write_text("class Starlette:\n    async def __call__(self, scope, receive, send):\n        pass\n")
    check = "import starlette.applications as a; print(getattr(a.Starlette.__call__, '_pdms_queries', False))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(events.PATCH_DIR), str(tmp_path)]), "PDMS_QUERY_STATS": "1"}
    out = subprocess.run([sys.executable, "-c", check], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"


def test_on_by_default_and_saved_from_the_ui(monkeypatch) -> None:
    from pdms_cli.ui import jobs as ui_jobs

    assert Defaults().query_stats is True
    assert ui_jobs.defaults_from({"query_stats": False}, Defaults()).query_stats is False
    monkeypatch.setattr(Config, "save", lambda self: None)
    cfg = Config(users={"agent": DevUser("u2", "a@x.com")}, dbs={"local": Database("localhost")}, defaults=Defaults())
    actions.save_defaults(cfg, replace(cfg.defaults, query_stats=False))
    assert cfg.defaults.query_stats is False


def test_consumers_are_not_measured(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set())
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    monkeypatch.setattr(actions, "consumer_of",
                        lambda cfg, service: (events.Queue("q.fifo"), events.Consumer("broker/x", "main.handler")))
    monkeypatch.setattr(actions, "events_setup", lambda cfg, service, mode: actions.EventsSetup({}, "local", "local"))
    cfg = Config(users={"agent": DevUser("u2", "a@x.com")}, dbs={"local": Database("localhost")}, defaults=Defaults())
    launch = actions.plan_service(cfg, Path(tmp_path), user_name="agent", db_name="local")
    assert "PDMS_QUERY_STATS" not in actions.service_env(cfg, launch)
