"""pdms inside WSL (Windows Subsystem for Linux): services, repos and tools live in Linux, the desktop in Windows.

Everything that runs keeps working as on Linux. What reaches the desktop must go to Windows instead: the browser
(``explorer.exe`` opens a URL in the default one), the browsers the extension is installed in (``C:\\Program
Files``, the user's ``AppData``), a folder those browsers can open (in the user's ``AppData\\Local``) and the
certificate the web app is served with (mkcert's CA, trusted in Linux only, see :func:`certificate`). WSL2 forwards
``127.0.0.1`` to Windows, so the browser reaches pdms ui, the proxy and the web app as usual.

Windows' programs are called through WSL's interop (``cmd.exe``, ``wslpath``); each lookup is kept once found.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

WINDOWS_TOOLS = Path("/mnt/c/Windows/System32")
TIMEOUT = 15
MKCERT_CA = Path.home() / ".vite-plugin-mkcert" / "rootCA.pem"  # the web app's vite-plugin-mkcert


@functools.lru_cache(maxsize=1)
def detected() -> bool:
    """Whether pdms runs inside WSL."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME", "")


def _tool(name: str) -> str | None:
    """A Windows program (cmd.exe, explorer.exe, certutil.exe): on the PATH WSL gives, or in System32."""
    found = shutil.which(name)
    if found:
        return found
    candidate = WINDOWS_TOOLS / name
    return str(candidate) if candidate.exists() else None


def _run(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                                cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)  # cmd.exe dislikes \\wsl$ cwds
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def to_windows(path: Path) -> str:
    """``/mnt/c/Users/ana`` → ``C:\\Users\\ana``; a Linux path → ``\\\\wsl.localhost\\<distro>\\...``."""
    text = str(path)
    match = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", text)
    if match:
        return f"{match.group(1).upper()}:" + (match.group(2) or "\\").replace("/", "\\")
    if wslpath := shutil.which("wslpath"):
        if converted := _run([wslpath, "-w", text]):
            return converted
    return f"\\\\wsl.localhost\\{distro()}" + text.replace("/", "\\")


def to_linux(path: str) -> Path:
    """``C:\\Users\\ana`` → ``/mnt/c/Users/ana``."""
    match = re.match(r"^([a-zA-Z]):[\\/]?(.*)$", path.strip())
    if match:
        return Path("/mnt") / match.group(1).lower() / match.group(2).replace("\\", "/")
    return Path(path.strip())


@functools.lru_cache(maxsize=8)
def windows_variable(name: str) -> str:
    """A Windows environment variable of the user (USERPROFILE, LOCALAPPDATA, ProgramFiles...), "" if unknown."""
    cmd = _tool("cmd.exe")
    if not cmd:
        return ""
    value = _run([cmd, "/d", "/c", f"echo %{name}%"]) or ""
    return "" if value == f"%{name}%" else value.splitlines()[-1].strip() if value else ""


def windows_dir(name: str) -> Path | None:
    """The Linux path of a Windows folder variable (``LOCALAPPDATA`` → ``/mnt/c/Users/ana/AppData/Local``)."""
    value = windows_variable(name)
    path = to_linux(value) if value else None
    return path if path and path.is_dir() else None


def open_url(url: str) -> bool:
    """Open ``url`` in the user's default Windows browser."""
    for opener in (shutil.which("wslview"), _tool("explorer.exe")):
        if not opener:
            continue
        try:
            subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True  # explorer.exe exits with 1 even when it opened it
        except OSError:
            continue
    return False


def on_windows_drive(path: Path) -> bool:
    """A path on a Windows drive (``/mnt/c/...``): Linux tools see it through a slow bridge."""
    return bool(re.match(r"^/mnt/[a-zA-Z](/|$)", str(path)))


# --------------------------------------------------------------------------- the web app's certificate


def _thumbprint(pem: Path) -> str | None:
    """The SHA-1 of a certificate, as Windows names it in its stores."""
    try:
        der = ssl.PEM_cert_to_DER_cert(pem.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    return hashlib.sha1(der).hexdigest()  # noqa: S324 - the name Windows gives a certificate, not a hash for security


def certificate() -> tuple[str, str]:
    """Whether Windows trusts the CA of the web app's certificate: ("ok" | "untrusted" | "unknown", the CA's
    Windows path). Without it, the Windows browser warns about https://localhost:3000."""
    if not MKCERT_CA.is_file():
        return "unknown", ""
    windows = to_windows(MKCERT_CA)
    certutil, thumb = _tool("certutil.exe"), _thumbprint(MKCERT_CA)
    if not certutil or not thumb:
        return "unknown", windows
    return ("ok" if _run([certutil, "-user", "-store", "Root", thumb]) is not None else "untrusted"), windows


def trust_command() -> list[str]:
    """Add the web app's CA to the Windows user's trusted roots (Windows asks to confirm; no admin needed)."""
    return [_tool("certutil.exe") or "certutil.exe", "-user", "-addstore", "Root", to_windows(MKCERT_CA)]
