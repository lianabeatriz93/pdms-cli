"""What ``pdms ui`` does to services: the same actions as the CLI, run in a thread so the page stays live.

A job belongs to one instance (or the proxy) and shows in the state while it runs, with its phase; a failed one
stays there with its error until the next action on that instance or until it is dismissed. Everything that needs
an answer (a protected database, the local ElasticMQ) is checked before the job starts, so the page can ask first.

One module per area, each with its kind of job (``ServiceJobs``, ``StackJobs``... over ``JobsCore``) and the
functions the page calls for it; :class:`Jobs` (all_jobs.py) is all of them in one object. Every name answers here as
``jobs.<name>``, from its module, as in actions (see forwarding.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# The modules jobs use, reachable as jobs.<module> as before (tests replace their functions there).
from ... import (  # noqa: F401
    actions,
    events,
    frontend,
    i18n,
    images,
    instances,
    localcopy,
    localdb,
    migrations,
    proxy,
    repos,
    routes,
    runner,
    testruns,
    transfer,
    update,
    userimport,
)
from ...config import Config  # noqa: F401 - tests replace jobs.Config.load
from ...forwarding import forward
from . import (
    all_jobs,
    core,
    data,
    home,
    local_events,
    local_frontend,
    local_proxy,
    repo_info,
    repo_switch,
    services,
    settings_screen,
    stacks,
    testdbs,
    updating,
)

MODULES = (core, repo_info, services, testdbs, data, repo_switch, updating, stacks, local_proxy, local_frontend, home,
           local_events, settings_screen, all_jobs)
HOME = forward(__name__, MODULES)

if TYPE_CHECKING:  # what editors and type checkers see as jobs.<name>
    from .all_jobs import *  # noqa: F403
    from .core import *  # noqa: F403
    from .data import *  # noqa: F403
    from .home import *  # noqa: F403
    from .local_events import *  # noqa: F403
    from .local_frontend import *  # noqa: F403
    from .local_proxy import *  # noqa: F403
    from .repo_info import *  # noqa: F403
    from .repo_switch import *  # noqa: F403
    from .services import *  # noqa: F403
    from .settings_screen import *  # noqa: F403
    from .stacks import *  # noqa: F403
    from .testdbs import *  # noqa: F403
    from .updating import *  # noqa: F403
