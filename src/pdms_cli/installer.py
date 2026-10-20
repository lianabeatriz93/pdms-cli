"""Smart install: skip ``poetry lock && poetry install`` when nothing that affects it has changed, and only reinstall
the copied packages whose code changed when that is all that did (see :func:`code_only_changes`).

The fingerprint of a service covers its ``pyproject.toml`` and ``poetry.lock`` plus, recursively, its local path
dependencies: the whole source tree of the ones installed as a copy (``develop = false``) and only the
``pyproject.toml`` of editable ones (``develop = true``), whose code changes are picked up without reinstalling.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import tomlkit

from .instances import state_dir

try:
    import tomllib  # Python 3.11+: several times faster than tomlkit, which only reading does not need
except ImportError:  # pragma: no cover - Python 3.10
    tomllib = None

SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "tests",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".log")
_records_lock = threading.Lock()  # several services may finish installing at once


def fingerprints_path() -> Path:
    return state_dir() / "installs.json"


def _poetry_dependency_tables(data: dict) -> list[dict]:
    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
    return [t for t in tables if isinstance(t, dict)]


_deps_cache: dict[Path, tuple[tuple[int, int], list[tuple[Path, bool]]]] = {}


def _read_toml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return tomllib.loads(text) if tomllib else tomlkit.parse(text).unwrap()


def path_dependencies(project: Path) -> list[tuple[Path, bool]]:
    """``(directory, develop)`` of the local path dependencies declared in ``project/pyproject.toml``. Kept while the
    file does not change: a repo's 250 services read the same shared packages' files again and again."""
    pyproject = project / "pyproject.toml"
    try:
        stat = pyproject.stat()
    except OSError:
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _deps_cache.get(pyproject)
    if cached and cached[0] == stamp:
        return list(cached[1])
    try:
        data = _read_toml(pyproject)
    except Exception:  # noqa: BLE001 - unreadable pyproject: no known dependencies
        return []
    found = []
    for table in _poetry_dependency_tables(data):
        for spec in table.values():
            specs = spec if isinstance(spec, list) else [spec]
            for item in specs:
                if isinstance(item, dict) and "path" in item:
                    found.append(((project / item["path"]).resolve(), bool(item.get("develop", False))))
    _deps_cache[pyproject] = (stamp, found)
    return list(found)


def copied_dependencies(project: Path) -> list[Path]:
    """Directories of every local path dependency installed as a copy (``develop = false``), transitive ones included.

    ``poetry install`` does not reinstall them while their version stays the same, so their code changes never reach
    the virtualenv unless they are reinstalled by hand.
    """
    copied: list[Path] = []
    seen: set[Path] = set()
    pending = path_dependencies(project)
    while pending:
        directory, develop = pending.pop(0)
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        if not develop:
            copied.append(directory)
        pending.extend(path_dependencies(directory))
    return copied


def _hash_file_meta(digest: "hashlib._Hash", path: Path, root: Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    digest.update(f"{path.relative_to(root).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())


def _hash_tree(digest: "hashlib._Hash", root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if not name.endswith(SKIP_SUFFIXES):
                _hash_file_meta(digest, Path(dirpath) / name, root)


SERVICE_PART = "(service)"  # the service's own pyproject.toml + poetry.lock


def _walk(service: Path) -> list[tuple[str, Path, bool]]:
    """``(part name, directory, develop)`` of every local path dependency, transitive ones included."""
    found: list[tuple[str, Path, bool]] = []
    names: set[str] = {SERVICE_PART}
    seen: set[Path] = set()
    pending = path_dependencies(service)
    while pending:
        directory, develop = pending.pop()
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        name = directory.name if directory.name not in names else str(directory)
        names.add(name)
        found.append((name, directory, develop))
        pending.extend(path_dependencies(directory))
    return found


def dependency_fingerprints(service: Path) -> dict[str, str]:
    """One digest per part that affects the install: the service itself and each local path dependency."""
    parts: dict[str, str] = {}
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "poetry.lock"):
        _hash_file_meta(digest, service / name, service)
    parts[SERVICE_PART] = digest.hexdigest()
    for name, directory, develop in _walk(service):
        digest = hashlib.sha256(f"develop={develop}\n".encode())
        if develop:
            _hash_file_meta(digest, directory / "pyproject.toml", directory)
        else:
            _hash_tree(digest, directory)
        parts[name] = digest.hexdigest()
    return parts


def manifests_digest(service: Path) -> str:
    """The content of what decides which packages get installed: the service's ``pyproject.toml`` and
    ``poetry.lock`` and every local path dependency's ``pyproject.toml``, with how it is installed. While it stays the
    same, whatever changed is only code."""
    digest = hashlib.sha256()
    files = [("", service / "pyproject.toml"), ("", service / "poetry.lock")]
    files += [(f"{name}|develop={develop}", directory / "pyproject.toml") for name, directory, develop in _walk(service)]
    for label, path in files:
        try:
            content = path.read_bytes()
        except OSError:
            content = b"-"
        digest.update(f"{label}|{path.name}|{len(content)}\n".encode() + content)
    return digest.hexdigest()


def _combine(parts: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(parts.items()):
        digest.update(f"{name}={value}\n".encode())
    return digest.hexdigest()


def fingerprint(service: Path) -> str:
    return _combine(dependency_fingerprints(service))


def changed_parts(recorded: dict[str, str] | None, service: Path) -> list[str] | None:
    """Parts that changed since ``recorded`` was taken, or None if nothing was recorded."""
    if not recorded:
        return None
    current = dependency_fingerprints(service)
    names = sorted(set(recorded) | set(current))
    return [n for n in names if recorded.get(n) != current.get(n)]


def _load() -> dict[str, dict | str]:
    try:
        return json.loads(fingerprints_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _record(service: Path) -> dict:
    """What :func:`remember` kept for the service (only the fingerprint, as older versions of pdms kept it)."""
    value = _load().get(str(service))
    if isinstance(value, str):
        return {"fingerprint": value}
    return value if isinstance(value, dict) else {}


def is_up_to_date(service: Path) -> bool:
    return _record(service).get("fingerprint") == fingerprint(service)


def code_only_changes(service: Path) -> list[Path] | None:
    """The copied path dependencies to reinstall when nothing changed since the last install but their code, or
    None when something else did (or pdms does not know): then it takes a whole ``poetry lock && poetry install``.

    It is what usually changes after a ``git pull``, and ``pip install --no-deps --force-reinstall`` of those
    packages is all it needs; poetry would not even pick that change up.
    """
    record = _record(service)
    if not isinstance(record.get("parts"), dict) or record.get("manifests") != manifests_digest(service):
        return None
    current = dependency_fingerprints(service)
    copied = {name: directory for name, directory, develop in _walk(service) if not develop}
    changed = [name for name in sorted(set(record["parts"]) | set(current))
               if record["parts"].get(name) != current.get(name) and name != SERVICE_PART]
    if any(name not in copied for name in changed):
        return None
    return [copied[name] for name in changed]


def remember(service: Path) -> None:
    """Record what got installed (``poetry lock`` may have rewritten the lock file)."""
    parts = dependency_fingerprints(service)
    record = {"fingerprint": _combine(parts), "parts": parts, "manifests": manifests_digest(service)}
    with _records_lock:
        data = _load()
        data[str(service)] = record
        path = fingerprints_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
