"""The Python a service's virtualenv runs on, against the one its ``pyproject.toml`` asks for.

Poetry keeps using a service's ``.venv`` even when the project no longer accepts its Python (PDMS moved from
``>=3.10,<3.12`` to ``>=3.12,<3.14``): ``poetry env info`` calls the old one valid and the service keeps running on
it. pdms tells the user (:class:`actions.PythonMismatch`) and, once they agree, recreates the virtualenv with the
lowest installed Python the project accepts (the Lambdas run the lowest one) before installing again.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import installer

WINDOWS = sys.platform == "win32"
VERSION = re.compile(r"(\d+)(?:\.(\d+|\*))?(?:\.(\d+|\*))?")
CLAUSE = re.compile(r"^(\^|~=|~|>=|<=|!=|==|>|<|=)?\s*(\d+(?:\.(?:\d+|\*)){0,2})$")
MINORS = range(8, 20)  # python3.8 ... python3.19 on the PATH

Version = tuple[int, int, int]


def parse_version(text: str) -> Version | None:
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", text)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)) if match else None


def show(version: Version) -> str:
    return ".".join(map(str, version))


def _clause(text: str) -> list[tuple[str, Version]] | None:
    """One poetry/PEP 440 clause as simple comparisons; None when it is not understood."""
    match = CLAUSE.match(text.strip())
    if not match:
        return None
    op, raw = match.group(1) or "==", match.group(2)
    parts = raw.split(".")
    wildcard = "*" in parts
    nums = [int(part) for part in parts if part != "*"]
    given = len(nums)
    low = (nums + [0, 0, 0])[:3]
    low_v: Version = (low[0], low[1], low[2])

    def bump(index: int) -> Version:
        up = low[:index] + [0] * (3 - index)
        up[index - 1] += 1
        return (up[0], up[1], up[2])

    if op in ("==", "=") and (wildcard or given < 3):
        return [(">=", low_v), ("<", bump(given))]
    if op == "^":
        first = next((i for i, n in enumerate(low) if n), given - 1)
        return [(">=", low_v), ("<", bump(min(first, given - 1) + 1))]
    if op == "~":
        return [(">=", low_v), ("<", bump(max(min(given, 2), 1)))]
    if op == "~=":
        return [(">=", low_v), ("<", bump(max(given - 1, 1)))]
    return [("==" if op == "=" else op, low_v)]


def accepts(spec: str, version: Version) -> bool | None:
    """Whether ``version`` meets ``spec`` (``>=3.12,<3.14``, ``^3.12``, ``3.12.*``, ``>=3.9 || ^3.12``...); None
    when pdms cannot read it (then it says nothing)."""
    any_ok = False
    for alternative in spec.split("||"):
        alternative = re.sub(r"([<>=!~^]+)\s+", r"\1", alternative.strip())  # ">= 3.12" is ">=3.12"
        clauses = [clause for part in re.split(r"[,\s]+(?=[<>=!~^\d])", alternative) if part.strip()
                   for clause in [_clause(part)]]
        if not clauses or any(clause is None for clause in clauses):
            return None
        checks = [check for clause in clauses for check in clause]  # type: ignore[union-attr]
        compare = {">=": version.__ge__, ">": version.__gt__, "<=": version.__le__, "<": version.__lt__,
                   "==": version.__eq__, "!=": version.__ne__}
        if all(compare[op](bound) for op, bound in checks):
            any_ok = True
    return any_ok


def wanted(service: Path) -> str:
    """The Python the service asks for in its ``pyproject.toml`` ("" when it says nothing)."""
    try:
        data = installer._read_toml(service / "pyproject.toml")
    except (OSError, ValueError):
        return ""
    spec = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python") \
        or data.get("project", {}).get("requires-python") or ""
    return str(spec).strip()


def venv_version(service: Path) -> Version | None:
    """The Python of the service's ``.venv`` (from its ``pyvenv.cfg``), or None without one."""
    try:
        text = (service / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version_info", "version"):
            return parse_version(value)
    return None


# --------------------------------------------------------------------------- Pythons on this computer


def _version_of(program: str) -> Version | None:
    try:
        result = subprocess.run([program, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(result.stdout) if result.returncode == 0 else None


def _candidates() -> list[str]:
    """Pythons that may be installed: uv's, pyenv's and the ``python3.X`` on the PATH (``py`` on Windows)."""
    found: list[str] = []
    if uv := shutil.which("uv"):
        try:
            result = subprocess.run([uv, "python", "list", "--only-installed", "--output-format", "json"],
                                    capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            import json

            found += [str(item["path"]) for item in json.loads(result.stdout or "[]") if item.get("path")] \
                if result.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass
    pyenv = Path(os.environ.get("PYENV_ROOT") or Path.home() / ".pyenv") / "versions"
    if pyenv.is_dir():
        for folder in sorted(pyenv.iterdir()):
            python = folder / ("python.exe" if WINDOWS else "bin/python")
            if python.exists():
                found.append(str(python))
    for minor in MINORS:
        if path := shutil.which(f"python3.{minor}"):
            found.append(path)
    if WINDOWS and (launcher := shutil.which("py")):
        try:
            result = subprocess.run([launcher, "-0p"], capture_output=True, text=True, timeout=15)
            found += [line.split()[-1] for line in result.stdout.splitlines() if line.strip().endswith(".exe")]
        except (OSError, subprocess.SubprocessError):
            pass
    return list(dict.fromkeys(found))


_installed: tuple[float, list[tuple[Version, str]]] | None = None
REFRESH_EVERY = 60  # seconds: looking again costs a few processes


def installed(refresh: bool = False) -> list[tuple[Version, str]]:
    """``[(version, program)]`` of the Pythons found here, lowest first. Looked for once, and again with ``refresh``
    at most once a minute (pdms ui runs for days; the user may install one meanwhile)."""
    global _installed
    if _installed is None or (refresh and time.monotonic() - _installed[0] > REFRESH_EVERY):
        seen: dict[Version, str] = {}
        for program in _candidates():
            if (version := _version_of(program)) and version not in seen:
                seen[version] = program
        _installed = (time.monotonic(), sorted(seen.items()))
    return _installed[1]


def best_for(spec: str) -> tuple[Version, str] | None:
    """The lowest installed Python ``spec`` accepts (the Lambdas run the lowest supported one)."""
    return next(((version, program) for version, program in installed() if accepts(spec, version)), None)


@dataclass
class Mismatch:
    service: Path
    have: Version  # the .venv's
    wants: str  # the pyproject's
    use: tuple[Version, str] | None  # the Python it would be recreated with, None when none is installed

    def describe(self) -> dict:
        return {"service": self.service.name, "path": str(self.service), "have": show(self.have),
                "wants": self.wants, "use": show(self.use[0]) if self.use else "",
                "program": self.use[1] if self.use else ""}


def mismatch(service: Path) -> Mismatch | None:
    """The service's virtualenv runs a Python its project no longer accepts."""
    have = venv_version(service)
    spec = wanted(service)
    if not have or not spec or accepts(spec, have) is not False:
        return None
    use = best_for(spec)
    if use is None:  # maybe installed since pdms looked
        installed(refresh=True)
        use = best_for(spec)
    return Mismatch(service, have, spec, use)


def suggested(spec: str) -> str:
    """The lowest ``3.X`` that ``spec`` accepts, to tell what to install (``3.12`` for ``>=3.12,<3.14``)."""
    return next((f"3.{minor}" for minor in MINORS if accepts(spec, (3, minor, 0)) or accepts(spec, (3, minor, 99))),
                "3")
