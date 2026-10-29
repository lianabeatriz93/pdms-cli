"""Whether the CI's installer jobs have to run: whether something they try changed between BASE and HEAD.

They install the built wheel with install.sh / install.ps1, run ``pdms --version`` and ``--help``, and add and remove
the app menu entry. So they depend on installer/, on what pyproject.toml says about the package (its dependencies,
scripts, Python, build), on the code of the menu entries and the launcher, and on their own job in ci.yml. Not on
uv.lock (``uv tool install`` of a wheel resolves on its own), the dev dependencies or the rest of the code, which the
tests cover. macOS runners can queue for a long time: they wait only when it matters.

Usage: python installers_changed.py <base>; prints the reasons and ends with ``installers=true`` or ``false``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib

PATHS = (
    "installer/",
    "src/pdms_cli/desktop.py",
    "src/pdms_cli/installer.py",
    "src/pdms_cli/launcher.py",
    "src/pdms_cli/commands/ui.py",
)
WORKFLOW = ".github/workflows/ci.yml"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def show(ref: str, path: str) -> str:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return ""


def packaging(text: str) -> dict:
    """What pyproject.toml says about the package the installers install."""
    data = tomllib.loads(text) if text else {}
    project = data.get("project", {})
    keys = ("dependencies", "optional-dependencies", "scripts", "gui-scripts", "requires-python")
    return {**{key: project.get(key) for key in keys},
            "build-system": data.get("build-system"), "hatch": data.get("tool", {}).get("hatch")}


def installers_job(text: str) -> str:
    """The installers job of ci.yml, as text: from its key to the next job's."""
    match = re.search(r"^  installers:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.M | re.S)
    return match.group(0) if match else ""


def reasons(base: str, head: str = "HEAD") -> list[str]:
    files = git("diff", "--name-only", base, head).split()
    found = [path for path in files if path.startswith(PATHS)]
    if "pyproject.toml" in files and packaging(show(base, "pyproject.toml")) != packaging(show(head, "pyproject.toml")):
        found.append("pyproject.toml (the package)")
    if WORKFLOW in files and installers_job(show(base, WORKFLOW)) != installers_job(show(head, WORKFLOW)):
        found.append(f"{WORKFLOW} (the installers job)")
    return found


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else ""
    if not base or set(base) == {"0"}:  # a run by hand, or a new branch with nothing to compare to
        print("installers=true")
        return
    found = reasons(base)
    for reason in found:
        print(f"  {reason}")
    print(f"installers={'true' if found else 'false'}")


if __name__ == "__main__":
    main()
