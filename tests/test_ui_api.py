"""pdms ui's API without HTTP: every route the page calls exists, and errors come back as the page expects them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pdms_cli import actions
from pdms_cli.ui import api as ui_api
from pdms_cli.ui import jobs as ui_jobs
from pdms_cli.ui.control import Control

STATIC = Path(ui_api.__file__).parent / "static"
SERVED_BY_THE_SERVER = {"/api/stream", "/api/emails/html", "/api/logs", "/api/logs/stream", "/api/logs/streams"}


def routed(path: str) -> bool:
    if path in ui_api.GETS or path in ui_api.POSTS or path in SERVED_BY_THE_SERVER:
        return True
    parts = path.split("/")
    if len(parts) == 4 and parts[2] in ui_api.VERBS:
        return parts[3] in ui_api.VERBS[parts[2]] or parts[2] == "data"  # Data's own verbs: ui_jobs.data_action
    return len(parts) == 5 and parts[2] in ui_api.NAMED and parts[4] in ui_api.NAMED[parts[2]]


def test_every_path_the_page_calls_has_a_route() -> None:
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.rglob("*.js"))
    paths = set(re.findall(r"""["'`](/api/[a-z0-9/_-]*[a-z0-9])["'`]""", scripts))
    assert len(paths) > 60  # the pattern still finds them
    assert sorted(path for path in paths if not routed(path)) == []


def test_every_verb_of_a_named_route_the_page_builds_has_a_route() -> None:
    """The verbs after a name: written out (``/api/repos/${name}/use``) or through the page's helpers
    (``${stackPath(name)}/save``, ``settingsPath("dbs", name, "password")``, whose kind may be dbs or users)."""
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.rglob("*.js"))
    used = {group: set(re.findall(rf"/api/{group}/\$\{{[^}}]+\}}/([a-z-]+)", scripts)) for group in ui_api.NAMED}
    used["stacks"] |= set(re.findall(r"\$\{stackPath\([^)]*\)\}/([a-z-]+)", scripts))
    for kind, verb in re.findall(r"settingsPath\((\"\w+\"|\w+), [^,]+, \"([a-z-]+)\"\)", scripts):
        for group in [kind.strip('"')] if kind.startswith('"') else ["dbs", "users"]:  # a variable: either one
            used[group].add(verb)
    assert used["stacks"] and used["dbs"]  # the patterns still find them
    for group, verbs in used.items():
        assert verbs <= set(ui_api.NAMED[group]), (group, verbs - set(ui_api.NAMED[group]))


@pytest.fixture
def api():
    hub = type("Hub", (), {"poke": lambda self: None, "latest": "", "watchers": 0, "build": lambda self: {}})()
    return ui_api.Api(hub, ui_jobs.Jobs(), Control(""))


def test_an_unknown_route_is_404(api) -> None:
    assert api.post("/api/nope", {}) == (404, {"error": "not found"})
    assert api.post("/api/stacks/tp/nope", {}) == (404, {"error": "not found"})
    assert api.post("/api/events/nope", {}) == (404, {"error": "not found"})
    assert api.get("/api/nope", {}) is None


def test_errors_come_back_as_the_page_shows_them(api, monkeypatch) -> None:
    assert api.post("/api/doctor/run", {"databases": "yes"}) == (400, {"error": "databases must be true or false"})

    def invalid(body):
        raise actions.InvalidValue("port", "[bold]Must[/] be a number")

    monkeypatch.setattr(ui_jobs, "save_defaults", invalid)
    assert api.post("/api/defaults/save", {}) == (400, {"error": "Must be a number", "field": "port"})

    def protected(*args, **kwargs):
        raise actions.ProtectedDatabase("shared")

    monkeypatch.setattr(api.jobs, "start", protected)
    assert api.post("/api/run", {"service": "lead/a"}) == (409, {"decision": "protected_database", "name": "shared"})


def test_elasticmq_going_away_is_502(api, monkeypatch) -> None:
    def gone(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(ui_jobs, "events_purge", gone)
    monkeypatch.setattr(ui_jobs, "events_queues", gone)
    status, data = api.post("/api/events/purge", {"queues": []})
    assert status == 502 and data["error"].startswith("ElasticMQ")
    status, data = api.get("/api/events/queues", {})
    assert status == 502 and data["error"].startswith("ElasticMQ")


def test_github_not_answering_is_502(api, monkeypatch) -> None:
    def offline(*args):
        raise actions.ActionError("Could not reach GitHub: offline")

    monkeypatch.setattr(ui_api.ui_updates, "notes", offline)
    assert api.get("/api/update/notes", {"version": ["9.0.0"]}) == (502, {"error": "Could not reach GitHub: offline"})
