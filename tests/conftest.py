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


@pytest.fixture(autouse=True)
def no_aws(tmp_path_factory, monkeypatch):
    """Never the developer's AWS profiles nor a call to the real AWS (tests/test_awsenv.py fakes the CLI's answers)."""
    from pdms_cli import awsenv

    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path_factory.mktemp("aws") / "config"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    def refuse(profile, args, timeout):
        raise awsenv.AwsError("tests never call AWS")

    monkeypatch.setattr(awsenv, "_aws", refuse)


class _Answers:
    """The answers of an interactive command, in order: a value, or ``...`` for the question's default."""

    def __init__(self) -> None:
        self.queue: list = []
        self.asked: list[tuple[str, str]] = []
        self.then_defaults = False

    def __call__(self, *answers, then_defaults: bool = False) -> _Answers:
        self.queue.extend(answers)
        self.then_defaults = then_defaults
        return self

    def question(self, kind: str):
        def ask(message, *args, **kwargs):
            choices = kwargs.get("choices") or (args[0] if args else [])
            values = [getattr(choice, "value", choice) for choice in choices]

            def unsafe_ask():
                self.asked.append((kind, str(message)))
                if not self.queue and not self.then_defaults:
                    raise AssertionError(f"unexpected question ({kind}): {message}; asked before: {self.asked[:-1]}")
                answer = self.queue.pop(0) if self.queue else ...
                if answer is ...:
                    default = kwargs.get("default")
                    if kind == "confirm":
                        return True if default is None else default
                    if kind == "select":
                        return default if default is not None else values[0]
                    if kind == "checkbox":
                        return [getattr(c, "value", c) for c in choices if getattr(c, "checked", False)]
                    return default if default is not None else ""
                if kind == "select":
                    assert answer in values, f"{answer!r} is not a choice of {message!r}: {values}"
                return answer

            return type("Question", (), {"unsafe_ask": staticmethod(unsafe_ask), "ask": staticmethod(unsafe_ask)})()

        return ask


class _Terminal:
    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return ""


@pytest.fixture
def answers(monkeypatch):
    """A user at the terminal answering questionary's questions, in order (see :class:`_Answers`); fails on a
    question nobody expected and on answers left over."""
    import sys

    import questionary

    from pdms_cli import prompts
    from pdms_cli.commands import common

    script = _Answers()
    for kind in ("text", "select", "confirm", "checkbox", "password", "path", "autocomplete"):
        monkeypatch.setattr(questionary, kind, script.question(kind))
    monkeypatch.setattr(prompts, "require_tty", lambda: None)
    original = common.interactive_terminal  # imported by name in each module: replace every copy
    for module in [m for name, m in sys.modules.items() if name.startswith("pdms_cli.commands")]:
        if getattr(module, "interactive_terminal", None) is original:
            monkeypatch.setattr(module, "interactive_terminal", lambda: True)
    monkeypatch.setattr(sys, "stdin", _Terminal())
    yield script
    assert not script.queue, f"answers left over: {script.queue}; asked: {script.asked}"
