"""The banner shown when the menu opens."""

from __future__ import annotations

import io

from rich.console import Console

from pdms_cli import banner


def output(encoding: str, width: int = 100) -> str:
    buffer = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    console = Console(file=buffer, width=width, color_system=None, legacy_windows=False)
    banner.render(console, "0.2.2", "Local PDMS services, made easy")
    buffer.flush()
    return buffer.buffer.getvalue().decode(encoding)


def test_all_lines_have_the_same_width():
    assert len({len(line) for line in banner.BLOCKS}) == 1
    assert len({len(line) for line in banner.ASCII}) == 1


def test_block_letters_on_utf8_terminals():
    text = output("utf-8")
    assert "██████╗" in text and "v0.2.2" in text and "made easy" in text


def test_plain_ascii_where_block_characters_cannot_be_printed():
    text = output("cp1252")
    assert "|  _ \\" in text and "█" not in text and "v0.2.2" in text


def test_narrow_terminals_only_get_the_name():
    text = output("utf-8", width=20)
    assert "█" not in text and "pdms v0.2.2" in text
