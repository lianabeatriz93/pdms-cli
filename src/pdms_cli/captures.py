"""What went through the background proxy, for pdms ui's request detail: headers, bodies and the answer.

The proxy appends one JSON line per request to ``proxy-requests.jsonl`` in the state folder (only the user can read
it). The file is a ring of two: past a few MB it becomes ``.1`` and a new one starts, so it never grows. Bodies are
kept up to :data:`BODY_LIMIT`. The file keeps the headers as sent, so Replay works as the original request did;
what leaves for the page (:func:`public`) or a curl command has the secret ones hidden.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import shlex
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import instances
from .i18n import _

BODY_LIMIT = 64 * 1024
FILE_LIMIT = 4 << 20
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
HIDDEN = "(hidden)"
# A service's answer carries what the request asked the database (sqs_patch/pdms_queries.py): kept, not passed on.
QUERIES_HEADER = "x-pdms-queries"
REPEATED = 5  # the same statement this many times in one request: probably one query per row (as in pdms_queries)


def path() -> Path:
    return instances.state_dir() / "proxy-requests.jsonl"


def new_id() -> str:
    return secrets.token_hex(4)


def body(data: bytes | None) -> dict:
    """A body as the file keeps it: text up to the limit, or only its size when it is binary."""
    if not data:
        return {"size": 0, "text": ""}
    try:
        text = data[:BODY_LIMIT].decode("utf-8")
    except UnicodeDecodeError:
        return {"size": len(data), "binary": True}
    return {"size": len(data), "text": text, "truncated": len(data) > BODY_LIMIT}


class Recorder:
    """Appends captures from the proxy's threads, one line each."""

    def __init__(self, file: Path | None = None) -> None:
        self.file = file or path()
        self._lock = threading.Lock()

    def record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.file.stat().st_size > FILE_LIMIT:
                    os.replace(self.file, self.file.with_suffix(".jsonl.1"))
            except FileNotFoundError:
                pass
            fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # bodies and tokens
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)


def entry(ident: str, method: str, path_: str, status: int, target: str, seconds: float,
          request_headers: list[tuple[str, str]], request_body: bytes | None,
          response_headers: list[tuple[str, str]], response_body: bytes | None, db: dict | None = None,
          started_at: float = 0.0) -> dict:
    kept = {
        "id": ident, "at": datetime.now().isoformat(timespec="seconds"), "method": method, "path": path_,
        "status": status, "target": target, "ms": round(seconds * 1000),
        "request": {"headers": [list(pair) for pair in request_headers], "body": body(request_body)},
        "response": {"headers": [list(pair) for pair in response_headers], "body": body(response_body)},
    }
    if db is not None:
        kept["db"] = db
    if started_at:
        kept["started_at"] = round(started_at, 4)  # epoch: the request's events are placed in time from here
    return kept


def query_detail(headers: list[tuple[str, str]]) -> dict | None:
    """What the service said its request asked the database, from its answer's headers (None if it did not)."""
    for name, value in headers:
        if name.lower() != QUERIES_HEADER:
            continue
        try:
            data = json.loads(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("statements"), list) else None
    return None


def place_in_time(db: dict, sent_at: float, total_ms: int) -> dict:
    """``db`` with where the service's part falls in the proxy's time line: ``service_at_ms``, from when the proxy got
    the request until the service did (the proxy's own work, then the service's queue: uvicorn and the other
    requests of the page). Both clocks are this machine's."""
    received = db.get("received_at")
    if not isinstance(received, (int, float)):
        return db
    at = round((received - sent_at) * 1000)
    return {**db, "service_at_ms": max(0, min(at, total_ms))}


def db_totals(db: dict) -> tuple[int, int, int]:
    """``(queries, milliseconds in the database, times the most repeated statement ran)``."""
    statements = [s for s in db.get("statements", []) if isinstance(s, dict)]
    queries = sum(int(s.get("count", 0)) for s in statements)
    ms = sum(int(s.get("ms", 0)) for s in statements) + int(db.get("transaction_ms", 0))
    return queries, ms, max((int(s.get("count", 0)) for s in statements), default=0)


def find(ident: str, file: Path | None = None) -> dict | None:
    """The capture with this id, looking from the newest (the current file, then the previous one)."""
    file = file or path()
    for candidate in (file, file.with_suffix(".jsonl.1")):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            if f'"id": "{ident}"' in line:
                try:
                    return json.loads(line)
                except ValueError:
                    return None
    return None


def hide(name: str, value: str) -> str:
    if name.lower() not in SECRET_HEADERS:
        return value
    scheme = value.split(" ", 1)[0] if " " in value else ""
    return f"{scheme} {HIDDEN}" if scheme else HIDDEN


def public(capture: dict) -> dict:
    """The capture for the page: secret headers hidden."""
    result = json.loads(json.dumps(capture))
    for part in ("request", "response"):
        result[part]["headers"] = [[name, hide(name, value)] for name, value in result[part]["headers"]]
    return result


def curl(capture: dict, port: int) -> str:
    """A curl command that sends the request through the proxy again, without the secret headers."""
    words = ["curl", "-i", "-X", capture["method"], f"http://localhost:{port}{capture['path']}"]
    for name, value in capture["request"]["headers"]:
        if name.lower() not in SECRET_HEADERS and name.lower() not in ("origin", "referer", "accept-encoding"):
            words += ["-H", f"{name}: {value}"]
    sent = capture["request"]["body"]
    if sent.get("text"):
        words += ["--data-raw", sent["text"]]
    return " ".join(shlex.quote(word) for word in words)


def replayable(capture: dict) -> str:
    """Why this request cannot be sent again ("" when it can)."""
    sent = capture["request"]["body"]
    if sent.get("binary"):
        return _("its body is binary")
    if sent.get("truncated"):
        return _("its body is larger than {size} KB", size=BODY_LIMIT // 1024)
    return ""


def replay(capture: dict, port: int, timeout: float = 300) -> tuple[int, float]:
    """Send the request again through the proxy on ``port``, as it came; ``(status, seconds)``."""
    sent = capture["request"]["body"]
    data = sent["text"].encode("utf-8") if sent.get("text") else None
    headers = {name: value for name, value in capture["request"]["headers"]}
    if data is not None:
        headers["Content-Length"] = str(len(data))
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(capture["method"], capture["path"], body=data, headers=headers)
        response = conn.getresponse()
        response.read()
        return response.status, time.monotonic() - started
    finally:
        conn.close()
