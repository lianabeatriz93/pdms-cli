"""Keep the English source strings and the Spanish catalog in sync."""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

from pdms_cli import i18n

SRC = Path(__file__).resolve().parents[1] / "src" / "pdms_cli"
SPANISH_HINT = re.compile(r"[áéíóúñ¿¡]", re.IGNORECASE)


def source_strings() -> set[str]:
    found = set()
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_string_has_a_spanish_translation():
    missing = sorted(source_strings() - i18n.ES.keys())
    assert not missing, "Missing in i18n.ES:\n" + "\n".join(missing)


def test_catalog_has_no_unused_entries():
    unused = sorted(i18n.ES.keys() - source_strings())
    assert not unused, "Unused in i18n.ES:\n" + "\n".join(unused)


@pytest.mark.parametrize("english", sorted(i18n.ES))
def test_placeholders_match(english):
    assert placeholders(english) == placeholders(i18n.ES[english])


def test_no_spanish_left_in_source():
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name == "i18n.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SPANISH_HINT.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_default_is_english_and_spanish_is_selectable(monkeypatch):
    monkeypatch.delenv("PDMS_LANG", raising=False)
    assert i18n.DEFAULT_LANGUAGE == "en"
    i18n.set_language("en")
    assert i18n._("Proxy stopped.") == "Proxy stopped."
    i18n.set_language("es")
    assert i18n._("Proxy stopped.") == "Proxy parado."
    assert i18n._("{key} stopped.", key="svc@8080") == "svc@8080 parado."
    i18n.set_language("fr")  # unknown languages fall back to English
    assert i18n.current_language() == "en"


def test_any_placeholder_name_is_allowed():
    i18n.set_language("en")
    assert i18n._("No endpoint contains '{text}'.", text="x") == "No endpoint contains 'x'."
