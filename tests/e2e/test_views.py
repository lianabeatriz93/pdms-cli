"""pdms ui in a browser: test copies of materialized views (Data → Test views) and who else may read them."""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

from pdms_cli.config import Config

SQL = ("CREATE MATERIALIZED VIEW public.pdms_report_lead_view_pdmp627 AS SELECT 1;\n"
       'GRANT SELECT ON public.pdms_report_lead_view_pdmp627 TO "pdm-reader";')
VIEWS = {"db": "web-dev", "repo": "/r", "branch": "feature/PDMP-627-x", "suffix": "_pdmp627", "can_create": True,
         "views": [{"view": "pdms_report_lead_view", "name": "pdms_report_lead_view_pdmp627", "exists": True,
                    "owner": "me", "in_use": False, "changed": True, "uncommitted": False, "current": True,
                    "sql": SQL}]}


def answer(route: Route, body: dict) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_copy_sql_of_a_changed_view(ui: Page) -> None:
    ui.context.grant_permissions(["clipboard-read", "clipboard-write"])
    ui.route("**/api/views", lambda route: answer(route, VIEWS))
    ui.click('nav a[data-view="data"]')
    row = ui.locator("#data-views tr")
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Copy SQL").click()
    expect(ui.locator(".toast").last).to_contain_text("SQL copied")
    assert ui.evaluate("navigator.clipboard.readText()") == SQL


def test_a_database_keeps_who_else_may_read_its_test_views(ui: Page) -> None:
    ui.click('a[data-view="settings"]')
    ui.click('#settings-tabs [data-tab="dbs"]')
    ui.click("#db-new")
    ui.fill("#db-name", "team-db")
    ui.fill("#db-host", "db.example.com")
    ui.fill("#db-user", "pdm-writer")
    ui.fill("#db-view-readers", "pdm-reader, alice,, pdm-reader")
    ui.click("#db-save")
    expect(ui.locator("#db-dialog")).to_be_hidden()
    assert Config.load().dbs["team-db"].view_readers == ["pdm-reader", "alice"]
