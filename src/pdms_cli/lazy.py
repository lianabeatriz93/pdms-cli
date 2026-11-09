"""Modules that pdms imports only when it uses them: `pdms --version`, completion and scripts start without them.

``questionary`` (with prompt_toolkit) is a tenth of a second of every start, and only menus and questions need it.
Each attribute is looked up in the real module when used, so tests that patch the module's functions still work.
"""

from __future__ import annotations

import importlib
from typing import Any


class LazyModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(importlib.import_module(self._name), attr)

    def __repr__(self) -> str:
        return f"<lazy module {self._name}>"


questionary: Any = LazyModule("questionary")
