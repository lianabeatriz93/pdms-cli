"""Dialogs: copying from one opened over another, and their buttons always in sight however long they get."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

SQL = "CREATE MATERIALIZED VIEW pdms_report_lead_view_pdmp627 AS SELECT 1;"
NO_CLIPBOARD_API = """
// As in pdms ui's own window (pywebview), where the page may not use the clipboard API: only execCommand copies.
Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("denied")) } });
window.copied = [];
document.addEventListener("copy", (event) => window.copied.push(event.target.value ?? String(getSelection())));
"""


def answer(route: Route, status: int, body: dict) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def views_pending(route: Route) -> None:
    answer(route, 409, {"decision": "test_views_pending", "views": [{
        "view": "pdms_report_lead_view", "name": "pdms_report_lead_view_pdmp627", "services": ["lead-tp-create"],
        "exists": False, "current": False, "uncommitted": False, "sql": SQL}]})


def open_views_question(ui: Page) -> None:
    ui.route("**/api/run", views_pending)
    ui.click('a[data-view="services"]')
    ui.click("#run-new")
    ui.locator("#run-services label", has_text="lead-tp-create").click()
    ui.click("#restart-go")
    expect(ui.locator("#views-dialog")).to_be_visible()


def test_copy_works_in_a_dialog_opened_over_another(ui: Page) -> None:
    ui.add_init_script(NO_CLIPBOARD_API)
    ui.reload()
    open_views_question(ui)
    ui.locator("#views-list").get_by_role("button", name="Copy").click()
    expect(ui.locator(".toast").last).to_contain_text("SQL copied")
    assert ui.evaluate("window.copied") == [SQL]


def test_a_long_dialog_keeps_its_buttons_in_the_window(ui: Page) -> None:
    ui.set_viewport_size({"width": 900, "height": 420})
    open_views_question(ui)
    dialog = ui.locator("#views-dialog")
    box, buttons = dialog.bounding_box(), ui.locator("#views-dialog .actions").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 420, box  # the dialog fits in the window
    assert buttons["y"] + buttons["height"] <= 420, buttons  # and so do its buttons, without scrolling
    expect(ui.locator("#views-apply")).to_be_in_viewport()
    # More content (the Start dialog with a warning, behind it, too): the buttons stay where they are.
    ui.evaluate("document.querySelector('#views-list').append(...Array.from({length: 30}, (_, i) => "
                "Object.assign(document.createElement('li'), {textContent: 'row ' + i})))")
    expect(ui.locator("#views-apply")).to_be_in_viewport()
    ui.click("#views-dialog button[value=cancel]")
    expect(ui.locator("#restart-go")).to_be_in_viewport()
