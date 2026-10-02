"""Console entry point: a missing dependency gets a hint instead of a traceback."""

from __future__ import annotations

import sys


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        # Redirected to a file on Windows the encoding is cp1252, which has no ✓: replace what it cannot write.
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if sys.stdout is None or sys.stderr is None:
        # pdmsw.exe (the app menu shortcut on Windows) has no console: write to pdms logs ui instead.
        try:
            from .ui.instance import redirect_output

            redirect_output()
        except Exception:  # noqa: BLE001 - a missing dependency is reported below, and there is nowhere to show it
            pass
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
