"""What the page, the tray icon and a second ``pdms ui`` can ask of the running pdms ui: show it, quit, restart."""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable


class Control:
    def __init__(self, url: str, token: str = "", relaunch: list[str] | None = None) -> None:
        self.url = url
        self.token = token
        self.relaunch = relaunch or []  # the command that opens this pdms ui again (after an update)
        self.restart = False  # quit to start again (after an update): pdms ui's command does it
        self._show: Callable[[], None] | None = None
        self._close: Callable[[], None] = lambda: None
        self.quitting = threading.Event()

    def attach(self, show: Callable[[], None] | None, close: Callable[[], None]) -> None:
        """``show`` brings the window to the front (None: there is no window, the browser opens instead);
        ``close`` ends pdms ui's loop, from any thread."""
        self._show, self._close = show, close

    @property
    def has_window(self) -> bool:
        return self._show is not None

    def show(self) -> None:
        if self._show:
            self._show()
        else:
            self.open_browser()

    def open_browser(self) -> None:
        webbrowser.open(self.url)

    def quit(self, restart: bool = False) -> None:
        self.restart = restart
        self.quitting.set()
        self._close()
