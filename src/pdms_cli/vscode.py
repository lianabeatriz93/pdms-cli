"""VS Code debug configurations (``.vscode/launch.json``) for services.

Secrets never go into launch.json: each configuration points to an ``envFile`` kept in pdms' state dir
with ``0600`` permissions, since ``.vscode`` usually is not git-ignored.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .i18n import _
from .instances import detach_options, state_dir

NAME_PREFIX = "pdms: "


def env_file_path(service: Path) -> Path:
    return state_dir() / "env" / f"{service.name}.env"


def write_env_file(service: Path, env: dict[str, str]) -> Path:
    path = env_file_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{key}="{value}"' for key, value in env.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def workspace_root(service: Path) -> Path:
    """Git root of the service (what is usually opened in VS Code), falling back to the service itself."""
    for path in (service, *service.parents):
        if (path / ".git").exists():
            return path
    return service


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, leaving string contents intact."""
    out, i, in_str = [], 0, False
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1 : i + 2])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_launch(path: Path) -> tuple[dict, bool]:
    """Parsed launch.json and whether comments had to be dropped to read it."""
    if not path.exists():
        return {"version": "0.2.0", "configurations": []}, False
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(text)), True


def upsert_configuration(
    service: Path, *, python: Path, env_file: Path, host: str, port: int, description: str,
    program: list[str] | None = None,
) -> tuple[Path, str, Path | None]:
    """Add or replace this service's pdms configuration. Returns (launch.json, config name, backup path)."""
    root = workspace_root(service)
    launch = root / ".vscode" / "launch.json"
    data, had_comments = load_launch(launch)
    configs = data.setdefault("configurations", [])
    base = f"{NAME_PREFIX}{service.name}"
    name = f"{base} · {description}"
    try:
        cwd = "${workspaceFolder}/" + service.relative_to(root).as_posix()
    except ValueError:
        cwd = str(service)
    config = {
        "name": name,
        "type": "debugpy",
        "request": "launch",
        **({"program": program[0], "args": program[1:]} if program else
           {"module": "uvicorn", "args": ["main:app", "--host", host, "--port", str(port)]}),
        "cwd": cwd,
        "python": str(python),
        "envFile": str(env_file),
        "justMyCode": False,
        "console": "integratedTerminal",
        "presentation": {"group": "pdms"},
    }
    same = lambda c: c.get("name") == base or str(c.get("name", "")).startswith(f"{base} · ")  # noqa: E731
    index = next((i for i, c in enumerate(configs) if same(c)), None)
    if index is None:
        configs.append(config)
    else:
        configs[index] = config

    backup = None
    if had_comments:
        backup = launch.with_suffix(".json.bak")
        shutil.copy2(launch, backup)
    launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return launch, name, backup


# --------------------------------------------------------------------------- opening VS Code

# Where the installers put the ``code`` command when "Shell Command: Install 'code' command in PATH" was never run.
CODE_FALLBACKS = {
    "darwin": ["/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"],
    "win32": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"),
              os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd")],
}


def code_command() -> str | None:
    """VS Code's ``code`` command, or None when it is not installed."""
    found = shutil.which("code")
    if found:
        return found
    return next((path for path in CODE_FALLBACKS.get(sys.platform, []) if Path(path).is_file()), None)


def open_in_code(folder: Path, file: Path | None = None, line: int = 0) -> None:
    """Open ``folder`` in VS Code (its window if already open) and, if given, ``file`` at ``line``."""
    code = code_command()
    if not code:
        raise RuntimeError(_("VS Code's code command was not found. In VS Code: Ctrl+Shift+P → "
                             "Shell Command: Install 'code' command in PATH."))
    cmd = [code, str(folder)]
    if file:
        cmd += ["-g", f"{file}:{line}" if line else str(file)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detach_options())
