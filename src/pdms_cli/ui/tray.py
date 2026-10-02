"""The tray icon of ``pdms ui --window``: open the window, open it in the browser, quit.

With a tray icon, closing the window only hides it and pdms ui keeps running (``--hidden`` starts it that way, to
open at login); without one, closing the window stops pdms ui as before. Linux uses Qt's own tray icon, since the
window already runs on Qt; Windows and macOS use pystray (in the desktop extra). Anything missing means no tray.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from importlib import resources

from ..i18n import _

STATIC = resources.files("pdms_cli.ui") / "static"


class Tray:
    """What the menu does; ``stop`` removes the icon (set by the toolkit that shows it)."""

    def __init__(self, on_open: Callable[[], None], on_browser: Callable[[], None], on_quit: Callable[[], None]):
        self.on_open, self.on_browser, self.on_quit = on_open, on_browser, on_quit
        self.stop: Callable[[], None] = lambda: None


def available() -> bool:
    if sys.platform.startswith("linux"):
        return importlib.util.find_spec("PyQt6") is not None
    return importlib.util.find_spec("pystray") is not None and importlib.util.find_spec("PIL") is not None


def start(tray: Tray) -> bool:
    """Show the icon (before ``webview.start``, in the main thread); False when this system cannot."""
    if not available():
        return False
    try:
        return _start_qt(tray) if sys.platform.startswith("linux") else _start_pystray(tray)
    except Exception as exc:  # noqa: BLE001 - no tray on this desktop: the window works as without one
        print(f"pdms ui: no tray icon ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return False


def _start_qt(tray: Tray) -> bool:
    import PyQt6.QtWebEngineWidgets  # noqa: F401 - Qt wants WebEngine loaded before the application exists
    from PyQt6.QtCore import QMetaObject, Qt
    from PyQt6.QtGui import QAction, QGuiApplication, QIcon
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    # pywebview reuses this application. Its name and desktop file name tie the window to pdms.desktop (the icon
    # in the dock and the task switcher).
    app = QApplication.instance() or QApplication(sys.argv)
    QGuiApplication.setApplicationName("pdms")
    QGuiApplication.setDesktopFileName("pdms")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return False
    with resources.as_file(STATIC / "icon.png") as path:
        icon = QIcon(str(path))
    menu = QMenu()
    for label, action in ((_("Open pdms"), tray.on_open), (_("Open in the browser"), tray.on_browser)):
        item = QAction(label, menu)
        item.triggered.connect(lambda _checked=False, action=action: action())
        menu.addAction(item)
    menu.addSeparator()
    quit_item = QAction(_("Quit pdms ui"), menu)
    quit_item.triggered.connect(lambda _checked=False: tray.on_quit())
    menu.addAction(quit_item)
    icon_item = QSystemTrayIcon(icon, app)
    icon_item.setToolTip("pdms")
    icon_item.setContextMenu(menu)
    icon_item.activated.connect(
        lambda reason: tray.on_open() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    icon_item.show()
    tray._keep = (icon_item, menu)  # type: ignore[attr-defined]  # Qt objects die with their last reference

    def stop() -> None:
        # Called from any thread: hide the icon and end the Qt loop in Qt's own thread.
        QMetaObject.invokeMethod(icon_item, "hide", Qt.ConnectionType.QueuedConnection)
        QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)

    tray.stop = stop
    return True


def _start_pystray(tray: Tray) -> bool:
    import threading

    import pystray
    from PIL import Image

    with resources.as_file(STATIC / "icon.png") as path:
        image = Image.open(path)
        image.load()
    icon = pystray.Icon("pdms", image, "pdms", menu=pystray.Menu(
        pystray.MenuItem(_("Open pdms"), lambda: tray.on_open(), default=True),
        pystray.MenuItem(_("Open in the browser"), lambda: tray.on_browser()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_("Quit pdms ui"), lambda: tray.on_quit()),
    ))
    if sys.platform == "darwin":
        icon.run_detached()  # AppKit: it joins the main loop that pywebview runs
    else:
        threading.Thread(target=icon.run, name="pdms-tray", daemon=True).start()
    tray.stop = icon.stop
    return True
