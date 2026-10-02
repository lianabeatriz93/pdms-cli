"""What changed while pdms ui runs, for Home's Recent panel, and the desktop notifications about failures.

Each new state is compared with the previous one: a service that failed to load or exited by itself, one that
loads again after an error, and each job that ended (done or failed). Entries carry what happened, not a text,
so the page says it in its own language. A service failing or exiting also goes to the desktop when
``defaults.notify`` is on: one notification per change of state, however many services it touched.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from datetime import datetime

from .. import desktop
from ..i18n import _

LIMIT = 30
ALIVE = ("ok", "busy", "starting")


class Recent:
    def __init__(self, notify: Callable[[str, str], None] | None = None, limit: int = LIMIT) -> None:
        self.notify = notify or show_notification
        self.limit = limit
        self._items: list[dict] = []
        self._previous: dict | None = None
        self._lock = threading.Lock()

    def items(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def observe(self, state: dict, notify: bool = True) -> None:
        """Record what changed since the previous state (the first one is only remembered)."""
        with self._lock:
            previous, self._previous = self._previous, state
            if previous is None:
                return
            fresh = changes(previous, state)
            self._items = (fresh[::-1] + self._items)[: self.limit]
        # Only what happened by itself: a job that failed was asked for on the page, which shows it.
        failures = [entry for entry in fresh if entry["event"] in ("failed", "exited")]
        if notify and failures:
            self.notify("pdms", notification_text(failures))


def changes(before: dict, after: dict) -> list[dict]:
    at = datetime.now().isoformat(timespec="seconds")
    found = []
    old = {inst["key"]: inst for inst in before.get("instances", [])}
    old_jobs, jobs = before.get("jobs", {}), after.get("jobs", {})
    for inst in after.get("instances", []):
        key, status = inst["key"], inst["status"]
        was = old.get(key, {}).get("status")
        if was is None or was == status or key in jobs or key in old_jobs:  # a job says it itself
            continue
        if status == "error":
            found.append({"at": at, "kind": "bad", "event": "failed", "key": key, "detail": inst.get("detail", "")})
        elif status == "stopped" and was in ALIVE:
            found.append({"at": at, "kind": "bad", "event": "exited", "key": key, "detail": ""})
        elif status == "ok" and was == "error":
            found.append({"at": at, "kind": "ok", "event": "recovered", "key": key, "detail": ""})
    for key, job in jobs.items():
        if job.get("error") and not old_jobs.get(key, {}).get("error"):
            found.append({"at": at, "kind": "bad", "event": "job-failed", "key": key, "action": job["action"],
                          "detail": job["error"]})
    for key, job in old_jobs.items():
        if not job.get("error") and key not in jobs:
            found.append({"at": at, "kind": "ok", "event": "done", "key": key, "action": job["action"], "detail": ""})
    return found


def notification_text(failures: list[dict]) -> str:
    if len(failures) > 1:
        return _("{n} problems: {keys}", n=len(failures), keys=", ".join(entry["key"] for entry in failures))
    entry = failures[0]
    if entry["event"] == "failed":
        return _("{key} failed to load: {detail}", key=entry["key"], detail=entry["detail"] or "?")
    return _("{key} stopped by itself.", key=entry["key"])


# Set by the tray on Windows (pystray), where it is the way to show a notification without a dialog.
tray_notify: Callable[[str, str], None] | None = None


def show_notification(title: str, message: str) -> None:
    """A desktop notification, in a thread: it must never hold up the state."""
    def show() -> None:
        if sys.platform == "win32":
            if tray_notify:
                try:
                    tray_notify(title, message)
                except Exception:  # noqa: BLE001, S110 - best effort, like desktop.notify
                    pass
            return  # desktop.notify would open a dialog there
        desktop.notify(title, message)

    threading.Thread(target=show, name="pdms-ui-notify", daemon=True).start()
