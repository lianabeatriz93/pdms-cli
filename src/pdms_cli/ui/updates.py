"""New pdms versions in pdms ui: the daily check, the release notes, and updating and restarting from the page.

The check is the CLI's (same cache, once a day, off with ``update_check``). Updating runs what ``pdms self-update``
runs and then restarts pdms ui on the same port and with the same token, so the page and the window come back by
themselves. The proxy runs on pdms's own code, so it is stopped and started again by the new pdms ui (the
``ui-after-update.json`` note, which also tells it whether the update went through). On Windows, where a running
pdms cannot replace its own files, a PowerShell helper installs it once pdms ui has exited and then opens it again.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .. import __version__, actions, instances, proxy, update
from ..config import Config
from ..i18n import _
from . import instance as ui_instance
from .control import Control

KEY = "update"  # its job, and its log in the logs panel
CHECK_EVERY = 3600  # seconds between looks at whether a daily check is due


def log_path() -> Path:
    return instances.log_path(KEY)


def channel() -> bool:
    """Pre-releases count when this pdms is one."""
    return update.is_prerelease(__version__)


def state(cfg: Config) -> dict:
    """What the page needs: the versions (running, on disk, latest known) and how this install updates."""
    enabled = update.checks_enabled(cfg.defaults.update_check)
    latest, checked_at = update.cached_latest(channel())
    kind = update.install_kind()
    running = proxy.running_proxy()
    return {
        "current": __version__,
        "installed": update.installed_version(),
        "latest": latest if kind != "editable" and latest and update.is_newer(latest) else "",
        "checks": enabled,
        "checked_at": datetime.fromtimestamp(checked_at).astimezone().isoformat(timespec="seconds") if checked_at else "",
        "kind": kind,
        "updates_itself": update.updates_itself(),
        "command": command_text(latest) if latest else "",
        "proxy": ({"port": running["port"], "background": bool(running.get("background"))} if running else None),
    }


def command_text(version: str) -> str:
    cmd = update.upgrade_command(version)
    return subprocess.list2cmdline(cmd) if sys.platform == "win32" else shlex.join(cmd)


def check_now() -> dict:
    """Ask GitHub now (Settings → Check now); :class:`actions.ActionError` when it cannot be reached."""
    try:
        latest = update.check_now(channel())
    except Exception as exc:  # noqa: BLE001 - network errors of any kind
        raise actions.ActionError(_("Could not reach GitHub: {error}", error=exc)) from exc
    return {"latest": latest if update.is_newer(latest) else "", "current": __version__}


def notes(target: str) -> list[dict]:
    try:
        return update.release_notes(target)
    except Exception as exc:  # noqa: BLE001
        raise actions.ActionError(_("Could not reach GitHub: {error}", error=exc)) from exc


def watch(cfg_loader: Callable[[], Config], on_change: Callable[[], None], stopped: threading.Event) -> None:
    """Refresh the cached latest version when the daily check is due, for as long as pdms ui runs."""
    while not stopped.is_set():
        try:
            if update.checks_enabled(cfg_loader().defaults.update_check) and update.check_due(channel()):
                update.refresh(channel(), timeout=10)
                on_change()
        except Exception:  # noqa: BLE001 - only a hint: never stop pdms ui for it
            pass
        stopped.wait(CHECK_EVERY)


# --------------------------------------------------------------------------- updating


def proxy_relaunch(running: dict) -> dict:
    """How to start the background proxy again exactly as it runs now."""
    return {key: running.get(key) for key in ("repo", "port", "env", "remote", "as", "timeout")}


def start_proxy_again(saved: dict) -> None:
    """Start the proxy as it ran before the update (``frontend/.env.local`` still points to it)."""
    if proxy.running_proxy():
        return
    cmd = proxy.background_command(
        Path(saved["repo"]), port=int(saved["port"]), env=saved.get("env") or "dev",
        remote=saved.get("remote") or None, user_name=saved.get("as") or None, timeout=int(saved.get("timeout") or 300),
    )
    env = {key: value for key, value in os.environ.items() if key != ui_instance.TOKEN_ENV}
    started = instances.spawn(cmd, Path(saved["repo"]), env, proxy.log_path())
    if actions.wait_for_proxy(actions.ProxyStarted(started.pid, int(saved["port"]), proxy.log_path())) == "stopped":
        last = instances.tail(str(proxy.log_path()), 1).strip()
        raise actions.ActionError(_("The proxy exited while starting: {line}", line=last or "?"))


def install(control: Control, target: str, restart_proxy: bool, report: Callable[[str], None]) -> None:
    """Update to ``target`` and restart pdms ui (the job's work; ``report(phase)`` shows how it goes)."""
    cmd = update.upgrade_command(target)
    running = proxy.running_proxy()
    after: dict = {"from": __version__, "to": target, "proxy": None}
    # A proxy in a terminal of its own is left alone; a background one comes back with the new version.
    if running and running.get("background") and (restart_proxy or sys.platform == "win32"):
        after["proxy"] = proxy_relaunch(running)
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(f"# pdms {datetime.now():%Y-%m-%d %H:%M:%S} · {__version__} → {target} · {shlex.join(cmd)}\n")
    if sys.platform == "win32":
        if after["proxy"]:
            report("stopping proxy")
            proxy.stop(running)
        ui_instance.save_after_update(after)
        update.spawn_windows_update(cmd, log=log, relaunch=control.relaunch, env={ui_instance.TOKEN_ENV: control.token})
        control.quit()
        return
    report(f"installing pdms {target}")
    with open(log, "a", encoding="utf-8", errors="replace") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise actions.ActionError(_("The update failed (exit code {code}); its log says why.", code=result.returncode))
    if after["proxy"]:
        report("stopping proxy")
        proxy.stop(running)
    ui_instance.save_after_update(after)
    report("restarting")
    control.quit(restart=True)


def after_restart(note: dict | None) -> tuple[str, dict | None]:
    """What the restarted pdms ui found: an error if the update did not happen, and the proxy to start again."""
    if not note:
        return "", None
    error = ""
    if note.get("to") and note["to"] != __version__:
        error = _("The update to {version} did not finish (pdms {current} is still installed); its log says why.",
                  version=note["to"], current=__version__)
    return error, note.get("proxy")
