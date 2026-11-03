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


def test_read_by_says_which_running_services_read_the_copy(ui: Page) -> None:
    reader = {"service": "report-lead-run", "key": "report-lead-run:28104", "view": "pdms_report_lead_view"}
    views = {**VIEWS, "views": [{**VIEWS["views"][0], "in_use": True, "read_by": [
        {**reader, "reads": "pdms_report_lead_view_pdmp627", "next": "pdms_report_lead_view_pdmp627"},
        {**reader, "service": "report-download", "reads": "pdms_report_lead_view", "next": "pdms_report_lead_view_pdmp627"}]}]}
    ui.route("**/api/views", lambda route: answer(route, views))
    ui.click('nav a[data-view="data"]')
    cell = ui.locator("#data-views .read-by")
    expect(cell).to_contain_text("report-lead-run the copy")
    expect(cell).to_contain_text("report-download restart: the copy")


def test_a_running_service_shows_the_view_it_reads(machine, ui: Page) -> None:
    import os

    from pdms_cli import instances

    service = machine / "pdms" / "backend" / "report-lead-run"
    instances.save({"r": instances.Instance(
        key="report-lead-run:28104", pid=os.getpid(), service=str(service), host="127.0.0.1", port=28104, user="agent",
        db="local", reload=False, log=str(machine / "r.log"), started_at="2026-11-03T10:00:00",
        created=instances.creation_time(os.getpid()), views={"pdms_report_lead_view": "pdms_report_lead_view_pdmp627"})})
    cfg = Config.load()
    cfg.defaults.test_views, cfg.defaults.test_views_db = {"pdms_report_lead_view": "pdms_report_lead_view_pdmp627"}, "local"
    cfg.save()
    ui.click('nav a[data-view="services"]')
    expect(ui.locator(".view-tag.test")).to_have_text("pdms_report_lead_view_pdmp627")
    cfg.defaults.test_views = {}  # Data → Test views: back to the real view, from the next start
    cfg.save()
    expect(ui.locator(".view-tag.test")).to_have_text("restart to read pdms_report_lead_view")
