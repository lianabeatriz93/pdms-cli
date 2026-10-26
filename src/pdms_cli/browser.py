"""The browser extension of pdms: the known front (https://dev-pdm.alivi.com/...) opens the frontend pdms runs.

The extension (``browser_extension/``, Manifest V3) asks the running pdms ui what to do every half a minute: a POST
to ``/extension/state`` that only an extension can send (``Origin: chrome-extension://<its id>``), answered with the
hosts to redirect and where to (``https://localhost:3000``), or nothing while ``front_redirect`` is off or the
frontend is not running. It redirects with ``declarativeNetRequest`` rules, keeping the path and the query, so a link
of an email or of the backend opens the local app. Without pdms ui it does nothing.

Chrome, Edge, Brave and Chromium load it unpacked from a folder of pdms' state (``extension_dir``): pdms opens the
browser's extensions page and the user loads the folder there, once. The ``key`` in its manifest keeps its id the
same everywhere. Each request it sends says which browser it runs in, so pdms ui knows where it is installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import frontend, instances
from .config import Config

SOURCE = resources.files("pdms_cli") / "browser_extension"
FILES = ("manifest.json", "background.js", "icon.png")
EXTENSION_ID = "ppmcicngmcaokmhnkgcgelkconeimaka"  # from the key of its manifest
STATE_PATH = "/extension/state"
# The extension may only redirect pages of alivi.com (its host permissions).
HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+alivi\.com$")
CONNECTED_FOR = 90  # seconds: the extension asks every 30
FOUND_FOR = 60  # seconds a look for installed browsers is kept
SEEN_KEYS = ("chrome", "edge", "brave", "chromium", "firefox")
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"


@dataclass(frozen=True)
class Browser:
    key: str
    name: str
    page: str  # its extensions page
    commands: tuple[str, ...]  # on Linux
    exe: str  # on Windows (App Paths)
    folders: tuple[str, ...]  # on Windows, under Program Files or LocalAppData
    app: str  # on macOS


BROWSERS = (
    Browser("chrome", "Google Chrome", "chrome://extensions", ("google-chrome", "google-chrome-stable"),
            "chrome.exe", ("Google/Chrome/Application",), "Google Chrome"),
    Browser("edge", "Microsoft Edge", "edge://extensions", ("microsoft-edge", "microsoft-edge-stable"),
            "msedge.exe", ("Microsoft/Edge/Application",), "Microsoft Edge"),
    Browser("brave", "Brave", "brave://extensions", ("brave-browser", "brave"),
            "brave.exe", ("BraveSoftware/Brave-Browser/Application",), "Brave Browser"),
    Browser("chromium", "Chromium", "chrome://extensions", ("chromium", "chromium-browser"),
            "chromium.exe", ("Chromium/Application",), "Chromium"),
)
BY_KEY = {browser.key: browser for browser in BROWSERS}


def extension_dir() -> Path:
    return instances.state_dir() / "browser-extension"


def seen_path() -> Path:
    return instances.state_dir() / "browser-seen.json"


def version() -> str:
    return json.loads(SOURCE.joinpath("manifest.json").read_text(encoding="utf-8"))["version"]


def write_extension() -> Path:
    """Copy the extension to :func:`extension_dir` (only the files that changed); the folder to load."""
    target = extension_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        data = SOURCE.joinpath(name).read_bytes()
        file = target / name
        if not file.is_file() or file.read_bytes() != data:
            file.write_bytes(data)
    return target


def refresh_extension() -> None:
    """Bring the folder up to date after a pdms update, when there is one: the extension reloads itself."""
    if extension_dir().is_dir():
        write_extension()


# --------------------------------------------------------------------------- browsers on this computer


def _windows_exe(browser: Browser) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{browser.exe}") as key:
                path = str(winreg.QueryValue(key, None))
        except OSError:
            continue
        if path and Path(path.strip('"')).is_file():
            return path.strip('"')
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        for folder in browser.folders:
            if (root := os.environ.get(base)) and (path := Path(root) / folder / browser.exe).is_file():
                return str(path)
    return None


def executable(browser: Browser) -> str | None:
    """Full path of the browser's program, or None when it is not installed."""
    if WINDOWS:
        return _windows_exe(browser)
    if MAC:
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            if (path := apps / f"{browser.app}.app" / "Contents" / "MacOS" / browser.app).is_file():
                return str(path)
        return None
    return next((path for command in browser.commands if (path := shutil.which(command))), None)


_found: tuple[float, dict[str, str]] = (0.0, {})


def installed() -> dict[str, str]:
    """``{key: program}`` of the browsers installed here (looked for at most once a minute)."""
    global _found
    at, found = _found
    if time.monotonic() - at > FOUND_FOR or not at:
        found = {browser.key: path for browser in BROWSERS if (path := executable(browser))}
        _found = (time.monotonic(), found)
    return found


def open_page(browser: Browser) -> bool:
    """Open the browser's extensions page (a page cannot link to it); False when it could not."""
    program = installed().get(browser.key) or executable(browser)
    if not program:
        return False
    try:
        subprocess.Popen([program, browser.page], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **instances.detach_options())
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- what the extension asks


def is_extension(origin: str) -> bool:
    """Whether a request comes from this extension (pages cannot send these origins)."""
    return origin == f"chrome-extension://{EXTENSION_ID}" or origin.startswith("moz-extension://")


def seen() -> dict[str, dict]:
    """``{browser: {"at": epoch seconds, "version": ...}}``: when the extension last asked from each browser."""
    try:
        data = json.loads(seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if key in SEEN_KEYS and isinstance(value, dict)} \
        if isinstance(data, dict) else {}


def record_seen(browser: object, extension_version: object) -> None:
    if browser not in SEEN_KEYS:
        return
    data = seen()
    data[str(browser)] = {"at": time.time(), "version": str(extension_version or "")[:20]}
    seen_path().parent.mkdir(parents=True, exist_ok=True)
    seen_path().write_text(json.dumps(data), encoding="utf-8")


def target() -> str:
    """Where the known front goes: the frontend pdms runs, once it answers ("" while not)."""
    current = frontend.running()
    if not current:
        return ""
    scheme = frontend.scheme(int(current["port"]))
    return frontend.url(int(current["port"]), scheme) if scheme else ""


def answer(cfg: Config) -> dict:
    """What the extension does now: redirect ``hosts`` to ``target`` while ``active``."""
    where = target() if cfg.defaults.front_redirect else ""
    return {"pdms": "front-redirect", "active": bool(where), "target": where,
            "hosts": list(cfg.defaults.front_redirect_hosts), "version": version()}


def status(cfg: Config) -> dict:
    """The redirect and the extension in each browser installed here (or where it was seen)."""
    found, last = installed(), seen()
    now = time.time()
    rows = []
    for key in (*BY_KEY, "firefox"):
        if key not in found and key not in last:
            continue
        at = float(last.get(key, {}).get("at") or 0)
        rows.append({
            "key": key, "name": BY_KEY[key].name if key in BY_KEY else "Firefox", "found": key in found, "seen": datetime.fromtimestamp(at).isoformat(timespec="seconds") if at else "",
            "connected": bool(at) and now - at < CONNECTED_FOR,
        })
    return {
        "redirect": cfg.defaults.front_redirect, "hosts": list(cfg.defaults.front_redirect_hosts),
        "folder": str(extension_dir()), "browsers": rows,
    }
