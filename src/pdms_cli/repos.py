"""PDMS checkouts ("repos"): detection, registration and which one is active.

Several checkouts can be registered (e.g. ``pdms`` and ``pdms_v2``); one of them is the *current* repo, used to list
services, resolve stacks, run migrations and read routes. A command can also run against another repo just for
that invocation (see :func:`use_for_this_command`).
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config, Repo

# Set when the user picks "only for this command" (or when there is no terminal to ask).
_session_root: Path | None = None


def is_repo_root(path: Path) -> bool:
    return (path / "backend" / "snakesdk").is_dir()


def find_repo_root(start: Path) -> Path | None:
    start = start.resolve()
    for path in (start, *start.parents):
        if is_repo_root(path):
            return path
    return None


def alias_of(cfg: Config, root: Path) -> str | None:
    root = root.resolve()
    for alias, repo in cfg.repos.items():
        if repo.root.resolve() == root:
            return alias
    return None


def suggest_alias(cfg: Config, root: Path) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-") or "pdms"
    alias, n = base, 2
    while alias in cfg.repos:
        alias, n = f"{base}-{n}", n + 1
    return alias


def register(cfg: Config, root: Path, alias: str | None = None) -> str:
    """Add ``root`` to the known repos (if needed) and return its alias."""
    existing = alias_of(cfg, root)
    if existing:
        return existing
    alias = alias or suggest_alias(cfg, root)
    cfg.repos[alias] = Repo(path=str(root.resolve()))
    if not cfg.current_repo:
        cfg.current_repo = alias
    return alias


def repo_of(cfg: Config, path: str | Path) -> str | None:
    """Alias of the registered repo that contains ``path`` (e.g. a running instance's service)."""
    path = Path(path).resolve()
    best: tuple[int, str] | None = None
    for alias, repo in cfg.repos.items():
        root = repo.root.resolve()
        if path == root or root in path.parents:
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, alias)
    return best[1] if best else None


def use_for_this_command(root: Path | None) -> None:
    global _session_root
    _session_root = root.resolve() if root else None


def git_branch(root: Path) -> str:
    """The branch checked out in ``root`` (the short commit when detached), read from .git without running git;
    "" when it is not a git checkout."""
    git = root / ".git"
    try:
        if git.is_file():  # a worktree: "gitdir: <path>"
            git = root / git.read_text(encoding="utf-8").split(":", 1)[1].strip()  # absolute stays absolute
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, IndexError):
        return ""
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:8]


def active_root(cfg: Config) -> Path | None:
    if _session_root is not None:
        return _session_root
    return cfg.repo.root if cfg.repo else None


def active_backend(cfg: Config) -> Path | None:
    """Services folder of the repo this command works with."""
    if _session_root is not None:
        alias = alias_of(cfg, _session_root)
        return cfg.repos[alias].backend_dir if alias else _session_root / "backend"
    return cfg.repo.backend_dir if cfg.repo else None


def _read_env_file(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def remote_from_frontend(root: Path) -> str | None:
    """Remote API base from ``frontend/.env`` (VITE_APP_API_URL + stage), e.g. https://<id>.../dev.

    ``.env.local`` is ignored on purpose: it may point to the local proxy itself.
    """
    env = _read_env_file(root / "frontend" / ".env")
    base, version = env.get("VITE_APP_API_URL", "").rstrip("/"), env.get("VITE_APP_API_URL_VERSION", "").strip("/")
    if not base.startswith("http") or "localhost" in base or "127.0.0.1" in base:
        return None
    url = f"{base}/{version}" if version else base
    for suffix in ("/api/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _env_key(line: str) -> str | None:
    return None if line.lstrip().startswith("#") or "=" not in line else line.split("=", 1)[0].strip()


def point_frontend_to(root: Path, proxy_url: str) -> dict | None:
    """Set VITE_APP_API_URL/VERSION in ``frontend/.env.local`` (git-ignored) to go through the proxy.

    Returns what :func:`restore_frontend` needs to undo it: the file, whether it existed and the original line of
    each key (None when the key was not set), or None when the repo has no frontend.
    """
    frontend = root / "frontend"
    if not frontend.is_dir():
        return None
    path = frontend / ".env.local"
    wanted = {"VITE_APP_API_URL": proxy_url, "VITE_APP_API_URL_VERSION": "api/v1"}
    existed = path.exists()
    lines = path.read_text(encoding="utf-8").splitlines() if existed else []
    previous: dict[str, str | None] = dict.fromkeys(wanted)
    for i, line in enumerate(lines):
        key = _env_key(line)
        if key in wanted and previous[key] is None:
            previous[key] = line
            lines[i] = f"{key}={wanted[key]}"
    lines += [f"{key}={value}" for key, value in wanted.items() if previous[key] is None]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "existed": existed, "proxy_url": proxy_url, "previous": previous}


def restore_frontend(change: dict) -> bool:
    """Undo :func:`point_frontend_to`; keys edited since then are left alone. True if the file was touched."""
    path = Path(change["path"])
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    ours = {"VITE_APP_API_URL": change["proxy_url"], "VITE_APP_API_URL_VERSION": "api/v1"}
    kept, touched = [], False
    for line in lines:
        key = _env_key(line)
        if key in ours and line == f"{key}={ours[key]}":
            touched = True
            original = change["previous"].get(key)
            if original is not None:
                kept.append(original)
            continue
        kept.append(line)
    if not touched:
        return False
    if not change["existed"] and not any(line.strip() for line in kept):
        path.unlink()
    else:
        path.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
    return True


def frontend_uses(root: Path, proxy_url: str) -> bool:
    env = _read_env_file(root / "frontend" / ".env.local")
    return env.get("VITE_APP_API_URL") == proxy_url and env.get("VITE_APP_API_URL_VERSION") == "api/v1"


def translate(service: Path, old_root: Path, new_root: Path) -> Path | None:
    """Same service in another checkout, if it exists there."""
    try:
        candidate = new_root / service.resolve().relative_to(old_root.resolve())
    except ValueError:
        return None
    return candidate if (candidate / "pyproject.toml").is_file() and (candidate / "main.py").is_file() else None
