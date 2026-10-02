"""pdms ui in the system: an entry in the app menu (``pdms ui --install``), opening it at login, and notifications.

Linux gets a ``.desktop`` file and its icon under ``~/.local/share``, and another one in ``~/.config/autostart`` to
open at login. Windows gets shortcuts in the Start menu and in its Startup folder, made with PowerShell (no extra
package) and pointing to ``pdmsw.exe``, the same pdms without a console window. macOS gets a small
``~/Applications/pdms.app`` that runs pdms, and a LaunchAgent to open at login.

Every entry runs the ``pdms`` that uv puts on the PATH (``~/.local/bin``), which ``pdms self-update`` keeps in place.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .instances import state_dir

APP_NAME = "pdms"
APP_ID = "com.alivi.pdms"
COMMENT = "Run PDMS services locally"
STATIC = resources.files("pdms_cli.ui") / "static"


# --------------------------------------------------------------------------- what the entries run


def pdms_executable(gui: bool = False) -> Path:
    """This ``pdms`` (``pdmsw`` on Windows when ``gui``: no console window), else the one on the PATH."""
    running = Path(sys.argv[0])
    found = shutil.which("pdms")
    if running.stem in ("pdms", "pdmsw") and running.is_file():
        exe = running.absolute()
    else:
        exe = Path(found) if found else running.absolute()
    if exe.stem == "pdmsw":
        exe = exe.with_name(exe.name.replace("pdmsw", "pdms"))
    if gui and sys.platform == "win32" and (windowless := exe.with_name("pdmsw.exe")).is_file():
        return windowless
    return exe


def ui_arguments(hidden: bool = False) -> list[str]:
    """``pdms ui`` as the app menu opens it: in its window, with its output in ``pdms logs ui``."""
    return ["ui", "--window", "--detached", *(["--hidden"] if hidden else [])]


def command(hidden: bool = False) -> list[str]:
    return [str(pdms_executable(gui=True)), *ui_arguments(hidden)]


def _home(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / default)


# --------------------------------------------------------------------------- Linux


def desktop_quote(arg: str) -> str:
    """One argument of a .desktop ``Exec`` line, quoted as its spec asks."""
    if arg and not any(char in arg for char in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def desktop_entry(hidden: bool = False) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "GenericName=PDMS",
        f"Comment={COMMENT}",
        f"Exec={' '.join(desktop_quote(arg) for arg in command(hidden))}",
        f"Icon={APP_NAME}",
        "Terminal=false",
        "Categories=Development;",
        f"StartupWMClass={APP_NAME}",
        "StartupNotify=true",
    ]
    if hidden:
        lines.append("X-GNOME-Autostart-enabled=true")
    return "\n".join(lines) + "\n"


def linux_menu_file() -> Path:
    return _home("XDG_DATA_HOME", ".local/share") / "applications" / f"{APP_NAME}.desktop"


def linux_icons() -> list[tuple[Path, str]]:
    """Where the icon goes (``Icon=pdms`` finds it in the user's hicolor theme) and from which file."""
    icons = _home("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor"
    return [(icons / "scalable" / "apps" / f"{APP_NAME}.svg", "icon.svg"),
            (icons / "256x256" / "apps" / f"{APP_NAME}.png", "icon.png")]


def linux_autostart_file() -> Path:
    return _home("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_NAME}.desktop"


def _write(path: Path, data: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _refresh_linux_menu() -> None:
    """Some desktops only notice a new entry after these (both optional; the entry works without them)."""
    for tool, arg in (("update-desktop-database", linux_menu_file().parent),
                      ("gtk-update-icon-cache", linux_icons()[0][0].parents[2])):
        if shutil.which(tool):
            subprocess.run([tool, "-q", str(arg)], capture_output=True, check=False)


# --------------------------------------------------------------------------- Windows


def windows_programs() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def windows_menu_file() -> Path:
    return windows_programs() / f"{APP_NAME}.lnk"


def windows_autostart_file() -> Path:
    return windows_programs() / "Startup" / f"{APP_NAME}.lnk"


def windows_icon() -> Path:
    """A copy of the icon outside the package, which an update replaces."""
    return state_dir() / f"{APP_NAME}.ico"


# The values travel in environment variables, so no quoting of paths is needed.
SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PDMS_LNK); "
    "$s.TargetPath = $env:PDMS_TARGET; $s.Arguments = $env:PDMS_ARGS; $s.IconLocation = $env:PDMS_ICON; "
    "$s.Description = $env:PDMS_COMMENT; $s.WorkingDirectory = $env:USERPROFILE; $s.Save()"
)


def windows_shortcut(path: Path, hidden: bool = False) -> None:
    exe, *args = command(hidden)
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PDMS_LNK": str(path), "PDMS_TARGET": exe, "PDMS_ARGS": subprocess.list2cmdline(args),
           "PDMS_ICON": f"{windows_icon()},0", "PDMS_COMMENT": COMMENT}
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
                            env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or f"powershell exited with {result.returncode}")


# --------------------------------------------------------------------------- macOS


def mac_app() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def mac_autostart_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.ui.plist"


def icns(png: bytes) -> bytes:
    """An .icns holding one PNG; ``ic08`` is the 256×256 slot, the size of icon.png."""
    entry = b"ic08" + struct.pack(">I", 8 + len(png)) + png
    return b"icns" + struct.pack(">I", 8 + len(entry)) + entry


def mac_info_plist() -> bytes:
    return plistlib.dumps({
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": APP_ID,
        "CFBundleExecutable": APP_NAME, "CFBundleIconFile": APP_NAME, "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__, "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
    })


def mac_launcher() -> str:
    return f"#!/bin/sh\nexec {shlex.join(command())} \"$@\"\n"


def mac_launch_agent() -> bytes:
    return plistlib.dumps({"Label": f"{APP_ID}.ui", "ProgramArguments": command(hidden=True), "RunAtLoad": True})


# --------------------------------------------------------------------------- install / uninstall


def menu_entry() -> Path:
    """Where the app menu entry lives on this system."""
    if sys.platform == "win32":
        return windows_menu_file()
    if sys.platform == "darwin":
        return mac_app()
    return linux_menu_file()


def autostart_entry() -> Path:
    if sys.platform == "win32":
        return windows_autostart_file()
    if sys.platform == "darwin":
        return mac_autostart_file()
    return linux_autostart_file()


def installed() -> bool:
    return menu_entry().exists()


def install() -> Path:
    """Add pdms ui to the app menu (again, to point it to the current pdms); where the entry is."""
    if sys.platform == "win32":
        _write(windows_icon(), (STATIC / "icon.ico").read_bytes())
        windows_shortcut(windows_menu_file())
    elif sys.platform == "darwin":
        contents = mac_app() / "Contents"
        _write(contents / "Info.plist", mac_info_plist())
        _write(contents / "MacOS" / APP_NAME, mac_launcher(), 0o755)
        _write(contents / "Resources" / f"{APP_NAME}.icns", icns((STATIC / "icon.png").read_bytes()))
    else:
        for target, source in linux_icons():
            _write(target, (STATIC / source).read_bytes())
        _write(linux_menu_file(), desktop_entry(), 0o755)
        _refresh_linux_menu()
    return menu_entry()


def set_autostart(enabled: bool) -> None:
    """Open pdms ui (in the tray, without a window) when the user logs in, or stop doing it."""
    entry = autostart_entry()
    if not enabled:
        entry.unlink(missing_ok=True)
        return
    if sys.platform == "win32":
        _write(windows_icon(), (STATIC / "icon.ico").read_bytes())
        windows_shortcut(entry, hidden=True)
    elif sys.platform == "darwin":
        _write(entry, mac_launch_agent())
    else:
        _write(entry, desktop_entry(hidden=True))


def uninstall() -> list[Path]:
    """Remove the app menu entry, its icons and opening at login; what was removed."""
    removed = []
    paths = [menu_entry(), autostart_entry()]
    if sys.platform == "win32":
        paths.append(windows_icon())
    elif sys.platform != "darwin":
        paths += [target for target, _source in linux_icons()]
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    if sys.platform not in ("win32", "darwin") and removed:
        _refresh_linux_menu()
    return removed


# --------------------------------------------------------------------------- notifications


def notify(title: str, message: str) -> None:
    """Tell the user something when there is no terminal (pdms ui opened from the app menu); best effort."""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)  # type: ignore[attr-defined]  # MB_ICONINFORMATION
        elif sys.platform == "darwin":
            script = f"display notification {_applescript(message)} with title {_applescript(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, check=False, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name", APP_NAME, "--icon", APP_NAME, title, message],
                           capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
