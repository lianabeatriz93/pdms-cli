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
# Folders --reload never watches: their changes do not need a restart (and "tests" changes while tests run).
RELOAD_SKIP = {"tests", "__pycache__", "node_modules"}


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


def _in_project_python(service: Path) -> Path | None:
    """The interpreter of ``<service>/.venv``: poetry always uses that virtualenv when it exists."""
    venv = service / ".venv"
    python = venv / "Scripts" / "python.exe" if WINDOWS else venv / "bin" / "python"
    return python if python.exists() else None


def poetry_python(service: Path) -> Path | None:
    """Interpreter of the service's poetry virtualenv, or None if it has not been created yet."""
    if python := _in_project_python(service):
        return python  # without asking poetry, which takes half a second
    try:
        result = subprocess.run([poetry(), "env", "info", "-e"], cwd=service, capture_output=True, text=True,
                                env=poetry_environ())
    except OSError:  # no poetry, or no such folder
        return None
    python = Path(result.stdout.strip()) if result.returncode == 0 else None
    return python if python and python.exists() else None


def direct(service: Path, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """``poetry run <python|module> ...`` as a plain call to the service's interpreter, with the environment
    ``poetry run`` would give it (the virtualenv first on the PATH).

    Starting poetry costs every service half a second of CPU and ~60 MB, and on Windows, where it cannot exec,
    ``poetry run`` stays alive as the parent of the service. Anything else, or a service without a virtualenv yet,
    is returned as it was.
    """
    if cmd[:2] != [poetry(), "run"] or len(cmd) < 3 or not (python := poetry_python(service)):
        return cmd, env
    program, args = cmd[2], cmd[3:]
    call = [str(python), *args] if program == "python" else [str(python), "-m", program, *args]
    bin_dir = python.parent
    path = env.get("PATH", os.environ.get("PATH", ""))
    return call, {**env, "VIRTUAL_ENV": str(bin_dir.parent), "PATH": str(bin_dir) + os.pathsep + path}


def poetry() -> str:
    """Full path of the poetry executable (on Windows it may be poetry.exe or poetry.cmd)."""
    return shutil.which("poetry") or "poetry"


def ensure_poetry() -> None:
    if not shutil.which("poetry"):
        raise RuntimeError(_("'poetry' was not found in PATH."))


def install(service: Path, output: IO[str] | None = None, only: list[Path] | None = None,
            python: str | None = None) -> None:
    """``poetry lock && poetry install``, printing to the terminal or, with ``output``, writing everything there.

    With ``only`` (see :func:`installer.code_only_changes`) just those copied packages are reinstalled, without poetry.
    With ``python`` the virtualenv is first made again on that interpreter (see :mod:`pythons`).
    """
    redirect = {"stdout": output, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL} if output else {}
    env = poetry_environ()

    def run(cmd: list[str]) -> None:
        if output:
            output.write(f"$ {' '.join(cmd)}\n")
            output.flush()
        call, call_env = direct(service, cmd, env)
        subprocess.run(call, cwd=service, env=call_env, check=True, **redirect)

    if python:
        if output:
            output.write(f"$ rm -rf {service / '.venv'}\n")
            output.flush()
        shutil.rmtree(service / ".venv", ignore_errors=True)
        run([poetry(), "env", "use", python])
        only = None
    if only is None:
        run([poetry(), "lock"])
        run([poetry(), "install"])
    # poetry keeps a copied path dependency whose version did not change, even if its code did
    copied = installer.copied_dependencies(service) if only is None else only
    if copied:
        run([poetry(), "run", "python", "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall",
             *map(str, copied)])


def reload_dirs(service: Path) -> list[str]:
    """The service's folders with Python code, for ``--reload-dir``.

    Without watchfiles (PDMS services do not have it) uvicorn checks every ``*.py`` under the folders it watches
    four times a second. Watching the whole service folder includes its ``.venv``: ~5,000 files and about two
    thirds of a CPU core per service, all the time. Its own code is a few dozen files.
    """
    try:
        return sorted(
            path.name for path in service.iterdir()
            if path.is_dir() and not path.name.startswith(".") and path.name not in RELOAD_SKIP
            and next(path.rglob("*.py"), None) is not None
        )
    except OSError:
        return []


def uvicorn_command(host: str, port: int, reload: bool, watch: list[str] | None = None) -> list[str]:
    """``uvicorn main:app``; with ``reload``, watching only the folders in ``watch`` (all of the service when empty)."""
    cmd = [poetry(), "run", "uvicorn", "main:app"]
    if reload:
        cmd.append("--reload")
        for folder in watch or []:
            cmd += ["--reload-dir", folder]
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
