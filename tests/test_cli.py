"""The command line entrypoint: every command is registered once, in the order pdms --help shows."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from pdms_cli import cli


def test_command_order_lists_every_command_once() -> None:
    assert [cli.command_name(c) for c in cli.app.registered_commands] == cli.COMMAND_ORDER


def test_help_shows_the_commands_in_that_order(monkeypatch) -> None:
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    output = CliRunner().invoke(cli.app, ["--help"], env={"COLUMNS": "200"}).output
    # In CI (GITHUB_ACTIONS) rich adds colours; the table borders depend on the terminal.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    listed = [m.group(1) for m in re.finditer(r"^\W*([a-z][a-z-]*)\s{2,}\S", plain, re.M)]
    assert [name for name in listed if name in cli.COMMAND_ORDER] == cli.COMMAND_ORDER
