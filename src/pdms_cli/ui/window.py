"""``pdms ui --window``: the same page in a window of its own (pywebview) instead of a browser tab.

pywebview comes with the optional ``desktop`` extra: on Windows it uses Edge WebView2 and on macOS WebKit, both part of
the system; on Linux it uses Qt (PyQt6 and its WebEngine, installed with the extra), since the GTK backend needs
system packages to build. On Windows and macOS pywebview is small, so ``pdms ui --window`` installs it by itself
when it is missing; on Linux the user decides (Qt is a large download). The UI server keeps running in a thread.

With a tray icon (:mod:`.tray`) closing the window hides it and pdms ui stops from the tray's Quit; without one,
closing the window stops pdms ui.
"""

from __future__ import annotations

import ctypes.util
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from importlib import resources

from .. import __version__, update
from ..i18n import _
from . import tray as ui_tray
from .control import Control

# The desktop extra (pyproject.toml) on Windows and macOS, installed into pdms's own environment.
DESKTOP_REQUIREMENTS = ["pywebview>=5", "pystray>=0.19"]
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


def icon_name() -> str:
    """The window's icon: Windows takes an .ico, Qt (Linux) and macOS's Dock a PNG; both are drawn from icon.svg."""
    return "icon.ico" if sys.platform == "win32" else "icon.png"


def missing_system_library() -> str | None:
    """What Qt still needs from the system to open a window here, or None.

    Since Qt 6.5 its X11 (xcb) plugin needs libxcb-cursor, which is not part of the wheels; without it Qt aborts the
    whole process, so this has to be checked before."""
    if not sys.platform.startswith("linux"):
        return None
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    on_x11 = platform.startswith("xcb") or (not platform and not os.environ.get("WAYLAND_DISPLAY"))
    if on_x11 and ctypes.util.find_library("xcb-cursor") is None:
        return _("Qt needs the system library libxcb-cursor to open windows on X11; install it with "
                 "sudo apt install libxcb-cursor0 (Fedora, Arch: xcb-util-cursor)")
    return None


def open_window(url: str, control: Control, hidden: bool = False) -> None:
    """Show ``url`` in a native window until pdms ui quits (blocks; pywebview needs the main thread).

    ``hidden`` starts with only the tray icon (when there is one)."""
    if missing := missing_system_library():
        raise RuntimeError(missing)
    tray = ui_tray.Tray(on_open=control.show, on_browser=control.open_browser, on_quit=control.quit)
    has_tray = ui_tray.start(tray)  # before pywebview: on Linux it creates the Qt application pywebview reuses
    import webview

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Docs and /docs open in the browser, not here
    webview.settings["ALLOW_DOWNLOADS"] = True  # Settings → Export… downloads a file
    window = webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE, text_select=True,
                                   hidden=hidden and has_tray)
    minimized = []

    def closing() -> bool | None:
        if has_tray and not control.quitting.is_set():
            window.hide()  # pdms ui stays in the tray
            return False
        return None

    def show() -> None:
        if minimized:
            window.restore()
        window.show()
        window.on_top = True  # brings it in front of the other windows...
        window.on_top = False  # ...without keeping it there

    def close() -> None:
        tray.stop()
        window.destroy()

    def pick_folder(start: str = "") -> str:
        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        chosen = window.create_file_dialog(kind, directory=start or "")
        return str(chosen[0]) if chosen else ""

    control.pick_folder = pick_folder
    window.events.closing += closing
    window.events.minimized += lambda: minimized.append(True)
    window.events.restored += lambda: minimized.clear()
    control.attach(show, close)
    try:
        with resources.as_file(resources.files("pdms_cli.ui") / "static" / icon_name()) as icon:
            webview.start(gui="qt" if sys.platform.startswith("linux") else None, icon=str(icon))
    finally:
        tray.stop()
