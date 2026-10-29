"""pdms ui in a real browser: the page and its scripts against the real server, on a configuration of their own.

They run with ``PDMS_E2E=1`` (the CI's e2e job, or ``PDMS_E2E=1 uv run pytest tests/e2e``) and are skipped otherwise:
they need a browser, Playwright's Chromium or else the system's Google Chrome. The server and the state are the real
ones; what would start real processes (services, the proxy, updates) is answered in the browser with ``page.route``,
so a test also decides what the server says.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from pdms_cli.config import Config, Database, DevUser, Repo
from pdms_cli.ui import server as ui_server
from pdms_cli.ui.control import Control

TOKEN = "e2e-token"
SERVICES = ("lead/lead-tp-create", "lead/lead-tp-list", "driver/driver-get")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("PDMS_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="pdms ui in a browser: PDMS_E2E=1 to run it")
    for item in items:
        if "tests/e2e/" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_type):
    """Playwright's own Chromium when it is installed (the CI), else the system's Chrome (no download)."""
    if not Path(browser_type.executable_path).exists() and shutil.which("google-chrome"):
        return {**browser_type_launch_args, "channel": "chrome"}
    return browser_type_launch_args


@pytest.fixture
def machine(tmp_path, monkeypatch) -> Path:
    """A home, a configuration (two users, a local and a protected database) and a repo with three services."""
    for name, sub in (("HOME", ""), ("USERPROFILE", ""), ("XDG_CONFIG_HOME", ".config"),
                      ("XDG_STATE_HOME", ".local/state"), ("XDG_DATA_HOME", ".local/share")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    repo = tmp_path / "pdms"
    for service in SERVICES:
        folder = repo / "backend" / service
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_text('[tool.poetry]\nname = "x"\n')
        (folder / "main.py").write_text("app = None\n")
    Config(
        users={"agent": DevUser("u1", "agent@x.com"), "supervisor": DevUser("u2", "boss@x.com")},
        dbs={"local": Database("localhost"), "shared": Database("db.example.com", password="hunter2", protected=True)},
        repos={"pdms": Repo(str(repo))}, current_repo="pdms", last_user="agent", last_db="local",
    ).save()
    return tmp_path


@pytest.fixture
def pdms_ui(machine):
    """The real pdms ui server (state, jobs, API) on a free port; yields the link that opens it."""
    hub, jobs = ui_server.make_app()
    control = Control("http://127.0.0.1:1/")
    control.attach(lambda: None, lambda: None)
    server = ui_server.make_server("127.0.0.1", 0, TOKEN, hub, jobs, control)
    thread = threading.Thread(target=ui_server.serve, args=(server, hub), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(5)


@pytest.fixture
def ui(page, pdms_ui):
    """The page open on Home, failing the test on any error of its scripts."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # The console only says "Failed to load resource" for any answer >= 400, and 400/409 are how the server tells the
    # page something it shows (nothing configured yet, a question): a missing route or a crash is what fails.
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" and not message.text.startswith("Failed to load resource") else None)
    page.on("response", lambda response: errors.append(f"{response.status} {response.request.method} {response.url}")
            if (response.status == 404 or response.status >= 500) and response.url.startswith(pdms_ui) else None)
    page.goto(f"{pdms_ui}/?token={TOKEN}")
    page.wait_for_selector("#view-home:not([hidden])")
    yield page
    assert not errors, errors
