"""Every connection to a configured database notices a dead tunnel, and the checks give up on a stuck query."""

from __future__ import annotations

import sys

import psycopg

from pdms_cli import health, postgres
from pdms_cli.config import Database


def opened(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs) or object())
    return calls


def test_connections_have_keepalives(monkeypatch) -> None:
    calls = opened(monkeypatch)
    postgres.connect(Database("db.example.com", port=5433, password="pw"), 7, autocommit=True)
    (call,) = calls
    assert call["host"] == "db.example.com" and call["port"] == 5433 and call["password"] == "pw"
    assert call["connect_timeout"] == 7 and call["autocommit"] is True
    assert call["keepalives"] == 1 and call["keepalives_idle"] == 30
    assert "options" not in call  # no statement timeout unless asked: copying views may take minutes


def test_health_checks_stop_a_stuck_query(monkeypatch) -> None:
    calls = opened(monkeypatch)
    health.connect(Database("localhost"), 5)
    assert calls[0]["options"] == "-c statement_timeout=30000"


def test_questionary_is_imported_when_used() -> None:
    import subprocess

    code = "import sys, pdms_cli.cli; print('questionary' in sys.modules, 'prompt_toolkit' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]


def test_patching_through_the_lazy_module_patches_the_module(monkeypatch) -> None:
    import questionary

    from pdms_cli.lazy import questionary as lazy

    real = questionary.confirm
    with monkeypatch.context() as patch:
        patch.setattr(lazy, "confirm", "patched")
        assert questionary.confirm == "patched" and lazy.confirm == "patched"
    assert questionary.confirm is real and "confirm" not in vars(lazy)
    monkeypatch.setattr(questionary, "confirm", "again")  # what the answers fixture does
    assert lazy.confirm == "again"
