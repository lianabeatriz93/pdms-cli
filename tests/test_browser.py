"""The browser extension: what pdms ui tells it, who may ask, the known fronts and installing it."""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, browser, cli, frontend
from pdms_cli.config import FRONT_HOSTS, Config
from pdms_cli.ui import jobs as ui_jobs

from test_ui_server import post, request, ui  # noqa: F401 - ui is a fixture

EXTENSION = f"chrome-extension://{browser.EXTENSION_ID}"


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    Config().save()
    return tmp_path


@pytest.fixture
def front(monkeypatch):
    """The frontend pdms started, answering https on 3000."""
    monkeypatch.setattr(frontend, "running", lambda: {"pid": 1, "port": 3000})
    monkeypatch.setattr(frontend, "scheme", lambda port, timeout=1.5: "https")


def test_the_id_comes_from_the_key_of_the_manifest() -> None:
    manifest = json.loads(browser.SOURCE.joinpath("manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == browser.EXTENSION_ID
    assert manifest["version"] == browser.version()
    files = [path.name for path in browser.SOURCE.iterdir() if path.is_file() and path.name != browser.XPI]
    assert sorted(files) == sorted(browser.FILES)


def test_the_folder_is_written_once_and_refreshed_only_when_there() -> None:
    browser.refresh_extension()
    assert not browser.extension_dir().exists()
    folder = browser.write_extension()
    assert sorted(path.name for path in folder.iterdir()) == sorted(browser.FILES)
    (folder / "background.js").write_text("old", encoding="utf-8")
    browser.refresh_extension()
    assert (folder / "background.js").read_bytes() == browser.SOURCE.joinpath("background.js").read_bytes()


def test_the_known_fronts_are_hosts_of_alivi_com() -> None:
    assert actions.check_front_hosts([" https://QA-pdm.alivi.com/leads?id=1 ", "dev-pdm.alivi.com/x", "qa-pdm.alivi.com"]) \
        == ["qa-pdm.alivi.com", "dev-pdm.alivi.com"]
    for wrong in (["example.com"], ["evil-alivi.com"], ["alivi.com.example.com"], ["   "], []):
        with pytest.raises(actions.InvalidValue) as error:
            actions.check_front_hosts(wrong)
        assert error.value.field == "front_redirect_hosts"


def test_it_redirects_only_when_on_and_the_frontend_answers(cfg, monkeypatch) -> None:
    config = Config.load()
    assert browser.answer(config) == {
        "pdms": "front-redirect", "active": False, "target": "", "redirect": False, "hosts": list(FRONT_HOSTS),
        "version": browser.version(), "language": "en",
    }
    monkeypatch.setattr(frontend, "running", lambda: {"pid": 1, "port": 3001})
    monkeypatch.setattr(frontend, "scheme", lambda port, timeout=1.5: "https")
    off = browser.answer(config)
    assert (off["active"], off["target"]) == (False, "https://localhost:3001")  # the panel says where it would go
    config.defaults.front_redirect = True
    assert browser.answer(config)["active"]
    monkeypatch.setattr(frontend, "scheme", lambda port, timeout=1.5: "")  # still starting
    waiting = browser.answer(config)
    assert (waiting["active"], waiting["redirect"], waiting["target"]) == (False, True, "")
    monkeypatch.setattr(frontend, "running", lambda: None)
    assert not browser.answer(config)["active"]
    config.defaults.language = "es"
    assert browser.answer(config)["language"] == "es"


def test_the_status_lists_installed_browsers_and_where_it_asked_from(cfg, monkeypatch) -> None:
    monkeypatch.setattr(browser, "installed", lambda: {"chrome": "/usr/bin/google-chrome", "brave": "/usr/bin/brave"})
    browser.record_seen("brave", "1.0.0")
    browser.record_seen("firefox", "1.0.0")
    browser.record_seen("../../etc", "1.0.0")  # not a browser: ignored
    seen = browser.seen()
    seen["firefox"]["at"] = time.time() - 3600
    browser.seen_path().write_text(json.dumps(seen), encoding="utf-8")
    rows = {row["key"]: row for row in browser.status(Config.load())["browsers"]}
    assert list(rows) == ["chrome", "brave", "firefox"]
    assert (rows["chrome"]["found"], rows["chrome"]["connected"], rows["chrome"]["seen"]) == (True, False, "")
    assert rows["brave"]["connected"] and rows["brave"]["found"]
    assert rows["firefox"]["seen"] and not rows["firefox"]["connected"] and not rows["firefox"]["found"]


def test_only_the_extension_may_ask(ui, cfg, front) -> None:  # noqa: F811
    port = ui[0]

    def ask(headers: dict[str, str]):
        body = json.dumps({"browser": "edge", "version": "1.0.0"}).encode()
        response, raw, _conn = request(port, browser.STATE_PATH, {"Content-Type": "application/json", **headers},
                                       "POST", body)
        return response.status, raw

    assert ask({"Origin": f"http://127.0.0.1:{port}"})[0] == 403  # pdms ui's own page has no business here
    assert ask({"Origin": "https://evil.example"})[0] == 403
    assert ask({"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"})[0] == 403  # another extension
    assert ask({})[0] == 403
    assert "edge" not in browser.seen()
    status, raw = ask({"Origin": EXTENSION})
    assert status == 200
    assert json.loads(raw)["active"] is False  # the redirect is off
    assert "edge" in browser.seen()
    response, _raw, _conn = request(port, browser.STATE_PATH, {"Host": "evil.example", "Origin": EXTENSION}, "POST",
                                    b"{}")
    assert response.status == 403  # DNS rebinding


def test_its_panel_turns_the_redirect_on_and_off(ui, cfg, front) -> None:  # noqa: F811
    port = ui[0]

    def turn(on: object, origin: str = EXTENSION):
        body = json.dumps({"browser": "chrome", "version": browser.version(), "on": on}).encode()
        response, raw, _conn = request(port, browser.REDIRECT_PATH,
                                       {"Content-Type": "application/json", "Origin": origin}, "POST", body)
        return response.status, json.loads(raw) if raw.startswith(b"{") else {}

    status, data = turn(True)
    assert status == 200 and data["active"] and data["redirect"] and Config.load().defaults.front_redirect
    assert turn(False, f"http://127.0.0.1:{port}")[0] == 403  # only the extension
    assert turn("off")[0] == 400
    assert Config.load().defaults.front_redirect is True
    status, data = turn(False)
    assert status == 200 and not data["active"] and Config.load().defaults.front_redirect is False


def test_the_page_turns_it_on_sets_the_fronts_and_installs(ui, cfg, monkeypatch) -> None:  # noqa: F811
    port = ui[0]
    assert post(port, "/api/browser/redirect", {"on": True}) == (200, {})
    assert Config.load().defaults.front_redirect is True
    assert post(port, "/api/browser/redirect", {"on": "yes"})[0] == 400
    assert post(port, "/api/browser/hosts", {"hosts": ["https://uat-pdm.alivi.com/"]}) == (
        200, {"hosts": ["uat-pdm.alivi.com"]})
    status, data = post(port, "/api/browser/hosts", {"hosts": ["example.com"]})
    assert status == 400 and "alivi.com" in data["error"]
    assert Config.load().defaults.front_redirect_hosts == ["uat-pdm.alivi.com"]

    opened = []
    monkeypatch.setattr(browser, "installed", lambda: {"edge": "/usr/bin/microsoft-edge"})
    monkeypatch.setattr(browser, "open_page", lambda found: opened.append(found.key) or True)
    status, data = post(port, "/api/browser/install", {"browser": "edge"})
    assert status == 200 and opened == ["edge"]
    assert data == {"name": "Microsoft Edge", "page": "edge://extensions", "folder": str(browser.extension_dir()),
                    "opened": True}
    assert (browser.extension_dir() / "manifest.json").is_file()
    monkeypatch.setattr(browser, "executable", lambda found: None)
    assert post(port, "/api/browser/install", {"browser": "brave"})[0] == 400  # not installed here
    assert post(port, "/api/browser/install", {"browser": "netscape"})[0] == 400


def test_the_defaults_form_keeps_and_checks_the_fronts(cfg) -> None:
    current = Config.load().defaults
    assert ui_jobs.defaults_from({}, current).front_redirect_hosts == list(FRONT_HOSTS)
    with pytest.raises(actions.InvalidValue):
        ui_jobs.defaults_from({"front_redirect_hosts": "dev-pdm.alivi.com"}, current)
    with pytest.raises(actions.InvalidValue):
        actions.save_defaults(Config.load(), ui_jobs.defaults_from({"front_redirect_hosts": ["example.com"]}, current))


def test_the_command_turns_it_on_with_other_fronts(cfg, monkeypatch) -> None:
    result = CliRunner().invoke(cli.app, ["browser", "redirect", "on", "--host", "https://qa-pdm.alivi.com/x"])
    assert result.exit_code == 0, result.output
    assert (Config.load().defaults.front_redirect, Config.load().defaults.front_redirect_hosts) == (
        True, ["qa-pdm.alivi.com"])
    assert CliRunner().invoke(cli.app, ["browser", "redirect", "on", "--host", "example.com"]).exit_code == 1
    assert CliRunner().invoke(cli.app, ["browser", "redirect", "maybe"]).exit_code == 1
    monkeypatch.setattr(browser, "installed", lambda: {"chrome": "/usr/bin/google-chrome"})
    monkeypatch.setattr(browser, "open_page", lambda found: False)
    result = CliRunner().invoke(cli.app, ["browser", "install"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "chrome://extensions" in result.output and str(browser.extension_dir()) in result.output
    assert "Google Chrome" in CliRunner().invoke(cli.app, ["browser"]).output


def test_firefox_gets_its_own_manifest(tmp_path) -> None:
    folder = browser.write_firefox_source(tmp_path / "firefox")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "key" not in manifest and manifest["background"] == {"scripts": ["texts.js", "background.js"]}
    assert manifest["browser_specific_settings"]["gecko"]["id"] == browser.GECKO_ID
    assert manifest["version"] == browser.version()
    assert sorted(path.name for path in folder.iterdir()) == sorted(browser.FILES)


def packaged(monkeypatch, tmp_path, xpi: bytes | None):
    """The package's extension folder, with or without the signed file release.yml puts there."""
    source = tmp_path / "package"
    source.mkdir()
    for name in browser.FILES:
        (source / name).write_bytes(browser.SOURCE.joinpath(name).read_bytes())
    if xpi is not None:
        (source / browser.XPI).write_bytes(xpi)
    monkeypatch.setattr(browser, "SOURCE", source)


def test_firefox_opens_the_signed_file_that_came_with_pdms(cfg, monkeypatch, tmp_path) -> None:
    packaged(monkeypatch, tmp_path, b"signed")
    monkeypatch.setattr(browser, "installed", lambda: {"firefox": "/usr/bin/firefox"})
    opened = []
    monkeypatch.setattr(browser, "open_page", lambda found, target="": opened.append((found.key, target)) or True)
    found, xpi, ok = actions.install_extension("firefox")
    assert (found.name, ok, xpi.read_bytes()) == ("Firefox", True, b"signed")
    assert xpi.name == f"pdms-front-redirect-{browser.version()}.xpi" and opened == [("firefox", str(xpi))]


def test_a_snap_firefox_gets_it_in_downloads(cfg, monkeypatch, tmp_path) -> None:
    packaged(monkeypatch, tmp_path, b"signed")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(browser.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(browser, "installed", lambda: {"firefox": "/snap/bin/firefox"})
    assert browser.firefox_xpi().parent == tmp_path / "home" / "Downloads"


class Answer:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def read(self, *args) -> bytes:
        return self.body


def test_a_checkout_downloads_it_from_the_latest_release(cfg, monkeypatch, tmp_path) -> None:
    packaged(monkeypatch, tmp_path, None)
    monkeypatch.setattr(browser, "installed", lambda: {"firefox": "/usr/bin/firefox"})
    release = {"assets": [{"name": "pdms_cli-0.4.7-py3-none-any.whl", "browser_download_url": "https://x/whl"},
                          {"name": "pdms-front-redirect-1.0.0.xpi", "browser_download_url": "https://x/xpi"}]}
    asked = []

    def urlopen(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        asked.append(url)
        return Answer(json.dumps(release).encode() if url.endswith("/latest") else b"downloaded")

    monkeypatch.setattr(browser.urllib.request, "urlopen", urlopen)
    xpi = browser.firefox_xpi()
    assert (xpi.name, xpi.read_bytes()) == ("pdms-front-redirect-1.0.0.xpi", b"downloaded")
    assert asked[-1] == "https://x/xpi"
    release["assets"] = release["assets"][:1]
    with pytest.raises(actions.ActionError, match="no signed extension"):
        actions.install_extension("firefox")


def test_an_older_firefox_extension_asks_for_the_new_file(cfg, monkeypatch) -> None:
    monkeypatch.setattr(browser, "installed", lambda: {"firefox": "/usr/bin/firefox"})
    browser.record_seen("firefox", "0.9.0")
    browser.record_seen("chrome", "0.9.0")  # Chromium browsers load the new files by themselves
    rows = {row["key"]: row for row in browser.status(Config.load())["browsers"]}
    assert rows["firefox"]["outdated"] and not rows["chrome"]["outdated"]
    browser.record_seen("firefox", browser.version())
    assert not browser.status(Config.load())["browsers"][-1]["outdated"]


def test_it_reloads_only_once_the_folder_has_the_new_files(cfg) -> None:
    assert browser.answer(Config.load())["version"] == browser.version()  # no folder yet
    folder = browser.write_extension()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    (folder / "manifest.json").write_text(json.dumps({**manifest, "version": "0.9.0"}), encoding="utf-8")
    assert browser.answer(Config.load())["version"] == "0.9.0"  # pdms changed, the folder not yet: no reload
    browser.refresh_extension()
    assert browser.answer(Config.load())["version"] == browser.version()
