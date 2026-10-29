"""Versions: going back to an earlier one (which then is not offered again), the beta channel, and the bug report."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pdms_cli import __version__, actions, bugreport, cli, doctor, instances, proxy, update
from pdms_cli.commands import selfupdate
from pdms_cli.config import Config, Database
from pdms_cli.ui import updates as ui_updates

from test_ui_install import home, ui_with_control  # noqa: F401 - fixtures
from test_ui_server import get, post, wait_until

EARLIER = "0.0.1"  # older than any pdms that runs these tests


def fake_releases(monkeypatch, versions: list[str]) -> None:
    data = [{"tag_name": f"v{v}", "html_url": f"https://x/{v}", "body": f"notes {v}", "draft": False,
             "published_at": "2026-10-28T13:00:00Z"} for v in versions]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return json.dumps(data).encode()

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda request, timeout: Response())


# --------------------------------------------------------------------------- going back, skipping


def test_going_back_skips_the_version_left_and_a_newer_one_is_offered_again() -> None:
    update.remember_choice("0.4.11", current="0.4.12")
    assert update.skipped() == "0.4.12"
    assert update.offered("0.4.12", current="0.4.11") == ""  # the one that failed
    assert update.offered("0.4.13", current="0.4.11") == "0.4.13"  # its fix
    update.remember_choice("0.4.13", current="0.4.11")
    assert update.skipped() == ""  # passed it: nothing to remember


def test_a_skipped_version_is_never_announced(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "0.4.12")
    update.refresh(pre=False)
    update.skip("0.4.12")
    assert update.notice_due(pre=False, current="0.4.11", now=1000) is None
    update.skip("")
    assert update.notice_due(pre=False, current="0.4.11", now=1000) == "0.4.12"


def test_the_beta_channel_counts_pre_releases() -> None:
    assert not update.wants_pre("stable", current="0.4.12")
    assert update.wants_pre("beta", current="0.4.12")
    assert update.wants_pre("stable", current="0.4.13b1")  # running one: its stable comes after it


def test_releases_come_newest_first_with_their_date_and_kind(monkeypatch) -> None:
    fake_releases(monkeypatch, ["0.4.11", "0.4.13b1", "0.4.12"])
    found = update.releases()
    assert [r["version"] for r in found] == ["0.4.13b1", "0.4.12", "0.4.11"]
    assert found[0]["prerelease"] and not found[1]["prerelease"] and found[1]["date"] == "2026-10-28"


def test_the_channel_must_be_a_known_one(home) -> None:
    cfg = Config.load()
    with pytest.raises(actions.InvalidValue) as raised:
        actions.save_defaults(cfg, replace(cfg.defaults, update_channel="nightly"))
    assert raised.value.field == "update_channel"
    actions.save_defaults(cfg, replace(cfg.defaults, update_channel="beta"))
    assert Config.load().defaults.update_channel == "beta"


# --------------------------------------------------------------------------- pdms ui


def test_pdms_ui_lists_the_versions_and_betas_only_on_the_beta_channel(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, _control = ui_with_control
    fake_releases(monkeypatch, ["99.0.0b1", __version__, EARLIER])
    update.skip(EARLIER)
    versions = get(port, "/api/update/versions")[1]["versions"]
    assert [v["version"] for v in versions] == [__version__, EARLIER]
    assert versions[0]["current"] and versions[1]["skipped"]
    cfg = Config.load()
    actions.save_defaults(cfg, replace(cfg.defaults, update_channel="beta"))
    assert get(port, "/api/update/versions")[1]["versions"][0]["version"] == "99.0.0b1"


def test_skipping_the_offered_version_in_pdms_ui(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, _control = ui_with_control
    for name in ("CI", "PDMS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(update, "latest_version", lambda pre, timeout: "99.0.0")
    update.refresh(pre=False)
    assert ui_updates.state(Config.load())["latest"] == "99.0.0"
    assert post(port, "/api/update/skip", {"version": "99.0.0"}) == (200, {})
    info = ui_updates.state(Config.load())
    assert info["latest"] == "" and info["skipped"] == "99.0.0"
    assert post(port, "/api/update/skip", {"version": "nope"})[0] == 400


def test_going_back_from_pdms_ui_installs_that_version_and_skips_this_one(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, control = ui_with_control
    monkeypatch.setattr(ui_updates.sys, "platform", "linux")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(proxy, "running_proxy", lambda: None)
    ran = []
    monkeypatch.setattr(ui_updates.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0))
    quits = []
    control.attach(control._show, lambda: quits.append(control.restart))
    assert post(port, "/api/update/install", {"version": "x.y"})[0] == 400
    assert post(port, "/api/update/install", {"version": __version__})[0] == 400
    assert post(port, "/api/update/install", {"version": EARLIER})[0] == 202
    wait_until(lambda: quits)
    assert f"pdms_cli-{EARLIER}-py3-none-any.whl" in ran[0][-1]
    assert update.skipped() == __version__


# --------------------------------------------------------------------------- pdms self-update


def test_self_update_goes_back_to_an_earlier_version(home, monkeypatch) -> None:
    monkeypatch.setattr(selfupdate.sys, "platform", "linux")
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    monkeypatch.setattr(proxy, "running_proxy", lambda: None)
    ran = []
    monkeypatch.setattr(selfupdate.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0))
    result = CliRunner().invoke(cli.app, ["self-update", "--version", f"v{EARLIER}"])
    assert result.exit_code == 0, result.output
    assert "going back to" in result.output and "will not offer" in result.output
    assert EARLIER in ran[0][-1] and update.skipped() == __version__

    result = CliRunner().invoke(cli.app, ["self-update", "--version", "latest"])
    assert result.exit_code == 1 and "--list" in result.output


def test_self_update_lists_the_versions(home, monkeypatch) -> None:
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    fake_releases(monkeypatch, ["99.0.0b1", __version__, EARLIER])
    result = CliRunner().invoke(cli.app, ["self-update", "--list"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output and EARLIER in result.output and "99.0.0b1" not in result.output
    assert "you have it" in result.output
    assert "99.0.0b1" in CliRunner().invoke(cli.app, ["self-update", "--list", "--pre"]).output


# --------------------------------------------------------------------------- bug report


def test_the_bug_report_hides_secrets() -> None:
    text = ("db postgresql://pdm:Sup3r-secret@host:5432/pdm · password=abc · token: tok123 · AKIAABCDEFGHIJKLMNOP\n"
            "Sup3r-secret alone · the local copy")
    hidden = bugreport.redact(text, ["Sup3r-secret", "local"])
    assert "Sup3r-secret" not in hidden and "abc" not in hidden and "tok123" not in hidden and "AKIA" not in hidden
    assert "the local copy" in hidden  # a short password ("local") is not hidden everywhere


def test_the_bug_report_has_what_it_takes_and_no_password(home, monkeypatch) -> None:
    cfg = Config.load()
    cfg.dbs["web-dev"] = Database(host="db.example", user="pdm", password="Sup3r-secret")
    cfg.defaults.env = {"API_KEY": "value-of-the-key"}
    cfg.save()
    monkeypatch.setattr(doctor, "run_all", lambda cfg, databases=False: [
        doctor.Check("Network", "Tunnel to web-dev", doctor.FAIL, "nothing listens", "start the tunnel")])
    proxy.log_path().parent.mkdir(parents=True, exist_ok=True)
    proxy.log_path().write_text("connecting to postgresql://pdm:Sup3r-secret@db.example/pdm\nboom\n")
    (home / "Downloads").mkdir()
    path = bugreport.write(Config.load())
    assert path.parent == home / "Downloads" and path.name.startswith("pdms-bug-report-")
    text = path.read_text(encoding="utf-8")
    assert f"pdms: {__version__}" in text and "[fail] Network · Tunnel to web-dev" in text
    assert "databases: web-dev" in text and "env = ['API_KEY']" in text and "### proxy" in text and "boom" in text
    assert "Sup3r-secret" not in text and "value-of-the-key" not in text


def test_the_bug_report_includes_the_logs_of_failed_services(home, monkeypatch) -> None:
    log = instances.log_path("lead-tp-create@28101")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("Traceback (most recent call last):\nImportError: no module named x\n")
    inst = instances.Instance(key="lead-tp-create@28101", pid=0, service="/repo/lead/lead-tp-create", host="0.0.0.0",
                              port=28101, user="agent", db="web-dev", reload=True, log=str(log), started_at="10:00")
    monkeypatch.setattr(instances, "load", lambda: {inst.key: inst})
    monkeypatch.setattr(doctor, "run_all", lambda cfg, databases=False: [])
    text = bugreport.build(Config.load())
    assert "lead-tp-create@28101: stopped · user agent · db web-dev" in text
    assert "### lead-tp-create@28101" in text and "ImportError: no module named x" in text


def test_pdms_bug_report_command(home, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "run_all", lambda cfg, databases=False: [])
    result = CliRunner().invoke(cli.app, ["bug-report", "-o", str(home / "reports")])
    assert result.exit_code == 0, result.output
    assert len(list((home / "reports").glob("pdms-bug-report-*.md"))) == 1


def test_pdms_ui_saves_a_bug_report(ui_with_control, monkeypatch) -> None:
    port, _shown, _jobs, _control = ui_with_control
    monkeypatch.setattr(doctor, "run_all", lambda cfg, databases=False: [])
    status, data = post(port, "/api/bug-report")  # no Downloads in this home: the home itself
    assert status == 200 and data["path"].endswith(".md") and Path(data["path"]).is_file()
