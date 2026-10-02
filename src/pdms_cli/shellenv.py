"""The environment of the user's shell, for pdms ui opened from the app menu or at login (Linux and macOS).

A desktop session does not run ``~/.bashrc`` / ``~/.zshrc``, where pyenv, nvm, Poetry and the like usually add
themselves to ``PATH``; without them pdms would not find the tools a terminal finds. So pdms asks the user's
interactive login shell for its environment, once, and takes its ``PATH`` and the variables it does not have yet.
Startup files may hang without a terminal (a completion script waiting for one), so the folders where those tools
usually live are added too when they exist, whatever the shell said.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 5  # seconds: a shell startup that hangs (a prompt, a completion script, a network mount) must not stop pdms ui
SKIP = {"PWD", "OLDPWD", "SHLVL", "_", "PS1", "TERM", "COLUMNS", "LINES"}


def read(shell: str | None = None, timeout: float = TIMEOUT) -> dict[str, str]:
    """The environment ``shell`` (``$SHELL``) ends up with as an interactive login shell; {} if it cannot tell.

    It goes to a file, not a pipe: something the startup files leave running in the background may keep a pipe open
    for ever, and the startup files' own output is not mixed in."""
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    with tempfile.TemporaryDirectory(prefix="pdms-env-") as folder:
        target = Path(folder) / "env"
        try:
            subprocess.run([shell, "-ilc", 'env -0 > "$PDMS_ENV_FILE"'], timeout=timeout,
                           env={**os.environ, "PDMS_ENV_FILE": str(target)}, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out = target.read_bytes()
        except (OSError, subprocess.SubprocessError):
            return {}
    found = {}
    for item in out.split(b"\0"):
        key, sep, value = item.decode("utf-8", "replace").partition("=")
        if sep and key and "\n" not in key and key != "PDMS_ENV_FILE":
            found[key] = value
    return found


def known_folders(home: Path | None = None, environ: dict[str, str] | None = None) -> list[Path]:
    """Where pyenv, uv, pipx, cargo, nvm, Homebrew and asdf put their commands, if they are on this computer."""
    home = home or Path.home()
    environ = os.environ if environ is None else environ  # type: ignore[assignment]
    pyenv = Path(environ.get("PYENV_ROOT") or home / ".pyenv")
    folders = [pyenv / "shims", pyenv / "bin"]
    nvm = Path(environ.get("NVM_DIR") or home / ".nvm")
    default = nvm / "alias" / "default"
    if default.is_file():
        wanted = default.read_text(encoding="utf-8", errors="replace").strip().lstrip("v")
        versions = sorted((nvm / "versions" / "node").glob(f"v{wanted}*"), reverse=True)
        folders += [version / "bin" for version in versions[:1]]
    folders += [home / ".local" / "bin", home / ".cargo" / "bin", home / ".asdf" / "shims",
                Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return [folder for folder in folders if folder.is_dir()]


def adopt(environ: dict[str, str] | None = None, found: dict[str, str] | None = None,
          home: Path | None = None) -> list[str]:
    """Take the shell's ``PATH`` and the variables missing from ``environ`` (os.environ) from ``found`` (None: none),
    then add the known tool folders it still lacks; the names changed."""
    if sys.platform == "win32":
        return []
    environ = os.environ if environ is None else environ  # type: ignore[assignment]
    changed = _take(environ, found) if found is not None else []
    path = environ.get("PATH", "").split(os.pathsep)
    missing = [str(folder) for folder in known_folders(home, environ) if str(folder) not in path]
    if missing:
        environ["PATH"] = os.pathsep.join(missing + [p for p in path if p])
        changed.append("PATH")
    pyenv = Path(environ.get("PYENV_ROOT") or (home or Path.home()) / ".pyenv")
    if "PYENV_ROOT" not in environ and (pyenv / "bin").is_dir():
        environ["PYENV_ROOT"] = str(pyenv)
        changed.append("PYENV_ROOT")
    return sorted(set(changed))


def adopt_in_background() -> list[str]:
    """:func:`adopt` the known folders now, and the shell's environment once it answers (startup files can take
    seconds), so pdms ui does not wait for it."""
    import threading

    changed = adopt(found={})
    threading.Thread(target=lambda: _take(os.environ, read()), name="pdms-shell-env", daemon=True).start()
    return changed


def _take(environ: dict[str, str], found: dict[str, str]) -> list[str]:
    changed = []
    for key, value in found.items():
        if key in SKIP or key.startswith("PDMS_UI_"):
            continue
        if (key == "PATH" or key not in environ) and environ.get(key) != value:
            environ[key] = value
            changed.append(key)
    return changed
