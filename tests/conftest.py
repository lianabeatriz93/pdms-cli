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
