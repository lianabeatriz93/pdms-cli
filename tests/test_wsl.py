"""pdms inside WSL: the desktop is Windows' (its browser, its browsers' folders, its trusted certificates)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdms_cli import browser, desktop, doctor, wsl
from pdms_cli.config import Config, Repo

# WSL is a Linux: its paths (/mnt/c/...) and lookups are Linux's, which Windows' pathlib does not follow.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pdms inside WSL runs on Linux")

TEST_CA = """-----BEGIN CERTIFICATE-----
MIICCjCCAXOgAwIBAgIUVj2TRwHrMQqn36jTw9hQmeRxVKgwDQYJKoZIhvcNAQEL
BQAwFzEVMBMGA1UEAwwMcGRtcyB0ZXN0IENBMB4XDTI2MTAyNzE0NTY0MVoXDTM2
MTAyNDE0NTY0MVowFzEVMBMGA1UEAwwMcGRtcyB0ZXN0IENBMIGfMA0GCSqGSIb3
DQEBAQUAA4GNADCBiQKBgQC5iSnM68H9nRCWDA0JwoO1A9d9pZv6ma4iVxqoz8Nm
fEsMNhriQh+XhmV06BPjGMWZiffy21ZRMrFH1WYEdTz7axerTMb7LpGDNnB1T7Ms
J+aeU1wbIX7UzoHHwsmZK0ARPwbN1GFJwh9Mg93ySbaP3mVGxuIFLV+y6+/Pww5k
0wIDAQABo1MwUTAdBgNVHQ4EFgQUgAy+fqRh/qitGkGhuRNrbbPJaPwwHwYDVR0j
BBgwFoAUgAy+fqRh/qitGkGhuRNrbbPJaPwwDwYDVR0TAQH/BAUwAwEB/zANBgkq
hkiG9w0BAQsFAAOBgQB0eZUhxntxxnHeQYMuC12auKRvCFB9M0M0z1pUBSfjTC/t
qvkxAGkp0Qs0EiGMQ3Pep3UezCAGs4JdoAN4nVjIPknUoN+FklBTCkAcZQSA53d6
21EnjJFxxz6STDzIHQfNlVCewd9TIeX1/Ib/VUp5Ri0eu+Qoq/rJ0njoHXam8Q==
-----END CERTIFICATE-----
"""


@pytest.fixture
def inside(monkeypatch, tmp_path):
    """pdms inside WSL, with a Windows drive in ``tmp_path / "c"`` (``C:\\``)."""
    drive = tmp_path / "c"
    variables = {"LOCALAPPDATA": "C:\\Users\\ana\\AppData\\Local", "ProgramFiles": "C:\\Program Files",
                 "ProgramFiles(x86)": "C:\\Program Files (x86)", "USERPROFILE": "C:\\Users\\ana"}
    monkeypatch.setattr(wsl, "detected", lambda: True)
    monkeypatch.setattr(wsl, "windows_variable", lambda name: variables.get(name, ""))
    real = wsl.to_linux

    def to_linux(path: str) -> Path:
        linux = real(path)
        return drive / linux.relative_to("/mnt/c") if str(linux).startswith("/mnt/c") else linux

    monkeypatch.setattr(wsl, "to_linux", to_linux)
    monkeypatch.setattr(browser, "_found", (0.0, {}))
    for value in variables.values():
        to_linux(value).mkdir(parents=True, exist_ok=True)
    return drive


def test_detected_by_the_variable_wsl_sets(monkeypatch) -> None:
    wsl.detected.cache_clear()
    monkeypatch.setattr(wsl.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    try:
        assert wsl.detected() and wsl.distro() == "Ubuntu"
    finally:
        wsl.detected.cache_clear()


def test_paths_between_linux_and_windows(monkeypatch) -> None:
    assert wsl.to_windows(Path("/mnt/c/Users/ana/AppData")) == "C:\\Users\\ana\\AppData"
    assert wsl.to_windows(Path("/mnt/d")) == "D:\\"
    assert wsl.to_linux("C:\\Users\\ana") == Path("/mnt/c/Users/ana")
    assert wsl.to_linux("d:/code") == Path("/mnt/d/code")
    monkeypatch.setattr(wsl.shutil, "which", lambda name: None)  # no wslpath: the network form
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert wsl.to_windows(Path("/home/ana/.local")) == "\\\\wsl.localhost\\Ubuntu\\home\\ana\\.local"
    assert wsl.on_windows_drive(Path("/mnt/c/code/pdms")) and not wsl.on_windows_drive(Path("/home/ana/pdms"))


def test_the_browser_that_opens_is_windows(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(wsl, "detected", lambda: True)
    monkeypatch.setattr(wsl, "open_url", lambda url: opened.append(url) or True)
    assert desktop.open_url("http://127.0.0.1:8765/?token=x") and opened == ["http://127.0.0.1:8765/?token=x"]


def test_the_extension_goes_where_windows_browsers_can_open_it(inside, monkeypatch) -> None:
    edge = inside / "Program Files (x86)" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_text("", encoding="utf-8")
    assert browser.installed() == {"edge": str(edge)}
    folder = browser.write_extension()
    assert folder == inside / "Users" / "ana" / "AppData" / "Local" / "pdms" / "browser-extension"
    assert (folder / "manifest.json").is_file()
    monkeypatch.setattr(wsl, "to_windows", lambda path: "C:\\" + str(path.relative_to(inside)).replace("/", "\\"))
    assert browser.shown(folder) == "C:\\Users\\ana\\AppData\\Local\\pdms\\browser-extension"
    started = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda cmd, **kwargs: started.append(cmd))
    assert browser.open_page(browser.BY_KEY["edge"])
    assert started[-1] == [str(edge), "edge://extensions"]
    assert browser.open_page(browser.BY_KEY["edge"], str(folder / "x.xpi"))  # a file: given as Windows names it
    assert started[-1] == [str(edge), "C:\\Users\\ana\\AppData\\Local\\pdms\\browser-extension\\x.xpi"]


def test_windows_start_menu_and_login_run_pdms_ui_inside_wsl(inside, monkeypatch) -> None:
    """Shortcuts of Windows (made by its PowerShell) that run a script starting pdms ui in WSL, with no window."""
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    variables = {"APPDATA": "C:\\Users\\ana\\AppData\\Roaming", "LOCALAPPDATA": "C:\\Users\\ana\\AppData\\Local"}
    monkeypatch.setattr(wsl, "windows_variable", lambda name: variables.get(name, ""))
    wsl.to_linux(variables["APPDATA"]).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wsl, "to_windows", lambda path: "C:\\" + str(path.relative_to(inside)).replace("/", "\\"))
    monkeypatch.setattr(desktop, "pdms_executable", lambda gui=False: Path("/home/ana/.local/bin/pdms"))
    made = []

    def powershell(cmd, env, **kwargs):
        made.append({key: env[key] for key in ("PDMS_LNK", "PDMS_TARGET", "PDMS_ARGS")} | {"WSLENV": env["WSLENV"]})
        Path(wsl.to_linux(env["PDMS_LNK"])).write_text("lnk", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", powershell)
    entry = desktop.install()
    desktop.set_autostart(True)
    programs = "C:\\Users\\ana\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs"
    launcher = "C:\\Users\\ana\\AppData\\Local\\pdms\\pdms-ui.vbs"
    assert made == [
        {"PDMS_LNK": programs + "\\pdms.lnk", "PDMS_TARGET": "wscript.exe", "PDMS_ARGS": f'"{launcher}"',
         "WSLENV": "PDMS_LNK:PDMS_TARGET:PDMS_ARGS:PDMS_ICON:PDMS_COMMENT"},
        {"PDMS_LNK": programs + "\\Startup\\pdms.lnk", "PDMS_TARGET": "wscript.exe",
         "PDMS_ARGS": f'"{launcher}" --no-browser', "WSLENV": "PDMS_LNK:PDMS_TARGET:PDMS_ARGS:PDMS_ICON:PDMS_COMMENT"},
    ]
    script = wsl.to_linux(launcher).read_text(encoding="utf-8")
    assert "wsl.exe -d Ubuntu --cd ~ -e /home/ana/.local/bin/pdms ui --detached" in script and ", 0, False" in script
    assert b"\r\n" in wsl.to_linux(launcher).read_bytes()  # a Windows script
    assert entry.exists() and desktop.installed()
    removed = desktop.uninstall()
    assert len(removed) == 4 and not entry.exists() and not wsl.to_linux(launcher).exists()


def test_the_certificate_windows_trusts_is_found_by_its_thumbprint(tmp_path, monkeypatch) -> None:
    ca = tmp_path / "rootCA.pem"
    ca.write_text(TEST_CA, encoding="ascii")
    assert wsl._thumbprint(ca) == "9a877db9d6d71dde8f35b9e291df3d25c29c203e"
    monkeypatch.setattr(wsl, "MKCERT_CA", ca)
    monkeypatch.setattr(wsl, "to_windows", lambda path: "\\\\wsl.localhost\\Ubuntu\\rootCA.pem")
    monkeypatch.setattr(wsl, "_tool", lambda name: f"/mnt/c/Windows/System32/{name}")
    trusted = {"9a877db9d6d71dde8f35b9e291df3d25c29c203e": False}
    monkeypatch.setattr(wsl, "_run", lambda cmd: "found" if trusted.get(cmd[-1]) else None)
    assert wsl.certificate() == ("untrusted", "\\\\wsl.localhost\\Ubuntu\\rootCA.pem")
    trusted["9a877db9d6d71dde8f35b9e291df3d25c29c203e"] = True
    assert wsl.certificate()[0] == "ok"
    assert wsl.trust_command() == ["/mnt/c/Windows/System32/certutil.exe", "-user", "-addstore", "Root",
                                   "\\\\wsl.localhost\\Ubuntu\\rootCA.pem"]


def test_doctor_says_what_reaches_windows(inside, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "certificate", lambda: ("untrusted", "\\\\wsl.localhost\\Ubuntu\\rootCA.pem"))
    cfg = Config(repos={"slow": Repo(path="/mnt/c/code/pdms"), "fast": Repo(path="/home/ana/pdms")})
    checks = {check.name: check for check in doctor.check_wsl(cfg)}
    assert checks["WSL"].status == doctor.OK
    assert checks["Repo slow"].status == doctor.WARN and "Repo fast" not in checks
    assert checks["Certificate of the web app"].hint == "Trust it in Windows: pdms browser trust"
    assert checks["Browsers of Windows"].status == doctor.WARN  # none installed in this fake C:
    monkeypatch.setattr(wsl, "detected", lambda: False)
    assert doctor.check_wsl(cfg) == []
