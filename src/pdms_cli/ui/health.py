"""The databases in use, measured while pdms ui runs: one round trip each minute, an hour of history for the page.

In use means the current database (the last one used) and those of the services running now. Each one keeps an
open connection (see :class:`pdms_cli.health.Meter`), closed when the database is no longer in use. While the Data
screen is open (it asks again each time, and with Test now) every database is measured, for :data:`ALL_FOR` seconds.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from .. import health, instances
from ..config import Config, Database

EVERY = 60
HISTORY = 60  # an hour of samples
ALL_FOR = 600  # seconds every database is measured after the Data screen asked


def in_use(cfg: Config) -> dict[str, Database]:
    names = [cfg.last_db] if cfg.last_db else []
    for inst in instances.load().values():
        if inst.db and inst.alive():
            names.append(inst.db)
    return {name: cfg.dbs[name] for name in dict.fromkeys(names) if name in cfg.dbs}


class Health:
    def __init__(self, on_change: Callable[[], None] = lambda: None, meter: health.Meter | None = None) -> None:
        self.on_change = on_change
        self.meter = meter or health.Meter()
        self._lock = threading.Lock()
        self._running = False
        self._history: dict[str, deque] = {}
        self._latest: dict[str, dict] = {}
        self._all_until = 0.0

    def run(self, every_database: bool = False) -> bool:
        """Measure now, in the background; False when a measure is already going. ``every_database``: not only the
        ones in use, for the next :data:`ALL_FOR` seconds (the Data screen)."""
        with self._lock:
            if every_database:
                self._all_until = time.monotonic() + ALL_FOR
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._work, name="pdms-ui-health", daemon=True).start()
        return True

    def measure(self) -> None:
        """Measure every database in use (here, in this thread)."""
        cfg = Config.load()
        with self._lock:
            every = time.monotonic() < self._all_until
        targets = dict(cfg.dbs) if every else in_use(cfg)
        self.meter.forget(set(targets))
        for name, db in targets.items():
            ms, error = self.meter.measure(name, db, cfg.defaults.db_timeout)
            way = health.route(db)
            with self._lock:
                history = self._history.setdefault(name, deque(maxlen=HISTORY))
                history.append(None if ms is None else round(ms))
                self._latest[name] = {
                    "name": name, "ms": None if ms is None else round(ms), "error": error,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "route": {"kind": way.kind, "address": way.address, "up": way.up, "process": way.process},
                }
        with self._lock:
            for gone in [name for name in self._latest if name not in targets]:
                self._latest.pop(gone)
                self._history.pop(gone, None)
        self.on_change()

    def _work(self) -> None:
        try:
            self.measure()
        finally:
            with self._lock:
                self._running = False

    def summary(self) -> dict:
        with self._lock:
            return {
                "dbs": [{**item, "history": list(self._history.get(name, ()))} for name, item in self._latest.items()],
                "running": self._running, "slow_ms": health.VERY_SLOW_MS,
            }

    def watch(self, stopped: threading.Event) -> None:
        while not stopped.is_set():
            self.run()
            stopped.wait(EVERY)
