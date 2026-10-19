"""The Python the GitHub workflows run against pdms must keep up with its code.

A step that only runs on a cache miss (the Flyway download) imported a function that had been moved: nobody saw it
until the cache expired, and then every run failed.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
IMPORT = re.compile(r"from (pdms_cli[.\w]*) import ([\w, ]+)")


def imports() -> list[tuple[str, str, str]]:
    found = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for module, names in IMPORT.findall(workflow.read_text(encoding="utf-8")):
            found += [(workflow.name, module, name.strip()) for name in names.split(",") if name.strip()]
    return found


def test_the_workflows_import_pdms() -> None:
    assert imports()  # the pattern still finds them


@pytest.mark.parametrize(("workflow", "module", "name"), imports())
def test_what_a_workflow_imports_exists(workflow: str, module: str, name: str) -> None:
    imported = importlib.import_module(module)
    assert hasattr(imported, name) or importlib.util.find_spec(f"{module}.{name}"), f"{workflow}: {module}.{name}"
