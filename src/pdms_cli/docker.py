"""Docker as pdms uses it: running its command line, whether the daemon answers, and reaching a database from a
container (``pdms migrate``'s Flyway and the local copy's pg_dump)."""

from __future__ import annotations

import socket
import subprocess
import sys

# Names that mean "this computer" (a container needs the host's network, or Docker Desktop's name for the host).
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def run(*args: str, timeout: float = 60) -> tuple[int, str]:
    """``docker <args>``: its exit code and output (stdout then stderr); code 1 and the error when it cannot run."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def available() -> tuple[bool, str]:
    """Whether the Docker daemon answers, and its version (or what went wrong)."""
    code, out = run("info", "--format", "{{.ServerVersion}}", timeout=15)
    return code == 0, out.splitlines()[-1] if out else ""


def reach(host: str) -> tuple[str, list[str]]:
    """The host a container connects to for ``host`` on this computer, and the ``docker run`` options it needs.

    On Linux the container shares the host's network, and a name of the host's /etc/hosts (devo-cli's tunnel: the
    RDS name → 127.0.0.6) is given to the container too, keeping the name (TLS checks it). Docker Desktop (macOS,
    Windows) cannot share the host's network: this computer is ``host.docker.internal`` there.
    """
    host = host.strip()
    if sys.platform.startswith("linux"):
        options = ["--network", "host"]
        try:
            address = socket.gethostbyname(host)
        except OSError:
            address = ""
        if address and address != host and host.lower() not in LOCAL_HOSTS:
            options += ["--add-host", f"{host}:{address}"]
        return host, options
    if host.lower() in LOCAL_HOSTS:
        return "host.docker.internal", []
    return host, []
