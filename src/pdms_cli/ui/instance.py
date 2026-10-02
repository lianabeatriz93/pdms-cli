"""One pdms ui at a time, its log, and what it has to do after restarting itself for an update.

The running UI writes its port and token to ``ui.json`` in the state folder (only the user can read it); a second
``pdms ui`` finds it there and asks the first one to show itself instead of starting another server. Opened from the
app menu there is no terminal, so its output goes to ``logs/ui.log`` (``pdms logs ui``).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .. import __version__, instances

KEY = "ui"
SHOW_PATH = "/api/ui/show"
TOKEN_HEADER = "X-Pdms-Token"
TOKEN_ENV = "PDMS_UI_TOKEN"  # the token a restart keeps, so open pages and windows reconnect


def state_path() -> Path:
    return instances.state_dir() / "ui.json"


def log_path() -> Path:
    return instances.log_path(KEY)


def after_update_path() -> Path:
    return instances.state_dir() / "ui-after-update.json"


# --------------------------------------------------------------------------- the running one


def remember(port: int, token: str, window: bool) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({
        "pid": os.getpid(), "created": instances.creation_time(os.getpid()), "port": port, "token": token,
        "window": window, "version": __version__, "started_at": datetime.now().isoformat(timespec="seconds"),
    })
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds the token
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)


def forget() -> None:
    """Remove ``ui.json`` if it is this process's."""
    current = load()
    if current and current.get("pid") == os.getpid():
        state_path().unlink(missing_ok=True)


def load() -> dict | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and {"pid", "port", "token"} <= data.keys() else None


def running() -> dict | None:
    """The pdms ui of ``ui.json`` while its process lives (not a recycled pid), else None."""
    current = load()
    if not current or current["pid"] == os.getpid():
        return None
    return current if instances.process_alive(int(current["pid"]), float(current.get("created") or 0)) else None


def url(current: dict) -> str:
    return f"http://127.0.0.1:{current['port']}/?token={current['token']}"


def show(current: dict, timeout: float = 5) -> bool:
    """Ask the running pdms ui to show its window (or open the browser); False if it does not answer."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{current['port']}{SHOW_PATH}", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", TOKEN_HEADER: str(current["token"])},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


# --------------------------------------------------------------------------- without a terminal


_redirected: Path | None = None


def redirect_output() -> Path:
    """Send this process's output (Python's and the GUI toolkit's) to ``ui.log``; the previous one is kept."""
    global _redirected
    if _redirected:
        return _redirected
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        instances.rotate_log(path)
    except OSError:  # another pdms ui still writes it (Windows locks it)
        pass
    log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - lives as long as pdms
    log.write(f"# pdms {__version__} · {datetime.now():%Y-%m-%d %H:%M:%S} · {' '.join(sys.argv)}\n")
    for fd in (1, 2):
        try:
            os.dup2(log.fileno(), fd)
        except OSError:  # no such descriptor (pdmsw.exe on Windows has no console)
            pass
    sys.stdout = sys.stderr = log
    _redirected = path
    return path


# --------------------------------------------------------------------------- after an update


def save_after_update(data: dict) -> None:
    """What the restarted pdms ui must do or tell: the version it should be, the proxy to start again..."""
    after_update_path().parent.mkdir(parents=True, exist_ok=True)
    after_update_path().write_text(json.dumps(data), encoding="utf-8")


def take_after_update() -> dict | None:
    """Read and remove what the previous pdms ui left for after its update."""
    path = after_update_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else None
