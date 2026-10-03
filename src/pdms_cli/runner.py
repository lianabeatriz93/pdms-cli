"""Locating services, installing dependencies and launching uvicorn."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import IO

from . import installer
from .config import Database, Defaults, DevUser
from .i18n import _

SKIP_DIRS = {"node_modules", "__pycache__", "tests", "frontend", "infra", "templates"}
# An active virtualenv (pdms under ``uv run`` or run from an activated venv) makes poetry use it instead of the
# service's own: poetry install would fill pdms's environment and poetry run would start the service from it.
ACTIVE_ENV = ("VIRTUAL_ENV", "CONDA_PREFIX")


def is_service(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "main.py").is_file()


def find_service_upwards(start: Path) -> Path | None:
    for path in (start, *start.parents):
        if is_service(path):
            return path
    return None


def find_services_below(root: Path, max_depth: int = 4) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root):
        path = Path(dirpath)
        depth = len(path.relative_to(root).parts)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        if depth >= max_depth:
            dirnames.clear()
        if is_service(path):
            found.append(path)
            dirnames.clear()
    return sorted(found)


WINDOWS = sys.platform == "win32"


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if WINDOWS:
            # On Windows SO_REUSEADDR lets a second socket bind a port in use; ask for exclusive use instead.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def next_free_port(host: str, port: int, exclude: set[int] = frozenset()) -> int:
    while port in exclude or not port_is_free(host, port):
        port += 1
    return port


def service_env(
    defaults: Defaults, user: DevUser, db: Database, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Only the variables pdms sets for a service (no inherited environment)."""
    return {
        "DEVELOPMENT_MODE": "True",
        "LOGGING_LEVEL": defaults.logging_level,
        **user.env(),
        "DB_PG_CONNECTION_STR": db.url(),
        **defaults.env,
        **(extra or {}),
    }


def build_env(
    defaults: Defaults, user: DevUser, db: Database, extra: dict[str, str] | None = None
) -> dict[str, str]:
    env = {**poetry_environ(), **service_env(defaults, user, db, extra)}
    if extra and "PYTHONPATH" in extra and os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = extra["PYTHONPATH"] + os.pathsep + os.environ["PYTHONPATH"]  # keep the user's entries
    return env


def poetry_environ() -> dict[str, str]:
    """This process's environment for poetry, without the virtualenv pdms itself may be running in."""
    return {key: value for key, value in os.environ.items() if key not in ACTIVE_ENV}


def poetry_python(service: Path) -> Path | None:
    """Interpreter of the service's poetry virtualenv, or None if it has not been created yet."""
    result = subprocess.run([poetry(), "env", "info", "-e"], cwd=service, capture_output=True, text=True,
                            env=poetry_environ())
    python = Path(result.stdout.strip()) if result.returncode == 0 else None
    return python if python and python.exists() else None


def poetry() -> str:
    """Full path of the poetry executable (on Windows it may be poetry.exe or poetry.cmd)."""
    return shutil.which("poetry") or "poetry"


def ensure_poetry() -> None:
    if not shutil.which("poetry"):
        raise RuntimeError(_("'poetry' was not found in PATH."))


def install(service: Path, output: IO[str] | None = None) -> None:
    """``poetry lock && poetry install``, printing to the terminal or, with ``output``, writing everything there."""
    redirect = {"stdout": output, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL} if output else {}
    env = poetry_environ()
    for cmd in ([poetry(), "lock"], [poetry(), "install"]):
        if output:
            output.write(f"$ {' '.join(cmd)}\n")
            output.flush()
        subprocess.run(cmd, cwd=service, env=env, check=True, **redirect)
    copied = installer.copied_dependencies(service)
    if copied:
        # poetry keeps a copied path dependency whose version did not change, even if its code did
        subprocess.run([poetry(), "run", "python", "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall",
                        *map(str, copied)], cwd=service, env=env, check=True, **redirect)


def uvicorn_command(host: str, port: int, reload: bool) -> list[str]:
    cmd = [poetry(), "run", "uvicorn", "main:app"]
    if reload:
        cmd.append("--reload")
    return [*cmd, "--host", host, "--port", str(port)]


def exec_server(service: Path, cmd: list[str], env: dict[str, str]) -> None:
    """Run the server in the foreground as if uvicorn had been started by hand.

    On macOS/Linux the current process is replaced (Ctrl+C and --reload behave exactly the same). Windows has no
    real exec, so the server runs as a child that shares the console (it receives Ctrl+C itself) and pdms exits
    with its exit code.
    """
    if WINDOWS:
        proc = subprocess.Popen(cmd, cwd=service, env=env)
        while True:
            try:
                sys.exit(proc.wait())
            except KeyboardInterrupt:
                continue  # the server got the same Ctrl+C and is shutting down; wait for it
    os.chdir(service)
    os.execvpe(cmd[0], cmd, env)


def test_connection(db: Database, timeout: int) -> str:
    import psycopg

    with psycopg.connect(
        host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password, connect_timeout=timeout
    ) as conn:
        return conn.execute("select version()").fetchone()[0]
