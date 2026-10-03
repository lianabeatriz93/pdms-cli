"""pdms ui in the system: app menu and login entries, one instance at a time, and updating from the page."""

from __future__ import annotations

import json
import os
import plistlib
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pdms_cli import __version__, actions, cli, desktop, proxy, transfer, update
from pdms_cli.commands import selfupdate
from pdms_cli.config import Config
from pdms_cli.ui import instance as ui_instance
from pdms_cli.ui import jobs as ui_jobs
from pdms_cli.ui import server as ui_server
from pdms_cli.ui import updates as ui_updates
from pdms_cli.ui.control import Control

from test_ui_server import TOKEN, post, request, wait_until


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A home of its own, with the XDG folders inside it and pdms at ~/.local/bin/pdms."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    pdms = tmp_path / ".local" / "bin" / "pdms"
    monkeypatch.setattr(desktop.shutil, "which", lambda name: str(pdms) if name == "pdms" else None)
    return tmp_path


# --------------------------------------------------------------------------- app menu and login


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and permissions")
def test_linux_menu_entry_icons_and_uninstall(home, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    entry = desktop.install()
    assert entry == home / ".local/share/applications/pdms.desktop"
    text = entry.read_text()
    assert f"Exec={home}/.local/bin/pdms ui --window --detached\n" in text
    assert "Icon=pdms\n" in text and "Terminal=false\n" in text and "StartupWMClass=pdms\n" in text
    assert (home / ".local/share/icons/hicolor/scalable/apps/pdms.svg").is_file()
    assert (home / ".local/share/icons/hicolor/256x256/apps/pdms.png").is_file()
    assert desktop.installed()

    desktop.set_autostart(True)
    autostart = home / ".config/autostart/pdms.desktop"
    assert "ui --window --detached --hidden\n" in autostart.read_text()
    removed = desktop.uninstall()
    assert entry in removed and autostart in removed and not entry.exists() and not autostart.exists()
    assert not (home / ".local/share/icons/hicolor/256x256/apps/pdms.png").exists()
    assert desktop.uninstall() == []


def test_desktop_exec_quotes_what_needs_it() -> None:
    assert desktop.desktop_quote("/home/me/.local/bin/pdms") == "/home/me/.local/bin/pdms"
    assert desktop.desktop_quote("/home/my name/pdms") == '"/home/my name/pdms"'
    assert desktop.desktop_quote('a"b$c') == '"a\\"b\\$c"'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and permissions")
def test_macos_app_and_launch_agent(home, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    app = desktop.install()
    assert app == home / "Applications/pdms.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "pdms" and info["CFBundleIconFile"] == "pdms"
    launcher = app / "Contents/MacOS/pdms"
    assert launcher.read_text().startswith("#!/bin/sh\nexec ") and "ui --window --detached" in launcher.read_text()
    assert os.access(launcher, os.X_OK)
    icns = (app / "Contents/Resources/pdms.icns").read_bytes()
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns) and icns[8:12] == b"ic08"
    assert icns[16:24] == b"\x89PNG\r\n\x1a\n"

    desktop.set_autostart(True)
    agent = plistlib.loads((home / "Library/LaunchAgents/com.alivi.pdms.ui.plist").read_bytes())
    assert agent["RunAtLoad"] is True and agent["ProgramArguments"][-1] == "--hidden"
    desktop.uninstall()
    assert not app.exists() and not (home / "Library/LaunchAgents/com.alivi.pdms.ui.plist").exists()


def test_windows_shortcuts_point_to_pdmsw(home, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pdmsw.exe").write_text("")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: str(bin_dir / "pdms.exe") if name == "pdms" else None)
    made = []

    def run(cmd, env, **kwargs):
        made.append(env)
        Path(env["PDMS_LNK"]).write_text("lnk")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", run)
    entry = desktop.install()
    programs = home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
    assert entry == programs / "pdms.lnk" and entry.exists()
    assert made[0]["PDMS_TARGET"] == str(bin_dir / "pdmsw.exe") and made[0]["PDMS_ARGS"] == "ui --window --detached"
    assert made[0]["PDMS_ICON"].endswith("pdms.ico,0") and desktop.windows_icon().is_file()
    desktop.set_autostart(True)
    assert made[1]["PDMS_LNK"] == str(programs / "Startup" / "pdms.lnk") and made[1]["PDMS_ARGS"].endswith("--hidden")

    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, env, **kw: SimpleNamespace(returncode=1, stderr="denied"))
    with pytest.raises(OSError, match="denied"):
        desktop.install()


def test_cli_install_uninstall_and_at_login(home, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    result = CliRunner().invoke(cli.app, ["ui", "--install"])
    assert result.exit_code == 0, result.output
    assert "pdms is in the app menu" in result.output and desktop.installed()

    result = CliRunner().invoke(cli.app, ["ui", "--at-login"])
    assert result.exit_code == 0, result.output
    assert Config.load().defaults.ui_at_login and desktop.linux_autostart_file().is_file()
    result = CliRunner().invoke(cli.app, ["ui", "--not-at-login"])
    assert not Config.load().defaults.ui_at_login and not desktop.linux_autostart_file().exists()

    CliRunner().invoke(cli.app, ["ui", "--at-login"])
    result = CliRunner().invoke(cli.app, ["ui", "--uninstall"])
    assert "no longer in the app menu" in result.output
    assert not desktop.installed() and not Config.load().defaults.ui_at_login


def test_saving_the_default_sets_up_login_and_reports_failures(home, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    cfg = Config.load()
    cfg.defaults.ui_at_login = True
    actions.save_defaults(Config.load(), cfg.defaults)
    assert desktop.linux_autostart_file().is_file()

    def refuse(enabled: bool) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(desktop, "set_autostart", refuse)
    cfg = Config.load()
    cfg.defaults.ui_at_login = False
    with pytest.raises(actions.InvalidValue) as error:
        actions.save_defaults(Config.load(), cfg.defaults)
    assert error.value.field == "ui_at_login" and Config.load().defaults.ui_at_login  # not saved


def test_an_imported_config_keeps_this_computers_login_setting(home) -> None:
    mine, theirs = Config.load(), Config.load()
    theirs.defaults.ui_at_login, theirs.defaults.port = True, 9000
    plans = transfer.plan_import(mine, theirs, ["defaults"])
    assert plans[0].changed == ["defaults"]
    result = transfer.apply_import(mine, theirs, ["defaults"], set(), replace=True)
    assert result.defaults.port == 9000 and result.defaults.ui_at_login is False
    theirs.defaults.port = mine.defaults.port
    assert transfer.plan_import(mine, theirs, ["defaults"])[0].same == ["defaults"]


# --------------------------------------------------------------------------- one at a time


def test_the_running_ui_is_remembered_privately_and_forgotten(home) -> None:
    assert ui_instance.running() is None
    ui_instance.remember(8765, "tok", window=True)
    data = ui_instance.load()
    assert data["port"] == 8765 and data["token"] == "tok" and data["pid"] == os.getpid()
    if sys.platform != "win32":
        assert ui_instance.state_path().stat().st_mode & 0o777 == 0o600
    assert ui_instance.running() is None  # this very process does not count
    ui_instance.forget()
    assert ui_instance.load() is None


def test_a_dead_ui_does_not_count(home) -> None:
    ui_instance.state_path().parent.mkdir(parents=True)
    ui_instance.state_path().write_text(json.dumps({"pid": 2**22 + 12345, "port": 1, "token": "x"}))
    assert ui_instance.running() is None


@pytest.fixture
def ui_with_control(home):
    shown = []
    control = Control("http://127.0.0.1:1/")
    control.attach(lambda: shown.append(True), lambda: None)
    hub = ui_server.Hub(build=lambda: {"instances": []}, interval=0.05)
    jobs = ui_jobs.Jobs(hub.poke)
    server = ui_server.make_server("127.0.0.1", 0, TOKEN, hub, jobs, control)
    thread = threading.Thread(target=ui_server.serve, args=(server, hub), daemon=True)
    thread.start()
    yield server.server_address[1], shown, jobs, control
    server.shutdown()
    thread.join(5)


def test_a_second_pdms_ui_asks_the_first_to_show_itself(ui_with_control) -> None:
    port, shown, _jobs, _control = ui_with_control
    assert ui_instance.show({"port": port, "token": TOKEN}) and shown == [True]
    assert not ui_instance.show({"port": port, "token": "wrong"}) and shown == [True]
    # Another page cannot do it: no custom header without a preflight, and a foreign Origin is refused.
    response, _body, _conn = request(port, ui_instance.SHOW_PATH, {ui_instance.TOKEN_HEADER: TOKEN,
                                     "Origin": "http://evil.example", "Content-Type": "application/json"}, "POST", b"{}")
    assert response.status == 403 and shown == [True]


def test_pdms_ui_twice_shows_the_first_one(home, monkeypatch) -> None:
    monkeypatch.setattr(ui_instance, "running", lambda: {"port": 8765, "token": "t"})
    monkeypatch.setattr(ui_instance, "show", lambda current: True)
    monkeypatch.setattr(ui_server, "make_server", lambda *a, **k: pytest.fail("a second server"))
    result = CliRunner().invoke(cli.app, ["ui", "--no-browser"])
    assert result.exit_code == 0 and "already running" in result.output


def test_detached_output_goes_to_the_ui_log(home, monkeypatch) -> None:
    monkeypatch.setattr(ui_instance, "_redirected", None)
    monkeypatch.setattr(ui_instance.os, "dup2", lambda *args: None)
    monkeypatch.setattr(ui_instance.sys, "stdout", sys.stdout)
    monkeypatch.setattr(ui_instance.sys, "stderr", sys.stderr)
    path = ui_instance.redirect_output()
    print("hello from the menu")
    sys.stdout.flush()
    assert path == ui_instance.log_path() and "hello from the menu" in path.read_text()
    assert ui_instance.redirect_output() == path  # only once


# --------------------------------------------------------------------------- updates


def test_installed_version_follows_the_checkout() -> None:
    assert update.installed_version() == __version__  # an editable install: its pyproject.toml, not stale metadata


def test_release_notes_between_two_versions(monkeypatch) -> None:
    releases = [{"tag_name": f"v{v}", "html_url": f"https://x/{v}", "body": f"notes {v}", "draft": v == "0.2.9"}
                for v in ("0.3.0", "0.2.9", "0.2.8", "0.2.7", "0.2.6", "0.2.5")]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return json.dumps(releases).encode()

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda request, timeout: Response())
    notes = update.release_notes("0.2.8", current="0.2.5")
    assert [note["version"] for note in notes] == ["0.2.8", "0.2.7", "0.2.6"]
    assert notes[0] == {"version": "0.2.8", "url": "https://x/0.2.8", "body": "notes 0.2.8"}


def test_the_state_offers_a_newer_version(home, monkeypatch) -> None:
    for name in ("CI", "PDMS_NO_UPDATE_CHECK"):  # both turn the automatic check off
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "9.0.0")
    update.refresh(pre=False)
    info = ui_updates.state(Config.load())
    assert info["latest"] == "9.0.0" and info["current"] == __version__ and info["checks"]
    assert "pdms_cli-9.0.0-py3-none-any.whl" in info["command"]
    monkeypatch.setattr(update, "install_kind", lambda: "editable")
    assert ui_updates.state(Config.load())["latest"] == ""  # a checkout updates with git pull


def test_check_now_and_the_endpoints(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, _control = ui_with_control
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "9.0.0")
    assert post(port, "/api/update/check") == (200, {"latest": "9.0.0", "current": __version__})

    def offline(pre, timeout):
        raise OSError("no network")

    monkeypatch.setattr(update, "latest_version", offline)
    status, data = post(port, "/api/update/check")
    assert status == 400 and "Could not reach GitHub" in data["error"]


def test_an_install_without_uv_tool_is_refused(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, _control = ui_with_control
    monkeypatch.setattr(update, "updates_itself", lambda: False)
    status, data = post(port, "/api/update/install", {})
    assert status == 400 and "cannot update itself" in data["error"]


def test_update_restarts_the_ui_and_brings_the_proxy_back(ui_with_control, monkeypatch) -> None:
    port, _shown, jobs, control = ui_with_control
    monkeypatch.setattr(ui_updates.sys, "platform", "linux")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "9.0.0")
    update.refresh(pre=False)
    running = {"pid": 4242, "port": 8000, "repo": "/repo", "env": "dev", "remote": "", "as": "", "timeout": 300,
               "background": True}
    monkeypatch.setattr(proxy, "running_proxy", lambda: running)
    stopped, ran = [], []
    monkeypatch.setattr(proxy, "stop", lambda current: stopped.append(current["pid"]))
    monkeypatch.setattr(ui_updates.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0))
    quits = []
    control.attach(control._show, lambda: quits.append(control.restart))

    assert post(port, "/api/update/install", {"restart_proxy": True})[0] == 202
    wait_until(lambda: quits)
    assert quits == [True] and stopped == [4242]
    assert ran[0][1:4] == ["tool", "install", "--force"] and "9.0.0" in ran[0][-1]
    note = json.loads(ui_instance.after_update_path().read_text())
    assert note["to"] == "9.0.0" and note["proxy"]["port"] == 8000
    assert ui_updates.log_path().read_text().startswith(f"# pdms ")

    # The new pdms ui (here still this version: the update did not happen) says so and starts the proxy again.
    started = []
    monkeypatch.setattr(ui_updates, "start_proxy_again", lambda saved: started.append(saved["port"]))
    jobs.after_update(ui_instance.take_after_update())
    wait_until(lambda: started)
    assert started == [8000] and "did not finish" in jobs.snapshot()[ui_updates.KEY]["error"]
    assert ui_instance.take_after_update() is None


def test_a_failed_install_stays_on_the_page(ui_with_control, monkeypatch) -> None:
    port, _shown, jobs, control = ui_with_control
    monkeypatch.setattr(ui_updates.sys, "platform", "linux")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "9.0.0")
    update.refresh(pre=False)
    monkeypatch.setattr(proxy, "running_proxy", lambda: None)
    monkeypatch.setattr(ui_updates.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=2))
    control.attach(None, lambda: pytest.fail("no restart after a failure"))
    assert post(port, "/api/update/install", {})[0] == 202
    wait_until(lambda: jobs.snapshot().get(ui_updates.KEY, {}).get("error"))
    assert "exit code 2" in jobs.snapshot()[ui_updates.KEY]["error"]
    assert post(port, "/api/update/dismiss")[0] == 200 and ui_updates.KEY not in jobs.snapshot()


def test_on_windows_a_helper_updates_once_pdms_ui_exits(ui_with_control, monkeypatch) -> None:
    port, _shown, jobs, control = ui_with_control
    monkeypatch.setattr(ui_updates.sys, "platform", "win32")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "9.0.0")
    update.refresh(pre=False)
    running = {"pid": 4242, "port": 8000, "repo": "/repo", "background": True}
    monkeypatch.setattr(proxy, "running_proxy", lambda: running)
    monkeypatch.setattr(proxy, "stop", lambda current: None)
    spawned = []
    monkeypatch.setattr(update, "spawn_windows_update", lambda cmd, **kw: spawned.append((cmd, kw)))
    control.token, control.relaunch = "tok", ["pdmsw.exe", "ui", "--window"]
    quits = []
    control.attach(None, lambda: quits.append(control.restart))
    assert post(port, "/api/update/install", {"restart_proxy": False})[0] == 202
    wait_until(lambda: quits)
    assert quits == [False]  # the helper opens it again
    (cmd, kw), = spawned
    assert kw["relaunch"] == ["pdmsw.exe", "ui", "--window"] and kw["env"] == {ui_instance.TOKEN_ENV: "tok"}
    assert json.loads(ui_instance.after_update_path().read_text())["proxy"]["port"] == 8000  # locked files: always


def test_the_windows_helper_is_written_and_started(home, monkeypatch) -> None:
    started = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    update.spawn_windows_update(["uv", "tool", "install", "--force", "pdms-cli[desktop] @ https://x/a.whl"],
                                log=Path("u.log"), relaunch=["pdmsw.exe", "ui", "--window"])
    (cmd, kw), = started
    assert cmd[-2:] == ["-WaitPid", str(os.getpid())] and update.windows_helper_path().read_text(encoding="utf-8-sig")
    assert kw["env"]["PDMS_UPDATE_ARGS"] == 'tool install --force "pdms-cli[desktop] @ https://x/a.whl"'
    assert kw["env"]["PDMS_UPDATE_RELAUNCH"] == "pdmsw.exe" and kw["env"]["PDMS_UPDATE_RELAUNCH_ARGS"] == "ui --window"


def test_self_update_on_windows_hands_over_to_the_helper(home, monkeypatch) -> None:
    monkeypatch.setattr(selfupdate.sys, "platform", "win32")
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout=10: "9.0.0")
    monkeypatch.setattr(proxy, "running_proxy", lambda: None)
    spawned = []
    monkeypatch.setattr(update, "spawn_windows_update", lambda cmd, **kw: spawned.append(cmd))
    result = CliRunner().invoke(cli.app, ["self-update"])
    assert result.exit_code == 0, result.output
    assert "in a new window" in result.output and "9.0.0" in spawned[0][-1]

    monkeypatch.setattr(proxy, "running_proxy", lambda: {"pid": 1, "port": 8000})
    result = CliRunner().invoke(cli.app, ["self-update"])
    assert result.exit_code == 1 and "pdms stop proxy" in result.output


def test_the_entries_run_this_pdms_even_without_its_exe_suffix(home, monkeypatch) -> None:
    bin_dir = home / "bin"
    bin_dir.mkdir()
    for name in ("pdms.exe", "pdmsw.exe"):
        (bin_dir / name).write_text("")
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop.sys, "argv", [str(bin_dir / "pdms"), "ui", "--install"])
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)  # a fresh install is not on the PATH yet
    assert desktop.pdms_executable() == bin_dir / "pdms.exe"
    assert desktop.pdms_executable(gui=True) == bin_dir / "pdmsw.exe"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_the_shell_environment_comes_in_without_its_noise(tmp_path) -> None:
    from pdms_cli import shellenv

    # A shell whose startup files print things and add pyenv to PATH, as ~/.bashrc does.
    fake = tmp_path / "fakeshell"
    fake.write_text('#!/bin/sh\necho "welcome!"\nexport PATH="/opt/pyenv/shims:$PATH" PYENV_ROOT=/opt/pyenv\n'
                    'shift\nexec /bin/sh -c "$1"\n')
    fake.chmod(0o755)
    found = shellenv.read(str(fake))
    assert found["PATH"].startswith("/opt/pyenv/shims:") and found["PYENV_ROOT"] == "/opt/pyenv"

    environ = {"PATH": "/usr/bin", "HOME": "/home/me", "PDMS_UI_TOKEN": "keep"}
    changed = shellenv.adopt(environ, {**found, "HOME": "/elsewhere", "PDMS_UI_TOKEN": "other", "PWD": "/x"})
    assert set(changed) >= {"PATH", "PYENV_ROOT"} and environ["HOME"] == "/home/me"  # never replaces what it has
    assert environ["PDMS_UI_TOKEN"] == "keep" and "PWD" not in environ


def test_a_shell_that_hangs_or_fails_changes_nothing(tmp_path) -> None:
    from pdms_cli import shellenv

    assert shellenv.read(str(tmp_path / "missing-shell")) == {}
    hangs = tmp_path / "hangs"
    hangs.write_text("#!/bin/sh\nsleep 30\n")
    hangs.chmod(0o755)
    if sys.platform != "win32":
        assert shellenv.read(str(hangs), timeout=0.5) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX folders")
def test_known_tool_folders_are_added_even_if_the_shell_hangs(tmp_path) -> None:
    from pdms_cli import shellenv

    for folder in (".pyenv/shims", ".pyenv/bin", ".local/bin", ".nvm/versions/node/v22.22.0/bin"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / ".nvm/alias").mkdir(parents=True)
    (tmp_path / ".nvm/alias/default").write_text("22\n")
    environ = {"PATH": f"/usr/bin:{tmp_path}/.local/bin"}
    changed = shellenv.adopt(environ, found={}, home=tmp_path)
    path = environ["PATH"].split(os.pathsep)
    mine = [entry for entry in path if entry.startswith(str(tmp_path))]
    assert mine == [f"{tmp_path}/.pyenv/shims", f"{tmp_path}/.pyenv/bin", f"{tmp_path}/.nvm/versions/node/v22.22.0/bin",
                    f"{tmp_path}/.local/bin"]  # the one already there is not repeated, nor moved
    assert "/usr/bin" in path
    assert environ["PYENV_ROOT"] == f"{tmp_path}/.pyenv" and changed == ["PATH", "PYENV_ROOT"]
