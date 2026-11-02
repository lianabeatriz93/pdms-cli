"""The AWS logs screen of pdms ui: the Lambdas to choose from, and one read of their lines (see awslogs.py).

Each read is one GET. The page asks only while the screen is open: when it opens or a filter changes, and in Live
once a minute for what came since its last read (it shows each line once, by its id). The level is filtered on the
page, the rest by AWS's filter pattern and :func:`awslogs.keep` here.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime

from .. import actions, awslogs, repos
from ..config import Config

LAMBDAS_EVERY = 600  # seconds the account's Lambdas are kept before asking CloudWatch again
MAX_LIMIT = 2000
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")  # a Lambda's name, never an option for the AWS CLI

_lock = threading.Lock()
_listed: dict[str, tuple[float, list[str]]] = {}  # profile → (when, its Lambdas with logs)


def _roles(cfg: Config) -> list[awslogs.Suggestion]:
    root = repos.active_root(cfg)
    if not root or not root.is_dir():
        return []
    try:
        return awslogs.suggestions(root)
    except Exception:  # noqa: BLE001 - a repo whose Terraform cannot be read only loses the suggestions
        return []


def _listing(profile: str, fresh: bool) -> list[str]:
    with _lock:
        kept = _listed.get(profile)
    if kept and not fresh and time.monotonic() - kept[0] < LAMBDAS_EVERY:
        return kept[1]
    names = awslogs.lambdas(profile)
    with _lock:
        _listed[profile] = (time.monotonic(), names)
    return names


def lambdas(cfg: Config, fresh: bool = False) -> dict:
    """The account's Lambdas with logs, with the part each one takes in events, and the event flow (the broker and
    the SNS publishers: what the screen reads until the user chooses)."""
    profile = cfg.defaults.aws_profile
    if not profile:
        return {"profile": "", "lambdas": [], "flow": []}
    roles = {s.function: s for s in _roles(cfg)}
    answer: dict = {"profile": profile, "expired": False, "error": ""}
    try:
        names = _listing(profile, fresh)
    except awslogs.LogsError as exc:
        names = sorted(roles)  # still something to choose from: the repo's
        answer.update(expired=exc.expired, error=exc.message)
    answer["lambdas"] = [{"name": name, "role": roles[name].role if name in roles else "",
                          "detail": roles[name].detail if name in roles else ""} for name in names]
    answer["flow"] = [name for name, s in roles.items() if s.role in ("broker", "sns") and name in names]
    return answer


def _number(text: str, what: str) -> int | None:
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise actions.ActionError(f"{what} must be a number of milliseconds") from None


def read(cfg: Config, functions: str, start: str, end: str = "", event_type: str = "", grep: str = "",
         every_line: bool = False, limit: str = "") -> dict:
    """One read: the lines of ``functions`` (names, comma-separated) from ``start`` to ``end`` (ms; no end = now)
    that are events or errors (or every line), with ``event_type`` and ``grep``; ``asked`` is when it asked."""
    names = [name for name in functions.split(",") if name]
    if not names or not all(NAME.fullmatch(name) for name in names):
        raise actions.ActionError("functions must be Lambda names separated by commas")
    begin = _number(start, "start")
    if begin is None:
        raise actions.ActionError("start must be a number of milliseconds")
    finish = _number(end, "end")
    lines = max(1, min(_number(limit, "limit") or awslogs.LIMIT, MAX_LIMIT))
    profile = cfg.defaults.aws_profile
    answer: dict = {"profile": profile, "lines": [], "errors": {}, "more": [], "expired": False, "error": "",
                    "asked": awslogs.millis(datetime.now().astimezone())}
    if not profile:
        return answer
    try:
        found = awslogs.read(profile, names, begin, finish, awslogs.pattern(grep, event_type, not every_line), lines)
    except awslogs.LogsError as exc:
        answer.update(expired=exc.expired, error=exc.message)
        return answer
    answer["lines"] = [line.to_dict() for line in found.lines
                       if awslogs.keep(line, not every_line, event_type, "", grep)]
    answer["errors"] = {name: {"kind": error.kind, "message": error.message} for name, error in found.errors.items()}
    answer["more"] = sorted(found.more)
    return answer
