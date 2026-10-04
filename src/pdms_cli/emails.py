"""The emails services send through SES, kept for pdms ui or sent to one address only (see sqs_patch/pdms_email.py).

Every service pdms runs gets ``PDMS_EMAIL_LOG``: what it sends through SES is written there (``pdms logs emails``,
the Emails tab of pdms ui) and never reaches its recipients. With ``defaults.email_to`` it also gets
``PDMS_EMAIL_TO``, and its emails go to that address only, through the real SES of the chosen AWS profile, with
whom they were for at the start of the subject.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .instances import log_path as _log_path
from .instances import previous_log_path

KEY = "emails"  # its readable log: pdms logs emails, the Emails tab of pdms ui
ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
# An entry of the log (sqs_patch/pdms_email.write_log): when, service, kept | sent | failed, id and request.
HEADER = re.compile(r"^(\S+) (\S+) (kept|sent|failed) ([0-9a-f]{12})(?: #(\S+))?$")
HTML_MARK = "--- HTML ---"  # pdms_email.HTML_MARK
ID = re.compile(r"[0-9a-f]{12}")


def log_path() -> Path:
    return _log_path(KEY)


def valid_address(value: str) -> bool:
    return bool(ADDRESS.fullmatch(value))


def env(cfg: Config) -> dict[str, str]:
    """What a service needs to keep its emails (and, with an address, to send them there only)."""
    found = {"PDMS_EMAIL_LOG": str(log_path())}
    if cfg.defaults.email_to:
        found["PDMS_EMAIL_TO"] = cfg.defaults.email_to
    return found


def html_of(ident: str) -> str | None:
    """The HTML of the email with that id in the log (or the previous one), or None (unknown id, or no HTML)."""
    if not ID.fullmatch(ident):
        return None
    for path in (log_path(), previous_log_path(log_path())):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for at, line in enumerate(lines):
            match = HEADER.match(line)
            if not match or match.group(4) != ident:
                continue
            body = []
            for following in lines[at + 1:]:
                if following and not following.startswith(" "):
                    break
                body.append(following[2:])
            if HTML_MARK not in body:
                return None
            return "\n".join(body[body.index(HTML_MARK) + 1:])
    return None
