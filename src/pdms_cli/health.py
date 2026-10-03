"""How the line to each database is: the way it goes (this machine, a tunnel, straight out) and what one round trip
costs, which every query pays at least once. Also the line to the internet, for when everything is slow.

A tunnel is a database whose host name resolves to this machine (127.x.x.x) without being ``localhost``: a port
forward such as the SSM tunnels of devo-cli (``/etc/hosts`` sends the RDS name to 127.0.0.6, where socat listens).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

from .config import Database

SLOW_MS = 100  # from here on every query is noticeably slower than on this machine
VERY_SLOW_MS = 300  # pdms ui says so on Home: a list request makes about six round trips
LINE_HOST = ("1.1.1.1", 443)
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


@dataclass
class Route:
    kind: str  # "local", "tunnel" or "direct"
    address: str = ""  # where the tunnel listens, e.g. 127.0.0.6:5432
    up: bool | None = None  # whether something listens there (tunnels only)
    process: str = ""  # what listens there, when the system tells (socat, session-manager-plugin...)


def route(db: Database) -> Route:
    host = db.host.strip().lower()
    if host in LOCAL_NAMES:
        return Route("local")
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return Route("direct")
    if not ip.startswith("127."):
        return Route("direct")
    address = f"{ip}:{db.port}"
    up = listening(ip, db.port)
    return Route("tunnel", address, up, listener(ip, db.port) if up else "")


def listening(ip: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def listener(ip: str, port: int) -> str:
    """The name of the process listening on ``ip:port`` ("" when the system does not say)."""
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port \
                    and conn.laddr.ip in (ip, "0.0.0.0", "::") and conn.pid:
                return psutil.Process(conn.pid).name()
    except Exception:  # noqa: BLE001 - macOS needs root for other processes' sockets; the name is only a nicety
        pass
    return ""


def connect(db: Database, timeout: int):
    import psycopg

    return psycopg.connect(host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password,
                           connect_timeout=timeout, autocommit=True)


def round_trip(conn) -> float:
    """Milliseconds of one round trip: ``select 1`` on an open connection."""
    started = time.monotonic()
    conn.execute("select 1").fetchone()
    return (time.monotonic() - started) * 1000


def probe(db: Database, timeout: int) -> tuple[str, float]:
    """``(server version, round trip in ms)`` with a new connection; the driver's exception when it fails."""
    with connect(db, timeout) as conn:
        version = conn.execute("select version()").fetchone()[0].split(",")[0]
        return version, round_trip(conn)


def line(timeout: float = 3) -> float | None:
    """Milliseconds to open a connection to the internet (one round trip), or None when it does not answer."""
    started = time.monotonic()
    try:
        with socket.create_connection(LINE_HOST, timeout=timeout):
            return (time.monotonic() - started) * 1000
    except OSError:
        return None


def describe_ms(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


class Meter:
    """Keeps one open connection per database, so each measure is a single round trip (opening a connection costs a
    dozen, and would hide what the queries pay)."""

    def __init__(self) -> None:
        self._conns: dict[str, tuple[tuple, object]] = {}
        self._lock = threading.Lock()

    def measure(self, name: str, db: Database, timeout: int = 10) -> tuple[float | None, str]:
        """``(milliseconds, "")`` or ``(None, error)``."""
        key = (db.host, db.port, db.database, db.user, db.password)
        with self._lock:
            known = self._conns.get(name)
            conn = known[1] if known and known[0] == key else None
            if known and conn is None:
                self._close(name)
        try:
            if conn is None:
                conn = connect(db, timeout)
                with self._lock:
                    self._conns[name] = (key, conn)
            return round_trip(conn), ""
        except Exception as exc:  # noqa: BLE001 - any driver error is the answer; connect again next time
            with self._lock:
                self._close(name)
            return None, (str(exc).strip().splitlines() or [type(exc).__name__])[0]

    def forget(self, keep: set[str]) -> None:
        """Close the connections of the databases no longer measured."""
        with self._lock:
            for name in [n for n in self._conns if n not in keep]:
                self._close(name)

    def _close(self, name: str) -> None:
        _key, conn = self._conns.pop(name, (None, None))
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001 - it was broken already
            pass
