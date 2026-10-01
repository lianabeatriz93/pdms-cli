"""``pdms ui --window``: the same page in a window of its own (pywebview) instead of a browser tab.

pywebview comes with the optional ``desktop`` extra: on Windows it uses Edge WebView2 and on macOS WebKit, both part of
the system; on Linux it uses Qt (PyQt6 and its WebEngine, installed with the extra), since the GTK backend needs
system packages to build. The UI server keeps running in a thread and stops when the window closes.
"""

from __future__ import annotations

import sys

from .. import __version__, update

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


def open_window(url: str) -> None:
    """Show ``url`` in a native window until it is closed (blocks; pywebview needs the main thread)."""
    import webview

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Docs and /docs open in the browser, not here
    webview.settings["ALLOW_DOWNLOADS"] = True  # Settings → Export… downloads a file
    webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE, text_select=True)
    webview.start(gui="qt" if sys.platform.startswith("linux") else None)
