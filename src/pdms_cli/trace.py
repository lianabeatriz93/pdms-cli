"""Following one request through the services and events (see ``sqs_patch/pdms_trace.py`` for the service side).

The background proxy gives each request an id (its capture's) and sends it as ``X-Request-Id``. Services started by
pdms log it, pass it on in SQS messages (local events) and write each SQS send, SNS publish and consumer run to
:func:`path`, one JSON line each; :func:`steps` reads back the ones of a request for its waterfall.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import instances

HEADER = "x-request-id"
HEADER_NAME = "X-Request-Id"
ID = re.compile(r"[0-9a-f]{8}")
MAX_STEPS = 200  # a request that fans out into hundreds of messages shows the first ones
KINDS = ("sqs", "sns", "consumer", "email")


def path() -> Path:
    return instances.state_dir() / "trace.jsonl"


def steps(ident: str, file: Path | None = None) -> list[dict]:
    """The steps of request ``ident`` (oldest file first, then by time): kind, service, name, at (epoch), ms..."""
    if not ID.fullmatch(ident):
        return []
    file = file or path()
    found = []
    needle = f'"id": "{ident}"'
    for candidate in (file.with_suffix(".jsonl.1"), file):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if needle not in line:
                continue
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if isinstance(step, dict) and step.get("id") == ident and step.get("kind") in KINDS \
                    and isinstance(step.get("at"), (int, float)) and isinstance(step.get("ms"), (int, float)):
                found.append(step)
    found.sort(key=lambda step: step["at"])
    return found[:MAX_STEPS]
