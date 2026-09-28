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


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def latest_version(timeout: float = 10) -> str:
    """Version of the latest release, from the redirect of /releases/latest (no API rate limits)."""
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
