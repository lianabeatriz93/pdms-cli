"""Checking and installing new releases published on GitHub."""

from __future__ import annotations

import json
import re
import shutil
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


def upgrade_command(version: str) -> list[str]:
    return [shutil.which("uv") or "uv", "tool", "install", "--force", wheel_url(version)]


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


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


def check_due(pre: bool, now: float | None = None) -> bool:
    cache = load_cache()
    now = time.time() if now is None else now
    return cache.get("pre") != pre or now - float(cache.get("checked_at", 0)) >= CHECK_INTERVAL


def refresh(pre: bool, timeout: float = 3) -> None:
    """Store the latest version in the cache; network problems are ignored (it is only a hint)."""
    try:
        latest = latest_version(pre=pre, timeout=timeout)
    except Exception:  # noqa: BLE001 - offline, rate limited, GitHub down...
        return
    cache = load_cache()
    cache.update({"checked_at": time.time(), "latest": latest, "pre": pre})
    save_cache(cache)


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
