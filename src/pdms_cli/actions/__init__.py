"""What pdms does with services, stacks, the proxy, the local events and the configuration (repos, databases, users,
defaults), without prompting or printing: shared by the CLI and ``pdms ui``.

An action never asks. When it needs a decision it raises a :class:`Decision` (a busy port, a protected database,
the local ElasticMQ not running, pointing the frontend to the proxy); each front end answers it its own way (a questionary prompt, a dialog) and calls
the action again with the answer. Problems no answer can fix raise :class:`ActionError` with a message for the user.

One module per area (``__all__`` of each lists what it holds), and every name answers here as ``actions.<name>``,
always from its module: reading it gives what the module holds now, and replacing it (``monkeypatch.setattr(actions,
"start_service", ...)``) replaces it there, for every caller. So the modules call each other's functions through
the module (``services.plan_service(...)``), never with ``from .services import plan_service``: a replaced function
would not reach a copy. Classes and constants, which nothing replaces, are imported by name.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - tests replace actions.subprocess.run
import sys
import time  # noqa: F401 - and actions.time.sleep
from types import ModuleType
from typing import TYPE_CHECKING

# The modules the actions use, reachable as actions.<module> as before (tests replace their functions there).
from .. import (  # noqa: F401
    awsenv,
    browser,
    captures,
    codeartifact,
    desktop,
    emails,
    events,
    frontend,
    health,
    installer,
    instances,
    migrations,
    proxy,
    pythons,
    repos,
    routes,
    runner,
    testviews,
    trace,
    transfer,
    userimport,
    vscode,
)
from . import (
    aws,
    checks,
    debugging,
    errors,
    local_events,
    local_frontend,
    local_proxy,
    outside,
    repo_settings,
    services,
    settings,
    setups,
    stacks,
    views,
)

MODULES = (errors, checks, local_events, services, outside, debugging, stacks, setups, local_proxy, local_frontend,
           repo_settings, settings, aws, views)
HOME: dict[str, ModuleType] = {name: module for module in MODULES for name in module.__all__}


class _Actions(ModuleType):
    """``actions.<name>`` reads and writes the module that holds ``name``."""

    def __getattr__(self, name: str):
        module = HOME.get(name)
        if module is None:
            raise AttributeError(f"module 'pdms_cli.actions' has no attribute {name!r}")
        return getattr(module, name)

    def __setattr__(self, name: str, value) -> None:
        module = HOME.get(name) if "HOME" in self.__dict__ else None
        if module is not None:
            setattr(module, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        module = HOME.get(name)
        if module is not None:
            delattr(module, name)
        else:
            super().__delattr__(name)

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *HOME})


sys.modules[__name__].__class__ = _Actions

if TYPE_CHECKING:  # what editors and type checkers see as actions.<name>
    from .aws import *  # noqa: F403
    from .checks import *  # noqa: F403
    from .debugging import *  # noqa: F403
    from .errors import *  # noqa: F403
    from .local_events import *  # noqa: F403
    from .local_frontend import *  # noqa: F403
    from .local_proxy import *  # noqa: F403
    from .outside import *  # noqa: F403
    from .repo_settings import *  # noqa: F403
    from .services import *  # noqa: F403
    from .settings import *  # noqa: F403
    from .setups import *  # noqa: F403
    from .stacks import *  # noqa: F403
    from .views import *  # noqa: F403
