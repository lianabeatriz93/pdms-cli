"""One way to open a connection to a configured database, so every caller gets the same safeguards.

The remote databases are reached through an SSM tunnel over a slow link: a connection whose tunnel died would wait
for the operating system's TCP timeout (hours) on its next read. Keepalives notice a dead peer in about a minute.
"""

from __future__ import annotations

from typing import Any

from .config import Database

KEEPALIVES = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


def connect(db: Database, timeout: int, *, statement_timeout: int = 0, **options: Any):
    """A psycopg connection to ``db``; ``timeout`` seconds to connect, ``statement_timeout`` seconds per query (0: none)."""
    import psycopg

    if statement_timeout:
        options["options"] = f"-c statement_timeout={statement_timeout * 1000}"
    return psycopg.connect(host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password,
                           connect_timeout=timeout, **KEEPALIVES, **options)
