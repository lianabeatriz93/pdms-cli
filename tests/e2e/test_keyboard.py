"""pdms ui from the keyboard: tab lists follow the arrow keys, as in any tab list."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_the_arrows_move_between_tabs(ui: Page) -> None:
    ui.goto(ui.url.split("#")[0] + "#settings")
    tabs = ui.locator("#settings-tabs [role=tab]")
    tabs.first.click()
    expect(tabs.first).to_have_attribute("aria-selected", "true")
    ui.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")
    expect(tabs.nth(1)).to_be_focused()
    ui.keyboard.press("ArrowLeft")
    ui.keyboard.press("ArrowLeft")  # past the first: the last
    expect(tabs.last).to_have_attribute("aria-selected", "true")
    ui.keyboard.press("Home")
    expect(tabs.first).to_have_attribute("aria-selected", "true")
    expect(ui.locator("#settings-repos")).to_be_visible()


def test_a_screen_opened_later_shows_its_list(ui: Page) -> None:
    # Home was showing when the state came: the stacks were not built then, opening the screen builds them.
    ui.goto(ui.url.split("#")[0] + "#stacks")
    expect(ui.locator("#stacks-empty")).to_be_visible()
    ui.goto(ui.url.split("#")[0] + "#services")
    expect(ui.locator("#rows tr").first).to_be_visible()
