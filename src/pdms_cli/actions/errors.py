"""What actions raise: problems for the user to fix, and the decisions a front end must answer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import pythons

if TYPE_CHECKING:
    from .services import Unmigrated
    from .views import TestViewNeed

__all__ = [
    "ActionError", "Decision", "InvalidValue", "LocalEventsDown", "MigrationsPending", "PointFrontend", "PortBusy",
    "ProtectedDatabase", "PythonMismatch", "TestViewsPending",
]


class ActionError(Exception):
    """A problem the user has to fix (unknown user, bad option...); ``message`` is ready to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidValue(ActionError):
    """A value the user gave is not valid; ``field`` is its config key, so a form can point to it."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


class Decision(Exception):
    """The action needs an answer from the user before it can go on."""


class PortBusy(Decision):
    def __init__(self, port: int, free: int) -> None:
        super().__init__(f"port {port} is in use (next free: {free})")
        self.port, self.free = port, free


class ProtectedDatabase(Decision):
    def __init__(self, name: str) -> None:
        super().__init__(f"database {name!r} is protected")
        self.name = name


class LocalEventsDown(Decision):
    """Events must go to the local ElasticMQ, which is not running (answer: start it, then retry)."""

    def __init__(self, port: int) -> None:
        super().__init__(f"the local ElasticMQ is not running on port {port}")
        self.port = port


class PythonMismatch(Decision):
    """Services whose virtualenv runs a Python their project no longer accepts (answer: ``python=True`` to make it
    again on an accepted one before installing, ``python=False`` to keep it as it is)."""

    def __init__(self, items: list[pythons.Mismatch]) -> None:
        super().__init__(", ".join(f"{item.service.name} ({pythons.show(item.have)}, wants {item.wants})"
                                   for item in items))
        self.items = items


class MigrationsPending(Decision):
    """Local databases behind the migrations of pdms-db-migrations (answer: ``migrate=True`` applies them before the
    services start, ``migrate=False`` starts them as they are)."""

    def __init__(self, items: list[Unmigrated]) -> None:
        super().__init__(", ".join(f"{item.alias}: {len(item.versions)}" for item in items))
        self.items = items


class TestViewsPending(Decision):
    """Services about to start read materialized views you changed in your checkout (answer: ``views=True`` makes
    their test copies, or checks you ran their SQL, and has the services read them; ``views=False`` the real ones)."""

    def __init__(self, items: list[TestViewNeed]) -> None:
        super().__init__(", ".join(item.view for item in items))
        self.items = items


class PointFrontend(Decision):
    """Whether ``frontend/.env.local`` should point to the proxy while it runs (answer: ``frontend=True/False``)."""

    def __init__(self, url: str) -> None:
        super().__init__(f"point the frontend to {url}?")
        self.url = url
