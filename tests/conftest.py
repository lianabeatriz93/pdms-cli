"""Shared test setup."""

from __future__ import annotations

import pytest

from pdms_cli import i18n


@pytest.fixture(autouse=True)
def english():
    """Tests assert English texts, whatever language the developer's own config uses."""
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture(autouse=True)
def own_state(tmp_path_factory, monkeypatch):
    """Never touch the developer's own pdms state (a running pdms ui, its proxy...)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def fresh_strays():
    """pdms ui keeps the services found outside pdms for a few seconds: never from another test."""
    from pdms_cli.ui import state as ui_state

    ui_state.forget_strays()
    yield
    ui_state.forget_strays()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Doctor's Network checks and pdms ui's health would reach the internet and resolve database host names: tests
    get a quick line and direct routes (tests/test_health.py tries the real ones against local sockets)."""
    from pdms_cli import health

    monkeypatch.setattr(health, "line", lambda timeout=3: 20.0)
    monkeypatch.setattr(health, "route", lambda db: health.Route("local" if db.host in health.LOCAL_NAMES else "direct"))
