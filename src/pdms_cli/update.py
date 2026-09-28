"""Checking and installing new releases published on GitHub."""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from . import __version__

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
    """Version of the latest release; with ``pre``, alpha/beta/rc releases count too."""
    if pre:
        with urllib.request.urlopen(f"{API_RELEASES}?per_page=30", timeout=timeout) as response:
            tags = [r["tag_name"] for r in json.load(response) if not r.get("draft")]
        if not tags:
            raise RuntimeError("no release found")
        return max(tags, key=parse_version).lstrip("v")
    # The redirect of /releases/latest skips pre-releases and has no API rate limit.
    request = urllib.request.Request(f"{RELEASES}/latest", method="HEAD")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
    match = re.search(r"/tag/v?([0-9][^/]*)$", final)
    if not match:
        raise RuntimeError(f"no release found ({final})")
    return match.group(1)


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
