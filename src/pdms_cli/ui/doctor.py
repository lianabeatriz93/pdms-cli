"""``pdms doctor`` in pdms ui: the checks run in a thread and the last result stays for the page and Home.

They run once when pdms ui starts and every :data:`EVERY` seconds after that, without testing the databases (that
takes seconds and may need the VPN); the Doctor screen runs them again on demand, with the databases if asked.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime

from .. import doctor as diagnostics
from ..config import Config

EVERY = 15 * 60


class Doctor:
    def __init__(self, on_change: Callable[[], None] = lambda: None) -> None:
        self.on_change = on_change
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def run(self, databases: bool = False, timeout: int = 5) -> bool:
        """Start a run in the background; False when one is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        self.on_change()
        threading.Thread(target=self._work, args=(databases, timeout), name="pdms-ui-doctor", daemon=True).start()
        return True

    def _work(self, databases: bool, timeout: int) -> None:
        started = time.monotonic()
        try:
            checks = [asdict(check) for check in diagnostics.run_all(Config.load(), databases=databases, timeout=timeout)]
            error = ""
        except Exception as exc:  # noqa: BLE001 - shown on the page; the checks themselves never raise
            checks, error = [], f"{type(exc).__name__}: {exc}"
        result = {
            "checks": checks, "error": error, "databases": databases,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "took": round(time.monotonic() - started, 1),
        }
        with self._lock:
            self._latest, self._running = result, False
        self.on_change()

    def summary(self) -> dict:
        """For the state: counts, the problems (for Home) and whether it runs now."""
        latest = self.latest()
        if latest is None:
            return {"running": self._running, "at": "", "counts": {}, "problems": []}
        counts = {status: sum(c["status"] == status for c in latest["checks"])
                  for status in (diagnostics.OK, diagnostics.WARN, diagnostics.FAIL)}
        problems = [c for c in latest["checks"] if c["status"] != diagnostics.OK]
        return {"running": self._running, "at": latest["at"], "counts": counts, "problems": problems,
                "error": latest["error"]}

    def watch(self, stopped: threading.Event) -> None:
        """Run now and every :data:`EVERY` seconds while pdms ui runs (never while a run is going)."""
        while not stopped.is_set():
            self.run()
            stopped.wait(EVERY)
