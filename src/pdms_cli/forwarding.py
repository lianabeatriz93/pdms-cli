"""A package whose modules answer as one module: ``package.<name>`` reads and replaces each name where it lives.

Reading gives what the module that holds the name has now; replacing it (``monkeypatch.setattr(package, "f",
...)`` in a test) replaces it in that module, for every caller. That holds as long as the modules call each other's
functions through the module (``services.plan_service(...)``) and never keep a copy with
``from .services import plan_service``. Each module lists what it holds in ``__all__``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from types import ModuleType


def forward(package: str, modules: Iterable[ModuleType]) -> dict[str, ModuleType]:
    """Make ``package`` answer for the names of ``modules``; returns where each name lives."""
    home = {name: module for module in modules for name in module.__all__}

    class Package(ModuleType):
        def __getattr__(self, name: str):
            module = home.get(name)
            if module is None:
                raise AttributeError(f"module {package!r} has no attribute {name!r}")
            return getattr(module, name)

        def __setattr__(self, name: str, value) -> None:
            module = home.get(name)
            if module is not None:
                setattr(module, name, value)
            else:
                super().__setattr__(name, value)

        def __delattr__(self, name: str) -> None:
            module = home.get(name)
            if module is not None:
                delattr(module, name)
            else:
                super().__delattr__(name)

        def __dir__(self) -> list[str]:
            return sorted({*super().__dir__(), *home})

    sys.modules[package].__class__ = Package
    return home
