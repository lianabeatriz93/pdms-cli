"""The PDMS frontend: installing, building, starting it and pointing it to the proxy."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

from .. import codeartifact, frontend, instances, proxy, repos
from ..config import Config
from ..i18n import _
from . import checks, settings
from .errors import ActionError

__all__ = [
    "FrontendApi", "FrontendLaunch", "_yarn", "build_frontend", "check_frontend_tools", "codeartifact_ready",
    "fix_frontend_api", "frontend_api", "frontend_root", "install_frontend", "plan_frontend", "start_frontend",
    "stop_frontend", "wait_for_frontend",
]


@dataclass
class FrontendLaunch:
    """Everything decided to start the web app; :func:`prepare_frontend` and :func:`start_frontend` carry it out."""

    root: Path
    mode: str  # dev | build
    port: int
    install: bool  # run yarn install first
    build: str  # why it is built first ("" to serve the last build)

    @property
    def url(self) -> str:
        return frontend.url(self.port)


def frontend_root(cfg: Config) -> Path:
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    if not frontend.exists(root):
        raise ActionError(_("{path} has no frontend (frontend/package.json).", path=root))
    return root


def check_frontend_tools() -> None:
    """Node 22–24 and yarn, as the frontend's package.json asks."""
    version = frontend.node_version()
    if version is None:
        raise ActionError(_("Node.js was not found. Install Node 22 or 24 (for example with nvm install 22)."))
    if not frontend.node_supported(version):
        raise ActionError(_("The frontend needs Node 22 to 24 and this is Node {version} (nvm use 22).",
                            version=version))
    if frontend.yarn() is None:
        raise ActionError(_("yarn was not found. Install it with: npm install -g yarn"))


def plan_frontend(
    cfg: Config, *, mode: str = "dev", port: int | None = None, install: bool | None = None, rebuild: bool = False,
) -> FrontendLaunch:
    """Decide how the web app runs. Raises :class:`PortBusy` for the user to answer.

    ``install``: True forces ``yarn install``, False skips it, None runs it when node_modules does not match the
    lockfile. A build is made again only when something changed since the last one, or with ``rebuild``."""
    mode = settings._one_of("mode", mode, frontend.MODES)
    root = frontend_root(cfg)
    if current := frontend.running():
        raise ActionError(_("The frontend is already running ({mode}) at {url}. Stop it first: pdms stop frontend",
                            mode=current.get("mode", "dev"), url=frontend.url(current["port"])))
    check_frontend_tools()
    port = checks.free_port("127.0.0.1", port or frontend.PORT)
    wanted = install is True or (install is None and not frontend.dependencies_ok(root))
    build = ""
    if mode == "build":
        build = "rebuild asked" if rebuild else frontend.build_needed(root)
    return FrontendLaunch(root, mode, port, wanted, build)


def _yarn(cmd: list[str], cwd: Path, output: IO[str] | None) -> None:
    redirect = {"stdout": output, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL} if output else {}
    if output:
        output.write(f"$ {' '.join(cmd)}\n")
        output.flush()
    subprocess.run(cmd, cwd=cwd, env=frontend.environment(), check=True, **redirect)


def codeartifact_ready(
    folder: Path, notes: IO[str] | None = None, sso: Callable[[str], bool] | None = None, force: bool = False,
) -> list[str]:
    """Log in to the AWS CodeArtifact repositories of the web app whose token npm no longer accepts (all of them with
    ``force``), as ``frontend/codeartifact-login.sh`` does; the profiles used. An expired SSO session is given to
    ``sso`` (the terminal runs ``aws sso login``), else it says how to log in."""
    cfg = Config.load()
    used = []
    for domain in codeartifact.needed(folder):
        if not force and codeartifact.authorized(folder, domain) is not False:
            continue
        if notes:
            notes.write(_("The {namespace} packages need a new AWS CodeArtifact token; logging in...",
                          namespace=domain.namespace) + "\n")
            notes.flush()
        try:
            try:
                used.append(codeartifact.login(cfg, domain, notes))
            except codeartifact.LoginError as exc:
                if not (exc.expired and sso and sso(exc.profile)):
                    raise
                used.append(codeartifact.login(cfg, domain, notes))
        except codeartifact.LoginError as exc:
            if exc.expired:
                raise ActionError(_("The AWS session of {profile} is over: log in with aws sso login --profile "
                                    "{profile} (or from Data in pdms ui) and install the frontend again.",
                                    profile=exc.profile)) from exc
            raise ActionError(_("Could not log in to AWS CodeArtifact: {error}", error=exc.message)) from exc
    return used


def install_frontend(
    root: Path, output: IO[str] | None = None, log: Path | None = None, sso: Callable[[str], bool] | None = None,
    notes: IO[str] | None = None,
) -> None:
    """``yarn install`` in ``frontend/``, logged in to AWS CodeArtifact first when its token is over (see
    :func:`codeartifact_ready`). A failure that still looks like CodeArtifact logs in again and tries once more.
    ``notes`` gets what pdms says (``output`` when it is given, the terminal's otherwise)."""
    folder, notes = frontend.folder(root), output or notes
    codeartifact_ready(folder, notes, sso)
    try:
        _yarn(frontend.install_command(), folder, output)
        return
    except (OSError, subprocess.CalledProcessError) as exc:
        if not (log and frontend.needs_codeartifact(log)):
            raise ActionError(_("yarn install failed: {error}", error=exc)) from exc
    codeartifact_ready(folder, notes, sso, force=True)
    try:
        _yarn(frontend.install_command(), folder, output)
    except (OSError, subprocess.CalledProcessError) as exc:
        if log and frontend.needs_codeartifact(log):
            raise ActionError(_("yarn install could not download the {packages} packages from AWS CodeArtifact, "
                                "even after logging in: check the profile has the {role} role.",
                                packages="@alivi", role=codeartifact.ROLE)) from exc
        raise ActionError(_("yarn install failed: {error}", error=exc)) from exc


def build_frontend(root: Path, output: IO[str] | None = None) -> dict:
    """``yarn build`` (tsc + vite build into frontend/dist) and remember what it was built from."""
    try:
        _yarn(frontend.build_command(), frontend.folder(root), output)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ActionError(_("yarn build failed: {error}", error=exc)) from exc
    return frontend.remember_build(root)


def start_frontend(plan: FrontendLaunch) -> dict:
    """Start the dev server or the preview of the build in the background; waiting is :func:`wait_for_frontend`."""
    return frontend.start(plan.root, plan.mode, plan.port)


def wait_for_frontend(started: dict, timeout: float = 90) -> str:
    """``ok`` once it answers, ``stopped`` if it exited, ``starting`` when ``timeout`` runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not frontend.running():
            return "stopped"
        if frontend.responds(int(started["port"])):
            return "ok"
        time.sleep(0.5)
    return "starting"


def stop_frontend(current: dict | None = None) -> bool:
    """Stop the frontend pdms started; False when it was not running."""
    current = current or frontend.running()
    if not current:
        return False
    frontend.stop(current)
    return True


@dataclass
class FrontendApi:
    """The web app calls a local API nobody answers on: a proxy that stopped, or one on another port."""

    url: str  # VITE_APP_API_URL (+ version) it calls
    port: int
    proxy_port: int | None  # the proxy running now, if any
    leftover: bool  # frontend/.env.local still has the change a proxy that did not stop cleanly made


def frontend_api(cfg: Config) -> FrontendApi | None:
    """What is wrong with where the frontend of the current repo sends its API calls, or None when nothing is."""
    root = repos.active_root(cfg)
    if not frontend.exists(root):
        return None
    url = frontend.api_url(root, "dev")
    if not frontend.is_local(url):
        return None
    try:
        port = urlsplit(url).port or 80
    except ValueError:
        return None
    running = proxy.running_proxy()
    if running and running["port"] == port:
        return None
    if instances.responds("127.0.0.1", port, timeout=0.5):  # a service it calls directly, on purpose
        return None
    return FrontendApi(url, port, running["port"] if running else None, bool(proxy.frontend_change()))


def fix_frontend_api(cfg: Config) -> str:
    """Point the frontend to the running proxy ("pointed"), or put ``.env.local`` back as it was ("restored")."""
    problem = frontend_api(cfg)
    if problem is None:
        return ""
    root = repos.active_root(cfg)
    if problem.proxy_port:
        proxy.restore_frontend_change()  # the old proxy's change goes first, so this one undoes to the original
        change = repos.point_frontend_to(root, f"http://localhost:{problem.proxy_port}")
        if change:
            proxy.remember_frontend_change(change)
        return "pointed"
    if problem.leftover and proxy.restore_frontend_change():
        return "restored"
    raise ActionError(_("Nothing answers on port {port}: start the proxy (pdms proxy -b), or change VITE_APP_API_URL "
                        "in frontend/.env.local.", port=problem.port))
