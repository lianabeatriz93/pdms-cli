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
    assert sorted(path.name for path in browser.SOURCE.iterdir() if path.is_file()) == sorted(browser.FILES)


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
    assert browser.answer(config) == {"pdms": "front-redirect", "active": False, "target": "",
                                      "hosts": list(FRONT_HOSTS), "version": browser.version()}
    config.defaults.front_redirect = True
    monkeypatch.setattr(frontend, "running", lambda: None)
    assert not browser.answer(config)["active"]
    monkeypatch.setattr(frontend, "running", lambda: {"pid": 1, "port": 3001})
    monkeypatch.setattr(frontend, "scheme", lambda port, timeout=1.5: "")  # still starting
    assert not browser.answer(config)["active"]
    monkeypatch.setattr(frontend, "scheme", lambda port, timeout=1.5: "https")
    assert browser.answer(config) | {"version": ""} == {
        "pdms": "front-redirect", "active": True, "target": "https://localhost:3001", "hosts": list(FRONT_HOSTS),
        "version": "",
    }


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
