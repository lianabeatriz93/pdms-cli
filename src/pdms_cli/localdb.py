"""pdms's own Postgres: a ``pdms-postgres`` container (the image of the repo's docker-compose files) on port 5440, its
data in a named volume, so it survives the container being recreated and never touches the developer's own
docker-compose database.

It holds the test databases (``pdms_test_1`` … ``pdms_test_4``): empty, since the integration tests of pdms_v2 drop
and create every table themselves — which is why tests run only there and never on a database that keeps data.
Everything runs through ``docker exec`` (``psql`` is not needed on the machine).
"""

from __future__ import annotations

import re
import subprocess
import time

from . import actions, events, images
from .config import Database
from .i18n import _

CONTAINER = "pdms-postgres"
VOLUME = "pdms-postgres-data"
IMAGE = images.POSTGRES
PORT = 5440  # 5434 is docker-compose_dev's, 5439 docker-compose_tests'
USER = PASSWORD = "local"
MAIN_DB = "pdms"
TEST_PREFIX = "pdms_test_"
TEST_COUNT = 4
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def _docker(*args: str, timeout: float = 120) -> tuple[int, str]:
    return events._docker(*args, timeout=timeout)


def state() -> dict:
    """``{"exists", "running", "port"}`` of the container (port 0 when unknown)."""
    code, out = _docker("inspect", CONTAINER, "--format",
                        '{{.State.Running}}|{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                        timeout=15)
    if code != 0:
        return {"exists": False, "running": False, "port": 0}
    running, _sep, port = out.splitlines()[-1].partition("|")
    return {"exists": True, "running": running == "true", "port": int(port) if port.isdigit() else PORT}


def database(name: str, port: int = PORT) -> Database:
    return Database(host="localhost", port=port, database=name, user=USER, password=PASSWORD)


def _require_docker() -> None:
    ok, detail = events.docker_available()
    if not ok:
        raise actions.ActionError(_("Docker is not available: {detail}", detail=detail or _("docker not found")))


def up(port: int = PORT, wait: float = 60) -> dict:
    """Start the container (creating it, and its volume, the first time) and wait until Postgres answers."""
    _require_docker()
    now = state()
    if not now["exists"]:
        images.require([IMAGE])  # never downloaded without asking (images.Missing)
        code, out = _docker(
            "run", "-d", "--name", CONTAINER, "--label", "pdms=postgres", "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{port}:5432", "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-e", f"POSTGRES_DB={MAIN_DB}", "-v", f"{VOLUME}:/var/lib/postgresql", IMAGE,
            # jit off: it spent seconds compiling each wide query of the integration tests (PDMP-555)
            "-c", "jit=off", "-c", "max_connections=200",
            timeout=600,
        )
        if code != 0:
            raise actions.ActionError(_("Could not start {name}: {error}", name=CONTAINER, error=out.splitlines()[-1:]))
    elif not now["running"]:
        code, out = _docker("start", CONTAINER)
        if code != 0:
            raise actions.ActionError(_("Could not start {name}: {error}", name=CONTAINER, error=out.splitlines()[-1:]))
    deadline = time.monotonic() + wait
    while _docker("exec", CONTAINER, "pg_isready", "-U", USER, "-d", MAIN_DB, timeout=10)[0] != 0:
        if time.monotonic() > deadline:
            raise actions.ActionError(_("{name} does not answer yet; try again in a moment.", name=CONTAINER))
        time.sleep(1)
    return state()


def down() -> bool:
    """Stop the container (its data stays in the volume). Whether it was running."""
    if not state()["running"]:
        return False
    code, out = _docker("stop", CONTAINER)
    if code != 0:
        raise actions.ActionError(_("Could not stop {name}: {error}", name=CONTAINER, error=out))
    return True


def psql(sql: str, db: str = "postgres", timeout: float = 120) -> str:
    """Run ``sql`` in the container (unaligned, tuples only) and return what it printed."""
    code, out = _docker("exec", CONTAINER, "psql", "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", db, "-Atc", sql,
                        timeout=timeout)
    if code != 0:
        raise actions.ActionError(out.strip() or _("psql failed in {name}.", name=CONTAINER))
    return out


def databases() -> dict[str, int]:
    """Every database of the container with its size in bytes (templates left out)."""
    out = psql("select datname, pg_database_size(datname) from pg_database where not datistemplate order by 1")
    found = {}
    for line in out.splitlines():
        name, _sep, size = line.partition("|")
        if size.isdigit():
            found[name] = int(size)
    return found


def test_databases(found: dict[str, int] | None = None) -> list[str]:
    """The test databases there are, in order (pdms_test_1, pdms_test_2, …)."""
    names = [n for n in (found if found is not None else databases()) if n.startswith(TEST_PREFIX)]
    return sorted(names, key=lambda n: int(n[len(TEST_PREFIX):]) if n[len(TEST_PREFIX):].isdigit() else 10**6)


def is_test_database(name: str) -> bool:
    return name.startswith(TEST_PREFIX) and name[len(TEST_PREFIX):].isdigit()


def ensure_test_databases(count: int = TEST_COUNT) -> list[str]:
    """Create the test databases up to ``count`` (empty: the tests create their tables). The ones it created."""
    have = set(test_databases())
    created = []
    for n in range(1, count + 1):
        name = f"{TEST_PREFIX}{n}"
        if name not in have:
            psql(f'create database "{name}"')
            created.append(name)
    return created


def remove_test_database(name: str) -> None:
    if not is_test_database(name):
        raise actions.ActionError(_("'{name}' is not a test database.", name=name))
    psql(f'drop database if exists "{name}" with (force)')


def recreate_test_database(name: str) -> None:
    """Drop and create a test database again, empty (what a broken test run may need)."""
    remove_test_database(name)
    psql(f'create database "{name}"')


def available(timeout: float = 15) -> bool:
    """Whether the container runs and answers (without starting it)."""
    try:
        return state()["running"] and _docker("exec", CONTAINER, "pg_isready", "-U", USER, "-d", MAIN_DB,
                                              timeout=timeout)[0] == 0
    except (OSError, subprocess.SubprocessError):
        return False
