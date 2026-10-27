"""The browser extension of pdms: the known front (https://dev-pdm.alivi.com/...) opens the frontend pdms runs.

The extension (``browser_extension/``, Manifest V3) asks the running pdms ui what to do every half a minute: a POST
to ``/extension/state`` that only an extension can send (``Origin: chrome-extension://<its id>``), answered with the
hosts to redirect and where to (``https://localhost:3000``), or nothing while ``front_redirect`` is off or the
frontend is not running. It redirects with ``declarativeNetRequest`` rules, keeping the path and the query, so a link
of an email or of the backend opens the local app. Without pdms ui it does nothing.

Chrome, Edge, Brave and Chromium load it unpacked from a folder of pdms' state (``extension_dir``): pdms opens the
browser's extensions page and the user loads the folder there, once. The ``key`` in its manifest keeps its id the
same everywhere. Each request it sends says which browser it runs in, so pdms ui knows where it is installed.

Firefox only installs extensions Mozilla signed. The release workflow signs this one (unlisted on addons.mozilla.org,
with ``firefox_manifest``) whenever its version changes, puts it in the wheel (``firefox.xpi``) and in the GitHub
release; pdms opens that file in Firefox, which asks whether to add it. A checkout has no signed file: pdms downloads
the one of the latest release.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import frontend, i18n, instances, wsl
from .config import Config

SOURCE = resources.files("pdms_cli") / "browser_extension"
FILES = ("manifest.json", "background.js", "texts.js", "icon.png", "popup.html", "popup.js", "popup.css")
EXTENSION_ID = "ppmcicngmcaokmhnkgcgelkconeimaka"  # from the key of its manifest
STATE_PATH = "/extension/state"
REDIRECT_PATH = "/extension/redirect"  # its panel turns the redirect on or off
# The extension may only redirect pages of alivi.com (its host permissions).
HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+alivi\.com$")
CONNECTED_FOR = 90  # seconds: the extension asks every 30
FOUND_FOR = 60  # seconds a look for installed browsers is kept
SEEN_KEYS = ("chrome", "edge", "brave", "chromium", "firefox")
GECKO_ID = "pdms-front-redirect@alivi.com"  # its id in Firefox (addons.mozilla.org)
XPI = "firefox.xpi"  # the signed extension for Firefox, put in the package by release.yml
XPI_NAME = re.compile(r"^pdms-front-redirect-\d+(?:\.\d+)*\.xpi$")  # as the GitHub release has it
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
    Browser("firefox", "Firefox", "about:addons", ("firefox",), "firefox.exe", ("Mozilla Firefox",), "Firefox"),
)
BY_KEY = {browser.key: browser for browser in BROWSERS}


def extension_dir() -> Path:
    """Where the extension is written for the browser to load. Inside WSL the browsers are Windows': a folder of
    the Windows user (``AppData\\Local\\pdms``), which they can open."""
    if wsl.detected() and (local := wsl.windows_dir("LOCALAPPDATA")):
        return local / "pdms" / "browser-extension"
    return instances.state_dir() / "browser-extension"


def shown(path: Path) -> str:
    """A path as the user types it in their browser's dialogs (Windows' form inside WSL)."""
    return wsl.to_windows(path) if wsl.detected() else str(path)


def seen_path() -> Path:
    return instances.state_dir() / "browser-seen.json"


def version() -> str:
    return json.loads(SOURCE.joinpath("manifest.json").read_text(encoding="utf-8"))["version"]


def folder_version() -> str:
    """The version in :func:`extension_dir`, which Chromium browsers load: what the extension compares itself with
    to reload (the package's own while there is no folder)."""
    try:
        return str(json.loads((extension_dir() / "manifest.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return version()


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


def firefox_manifest() -> dict:
    """The manifest Firefox gets: its own id, an event page instead of a service worker, no Chrome key."""
    manifest = json.loads(SOURCE.joinpath("manifest.json").read_text(encoding="utf-8"))
    manifest.pop("key", None)
    manifest["background"] = {"scripts": ["texts.js", "background.js"]}
    manifest["browser_specific_settings"] = {"gecko": {
        "id": GECKO_ID, "strict_min_version": "140.0", "data_collection_permissions": {"required": ["none"]},
    }}
    return manifest


def write_firefox_source(target: Path) -> Path:
    """The extension as Firefox gets it, in ``target``: what release.yml signs (``python -m pdms_cli.browser``)."""
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if name != "manifest.json":
            (target / name).write_bytes(SOURCE.joinpath(name).read_bytes())
    (target / "manifest.json").write_text(json.dumps(firefox_manifest(), indent=2) + "\n", encoding="utf-8")
    return target


def download_xpi(timeout: float = 60) -> Path:
    """The signed extension of the latest release, downloaded to :func:`extension_dir`."""
    from .update import API_RELEASES

    request = urllib.request.Request(f"{API_RELEASES}/latest", headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        assets = json.load(response).get("assets") or []
    for asset in assets:
        name = str(asset.get("name", ""))
        if XPI_NAME.match(name):
            target = extension_dir() / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(str(asset["browser_download_url"]), timeout=timeout) as response:
                target.write_bytes(response.read())
            return target
    raise RuntimeError("the latest release has no signed extension for Firefox")


def firefox_xpi() -> Path:
    """The signed extension for Firefox, as a file Firefox can open: the one that came with pdms, else the latest
    release's. A snap's Firefox cannot read hidden folders, so it gets it in ~/Downloads."""
    packaged = SOURCE.joinpath(XPI)
    if packaged.is_file():
        xpi = extension_dir() / f"pdms-front-redirect-{version()}.xpi"
        xpi.parent.mkdir(parents=True, exist_ok=True)
        xpi.write_bytes(packaged.read_bytes())
    else:
        xpi = download_xpi()
    program = installed().get("firefox") or ""
    if "/snap/" in program:
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        xpi = Path(shutil.copy(xpi, downloads / xpi.name))
    return xpi


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


def _wsl_exe(browser: Browser) -> str | None:
    """Inside WSL: the Windows browser, run through WSL's interop."""
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = wsl.windows_dir(name)
        for folder in browser.folders if root else ():
            if (path := root / folder / browser.exe).is_file():
                return str(path)
    return None


def executable(browser: Browser) -> str | None:
    """Full path of the browser's program, or None when it is not installed."""
    if WINDOWS:
        return _windows_exe(browser)
    if wsl.detected():
        return _wsl_exe(browser)
    if MAC:
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            for name in (browser.app, browser.app.lower()):  # Firefox.app/Contents/MacOS/firefox
                if (path := apps / f"{browser.app}.app" / "Contents" / "MacOS" / name).is_file():
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


def open_page(browser: Browser, target: str = "") -> bool:
    """Open the browser's extensions page (a page cannot link to it), or ``target``; False when it could not."""
    program = installed().get(browser.key) or executable(browser)
    if not program:
        return False
    if target and wsl.detected():
        target = wsl.to_windows(Path(target))  # a Windows program, a Windows path
    try:
        subprocess.Popen([program, target or browser.page], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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
    """What the extension does now: redirect ``hosts`` to ``target`` while ``active``. Its icon and panel also show
    whether the redirect is on (``redirect``) when the frontend is not running, in pdms' language."""
    where = target()
    return {"pdms": "front-redirect", "active": cfg.defaults.front_redirect and bool(where), "target": where,
            "redirect": cfg.defaults.front_redirect, "hosts": list(cfg.defaults.front_redirect_hosts),
            "version": folder_version(), "language": i18n.configured(cfg.defaults.language)}


def status(cfg: Config) -> dict:
    """The redirect and the extension in each browser installed here (or where it was seen)."""
    found, last = installed(), seen()
    now = time.time()
    rows = []
    for key in BY_KEY:
        if key not in found and key not in last:
            continue
        at = float(last.get(key, {}).get("at") or 0)
        rows.append({
            "key": key, "name": BY_KEY[key].name, "found": key in found,
            # Firefox does not load newer files by itself: it needs the new signed file.
            "outdated": key == "firefox" and bool(at) and last[key].get("version") != version(), "seen": datetime.fromtimestamp(at).isoformat(timespec="seconds") if at else "",
            "connected": bool(at) and now - at < CONNECTED_FOR,
        })
    return {
        "redirect": cfg.defaults.front_redirect, "hosts": list(cfg.defaults.front_redirect_hosts),
        "folder": shown(extension_dir()), "browsers": rows,
    }


if __name__ == "__main__":
    # release.yml: python -m pdms_cli.browser firefox-source <folder> | version
    if sys.argv[1:2] == ["version"]:
        print(version())
    elif sys.argv[1:2] == ["firefox-source"] and len(sys.argv) == 3:
        write_firefox_source(Path(sys.argv[2]))
    else:
        sys.exit("usage: python -m pdms_cli.browser version | firefox-source <folder>")
