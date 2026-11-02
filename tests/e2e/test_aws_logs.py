"""pdms ui in a browser: AWS logs, with CloudWatch's answers given in the browser (no AWS)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from pdms_cli.config import Config

LAMBDAS = {"profile": "pdm-dev", "expired": False, "error": "", "flow": ["broker-sqs-event", "account-publish-ev"],
           "lambdas": [{"name": "account-publish-ev", "role": "sns", "detail": "sns-account-publish.fifo"},
                       {"name": "broker-sqs-event", "role": "broker", "detail": "broker.fifo"},
                       {"name": "auth-profile-get", "role": "", "detail": ""}]}


def line(ident: str, at: int, function: str, level: str, messages: list[dict], text: str = "") -> dict:
    return {"at": at, "time": "", "function": function, "stream": "2026/11/02/[$LATEST]abc", "id": ident,
            "text": text or f"{function} line {ident}", "level": level, "logger": "x", "where": "m:1", "request": "",
            "platform": False, "more": [], "messages": messages}


SENT = {"direction": "sent", "operation": "SendMessage", "destination": "lead_tp_export-sqs-queue.fifo", "kind": "sqs",
        "type": "lead-tp-export", "body": {"type": "lead-tp-export", "file_name": "tp.csv"}}
PUBLISHED = {"direction": "sent", "operation": "Publish", "destination": "sns-account-publish.fifo", "kind": "sns",
             "type": "TP_ACCOUNT_UPDATED", "body": {"event": "TP_ACCOUNT_UPDATED", "data": {"entity_id": "a-1"}}}
LINES = [line("1", 1_793_000_000_000, "broker-sqs-event", "DEBUG", [SENT]),
         line("2", 1_793_000_100_000, "account-publish-ev", "DEBUG", [PUBLISHED]),
         line("3", 1_793_000_200_000, "broker-sqs-event", "ERROR", [], "Failed event broker: boom")]


@pytest.fixture
def aws_profile(machine):
    cfg = Config.load()
    cfg.defaults.aws_profile = "pdm-dev"
    cfg.save()


def answer(route: Route, body: dict) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_aws_logs_reads_the_event_flow_and_shows_an_event(aws_profile, ui: Page) -> None:
    asked: list[dict] = []

    def read(route: Route) -> None:
        asked.append(parse_qs(urlparse(route.request.url).query))
        answer(route, {"profile": "pdm-dev", "lines": LINES, "errors": {}, "more": [], "expired": False, "error": "",
                       "asked": 1_793_000_300_000})

    ui.route("**/api/aws-logs/lambdas*", lambda route: answer(route, LAMBDAS))
    ui.route("**/api/aws-logs/read*", read)
    ui.click('nav a[data-view="aws-logs"]')
    rows = ui.locator("#awslogs-rows tr")
    expect(rows).to_have_count(3)
    assert asked[0]["functions"] == ["broker-sqs-event,account-publish-ev"] and "all" not in asked[0]
    expect(rows.first).to_contain_text("Failed event broker")  # newest first
    expect(ui.locator("#awslogs-chosen .pill")).to_have_count(2)
    expect(ui.locator("#awslogs-level button[data-level=ERROR] span")).to_have_text("1")

    rows.nth(2).click()
    detail = ui.locator("#awslogs-detail")
    expect(detail).to_be_visible()
    expect(ui.locator("#awslogs-d-title")).to_have_text("lead-tp-export")
    expect(detail).to_contain_text("→ lead_tp_export-sqs-queue.fifo · lead-tp-export")
    expect(detail.locator(".j-key").first).to_have_text('"type"')
    ui.click('#awslogs-d-tabs button[data-tab="raw"]')
    expect(detail.locator("pre")).to_have_text("broker-sqs-event line 1")

    ui.click('#awslogs-level button[data-level="ERROR"]')
    expect(rows).to_have_count(1)
    ui.click('#awslogs-level button[data-level=""]')

    ui.fill("#awslogs-type", "lead-tp-export")
    with ui.expect_request("**/api/aws-logs/read*") as request:
        ui.press("#awslogs-type", "Enter")
    assert parse_qs(urlparse(request.value.url).query)["type"] == ["lead-tp-export"]


def test_aws_logs_keeps_a_preset_of_chosen_lambdas(aws_profile, ui: Page) -> None:
    ui.route("**/api/aws-logs/lambdas*", lambda route: answer(route, LAMBDAS))
    ui.route("**/api/aws-logs/read*", lambda route: answer(route, {
        "profile": "pdm-dev", "lines": [], "errors": {}, "more": [], "expired": False, "error": "", "asked": 1}))
    ui.click('nav a[data-view="aws-logs"]')
    expect(ui.locator("#awslogs-empty")).to_be_visible()
    ui.click("#awslogs-pick")
    expect(ui.locator("#awslogs-pick-list label")).to_have_count(2)  # only those in events
    ui.uncheck("#awslogs-pick-events")
    expect(ui.locator("#awslogs-pick-list label")).to_have_count(3)
    ui.uncheck('#awslogs-pick-list input[value="account-publish-ev"]')
    ui.check('#awslogs-pick-list input[value="auth-profile-get"]')
    ui.fill("#awslogs-pick-name", "PDMP-1")
    ui.click("#awslogs-pick-go")
    expect(ui.locator("#awslogs-chosen .pill")).to_have_count(2)
    expect(ui.locator("#awslogs-preset")).to_have_value("PDMP-1")
    ui.reload()
    ui.click('nav a[data-view="aws-logs"]')
    expect(ui.locator("#awslogs-chosen")).to_contain_text("auth-profile-get")


def test_aws_logs_without_a_profile_says_where_to_choose_it(ui: Page) -> None:
    ui.click('nav a[data-view="aws-logs"]')
    expect(ui.locator("#awslogs-problem")).to_be_visible()
    expect(ui.locator("#awslogs-problem-title")).to_have_text("Choose the AWS profile of dev")


def test_an_expired_session_offers_to_sign_in(aws_profile, ui: Page) -> None:
    ui.route("**/api/aws-logs/lambdas*", lambda route: answer(route, {**LAMBDAS, "expired": True, "error": "Token expired"}))
    ui.route("**/api/aws-logs/read*", lambda route: answer(route, {
        "profile": "pdm-dev", "lines": [], "errors": {}, "more": [], "expired": True, "error": "Token expired", "asked": 1}))
    ui.click('nav a[data-view="aws-logs"]')
    expect(ui.locator("#awslogs-problem-title")).to_have_text("Sign in to AWS to read the logs of pdm-dev")
    expect(ui.locator("#awslogs-problem-actions")).to_contain_text("Sign in with AWS SSO")
