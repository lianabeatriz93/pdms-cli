"""Smart install: skip ``poetry lock && poetry install`` when nothing that affects it has changed.

The fingerprint of a service covers its ``pyproject.toml`` and ``poetry.lock`` plus, recursively, its local path
dependencies: the whole source tree of the ones installed as a copy (``develop = false``) and only the
``pyproject.toml`` of editable ones (``develop = true``), whose code changes are picked up without reinstalling.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import tomlkit

from .instances import state_dir

SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "tests",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".log")


def fingerprints_path() -> Path:
    return state_dir() / "installs.json"


def _poetry_dependency_tables(data: dict) -> list[dict]:
    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
    return [t for t in tables if isinstance(t, dict)]


def path_dependencies(project: Path) -> list[tuple[Path, bool]]:
    """``(directory, develop)`` of the local path dependencies declared in ``project/pyproject.toml``."""
    try:
        data = tomlkit.parse((project / "pyproject.toml").read_text()).unwrap()
    except Exception:  # noqa: BLE001 - unreadable pyproject: no known dependencies
        return []
    found = []
    for table in _poetry_dependency_tables(data):
        for spec in table.values():
            specs = spec if isinstance(spec, list) else [spec]
            for item in specs:
                if isinstance(item, dict) and "path" in item:
                    found.append(((project / item["path"]).resolve(), bool(item.get("develop", False))))
    return found


def _hash_file_meta(digest: "hashlib._Hash", path: Path, root: Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    digest.update(f"{path.relative_to(root)}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())


def _hash_tree(digest: "hashlib._Hash", root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if not name.endswith(SKIP_SUFFIXES):
                _hash_file_meta(digest, Path(dirpath) / name, root)


def fingerprint(service: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "poetry.lock"):
        _hash_file_meta(digest, service / name, service)
    seen: set[Path] = set()
    pending = path_dependencies(service)
    while pending:
        directory, develop = pending.pop()
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        digest.update(f"## {directory} develop={develop}\n".encode())
        if develop:
            _hash_file_meta(digest, directory / "pyproject.toml", directory)
        else:
            _hash_tree(digest, directory)
        pending.extend(path_dependencies(directory))
    return digest.hexdigest()


def _load() -> dict[str, str]:
    try:
        return json.loads(fingerprints_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_up_to_date(service: Path) -> bool:
    return _load().get(str(service)) == fingerprint(service)


def remember(service: Path) -> None:
    """Record the fingerprint after a successful install (``poetry lock`` may have rewritten the lock file)."""
    data = _load()
    data[str(service)] = fingerprint(service)
    path = fingerprints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
