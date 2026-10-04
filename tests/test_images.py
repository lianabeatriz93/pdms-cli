"""pdms never downloads a Docker image without asking: what needs one stops, the CLI asks, pdms ui shows a dialog,
Doctor lists them."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pdms_cli import cli, doctor, images, localdb
from pdms_cli.commands import common


@pytest.fixture
def no_images(monkeypatch):
    """Docker answers, but no image is there; records the docker calls."""
    calls = []
    monkeypatch.setattr(images, "present", lambda name: False)
    monkeypatch.setattr(localdb.events, "docker_available", lambda: (True, "27"))
    monkeypatch.setattr(localdb, "_docker", lambda *args, timeout=120: calls.append(args) or (1, "No such object"))
    return calls


def test_starting_pdms_postgres_stops_before_downloading(no_images) -> None:
    with pytest.raises(images.Missing) as missing:
        localdb.up()
    assert [image.name for image in missing.value.images] == [images.POSTGRES]
    assert "about 155 MB" in str(missing.value)
    assert not [call for call in no_images if call[0] in ("run", "pull")]  # nothing downloaded or created


def test_the_cli_says_what_to_download_without_a_terminal(no_images, monkeypatch) -> None:
    pulled = []
    monkeypatch.setattr(images, "pull", lambda name, output=None: pulled.append(name))
    result = CliRunner().invoke(cli.app, ["db", "local", "up"])
    text = " ".join(result.output.split())
    assert result.exit_code != 0 and f"docker pull {images.POSTGRES}" in text and not pulled


def test_the_cli_asks_and_goes_on_once_downloaded(monkeypatch) -> None:
    there = set()
    monkeypatch.setattr(common, "interactive_terminal", lambda: True)
    monkeypatch.setattr(common.questionary, "confirm",
                        lambda text, default: type("Q", (), {"unsafe_ask": lambda self: True})())
    monkeypatch.setattr(images, "present", lambda name: name in there)
    monkeypatch.setattr(images, "pull", lambda name, output=None: there.add(name))
    runs = []

    def work():
        runs.append(1)
        images.require([images.POSTGRES])
        return "started"

    assert common.with_images(work) == "started" and there == {images.POSTGRES} and len(runs) == 2


def test_doctor_lists_the_images_with_a_download_button(monkeypatch) -> None:
    monkeypatch.setattr(images, "present", lambda name: name == images.POSTGRES)
    checks = {check.name: check for check in doctor.check_images()}
    assert checks[images.POSTGRES].status == doctor.OK
    flyway = checks[images.FLYWAY]
    assert flyway.status == doctor.WARN and flyway.fix == f"pull_image:{images.FLYWAY}"
    assert "~360 MB" in flyway.detail and flyway.hint == f"docker pull {images.FLYWAY}"


def test_pdms_ui_asks_with_the_images_and_pulls_only_known_ones(monkeypatch) -> None:
    from pdms_cli import actions
    from pdms_cli.ui import jobs as ui_jobs
    from pdms_cli.ui import server as ui_server

    body = ui_server.decision_body(images.Missing([images.IMAGES[images.FLYWAY]]))
    assert body["decision"] == "images_missing" and body["images"][0]["download_mb"] == 360
    jobs = ui_jobs.Jobs()
    with pytest.raises(actions.ActionError, match="not an image pdms uses"):
        jobs.pull_images(["evil/image:latest"])
    with pytest.raises(actions.ActionError):
        jobs.pull_images([images.POSTGRES], then="rm -rf")
    monkeypatch.setattr(localdb, "state", lambda: {"exists": False, "running": False, "port": 0})
    monkeypatch.setattr(images, "present", lambda name: False)
    with pytest.raises(images.Missing):
        jobs.postgres("up")
