"""Console entry point: a missing dependency gets a hint instead of a traceback."""

from __future__ import annotations

import sys


def main() -> None:
    try:
        from .cli import _entrypoint
    except ModuleNotFoundError as exc:
        print(
            f"pdms: the dependency '{exc.name}' is missing (it was probably added by an update).\n"
            "  Installed from a local checkout: uv tool install -e <path to pdms-cli> --force\n"
            "  Installed with the installer:    run the installer again (https://github.com/lianabeatriz93/pdms-cli#installation)",
            file=sys.stderr,
        )
        sys.exit(1)
    _entrypoint()
