"""Environment checks for ``pdms doctor``.

Each check returns :class:`Check` items (ok / warn / fail) with a hint on how to fix them. They never raise: a
broken piece of the environment is exactly what doctor has to report.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import __version__, events, instances, installer, proxy, repos, routes, runner, update
from .config import Config, config_path
from .i18n import _

OK, WARN, FAIL = "ok", "warn", "fail"
SERVICE_PYTHONS = ("3.10", "3.11")  # PDMS services declare python = ">=3.10,<3.12"


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


# ---------------------------------------------------------------------- pdms itself


def check_pdms() -> list[Check]:
    section = "pdms"
    kind = update.install_kind()
    checks = [Check(section, _("Version"), OK, f"{__version__} ({kind})")]
    python = ".".join(map(str, sys.version_info[:3]))
    checks.append(Check(section, "Python", OK if sys.version_info >= (3, 10) else FAIL, python,
                        "" if sys.version_info >= (3, 10) else _("pdms needs Python 3.10 or newer.")))
    if kind != "editable":
        cache = update.load_cache()
        latest = cache.get("latest")
        if latest and update.is_newer(latest):
            checks.append(Check(section, _("Updates"), WARN, _("{latest} is available", latest=latest),
                                "pdms self-update"))
        elif latest:
            checks.append(Check(section, _("Updates"), OK, _("up to date")))
    return checks


def completion_installed(home: Path | None = None) -> str | None:
    """Where the shell completion was installed by ``pdms --install-completion``, if anywhere."""
    home = home or Path.home()
    candidates = [
        home / ".bash_completions" / "pdms.sh", home / ".zfunc" / "_pdms",
        home / ".config" / "fish" / "completions" / "pdms.fish",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    for profile in (home / "Documents" / "PowerShell", home / "Documents" / "WindowsPowerShell"):
        for script in profile.glob("*profile*.ps1") if profile.is_dir() else []:
            if "pdms" in script.read_text(encoding="utf-8", errors="replace").lower():
                return str(script)
    return None


def check_shell() -> list[Check]:
    section = _("Shell")
    location = completion_installed()
    return [Check(section, _("Tab completion"), OK if location else WARN,
                  location or _("not installed"), "" if location else "pdms --install-completion")]


# ---------------------------------------------------------------------- tools for the services


def python_versions_available() -> list[str]:
    """Python versions (major.minor) that Poetry could use for the services."""
    found = set()
    for version in SERVICE_PYTHONS:
        if shutil.which(f"python{version}"):
            found.add(version)
    if shutil.which("pyenv"):
        code, out = _run(["pyenv", "versions", "--bare"])
        if code == 0:
            found |= {v for v in SERVICE_PYTHONS for line in out.splitlines() if line.strip().startswith(v + ".")}
    if shutil.which("uv"):
        code, out = _run(["uv", "python", "list", "--only-installed"])
        if code == 0:
            found |= {v for v in SERVICE_PYTHONS if re.search(rf"cpython-{re.escape(v)}\.\d+", out)}
    if sys.platform == "win32" and shutil.which("py"):
        code, out = _run(["py", "--list"])
        if code == 0:
            found |= {v for v in SERVICE_PYTHONS if f"-{v}" in out or f":{v}" in out}
    return sorted(found)


def check_tools() -> list[Check]:
    section = _("Tools")
    checks = []
    poetry = shutil.which("poetry")
    if not poetry:
        checks.append(Check(section, "Poetry", FAIL, _("not found in PATH"),
                            _("Install it: https://python-poetry.org/docs/#installation")))
    else:
        code, out = _run([runner.poetry(), "--version"])
        match = re.search(r"(\d+)\.(\d+)", out)
        version = tuple(int(x) for x in match.groups()) if match else (0, 0)
        ok = code == 0 and version >= (1, 2)
        checks.append(Check(section, "Poetry", OK if ok else FAIL, out.splitlines()[-1] if out else poetry,
                            "" if ok else _("PDMS services need Poetry 1.2 or newer (dependency groups).")))
    docker_ok, docker_detail = events.docker_available()
    checks.append(Check(section, "Docker", OK if docker_ok else FAIL, docker_detail or _("not available"),
                        "" if docker_ok else _("Install Docker and start it: https://docs.docker.com/get-docker/")))
    pythons = python_versions_available()
    checks.append(Check(
        section, _("Python for the services"), OK if pythons else WARN,
        ", ".join(pythons) if pythons else _("no Python 3.10 / 3.11 found"),
        "" if pythons else _("Services need Python 3.10 or 3.11, e.g.: uv python install 3.11"),
    ))
    return checks


# ---------------------------------------------------------------------- configuration


def check_config(cfg: Config) -> list[Check]:
    section = _("Configuration")
    path = config_path()
    if not path.exists():
        return [Check(section, _("File"), WARN, _("{path} does not exist yet", path=path),
                      _("It is created the first time you save something (e.g. pdms user add)."))]
    checks = [Check(section, _("File"), OK, str(path))]
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        private = not mode & (stat.S_IRWXG | stat.S_IRWXO)
        checks.append(Check(section, _("Permissions"), OK if private else WARN, oct(mode),
                            "" if private else f"chmod 600 {path}"))
    checks.append(Check(section, _("Users"), OK if cfg.users else WARN, str(len(cfg.users)),
                        "" if cfg.users else "pdms user add / pdms user import"))
    no_password = [n for n, db in cfg.dbs.items() if not db.password]
    checks.append(Check(
        section, _("Databases"), OK if cfg.dbs and not no_password else WARN,
        str(len(cfg.dbs)) + (f" · {_('without password')}: {', '.join(no_password)}" if no_password else ""),
        "pdms db add" if not cfg.dbs else ("pdms db edit " + no_password[0] if no_password else ""),
    ))
    return checks


def check_databases(cfg: Config, timeout: int) -> list[Check]:
    section = _("Database connections")
    if not cfg.dbs:
        return []

    def probe(item: tuple[str, object]) -> Check:
        name, db = item
        try:
            version = runner.test_connection(db, timeout)
            return Check(section, name, OK, f"{db.host}:{db.port} · {version.split(',')[0]}")
        except Exception as exc:  # noqa: BLE001 - any driver error is the result
            return Check(section, name, FAIL, f"{db.host}:{db.port} · {str(exc).strip().splitlines()[0]}",
                         _("Check host/port/credentials with pdms db edit {name}, or the VPN.", name=name))

    with ThreadPoolExecutor(max_workers=min(8, len(cfg.dbs))) as pool:
        return list(pool.map(probe, cfg.dbs.items()))


# ---------------------------------------------------------------------- repo, ports and instances


def check_repo(cfg: Config) -> list[Check]:
    section = "Repo"
    root = repos.active_root(cfg)
    if root is None:
        return [Check(section, _("Current repo"), WARN, _("none"),
                      _("Run pdms inside a PDMS checkout, or: pdms repo add <path>"))]
    alias = repos.alias_of(cfg, root) or root.name
    if not root.is_dir():
        return [Check(section, _("Current repo"), FAIL, f"{alias} · {root} ({_('missing')})",
                      _("pdms repo remove {alias}, then pdms repo add <path>", alias=alias))]
    checks = [Check(section, _("Current repo"), OK, f"{alias} · {root}")]
    backend = root / "backend"
    count = len(runner.find_services_below(backend)) if backend.is_dir() else 0
    checks.append(Check(section, _("Services"), OK if count else FAIL, str(count),
                        "" if count else _("No services found in {path}.", path=backend)))
    tf = routes.terraform_dir(root, "dev")
    checks.append(Check(section, _("Proxy routes (Terraform dev)"), OK if tf.is_dir() else WARN,
                        str(tf) if tf.is_dir() else _("not found"),
                        "" if tf.is_dir() else _("pdms proxy needs infra/infra_auto/environments/dev.")))
    repo = cfg.repos.get(alias)
    remote = (repo.remote if repo else "") or repos.remote_from_frontend(root)
    checks.append(Check(section, _("Remote API for the proxy"), OK if remote else WARN, remote or _("not configured"),
                        "" if remote else _("pdms proxy --remote <url> (or set VITE_APP_API_URL in frontend/.env)")))
    return checks


def check_ports(cfg: Config) -> list[Check]:
    section = _("Ports")
    running = proxy.running_proxy()
    checks = []
    for label, port in ((_("Default service port"), cfg.defaults.port), (_("Proxy port"), 8000)):
        if label == _("Proxy port") and running and running.get("port") == port:
            checks.append(Check(section, label, OK, _("{port} · used by the pdms proxy", port=port)))
        elif runner.port_is_free(cfg.defaults.host, port) and port not in instances.running_ports():
            checks.append(Check(section, label, OK, _("{port} · free", port=port)))
        else:
            checks.append(Check(section, label, WARN, _("{port} · in use", port=port),
                                _("pdms will offer the next free port.")))
    return checks


def check_instances() -> list[Check]:
    section = _("Background services")
    items = list(instances.load().values())
    if not items:
        return [Check(section, _("Instances"), OK, _("none running"))]
    healths = instances.health_all(items)
    by_state: dict[str, list[str]] = {}
    for key, health in healths.items():
        by_state.setdefault(health.state, []).append(key)
    checks = [Check(section, _("Instances"), OK, ", ".join(f"{len(v)} {k}" for k, v in sorted(by_state.items())))]
    if by_state.get("error"):
        checks.append(Check(section, _("With errors"), FAIL, ", ".join(by_state["error"]), "pdms ps / pdms logs <instance>"))
    if by_state.get("stopped"):
        checks.append(Check(section, _("Stopped"), WARN, ", ".join(by_state["stopped"]), "pdms ps --clean"))
    stale = [i.key for i in items if healths[i.key].state != "stopped" and i.deps
             and installer.changed_parts(i.deps, Path(i.service))]
    if stale:
        checks.append(Check(section, _("Outdated installed code"), WARN, ", ".join(stale), "pdms restart <instance>"))
    return checks


def run_all(cfg: Config, *, databases: bool = True, timeout: int = 5) -> list[Check]:
    checks = check_pdms() + check_tools() + check_config(cfg)
    if databases:
        checks += check_databases(cfg, timeout)
    return checks + check_repo(cfg) + check_ports(cfg) + check_instances() + check_shell()
