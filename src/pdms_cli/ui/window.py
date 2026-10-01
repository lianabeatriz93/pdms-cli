"""``pdms ui --window``: the same page in a window of its own (pywebview) instead of a browser tab.

pywebview comes with the optional ``desktop`` extra: on Windows it uses Edge WebView2 and on macOS WebKit, both part of
the system; on Linux it uses Qt (PyQt6 and its WebEngine, installed with the extra), since the GTK backend needs
system packages to build. On Windows and macOS pywebview is small, so ``pdms ui --window`` installs it by itself
when it is missing; on Linux the user decides (Qt is a large download). The UI server keeps running in a thread and
stops when the window closes.
"""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys

from .. import __version__, update

# The desktop extra (pyproject.toml) on Windows and macOS, installed into pdms's own environment.
DESKTOP_REQUIREMENTS = ["pywebview>=5"]
TITLE = "pdms"
SIZE = (1400, 900)
MIN_SIZE = (800, 560)


def available() -> bool:
    return update.has_desktop()


def install_command() -> str:
    """How to add the window to this install."""
    if update.install_kind() == "editable":
        return "uv tool install -e '.[desktop]' --force"
    return f"uv tool install --force 'pdms-cli[desktop] @ {update.wheel_url(__version__)}'"


def installs_itself() -> bool:
    """Whether pywebview is installed on demand here (Windows and macOS, where it needs nothing but the system)."""
    return sys.platform in ("win32", "darwin")


def self_install_command() -> list[str]:
    """Add pywebview to the environment pdms runs from: with uv, or with that environment's own pip."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *DESKTOP_REQUIREMENTS]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", *DESKTOP_REQUIREMENTS]
    raise RuntimeError("neither uv nor pip is available")


def install_desktop() -> None:
    """Install pywebview next to pdms (its output goes to the terminal); :class:`RuntimeError` when that fails.

    It does not reinstall pdms, so it also works on Windows, where the running pdms.exe cannot be replaced, and
    ``pdms self-update`` keeps it afterwards (it reinstalls with the desktop extra when pywebview is there)."""
    result = subprocess.run(self_install_command())
    if result.returncode:
        raise RuntimeError(f"exit code {result.returncode}")
    importlib.invalidate_caches()
    if not available():
        raise RuntimeError("pywebview is still not importable")


def open_window(url: str) -> None:
    """Show ``url`` in a native window until it is closed (blocks; pywebview needs the main thread)."""
    import webview

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Docs and /docs open in the browser, not here
    webview.settings["ALLOW_DOWNLOADS"] = True  # Settings → Export… downloads a file
    webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE, text_select=True)
    webview.start(gui="qt" if sys.platform.startswith("linux") else None)
