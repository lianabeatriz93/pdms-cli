"""Loaded by this folder's sitecustomize.py in services run by pdms with ``PDMS_QUERY_STATS=1``: what each HTTP request
asked the database, without touching the service code. Queries are timed, never changed.

Every request gets two response headers:

- ``x-pdms-db: queries=14; time=5.231; connect=0.000; transactions=2`` (commits and rollbacks), readable in the
  browser's developer tools;
- ``x-pdms-queries``: base64 JSON with each distinct statement (its SQL text, never the values sent with it), how
  many times it ran, how long it took and the line of the service that ran it. pdms proxy keeps it with the request
  for pdms ui and does not pass it on.

The log gets one ``[pdms]`` line when a request spent a while in the database or ran the same statement many times
(usually one query per row of a list: N+1).
"""

import base64
import contextvars
import json
import os
import sys
import sysconfig
import threading
import time

REPEATED = 5  # the same statement this many times in one request is worth a warning
SLOW_DB = 1.0  # seconds in the database that make a request worth a line in the log
MAX_STATEMENTS = 40
MAX_SQL = 2000
HEADER_LIMIT = 32 * 1024  # what pdms proxy (http.client) and browsers take in one header, with room to spare

current = contextvars.ContextVar("pdms_queries", default=None)
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBRARY = tuple(os.sep + name + os.sep for name in (
    "sqlalchemy", "starlette", "fastapi", "anyio", "asyncio", "concurrent", "pydantic", "uvicorn",
))
_STDLIB = sysconfig.get_paths().get("stdlib") or ""


def log(message):
    print(f"[pdms] {message}", file=sys.stderr, flush=True)


def caller():
    """The innermost line of the service (or of a library of the repo) that led to this query."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if not (name.startswith(_HERE) or name.startswith("<") or any(part in name for part in _LIBRARY)
                or (_STDLIB and name.startswith(_STDLIB) and "site-packages" not in name)):
            return f"{name}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


class Request:
    """What one request asked the database (filled from any thread the request uses)."""

    def __init__(self):
        self.started = time.monotonic()
        self.received_at = time.time()  # wall clock: pdms proxy, on this machine, compares it with its own
        self.answered = None
        self.statements = {}  # SQL text -> [count, seconds, first start, last end, caller]
        self.connect = 0.0
        self.connections = 0
        self.transactions = 0  # commits and rollbacks: each one is a round trip
        self.transaction_seconds = 0.0
        self.lock = threading.Lock()

    def query(self, sql, started, seconds, where):
        sql = " ".join(sql.split())[:MAX_SQL]
        with self.lock:
            stat = self.statements.get(sql)
            if stat is None:
                self.statements[sql] = [1, seconds, started - self.started, started - self.started + seconds, where]
            else:
                stat[0] += 1
                stat[1] += seconds
                stat[3] = started - self.started + seconds

    def opened(self, seconds):
        with self.lock:
            self.connect += seconds
            self.connections += 1

    def ended(self, seconds):
        with self.lock:
            self.transactions += 1
            self.transaction_seconds += seconds

    def totals(self):
        with self.lock:
            stats = list(self.statements.values())
            queries = sum(s[0] for s in stats)
            seconds = sum(s[1] for s in stats) + self.transaction_seconds
            return queries, seconds

    def repeated(self):
        """The statement that ran the most times, when it ran at least :data:`REPEATED` times."""
        with self.lock:
            sql, stat = max(self.statements.items(), key=lambda item: item[1][0], default=("", [0]))
        return (sql, stat) if stat[0] >= REPEATED else None

    def summary(self):
        queries, seconds = self.totals()
        return f"queries={queries}; time={seconds:.3f}; connect={self.connect:.3f}; transactions={self.transactions}"

    def detail(self):
        with self.lock:
            items = sorted(self.statements.items(), key=lambda item: -item[1][1])
            data = {
                "v": 1, "received_at": round(self.received_at, 4),
                "answered_ms": round(((self.answered or time.monotonic()) - self.started) * 1000),
                "connect_ms": round(self.connect * 1000), "connections": self.connections,
                "transactions": self.transactions, "transaction_ms": round(self.transaction_seconds * 1000),
                "statements": [{"sql": sql, "count": s[0], "ms": round(s[1] * 1000), "first_ms": round(s[2] * 1000),
                                "last_ms": round(s[3] * 1000), "caller": s[4]} for sql, s in items[:MAX_STATEMENTS]],
                "more": max(0, len(items) - MAX_STATEMENTS),
            }
        while True:
            encoded = base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
            if len(encoded) <= HEADER_LIMIT or not data["statements"]:
                return encoded
            data["statements"].pop()  # the quickest ones go first
            data["more"] += 1

    def headers(self):
        return [(b"x-pdms-db", self.summary().encode()), (b"x-pdms-queries", self.detail().encode())]

    def log_line(self, method, path):
        """The ``[pdms]`` line for the service's log, or "" when nothing stood out."""
        queries, seconds = self.totals()
        repeated = self.repeated()
        if seconds < SLOW_DB and repeated is None:
            return ""
        text = f"{method} {path}: {queries} queries in {seconds:.2f} s"
        if self.connect >= 0.5:
            text += f" (+ {self.connect:.1f} s opening {self.connections} connection(s))"
        if repeated is not None:
            _sql, stat = repeated
            text += f" · the same query {stat[0]} times, {stat[1]:.2f} s"
            if stat[4]:
                text += f" ({short(stat[4])})"
        return text


def short(where):
    """``file:line`` from the service's folder when it is inside it."""
    cwd = os.getcwd() + os.sep
    return where[len(cwd):] if where.startswith(cwd) else where


def _timed(method, record):
    """Wrap a dialect method so the current request learns how long it took."""

    def wrapper(*args, **kwargs):
        request = current.get()
        if request is None:
            return method(*args, **kwargs)
        started = time.monotonic()
        try:
            return method(*args, **kwargs)
        finally:
            try:
                record(request, time.monotonic() - started)
            except Exception:  # noqa: BLE001 - measuring must never break the service
                pass

    setattr(wrapper, "_pdms_queries", True)
    return wrapper


def watch_sqlalchemy():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.engine.default import DefaultDialect

    if getattr(DefaultDialect.connect, "_pdms_queries", False):
        return

    def before(conn, cursor, statement, parameters, context, executemany):
        if current.get() is not None:
            conn.info["pdms_query_started"] = time.monotonic()

    def after(conn, cursor, statement, parameters, context, executemany):
        request = current.get()
        started = conn.info.pop("pdms_query_started", None)
        if request is None or started is None:
            return
        try:
            request.query(statement, started, time.monotonic() - started, caller())
        except Exception:  # noqa: BLE001 - measuring must never break the service
            pass

    event.listen(Engine, "before_cursor_execute", before)
    event.listen(Engine, "after_cursor_execute", after)
    DefaultDialect.connect = _timed(DefaultDialect.connect, Request.opened)
    DefaultDialect.do_commit = _timed(DefaultDialect.do_commit, Request.ended)
    DefaultDialect.do_rollback = _timed(DefaultDialect.do_rollback, Request.ended)


def measure_requests():
    import starlette.applications

    original = starlette.applications.Starlette.__call__
    if getattr(original, "_pdms_queries", False):
        return

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or current.get() is not None:  # a mounted app: the outer one measures
            return await original(self, scope, receive, send)
        request = Request()
        token = current.set(request)

        async def send_with_stats(message):
            if message["type"] == "http.response.start":
                request.answered = time.monotonic()
                try:
                    message = {**message, "headers": [*message.get("headers", []), *request.headers()]}
                except Exception:  # noqa: BLE001 - measuring must never break the service
                    pass
            await send(message)

        try:
            await original(self, scope, receive, send_with_stats)
        finally:
            current.reset(token)
            try:
                line = request.log_line(scope.get("method", ""), scope.get("path", ""))
                if line:
                    log(line)
            except Exception:  # noqa: BLE001 - same as above
                pass

    setattr(__call__, "_pdms_queries", True)
    starlette.applications.Starlette.__call__ = __call__


def install():
    if os.environ.get("PDMS_QUERY_STATS") != "1":
        return
    try:
        measure_requests()
        watch_sqlalchemy()
    except ImportError:  # not a web service with SQLAlchemy (poetry itself, a consumer...)
        pass
