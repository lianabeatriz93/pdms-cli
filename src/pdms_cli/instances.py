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
import time
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
    # SQS consumers (event Lambdas) have no port: the queue they read from.
    queue: str = ""
    # Each request in its own thread (defaults.parallel_requests); False also for instances from older pdms.
    parallel: bool = False
    # Commit of its repo when it started ("" when unknown): what changed since, for pdms ui's Home.
    commit: str = ""

    @property
    def is_consumer(self) -> bool:
        return bool(self.queue)

    @property
    def name(self) -> str:
        return Path(self.service).name

    def alive(self) -> bool:
        return process_alive(self.pid, self.created)


# How far the creation time psutil reports may move for the same process. psutil adds the process's start (counted
# from boot) to the boot time, and the boot time follows the wall clock: every NTP adjustment moves it by a second or
# more, so a strict comparison would take a running service for a stopped one (and "Forget" would lose it). A reused
# PID within this margin of the old process's start would need the PID counter to wrap in that time.
CREATED_SLACK = 30.0


def same_process(actual: float, created: float) -> bool:
    return abs(actual - created) < CREATED_SLACK


def process_alive(pid: int, created: float = 0.0) -> bool:
    """Whether ``pid`` is running (not a zombie) and, if ``created`` is known, is still the same process."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return not created or same_process(proc.create_time(), created)
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
    """Replace the registry in one step: pdms ui reads it every couple of seconds while the CLI writes it, and a
    half-written file would read as empty (the next save would then forget every instance)."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({k: asdict(v) for k, v in instances.items()}, indent=2), encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:  # Windows: someone is reading it right now
            if attempt == 39:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def make_key(service: Path, port: int) -> str:
    return f"{service.name}@{port}" if port else f"{service.name}@sqs"


def running_ports() -> set[int]:
    return {i.port for i in load().values() if i.port and i.alive()}


def spawn(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    """Start ``cmd`` detached, writing stdout+stderr to ``log`` (the previous run's log is kept as ``<log>.1``)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log)
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"# pdms {datetime.now():%Y-%m-%d %H:%M:%S} · {' '.join(cmd)}\n")
        fh.flush()
        return subprocess.Popen(
            cmd,
            cwd=cwd,
            # Logs are files read as UTF-8; without this, Windows would write them in cp1252 and a service
            # printing a non-cp1252 character would crash.
            env={"PYTHONIOENCODING": "utf-8", **env, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            **detach_options(),
        )


def start(
    service: Path, cmd: list[str], env: dict[str, str], *, host: str, port: int, user: str, db: str, reload: bool,
    deps: dict[str, str] | None = None, events: str = "aws", queue: str = "", parallel: bool = False, commit: str = "",
) -> Instance:
    key = make_key(service, port)
    log = log_path(key)
    proc = spawn(cmd, service, env, log)
    instance = Instance(
        key=key, pid=proc.pid, service=str(service), host=host, port=port, user=user, db=db,
        reload=reload, log=str(log), started_at=datetime.now().isoformat(timespec="seconds"),
        created=creation_time(proc.pid), deps=deps or {}, events=events, queue=queue,
        parallel=parallel, commit=commit,
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


def contains(path: str, marker: str) -> bool:
    """Whether a line of the file has ``marker``, reading only until the first one that does."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return any(marker in line for line in fh)
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- processes outside pdms

POLLER_SCRIPT = "pdms_sqs_poller.py"
# Who a process gets as its parent once the one that started it exited (Linux desktops use a systemd --user).
ADOPTIVE_PARENTS = {"systemd", "launchd", "init"}
# What pdms keeps from a stray's environment: who it runs as, its database and where its events go.
STRAY_ENV = ("DEV_USER_ID", "DEV_USERNAME", "DB_PG_CONNECTION_STR", "PDMS_SQS_ENDPOINT")


@dataclass
class Stray:
    """A service pdms started (or one like it) that is still running but is not in the registry any more."""

    key: str
    pid: int
    created: float
    service: str
    host: str
    port: int
    reload: bool
    queue: str
    log: str  # the pdms log it still writes to, "" when its output goes elsewhere
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return Path(self.service).name

    @property
    def started_at(self) -> str:
        return datetime.fromtimestamp(self.created).isoformat(timespec="seconds")


def _option(args: list[str], name: str) -> str:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return ""


def _file_name(arg: str) -> str:
    return re.split(r"[\\/]", arg)[-1].lower()


def launch_of(args: list[str]) -> tuple[str, int, bool, str] | None:
    """``(host, port, reload, queue)`` when ``args`` is a service the way pdms starts one: ``uvicorn main:app`` or
    the SQS poller of an event consumer; None for anything else."""
    if any(_file_name(arg) == POLLER_SCRIPT for arg in args):
        return "", 0, False, _option(args, "--queue-url").rstrip("/").rsplit("/", 1)[-1]
    if "main:app" in args and any(_file_name(arg).removesuffix(".exe") == "uvicorn" for arg in args):
        port = _option(args, "--port")
        return _option(args, "--host") or "127.0.0.1", int(port) if port.isdigit() else 8000, "--reload" in args, ""
    return None


def _orphaned(proc: psutil.Process) -> bool:
    """Whether the process that started ``proc`` exited (a foreground ``pdms run`` or a debugger still holds it)."""
    try:
        parent = proc.parent()
        if parent is None or parent.pid == 1:
            return True
        # Windows does not re-parent: the PID may now belong to a newer, unrelated process.
        return parent.create_time() > proc.create_time() or parent.name().lower() in ADOPTIVE_PARENTS
    except psutil.Error:
        return True


def _top(proc: psutil.Process) -> psutil.Process:
    """The process pdms would have started: ``poetry run`` stays as the parent where it cannot exec (Windows)."""
    try:
        while (parent := proc.parent()) and parent.name().lower().startswith("poetry"):
            proc = parent
    except psutil.Error:
        pass
    return proc


def _known_pids() -> set[int]:
    pids: set[int] = set()
    for inst in load().values():
        if not inst.alive():
            continue
        pids.add(inst.pid)
        try:
            pids.update(child.pid for child in psutil.Process(inst.pid).children(recursive=True))
        except psutil.Error:
            pass
    return pids


def _log_of(*procs: psutil.Process) -> str:
    logs = state_dir() / "logs"
    for proc in procs:
        try:
            for opened in proc.open_files():
                if Path(opened.path).parent == logs and opened.path.endswith(".log"):
                    return opened.path
        except psutil.Error:
            pass
    return ""


def _environment(proc: psutil.Process) -> dict[str, str]:
    try:
        env = proc.environ()
    except psutil.Error:
        return {}
    return {name: env[name] for name in STRAY_ENV if name in env}


def strays(roots: list[Path]) -> list[Stray]:
    """Services of the repos in ``roots`` running on their own: their pdms exited and the registry lost them."""
    roots = [root.resolve() for root in roots]
    known = _known_pids()
    found: dict[int, Stray] = {}
    for proc in psutil.process_iter(["cmdline"]):
        launch = launch_of(proc.info["cmdline"] or [])
        if launch is None or proc.pid in known:
            continue
        top = _top(proc)
        if top.pid in known or top.pid in found or not _orphaned(top):
            continue
        try:
            service = Path(proc.cwd()).resolve()
            created = top.create_time()
        except psutil.Error:
            continue
        if not (service / "main.py").is_file() or not any(service.is_relative_to(root) for root in roots):
            continue
        host, port, reload, queue = launch
        found[top.pid] = Stray(
            key=make_key(service, 0 if queue else port), pid=top.pid, created=created, service=str(service),
            host=host, port=0 if queue else port, reload=reload, queue=queue, log=_log_of(proc, top),
            env=_environment(proc),
        )
    return sorted(found.values(), key=lambda stray: stray.key)


def adopt(stray: Stray, *, user: str, db: str) -> Instance:
    """Put ``stray`` back in the registry, so pdms ps, logs, stop and restart know it again. Its output stays where
    it goes: in its old log when it still writes one, otherwise nowhere pdms can read (until it restarts)."""
    log = stray.log
    if not log:
        path = log_path(stray.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            rotate_log(path)
        except PermissionError:
            # Windows, where psutil may not tell which files a process holds: in use means it still writes there.
            log = str(path)
    if not log:
        path.write_text(_("# pdms {when} · adopted {key} (pid {pid}): its output does not come to this file. "
                          "pdms restart {key} to have its log here.\n", when=f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                          key=stray.key, pid=stray.pid), encoding="utf-8")
        log = str(path)
    instance = Instance(
        key=stray.key, pid=stray.pid, service=stray.service, host=stray.host, port=stray.port, user=user, db=db,
        reload=stray.reload, log=log, started_at=stray.started_at, created=stray.created,
        events="local" if "PDMS_SQS_ENDPOINT" in stray.env else "aws", queue=stray.queue,
    )
    registry = load()
    registry[stray.key] = instance
    save(registry)
    return instance


# --------------------------------------------------------------------------- health

# uvicorn logs these once the app imported fine (also after every --reload).
STARTED_MARKERS = ("Started server process", "Application startup complete")
# ...and these when importing/starting the app failed. With --reload the process stays alive after them.
FAILED_MARKERS = ("Traceback (most recent call last)", "Error loading ASGI app")
# The app serves requests from this line on, until --reload restarts it (the new process imports the app first).
# A traceback after it comes from a request that failed: the app goes on serving.
SERVING_MARKER = "Application startup complete"
RESTART_MARKERS = ("Reloading...", "Shutting down", "Finished server process", "Error loading ASGI app")
EXCEPTION_LINE = re.compile(r"^[\w.]*(Error|Exception|Exit)\b.*")


@dataclass
class Health:
    state: str  # ok | busy | starting | error | stopped
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


def last_marker(path: str, markers: tuple[str, ...], limit: int = 8 << 20) -> str:
    """The marker on the latest line of the file that has one (reading back from the end, at most ``limit`` bytes)."""
    try:
        with open(path, "rb") as fh:
            end = fh.seek(0, os.SEEK_END)
            start = max(0, end - limit)
            pos, rest = end, b""
            while pos > start:
                step = min(1 << 16, pos - start)
                pos -= step
                fh.seek(pos)
                lines = (fh.read(step) + rest).split(b"\n")
                rest = lines.pop(0) if pos > start else b""
                for line in reversed(lines):
                    text = line.decode("utf-8", errors="replace")
                    if found := next((m for m in markers if m in text), None):
                        return found
    except FileNotFoundError:
        pass
    return ""


def serving(log: str) -> bool:
    """Whether the app finished starting and has not been restarted since (it may be busy, not answering)."""
    return last_marker(log, (SERVING_MARKER, *RESTART_MARKERS)) == SERVING_MARKER


FRAME_LINE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)')


def last_traceback(log: str, lines: int = 300, limit: int = 120) -> list[str]:
    """The latest traceback of the log, from its first line to its exception (at most ``limit`` lines)."""
    text = tail(log, lines).splitlines()
    start = max((i for i, line in enumerate(text) if FAILED_MARKERS[0] in line), default=-1)
    if start < 0:
        return []
    end = next((i for i in range(start + 1, len(text))
                if not text[i].startswith((" ", "\t")) and EXCEPTION_LINE.match(text[i].strip())), len(text) - 1)
    block = text[start:end + 1]
    return block if len(block) <= limit else [*block[:limit // 2], "  …", *block[-(limit // 2):]]


POLLER_READY = "[pdms-poller] Polling"


def health(instance: Instance) -> Health:
    if not instance.alive():
        return Health("stopped")
    if instance.is_consumer:  # no HTTP: ready once the poller listens on its queue
        # Written once at the top of the log (each run starts a new one), then buried by the service's own output.
        return Health("ok") if contains(instance.log, POLLER_READY) else Health("starting")
    if responds(instance.host, instance.port):
        return Health("ok")
    if serving(instance.log):  # e.g. a sync DB call inside an async endpoint blocks uvicorn's only event loop
        return Health("busy", _("not answering while it works on a request"))
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
