"""Checking and installing new releases published on GitHub."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from . import __version__
from .instances import state_dir

REPO = "lianabeatriz93/pdms-cli"
RELEASES = f"https://github.com/{REPO}/releases"
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases"
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
STAGES = {"a": 0, "b": 1, "rc": 2}


def parse_version(text: str) -> tuple[int, ...]:
    """Sortable key for PEP 440 versions like 0.3.0, 0.3.0a1, 0.3.0rc2 (pre-releases sort before the final)."""
    match = VERSION.match(text.strip())
    if not match:
        return (0, 0, 0, -1, 0)
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch), STAGES[stage] if stage else 3, int(number or 0))


def is_prerelease(version: str) -> bool:
    match = VERSION.match(version.strip())
    return bool(match and match.group(4))


def latest_version(pre: bool = False, timeout: float = 10) -> str:
    """Version of the latest release; with ``pre``, alpha/beta/rc releases count too.

    Uses the GitHub API (one small JSON request, well within its 60 requests/hour limit for a daily check).
    """
    url = f"{API_RELEASES}?per_page=30" if pre else f"{API_RELEASES}/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    tags = [r["tag_name"] for r in data if not r.get("draft")] if pre else [data.get("tag_name", "")]
    tags = [t for t in tags if VERSION.match(t)]
    if not tags:
        raise RuntimeError("no release found")
    return max(tags, key=parse_version).lstrip("v")


def wheel_url(version: str) -> str:
    return f"{RELEASES}/download/v{version}/pdms_cli-{version}-py3-none-any.whl"


def install_kind() -> str:
    """``editable`` (a local checkout, updated with git), ``uv-tool`` or ``other``."""
    try:
        direct_url = distribution("pdms-cli").read_text("direct_url.json")
    except PackageNotFoundError:
        return "other"
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
        return "editable"
    return "uv-tool" if "uv" in Path(sys.prefix).parts and "tools" in Path(sys.prefix).parts else "other"


def has_desktop() -> bool:
    """Whether the desktop extra (``pdms ui --window``) is installed, so an update keeps it."""
    return importlib.util.find_spec("webview") is not None


def base_python() -> str:
    """The Python this pdms's environment was made with, for ``--python``; else its version, for uv to find.

    Without ``--python`` uv takes the first Python it finds, which may be too old (macOS's /usr/bin/python3 is 3.9)
    and is often another one when pdms ui was opened from the app menu, with a shorter PATH."""
    name = "python.exe" if sys.platform == "win32" else f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [Path(sys.executable).resolve()]
    try:
        config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
        home = re.search(r"^home\s*=\s*(.+?)\s*$", config, re.MULTILINE)
        if home:
            candidates.insert(0, Path(home.group(1)) / name)
    except OSError:
        pass
    for candidate in candidates:
        if candidate.is_file() and Path(sys.prefix).resolve() not in candidate.parents:
            return str(candidate)
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def upgrade_command(version: str) -> list[str]:
    source = f"pdms-cli[desktop] @ {wheel_url(version)}" if has_desktop() else wheel_url(version)
    return [shutil.which("uv") or "uv", "tool", "install", "--force", "--python", base_python(), source]


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def updates_itself() -> bool:
    """Whether ``pdms self-update`` installs the new version here (uv tool), rather than showing a command."""
    return install_kind() == "uv-tool" and shutil.which("uv") is not None


def installed_version() -> str:
    """The version installed on disk now, which differs from the running one after an update from elsewhere.

    A local checkout's is its pyproject.toml, as for ``__version__`` (the editable install's metadata is stale)."""
    from . import _checkout_version

    try:
        return _checkout_version() or distribution("pdms-cli").version
    except PackageNotFoundError:
        return __version__


def release_notes(target: str, current: str = __version__, timeout: float = 10) -> list[dict]:
    """The notes of every release after ``current`` up to ``target``, newest first: ``{version, url, body}``."""
    request = urllib.request.Request(f"{API_RELEASES}?per_page=30", headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    notes = []
    for release in data:
        tag = str(release.get("tag_name", ""))
        if release.get("draft") or not VERSION.match(tag):
            continue
        version = tag.lstrip("v")
        if is_newer(version, current) and not is_newer(version, target):
            notes.append({"version": version, "url": release.get("html_url", ""), "body": release.get("body") or ""})
    return sorted(notes, key=lambda note: parse_version(note["version"]), reverse=True)


# --------------------------------------------------------------------------- Windows

# On Windows a running pdms.exe (and the python.exe of its environment) cannot be replaced, so the update runs in a
# PowerShell of its own once this pdms has exited. Its values come in environment variables (PDMS_UPDATE_*).
WINDOWS_HELPER = r"""
param([int]$WaitPid)
$ErrorActionPreference = "Continue"
Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
$log = $env:PDMS_UPDATE_LOG
if ($log) {
    $p = Start-Process -FilePath $env:PDMS_UPDATE_EXE -ArgumentList $env:PDMS_UPDATE_ARGS -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
    $code = $p.ExitCode
    Get-Content "$log.out", "$log.err" -ErrorAction SilentlyContinue | Add-Content -Path $log -Encoding UTF8
    Remove-Item "$log.out", "$log.err" -ErrorAction SilentlyContinue
    Add-Content -Path $log -Encoding UTF8 -Value "# exit code $code"
} else {
    $argv = $env:PDMS_UPDATE_ARGV | ConvertFrom-Json
    & $env:PDMS_UPDATE_EXE @argv
    $code = $LASTEXITCODE
}
if ($env:PDMS_UPDATE_RELAUNCH) {
    Start-Process -FilePath $env:PDMS_UPDATE_RELAUNCH -ArgumentList $env:PDMS_UPDATE_RELAUNCH_ARGS
}
if (-not $log) {
    if ($code -eq 0) { Write-Host "pdms was updated." } else { Write-Host "The update failed (exit code $code)." }
    Read-Host "Press Enter to close this window"
}
exit $code
"""


def windows_helper_path() -> Path:
    return state_dir() / "update.ps1"


def spawn_windows_update(
    cmd: list[str], *, log: Path | None = None, relaunch: list[str] | None = None, env: dict[str, str] | None = None,
) -> None:
    """Run ``cmd`` after this process exits, in a console of its own (``log`` None) or hidden, writing to ``log``;
    then start ``relaunch``. The caller must exit right away."""
    windows_helper_path().parent.mkdir(parents=True, exist_ok=True)
    windows_helper_path().write_text(WINDOWS_HELPER, encoding="utf-8-sig")  # PowerShell 5.1 needs the BOM for UTF-8
    exe, *args = cmd
    values = {
        "PDMS_UPDATE_EXE": exe, "PDMS_UPDATE_ARGS": subprocess.list2cmdline(args), "PDMS_UPDATE_ARGV": json.dumps(args),
        "PDMS_UPDATE_LOG": str(log or ""), "PDMS_UPDATE_RELAUNCH": relaunch[0] if relaunch else "",
        "PDMS_UPDATE_RELAUNCH_ARGS": subprocess.list2cmdline(relaunch[1:]) if relaunch else "",
    }
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0) if log else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(windows_helper_path()),
         "-WaitPid", str(os.getpid())],
        env={**os.environ, **(env or {}), **values}, creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL if log else None, stderr=subprocess.DEVNULL if log else None,
    )


# --------------------------------------------------------------------------- new version notice

CHECK_INTERVAL = 24 * 3600  # seconds between checks, and between notices


def cache_path() -> Path:
    return state_dir() / "update-check.json"


def load_cache() -> dict:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(data: dict) -> None:
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_text(json.dumps(data), encoding="utf-8")


def checks_enabled(update_check: bool) -> bool:
    """Whether to look for new versions at all: the ``update_check`` default, not in CI, not a local checkout."""
    return (update_check and not os.environ.get("CI") and not os.environ.get("PDMS_NO_UPDATE_CHECK")
            and install_kind() != "editable")


def check_due(pre: bool, now: float | None = None) -> bool:
    cache = load_cache()
    now = time.time() if now is None else now
    return cache.get("pre") != pre or now - float(cache.get("checked_at", 0)) >= CHECK_INTERVAL


def refresh(pre: bool, timeout: float = 3) -> None:
    """Store the latest version in the cache; network problems are ignored (it is only a hint)."""
    try:
        check_now(pre, timeout)
    except Exception:  # noqa: BLE001 - offline, rate limited, GitHub down...
        return


def check_now(pre: bool, timeout: float = 10) -> str:
    """Ask GitHub for the latest version and store it in the cache; raises when GitHub cannot be reached."""
    latest = latest_version(pre=pre, timeout=timeout)
    cache = load_cache()
    cache.update({"checked_at": time.time(), "latest": latest, "pre": pre})
    save_cache(cache)
    return latest


def cached_latest(pre: bool) -> tuple[str, float]:
    """The latest version the last check found for this channel ("" if none), and when it checked."""
    cache = load_cache()
    if cache.get("pre") != pre:
        return "", 0.0
    return str(cache.get("latest") or ""), float(cache.get("checked_at") or 0)


def notice_due(pre: bool, current: str = __version__, now: float | None = None) -> str | None:
    """The newer version to announce, at most once per ``CHECK_INTERVAL``; marks it as announced."""
    cache = load_cache()
    now = time.time() if now is None else now
    latest = cache.get("latest")
    if not latest or cache.get("pre") != pre or not is_newer(latest, current):
        return None
    if cache.get("notified") == latest and now - float(cache.get("notified_at", 0)) < CHECK_INTERVAL:
        return None
    cache.update({"notified": latest, "notified_at": now})
    save_cache(cache)
    return latest
