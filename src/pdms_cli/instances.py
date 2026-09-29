"""Background service instances: registry, start, stop and logs.

Each instance runs detached from the terminal (its own session on macOS/Linux, its own process group on Windows),
is stopped together with all its child processes (the uvicorn reloader and its workers) and writes stdout+stderr
to a log file. Process handling goes through psutil so it behaves the same on Windows, macOS and Linux. The registry lives in
``~/.local/state/pdms/instances.json`` (override the base dir with ``XDG_STATE_HOME``).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import psutil

from .i18n import _

WINDOWS = sys.platform == "win32"


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "pdms"


def registry_path() -> Path:
    return state_dir() / "instances.json"


def log_path(key: str) -> Path:
    return state_dir() / "logs" / f"{key}.log"


def previous_log_path(log: str | Path) -> Path:
    return Path(f"{log}.1")


def rotate_log(log: Path) -> None:
    """Keep the previous run's log as ``<log>.1`` (one generation) so a crash is not lost on restart."""
    if log.exists() and log.stat().st_size > 0:
        os.replace(log, previous_log_path(log))


@dataclass
class Instance:
    key: str
    pid: int
    service: str
    host: str
    port: int
    user: str
    db: str
    reload: bool
    log: str
    started_at: str
    # Process creation time, to tell our process apart from a later one that reused the PID (0 = unknown).
    created: float = 0.0
    # Fingerprint of each installed part (service + local libraries) when it started; empty = unknown.
    deps: dict[str, str] = field(default_factory=dict)
    # Where it publishes SQS events: "local" (pdms ElasticMQ) or "aws".
    events: str = "aws"

    @property
    def name(self) -> str:
        return Path(self.service).name

    def alive(self) -> bool:
        return process_alive(self.pid, self.created)


def process_alive(pid: int, created: float = 0.0) -> bool:
    """Whether ``pid`` is running (not a zombie) and, if ``created`` is known, is still the same process."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return not created or abs(proc.create_time() - created) < 1
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


def creation_time(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0


def detach_options() -> dict:
    """Popen options so the service survives the terminal and does not receive its Ctrl+C."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid: int, created: float = 0.0, timeout: float = 10) -> None:
    """Terminate ``pid`` and all its descendants, escalating to kill after ``timeout`` seconds."""
    if not process_alive(pid, created):
        return
    try:
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        return
    if not WINDOWS:
        # Also reach processes of the session that were re-parented away from the tree.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _, remaining = psutil.wait_procs(procs, timeout=timeout)
    for proc in remaining:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(remaining, timeout=3)


def load() -> dict[str, Instance]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: Instance(**v) for k, v in data.items()}


def save(instances: dict[str, Instance]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: asdict(v) for k, v in instances.items()}, indent=2), encoding="utf-8")


def make_key(service: Path, port: int) -> str:
    return f"{service.name}@{port}"


def running_ports() -> set[int]:
    return {i.port for i in load().values() if i.alive()}


def start(
    service: Path, cmd: list[str], env: dict[str, str], *, host: str, port: int, user: str, db: str, reload: bool,
    deps: dict[str, str] | None = None, events: str = "aws",
) -> Instance:
    key = make_key(service, port)
    log = log_path(key)
    log.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log)
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"# pdms {datetime.now():%Y-%m-%d %H:%M:%S} · {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=service,
            env={**env, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            **detach_options(),
        )
    instance = Instance(
        key=key, pid=proc.pid, service=str(service), host=host, port=port, user=user, db=db,
        reload=reload, log=str(log), started_at=datetime.now().isoformat(timespec="seconds"),
        created=creation_time(proc.pid), deps=deps or {}, events=events,
    )
    instances = load()
    instances[key] = instance
    save(instances)
    return instance


def stop(instance: Instance, timeout: float = 10) -> None:
    """Stop the instance and all its child processes, then forget it."""
    kill_tree(instance.pid, instance.created, timeout)
    forget(instance.key)


def forget(key: str) -> None:
    instances = load()
    if instances.pop(key, None):
        save(instances)


def tail(path: str, lines: int = 20) -> str:
    try:
        return "".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])
    except FileNotFoundError:
        return ""


# --------------------------------------------------------------------------- health

# uvicorn logs these once the app imported fine (also after every --reload).
STARTED_MARKERS = ("Started server process", "Application startup complete")
# ...and these when importing/starting the app failed. With --reload the process stays alive after them.
FAILED_MARKERS = ("Traceback (most recent call last)", "Error loading ASGI app")
EXCEPTION_LINE = re.compile(r"^[\w.]*(Error|Exception|Exit)\b.*")


@dataclass
class Health:
    state: str  # ok | starting | error | stopped
    detail: str = ""


def responds(host: str, port: int, timeout: float = 1.5) -> bool:
    """Any HTTP answer (even 404) means the app is serving; the reloader alone accepts TCP but never answers."""
    target = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(host, host)
    conn = http.client.HTTPConnection(target, port, timeout=timeout)
    try:
        conn.request("GET", "/")
        conn.getresponse()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def startup_error(log: str, lines: int = 300) -> str:
    """The exception that broke the latest (re)start, or "" if the app loaded after it."""
    text = tail(log, lines).splitlines()
    last = lambda markers: max((i for i, l in enumerate(text) if any(m in l for m in markers)), default=-1)  # noqa: E731
    failed = last(FAILED_MARKERS)
    if failed <= last(STARTED_MARKERS):
        return ""
    exceptions = [l.strip() for l in text[failed:] if EXCEPTION_LINE.match(l.strip())]
    return exceptions[-1] if exceptions else _("app failed to load")


def health(instance: Instance) -> Health:
    if not instance.alive():
        return Health("stopped")
    if responds(instance.host, instance.port):
        return Health("ok")
    error = startup_error(instance.log)
    return Health("error", error) if error else Health("starting")


def health_all(items: list[Instance]) -> dict[str, Health]:
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(items))) as pool:
        return dict(zip((i.key for i in items), pool.map(health, items)))


# --------------------------------------------------------------------------- endpoints

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class Endpoint:
    method: str
    path: str
    summary: str = ""


def fetch_openapi(instance: Instance, timeout: float = 3) -> dict | None:
    """The live OpenAPI spec of a running instance (FastAPI serves it at /openapi.json)."""
    target = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(instance.host, instance.host)
    conn = http.client.HTTPConnection(target, instance.port, timeout=timeout)
    try:
        conn.request("GET", "/openapi.json")
        response = conn.getresponse()
        if response.status != 200:
            return None
        return json.loads(response.read())
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()


def endpoints(spec: dict) -> list[Endpoint]:
    found = []
    for path, operations in (spec.get("paths") or {}).items():
        for method, operation in operations.items():
            if method in HTTP_METHODS:
                found.append(Endpoint(method.upper(), path, (operation or {}).get("summary", "")))
    order = {m.upper(): i for i, m in enumerate(HTTP_METHODS)}
    return sorted(found, key=lambda e: (e.path, order[e.method]))
