"""pdms ui in a browser: every screen, starting a service, Settings, Versions and the command palette."""

from __future__ import annotations

import json
import os
import re

from playwright.sync_api import Page, Route, expect

from pdms_cli import update
from pdms_cli.config import Config

SCREENS = ("home", "services", "stacks", "proxy", "events", "data", "tests", "doctor", "settings")


def answer(route: Route, status: int, body: dict) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def test_every_screen_opens(ui: Page) -> None:
    for screen in SCREENS:
        ui.click(f'nav a[data-view="{screen}"], a[data-view="{screen}"]')
        expect(ui.locator(f"#view-{screen}")).to_be_visible()


def test_the_menu_folds_to_its_icons_and_stays_so(ui: Page) -> None:
    ui.set_viewport_size({"width": 1440, "height": 900})
    label = ui.locator('nav a[data-view="services"] .lbl')
    expect(label).to_be_visible()
    ui.click("#side-toggle")
    expect(label).to_be_hidden()
    expect(ui.locator("#side-toggle")).to_have_attribute("aria-expanded", "false")
    ui.reload()
    expect(label).to_be_hidden()
    ui.keyboard.press("Control+b")
    expect(label).to_be_visible()


def test_a_service_starts_once_however_fast_one_clicks(ui: Page) -> None:
    """While a start waits for the server or for an answer (migrations here), its button stays off (0.4.12)."""
    sent: list[dict] = []

    def run(route: Route) -> None:
        sent.append(json.loads(route.request.post_data or "{}"))
        if len(sent) == 1:
            answer(route, 409, {"decision": "migrations_pending", "dbs": [
                {"db": "local", "count": 2, "first": "V1", "last": "V2", "uncommitted": 0, "changed": []}]})
        else:
            answer(route, 202, {"job": "lead-tp-create@28100"})

    ui.route("**/api/run", run)
    # The job is made up: its logs too (the page opens them once the start is accepted).
    ui.route("**/api/logs/streams?*", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=""))
    ui.click('a[data-view="services"]')
    ui.click("#run-new")
    ui.locator("#run-services label", has_text="lead-tp-create").click()
    ui.dblclick("#restart-go")
    expect(ui.locator("#pending-dialog")).to_be_visible()
    expect(ui.locator("#restart-go")).to_be_disabled()
    ui.click("#pending-dialog #pending-apply")
    expect(ui.locator("#restart")).to_be_hidden()
    assert len(sent) == 2, sent
    assert sent[0]["service"] == "lead/lead-tp-create" and sent[0]["user"] == "agent" and sent[0]["db"] == "local"
    assert sent[1]["migrate"] is True


def test_several_services_start_at_once_or_become_a_stack(ui: Page) -> None:
    sent: list[dict] = []
    ui.route("**/api/run", lambda route: sent.append(json.loads(route.request.post_data or "{}"))
             or answer(route, 202, {"jobs": ["lead-tp-create@28100", "driver-get@28101"]}))
    ui.click('a[data-view="services"]')
    ui.click("#run-new")
    expect(ui.locator("#restart")).to_have_class(re.compile(r"\bl\b"))  # the list and the options side by side
    ui.locator("#run-services label", has_text="lead-tp-create").click()
    ui.locator("#run-services label", has_text="driver-get").click()
    expect(ui.locator("#run-picked-list .run-chip")).to_have_count(2)
    expect(ui.locator("#run-port-label")).to_be_hidden()  # each takes the next free port
    expect(ui.locator("#restart-go")).to_have_text("Start 2")
    ui.click("#restart-go")
    expect(ui.locator("#restart")).to_be_hidden()
    assert sent[0]["services"] == ["lead/lead-tp-create", "driver/driver-get"] and "service" not in sent[0]

    ui.click("#run-new")
    ui.fill("#run-filter", "lead")
    ui.click("#run-all")
    expect(ui.locator("#run-picked-list .run-chip")).to_have_count(2)
    ui.click("#run-save-stack")
    expect(ui.locator("#editor")).to_be_visible()
    expect(ui.locator("#editor-services input:checked")).to_have_count(2)


def test_settings_save_the_defaults_to_the_configuration(ui: Page) -> None:
    ui.click('a[data-view="settings"]')
    ui.click('#settings-tabs [data-tab="defaults"]')
    ui.fill("#default-port", "28500")
    ui.select_option("#default-update_channel", "beta")
    ui.click("#defaults-save")
    expect(ui.locator(".toast").last).to_be_visible()
    defaults = Config.load().defaults
    assert defaults.port == 28500 and defaults.update_channel == "beta"


def test_going_back_to_an_earlier_version(ui: Page, monkeypatch) -> None:
    monkeypatch.setattr(update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(update, "updates_itself", lambda: True)
    current = update.__version__
    versions = [{"version": v, "url": "", "body": f"## Fix\n- **ui**: fixed in {v}", "date": "2026-10-28",
                 "prerelease": False, "current": v == current, "skipped": False} for v in (current, "0.0.9")]
    installs: list[dict] = []
    ui.route("**/api/update/versions", lambda route: answer(route, 200, {"versions": versions}))
    ui.route("**/api/update/install", lambda route: installs.append(json.loads(route.request.post_data))
             or answer(route, 202, {"job": "update"}))
    ui.reload()  # the state now says this pdms updates itself
    ui.click('a[data-view="settings"]')
    ui.click('#settings-tabs [data-tab="defaults"]')
    ui.click("#version-list")
    rows = ui.locator("#versions-list .version-item")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("you have it")
    rows.nth(1).get_by_role("button", name="Notes").click()
    expect(ui.locator("#versions-notes")).to_contain_text("fixed in 0.0.9")
    rows.nth(1).get_by_role("button", name="Go back to it").click()
    expect(ui.locator("#confirm-title")).to_have_text("Go back to pdms 0.0.9?")
    with ui.expect_response("**/api/update/install"):
        ui.click("#confirm-yes")
    assert installs == [{"version": "0.0.9", "restart_proxy": True}]


def test_the_palette_finds_and_runs_an_action(ui: Page) -> None:
    ui.keyboard.press("Control+k")
    ui.keyboard.type("start service")
    ui.keyboard.press("Enter")
    expect(ui.locator("#restart")).to_be_visible()
    expect(ui.locator("#run-services label")).to_have_count(3)


def test_the_page_speaks_the_language_of_pdms(ui: Page) -> None:
    cfg = Config.load()
    cfg.defaults.language = "es"
    cfg.save()
    ui.reload()
    expect(ui.locator('a[data-view="services"] .lbl')).to_have_text("Servicios")
    ui.click('a[data-view="doctor"]')
    expect(ui.locator("#doctor-bug-report")).to_have_text("Informe de errores")


def test_a_new_stack_is_saved_in_the_configuration(ui: Page) -> None:
    ui.click('a[data-view="stacks"]')
    ui.click("#stack-new")
    ui.fill("#editor-name", "tp")
    for service in ("lead-tp-create", "lead-tp-list"):
        ui.locator("#editor-services label", has_text=service).locator("input").check()
    ui.select_option("#editor-user", "supervisor")
    ui.click("#editor-save")
    expect(ui.locator("#editor")).to_be_hidden()
    stack = Config.load().stacks["tp"]
    assert sorted(stack.services) == ["lead/lead-tp-create", "lead/lead-tp-list"] and stack.user == "supervisor"
    expect(ui.locator("#view-stacks")).to_contain_text("tp")


def test_requests_shows_the_proxy_requests_and_their_problems(ui: Page, machine) -> None:
    """A background proxy (this test's own pid stands for it) with a fast, a slow N+1 one and an error."""
    from pdms_cli import proxy

    proxy.state_path().parent.mkdir(parents=True, exist_ok=True)
    proxy.state_path().write_text(json.dumps({"pid": os.getpid(), "port": 28800, "background": True,
                                              "repo": str(machine / "pdms")}))
    proxy.log_path().parent.mkdir(parents=True, exist_ok=True)
    proxy.log_path().write_text(
        "10:00:01 GET    /api/v1/lead/1 200 → lead-tp-list@28101  42ms db 30ms 3q\n"
        "10:00:02 POST   /api/v1/lead 201 → lead-tp-create@28100  3100ms db 2900ms 40q ×12\n"
        "10:00:03 GET    /api/v1/driver/9 502 → remote  80ms\n", encoding="utf-8")
    ui.click('a[data-view="proxy"]')
    rows = ui.locator("#req-rows tr")
    expect(rows).to_have_count(3)
    for show, count in (("slow", 1), ("repeated", 1), ("errors", 1), ("all", 3)):
        ui.click(f'#req-seg button[data-show="{show}"]')
        expect(rows).to_have_count(count)
    expect(rows.nth(1)).to_contain_text("lead-tp-create@28100")


def test_a_rows_more_menu_runs_an_action_and_closes(ui: Page) -> None:
    ui.evaluate("""() => import("/static/js/core.js").then(({ moreButton }) => {
        window.ran = [];
        const more = moreButton("More", () => [
            { icon: "debug", label: "Debug", run: () => window.ran.push("debug") }, "-",
            { icon: "stop", label: "Stop", bad: true, run: () => window.ran.push("stop") }]);
        more.id = "more";
        document.querySelector("#view-home").append(more);
    })""")
    ui.click("#more")
    menu = ui.locator(".row-menu")
    expect(menu.get_by_role("menuitem")).to_have_count(2)
    expect(ui.locator("#more")).to_have_attribute("aria-expanded", "true")
    ui.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(ui.locator("#more")).to_be_focused()
    ui.click("#more")
    ui.keyboard.press("ArrowDown")
    ui.keyboard.press("Enter")
    expect(menu).to_have_count(0)
    assert ui.evaluate("window.ran") == ["stop"]
