"""``pdms bug-report``: one text file with what it takes to understand a problem on someone else's computer.

The versions and the system, the checks of ``pdms doctor``, the defaults, the services pdms runs and the end of the
logs that matter (pdms ui, the proxy, the last update, the frontend and every service that failed or stopped). It
never holds passwords: the configured database passwords, pdms ui's token, credentials inside URLs and anything
written as ``password=``/``secret=``/``token=`` are replaced with ``***``, and the extra environment variables keep
only their names. It is meant to be read before sending: plain text, nothing hidden.
"""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import __version__, doctor, frontend, instances, proxy, update, wsl
from .config import Config, config_path
from .ui import instance as ui_instance
from .ui import updates as ui_updates

LOG_LINES = 120  # of each log
HIDDEN = "***"
SECRET_PATTERNS = [
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), rf"\g<1>{HIDDEN}@"),  # postgresql://user:password@host
    (re.compile(r"(?i)\b((?:[a-z_]*_)?(?:password|passwd|pwd|secret|token|api_key)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;]+"),
     rf"\g<1>{HIDDEN}"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), HIDDEN),  # AWS access key ids
]


def redact(text: str, secrets: list[str]) -> str:
    """``secrets`` hidden wherever they show as a whole word; a short one (a local database's "local") only where the
    patterns find it, or every "local" of the report would go too."""
    for secret in sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(secret)}(?!\w)", HIDDEN, text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.rstrip()}\n"


def _system(cfg: Config) -> str:
    lines = [
        f"pdms: {__version__} ({update.install_kind()}), installed {update.installed_version()}",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"System: {platform.platform()}",
        f"WSL: {wsl.distro() or 'yes'}" if wsl.detected() else "WSL: no",
        f"Language: {cfg.defaults.language} · update channel: {cfg.defaults.update_channel}"
        + (f" · skipped: {update.skipped()}" if update.skipped() else ""),
        f"Configuration: {config_path()}",
    ]
    return "\n".join(lines)


def _doctor(cfg: Config, databases: bool) -> str:
    checks = doctor.run_all(cfg, databases=databases)
    lines = [f"[{c.status}] {c.section} · {c.name}: {c.detail}" + (f"  → {c.hint}" if c.hint and c.status != doctor.OK
                                                                     else "") for c in checks]
    return "\n".join(lines)


def _settings(cfg: Config) -> str:
    defaults = asdict(cfg.defaults)
    defaults["env"] = sorted(defaults.get("env") or {})  # the names only: their values may be secrets
    lines = [f"Repos: {len(cfg.repos)} · databases: {', '.join(cfg.dbs) or '-'} · users: {len(cfg.users)}"]
    lines += [f"{key} = {value!r}" for key, value in defaults.items()]
    return "\n".join(lines)


def _services() -> tuple[str, list[tuple[str, Path]]]:
    """The services pdms knows of, and the logs worth reading: of the ones that failed or stopped."""
    found = instances.load()
    if not found:
        return "None.", []
    healths = instances.health_all(list(found.values()))
    lines, logs = [], []
    for key, inst in sorted(found.items()):
        health = healths.get(key) or instances.Health("?")
        detail = f" ({health.detail})" if health.detail else ""
        lines.append(f"{key}: {health.state}{detail} · user {inst.user} · db {inst.db} · since {inst.started_at}"
                     + (" · on demand" if inst.on_demand else ""))
        if health.state in ("error", "stopped") and inst.log:
            logs.append((key, Path(inst.log)))
    return "\n".join(lines), logs


def _logs(extra: list[tuple[str, Path]]) -> str:
    known = [("pdms ui", ui_instance.log_path()), ("proxy", proxy.log_path()), ("update", ui_updates.log_path()),
             ("frontend", frontend.log_path())]
    parts = []
    for name, path in known + extra:
        if not path.is_file():
            continue
        parts.append(f"### {name} ({path})\n\n```\n{instances.tail(str(path), LOG_LINES).rstrip()}\n```\n")
    return "\n".join(parts) or "None."


def build(cfg: Config, *, databases: bool = False) -> str:
    services, failed_logs = _services()
    text = "\n".join([
        f"# pdms bug report · {datetime.now():%Y-%m-%d %H:%M:%S}\n",
        _section("pdms and the system", _system(cfg)),
        _section("Doctor", _doctor(cfg, databases)),
        _section("Defaults", _settings(cfg)),
        _section("Services", services),
        _section("Logs (the last lines)", _logs(failed_logs)),
    ])
    token = str((ui_instance.load() or {}).get("token") or "")
    return redact(text, [db.password for db in cfg.dbs.values() if db.password] + [token])


def default_folder() -> Path:
    """Where people look for a file they just made: Downloads (Windows' own inside WSL)."""
    if wsl.detected() and (profile := wsl.windows_dir("USERPROFILE")) and (profile / "Downloads").is_dir():
        return profile / "Downloads"
    home = Path.home()
    return next((path for path in (home / "Downloads", home / "Descargas") if path.is_dir()), home)


def write(cfg: Config, folder: Path | None = None, *, databases: bool = False) -> Path:
    folder = folder or default_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"pdms-bug-report-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(build(cfg, databases=databases), encoding="utf-8")
    return path
