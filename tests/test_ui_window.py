"""pdms ui --window: the same server, shown in a native window that stops it when closed."""

from __future__ import annotations

import http.client
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import tomlkit

from typer.testing import CliRunner

from pdms_cli import __version__, cli, update
from pdms_cli.commands import ui as ui_commands
from pdms_cli.ui import control as ui_control
from pdms_cli.ui import instance as ui_instance
from pdms_cli.ui import window as ui_window
from pdms_cli.ui.control import Control


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_install_command_fits_the_install(monkeypatch) -> None:
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    assert ui_window.install_command() == (
        f"uv tool install --force 'pdms-cli[desktop] @ {update.wheel_url(__version__)}'"
    )
    monkeypatch.setattr(update, "install_kind", lambda: "editable")
    assert ui_window.install_command() == "uv tool install -e '.[desktop]' --force"


def test_without_pywebview_it_says_how_to_add_it(monkeypatch) -> None:
    monkeypatch.setattr(ui_window, "available", lambda: False)
    monkeypatch.setattr(ui_window, "installs_itself", lambda: False)  # Linux: Qt is the user's call
    monkeypatch.setattr(update, "install_kind", lambda: "editable")
    result = CliRunner().invoke(cli.app, ["ui", "--window"])
    assert result.exit_code == 1
    assert "uv tool install -e '.[desktop]' --force" in result.output and "pdms ui" in result.output


def test_the_window_shows_the_page_and_closing_it_stops_the_server(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_window, "available", lambda: True)
    monkeypatch.setattr(ui_commands.desktop, "open_url", lambda url: (_ for _ in ()).throw(AssertionError("no browser")))
    seen = {}

    def open_window(url: str, control, hidden: bool = False) -> None:  # what the webview does: follow the link, keep the cookie, load the page
        parts = urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        conn.request("GET", f"{parts.path}?{parts.query}", headers={"Host": parts.netloc})
        response = conn.getresponse()
        response.read()
        seen["redirect"] = response.status
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        conn.request("GET", "/", headers={"Host": parts.netloc, "Cookie": cookie})
        page = conn.getresponse()
        seen["page"] = page.status, b"<title>pdms</title>" in page.read()
        conn.close()
        seen["port"] = parts.port

    monkeypatch.setattr(ui_window, "open_window", open_window)
    result = CliRunner().invoke(cli.app, ["ui", "--window", "--port", str(free_port())])
    assert result.exit_code == 0, result.output
    assert seen["redirect"] == 303 and seen["page"] == (200, True)
    assert "Close the window" in result.output
    with socket.socket() as sock:  # closed with the window
        assert sock.connect_ex(("127.0.0.1", seen["port"])) != 0


def test_a_window_that_cannot_open_says_so(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_window, "available", lambda: True)

    def broken(url: str, control, hidden: bool = False) -> None:
        raise RuntimeError("qt.qpa.plugin: Could not load the Qt platform plugin \"xcb\"")

    monkeypatch.setattr(ui_window, "open_window", broken)
    controls, opened = [], []
    attach = ui_control.Control.attach

    def remember(self, show, close) -> None:
        controls.append(self)
        attach(self, show, close)

    def browser(url: str) -> bool:  # the page opens; then the tray's Quit
        opened.append(url)
        threading.Timer(0.2, lambda: controls[-1]._close()).start()
        return True

    monkeypatch.setattr(ui_control.Control, "attach", remember)
    monkeypatch.setattr(ui_commands.desktop, "open_url", browser)
    result = CliRunner().invoke(cli.app, ["ui", "--window", "--port", str(free_port())])
    assert result.exit_code == 0, result.output
    assert "Could not open the window" in result.output and "xcb" in result.output
    assert len(opened) == 1 and "token=" in opened[0]  # it went on in the browser instead of stopping


def test_from_the_app_menu_a_failed_pywebview_install_opens_the_browser(monkeypatch, tmp_path) -> None:
    """No terminal to read: rather than not opening at all, pdms ui opens in the browser."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_window, "available", lambda: False)
    monkeypatch.setattr(ui_window, "installs_itself", lambda: True)
    monkeypatch.setattr(ui_window, "install_desktop", lambda: (_ for _ in ()).throw(RuntimeError("no wheel for 3.14")))
    monkeypatch.setattr(ui_commands.desktop, "notify", lambda title, message: None)
    monkeypatch.setattr(ui_instance, "redirect_output", lambda: tmp_path / "ui.log")  # keep pytest's output
    monkeypatch.setattr("pdms_cli.shellenv.adopt_in_background", lambda: set())
    controls, opened = [], []
    attach = ui_control.Control.attach

    def remember(self, show, close) -> None:
        controls.append(self)
        attach(self, show, close)

    monkeypatch.setattr(ui_control.Control, "attach", remember)
    monkeypatch.setattr(ui_commands.desktop, "open_url", lambda url: opened.append(url) or threading.Timer(
        0.2, lambda: controls[-1]._close()).start() or True)
    result = CliRunner().invoke(cli.app, ["ui", "--window", "--port", str(free_port())])
    assert result.exit_code == 1 and "no wheel for 3.14" in result.output  # in a terminal: it says so and stops
    result = CliRunner().invoke(cli.app, ["ui", "--window", "--detached", "--port", str(free_port())])
    assert result.exit_code == 0, result.output
    assert len(opened) == 1


def test_windows_and_macos_install_pywebview_by_themselves(monkeypatch) -> None:
    for platform, itself in (("win32", True), ("darwin", True), ("linux", False)):
        monkeypatch.setattr(ui_window.sys, "platform", platform)
        assert ui_window.installs_itself() is itself


def test_the_install_goes_into_pdms_own_environment(monkeypatch) -> None:
    monkeypatch.setattr(ui_window.shutil, "which", lambda name: "/bin/uv")
    assert ui_window.self_install_command() == [
        "/bin/uv", "pip", "install", "--python", sys.executable, *ui_window.DESKTOP_REQUIREMENTS]
    monkeypatch.setattr(ui_window.shutil, "which", lambda name: None)
    monkeypatch.setattr(ui_window.importlib.util, "find_spec", lambda name: object())
    assert ui_window.self_install_command() == [sys.executable, "-m", "pip", "install", *ui_window.DESKTOP_REQUIREMENTS]


def test_the_requirements_match_the_desktop_extra() -> None:
    pyproject = tomlkit.parse((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    # What Windows and macOS get: the requirements for every system plus the ones that are not for Linux.
    extra = [req.split(";")[0].strip() for req in pyproject["project"]["optional-dependencies"]["desktop"]
             if "sys_platform" not in req or "!= 'linux'" in req]
    assert ui_window.DESKTOP_REQUIREMENTS == extra


def test_a_missing_pywebview_is_installed_and_the_window_opens(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    installed = []
    monkeypatch.setattr(ui_window, "installs_itself", lambda: True)
    monkeypatch.setattr(ui_window, "available", lambda: bool(installed))
    monkeypatch.setattr(ui_window, "self_install_command", lambda: ["uv", "pip", "install", "pywebview>=5"])
    monkeypatch.setattr(ui_window.subprocess, "run", lambda cmd: installed.append(cmd) or SimpleNamespace(returncode=0))
    opened = []
    monkeypatch.setattr(ui_window, "open_window", lambda url, control, hidden=False: opened.append(url))
    result = CliRunner().invoke(cli.app, ["ui", "--window", "--port", str(free_port())])
    assert result.exit_code == 0, result.output
    assert installed == [["uv", "pip", "install", "pywebview>=5"]] and len(opened) == 1
    assert "installing it" in result.output and "pywebview installed" in result.output


def test_a_failed_install_says_how_to_do_it_by_hand(monkeypatch) -> None:
    monkeypatch.setattr(ui_window, "installs_itself", lambda: True)
    monkeypatch.setattr(ui_window, "available", lambda: False)
    monkeypatch.setattr(ui_window, "self_install_command", lambda: ["uv"])
    monkeypatch.setattr(ui_window.subprocess, "run", lambda cmd: SimpleNamespace(returncode=2))
    monkeypatch.setattr(update, "install_kind", lambda: "editable")
    result = CliRunner().invoke(cli.app, ["ui", "--window"])
    assert result.exit_code == 1
    assert "exit code 2" in result.output and "uv tool install -e '.[desktop]' --force" in result.output


def test_the_window_icon_fits_each_system(monkeypatch) -> None:
    static = Path(ui_window.__file__).parent / "static"
    for platform, name in (("win32", "icon.ico"), ("darwin", "icon.png"), ("linux", "icon.png")):
        monkeypatch.setattr(ui_window.sys, "platform", platform)
        assert ui_window.icon_name() == name and (static / name).stat().st_size > 1000


class FakeEvent(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self

    def set(self):
        return any(handler() is False for handler in self)


class FakeWindow:
    def __init__(self) -> None:
        self.events = SimpleNamespace(closing=FakeEvent(), minimized=FakeEvent(), restored=FakeEvent())
        self.calls: list[str] = []
        self.on_top = False

    def hide(self) -> None:
        self.calls.append("hide")

    def show(self) -> None:
        self.calls.append("show")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")


def fake_webview(monkeypatch, calls: dict, tray: bool = False) -> FakeWindow:
    window = FakeWindow()

    def create_window(*args, **kwargs):
        calls["window"] = (args, kwargs)
        return window

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        settings={}, create_window=create_window, start=lambda **kwargs: calls.setdefault("start", kwargs)))
    monkeypatch.setattr(ui_window, "missing_system_library", lambda: None)
    monkeypatch.setattr(ui_window.ui_tray, "start", lambda _tray: tray)
    return window


def test_the_window_gets_its_icon(monkeypatch) -> None:
    calls = {}
    fake_webview(monkeypatch, calls)
    ui_window.open_window("http://127.0.0.1:1/?token=x", Control("http://127.0.0.1:1/?token=x"))
    assert Path(calls["start"]["icon"]).name == ui_window.icon_name() and Path(calls["start"]["icon"]).is_file()
    assert calls["window"][0] == ("pdms", "http://127.0.0.1:1/?token=x")


@pytest.mark.parametrize("platform, env, found, missing", [
    ("linux", {}, None, True),  # X11 without libxcb-cursor: Qt would abort
    ("linux", {}, "libxcb-cursor.so.0", False),
    ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, None, False),  # Wayland does not need it
    ("linux", {"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "xcb"}, None, True),
    ("darwin", {}, None, False),
])
def test_a_missing_qt_library_is_said_before_qt_aborts(monkeypatch, platform, env, found, missing) -> None:
    monkeypatch.setattr(ui_window.sys, "platform", platform)
    for name in ("WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ui_window.ctypes.util, "find_library", lambda name: found)
    message = ui_window.missing_system_library()
    assert bool(message) is missing
    if missing:
        assert "sudo apt install libxcb-cursor0" in message
        with pytest.raises(RuntimeError, match="libxcb-cursor"):
            ui_window.open_window("http://127.0.0.1:1/", Control(""))


def test_with_a_tray_closing_hides_the_window_and_quit_closes_it(monkeypatch) -> None:
    calls = {}
    window = fake_webview(monkeypatch, calls, tray=True)
    control = Control("http://127.0.0.1:1/")
    ui_window.open_window(control.url, control, hidden=True)
    assert calls["window"][1]["hidden"] is True  # at login: only the tray icon
    assert window.events.closing.set() is True and window.calls == ["hide"]  # the close was cancelled
    control.show()  # the tray's Open, or a second pdms ui
    assert window.calls[-1] == "show"
    control.quit()
    assert window.events.closing.set() is False and window.calls[-1] == "destroy"


def test_without_a_tray_closing_the_window_stops_pdms_ui(monkeypatch) -> None:
    calls = {}
    window = fake_webview(monkeypatch, calls, tray=False)
    ui_window.open_window("http://127.0.0.1:1/", Control(""), hidden=True)
    assert calls["window"][1]["hidden"] is False  # nothing to bring it back from
    assert window.events.closing.set() is False and window.calls == []


def test_pdmsw_without_a_console_still_starts(monkeypatch) -> None:
    """pdmsw.exe (the Start menu's) has no console: Python gives it no stdin, and checking for a terminal on None
    stopped pdms ui before it began (AttributeError: 'NoneType' object has no attribute 'isatty')."""
    from pdms_cli import launcher
    from pdms_cli.commands.common import interactive_terminal

    ran = []
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr("pdms_cli.cli._entrypoint", lambda: ran.append(interactive_terminal()))
    launcher.main()
    assert ran == [False] and sys.stdin is not None and not sys.stdin.isatty()
    monkeypatch.setattr(sys, "stdin", None)
    assert interactive_terminal() is False
