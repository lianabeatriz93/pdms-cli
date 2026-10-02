"""The PDMS web app (``<repo>/frontend``, Vite + yarn), run by pdms: the dev server or a production build.

``dev`` runs ``yarn dev`` (hot reload). ``build`` runs ``yarn build`` (tsc + vite build into ``frontend/dist``) and
serves it with ``yarn preview``, to try the app as it runs in production. Both listen on https://localhost:3000 (the
repo's Vite config gets a certificate from mkcert, and ``vite preview`` uses the same one).

Like the background proxy it is a single process with a state file of its own (``frontend.json``), shown as
``frontend`` by ``pdms ps``, ``pdms logs`` and ``pdms stop``. A build is remembered (``frontend-build.json``) with what
went into it, so it is only built again when something changed: the API URL is fixed when it is built.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import instances, repos

KEY = "frontend"  # how pdms ps, stop and logs call it
PORT = 3000  # the repo's vite.config.ts; the login redirects expect it
MODES = ("dev", "build")
NODE_MAJORS = range(22, 25)  # package.json engines: >=22 <=24
# What a build depends on besides the API URL and the dependencies (relative to frontend/).
BUILD_SOURCES = ("src", "public", "index.html", "vite.config.ts", "tsconfig.json", "tsconfig.node.json",
                 "package.json", "amplifyconfiguration.json")
WINDOWS = sys.platform == "win32"


def folder(root: Path) -> Path:
    return root / "frontend"


def exists(root: Path | None) -> bool:
    return bool(root) and (folder(root) / "package.json").is_file()


def state_path() -> Path:
    return instances.state_dir() / "frontend.json"


def build_record_path() -> Path:
    return instances.state_dir() / "frontend-build.json"


def log_path() -> Path:
    return instances.log_path(KEY)


def install_log_path() -> Path:
    return log_path().with_suffix(".install.log")


def build_log_path() -> Path:
    return log_path().with_suffix(".build.log")


def is_key(key: str) -> bool:
    return key == KEY or key.startswith(f"{KEY}@")


# --------------------------------------------------------------------------- tools


def yarn() -> str | None:
    """Full path of yarn (on Windows yarn.cmd), or None."""
    return shutil.which("yarn")


def node_version() -> str | None:
    """``22.12.0``, or None when node is not installed."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().lstrip("v") or None


def node_supported(version: str) -> bool:
    match = re.match(r"(\d+)\.", version)
    return bool(match) and int(match.group(1)) in NODE_MAJORS


def dependencies_ok(root: Path) -> bool:
    """Whether ``node_modules`` matches package.json and yarn.lock (``yarn check --integrity``, under a second)."""
    tool = yarn()
    if not tool or not (folder(root) / "node_modules").is_dir():
        return False
    try:
        result = subprocess.run([tool, "check", "--integrity"], cwd=folder(root), capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install_command() -> list[str]:
    return [yarn() or "yarn", "install"]


def build_command() -> list[str]:
    return [yarn() or "yarn", "build"]


def serve_command(mode: str, port: int) -> list[str]:
    """``yarn dev`` or ``yarn preview`` on ``port``, failing instead of moving to another port when it is taken."""
    return [yarn() or "yarn", "dev" if mode == "dev" else "preview", "--port", str(port), "--strictPort"]


def environment() -> dict[str, str]:
    # Vite must not open a browser of its own, and its output goes to a log file: no colours.
    return {**os.environ, "BROWSER": "none", "NO_COLOR": "1", "FORCE_COLOR": "0"}


def needs_codeartifact(log: Path) -> bool:
    """Whether a failed ``yarn install`` could not download from AWS CodeArtifact (``@alivi/ui-kit``)."""
    text = instances.tail(str(log), 80).lower()
    return "codeartifact" in text and any(code in text for code in ("401", "403", "unauthorized", "forbidden"))


# --------------------------------------------------------------------------- API URL


def env_values(root: Path, mode: str) -> dict[str, str]:
    """The variables Vite loads for ``mode`` (dev → development, build → production), later files winning."""
    vite_mode = "development" if mode == "dev" else "production"
    values: dict[str, str] = {}
    for name in (".env", ".env.local", f".env.{vite_mode}", f".env.{vite_mode}.local"):
        values.update(repos._read_env_file(folder(root) / name))
    return values


def api_url(root: Path, mode: str) -> str:
    """Where the app sends its API calls (``VITE_APP_API_URL`` + ``VITE_APP_API_URL_VERSION``)."""
    env = env_values(root, mode)
    base, version = env.get("VITE_APP_API_URL", "").rstrip("/"), env.get("VITE_APP_API_URL_VERSION", "").strip("/")
    return f"{base}/{version}" if base and version else base


def is_local(url: str) -> bool:
    return "://localhost" in url or "://127.0.0.1" in url


# --------------------------------------------------------------------------- builds


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def newest_source(root: Path) -> float:
    """The latest change to the app's code and configuration (``.env*`` included)."""
    base = folder(root)
    newest = 0.0
    candidates = [base / name for name in BUILD_SOURCES] + list(base.glob(".env*"))
    for path in candidates:
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
        elif path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d != "node_modules" and not d.startswith(".")]
                for name in filenames:
                    try:
                        newest = max(newest, os.stat(os.path.join(dirpath, name)).st_mtime)
                    except OSError:
                        pass
    return newest


def build_inputs(root: Path) -> dict:
    return {"root": str(root), "api": api_url(root, "build"), "lock": _digest(folder(root) / "yarn.lock"),
            "sources": newest_source(root)}


def git_commit(root: Path) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        result = subprocess.run([git, "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def last_build() -> dict | None:
    """What the last build pdms made was built from: root, api, lock, sources, commit and when."""
    try:
        return json.loads(build_record_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def remember_build(root: Path) -> dict:
    record = {**build_inputs(root), "commit": git_commit(root),
              "built_at": datetime.now().isoformat(timespec="seconds")}
    build_record_path().parent.mkdir(parents=True, exist_ok=True)
    build_record_path().write_text(json.dumps(record), encoding="utf-8")
    return record


def build_needed(root: Path) -> str:
    """Why the app has to be built again ("" when the last build is still good): no build, other API, other
    dependencies or code changed since."""
    record = last_build()
    if not (folder(root) / "dist" / "index.html").is_file() or not record or record.get("root") != str(root):
        return "no build yet"
    now = build_inputs(root)
    if record.get("api") != now["api"]:
        return "the API URL changed"
    if record.get("lock") != now["lock"]:
        return "the dependencies changed"
    if now["sources"] > float(record.get("sources") or 0):
        return "the code changed"
    return ""


# --------------------------------------------------------------------------- the running app


def running() -> dict | None:
    """``{"pid", "port", "mode", "root", ...}`` of the frontend pdms started, while it runs."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if instances.process_alive(int(data["pid"]), float(data.get("created", 0))) else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None


def url(port: int, scheme: str = "https") -> str:
    return f"{scheme}://localhost:{port}"


def start(root: Path, mode: str, port: int) -> dict:
    """Start ``yarn dev`` or ``yarn preview`` detached (the build, if any, is done before) and remember it."""
    proc = instances.spawn(serve_command(mode, port), folder(root), environment(), log_path())
    record = {
        "pid": proc.pid, "created": instances.creation_time(proc.pid), "port": port, "mode": mode,
        "root": str(root), "api": api_url(root, mode), "log": str(log_path()),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(record), encoding="utf-8")
    return record


def forget(pid: int) -> None:
    try:
        if json.loads(state_path().read_text(encoding="utf-8")).get("pid") == pid:
            state_path().unlink()
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def stop(current: dict, timeout: float = 10) -> None:
    pid = int(current["pid"])
    instances.kill_tree(pid, float(current.get("created", 0)), timeout)
    forget(pid)


def scheme(port: int, timeout: float = 1.5) -> str:
    """``https`` (the repo's mkcert certificate) or ``http`` when Vite answers on ``port``; "" while it does not."""
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    for name, conn in (("https", http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=insecure)),
                       ("http", http.client.HTTPConnection("127.0.0.1", port, timeout=timeout))):
        try:
            conn.request("GET", "/", headers={"Host": f"localhost:{port}"})
            conn.getresponse()
            return name
        except (OSError, http.client.HTTPException):
            continue
        finally:
            conn.close()
    return ""


def responds(port: int) -> bool:
    return bool(scheme(port))


ERROR_LINE = re.compile(r"^\s*(error|Error:|\[vite\] error)", re.IGNORECASE)


def health(current: dict | None) -> instances.Health:
    """Its state; when it answers, ``detail`` is the scheme it answers with (https or http)."""
    if not current:
        return instances.Health("stopped")
    if found := scheme(int(current["port"])):
        return instances.Health("ok", found)
    errors = [line.strip() for line in instances.tail(current.get("log") or str(log_path()), 40).splitlines()
              if ERROR_LINE.match(line)]
    return instances.Health("error", errors[-1]) if errors else instances.Health("starting")


def last_line(log: Path) -> str:
    lines = [line for line in instances.tail(str(log), 20).splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""
