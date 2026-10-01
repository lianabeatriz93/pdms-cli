"""Interactive wizards built on questionary.

All prompts use ``unsafe_ask`` so Ctrl+C raises KeyboardInterrupt, handled once in the CLI entrypoint.
"""

from __future__ import annotations

import sys
from typing import Callable, Iterable

import questionary
import typer

from . import actions
from .config import EVENTS_MODES, LOG_LEVELS, Database, Defaults, DevUser
from .i18n import LANGUAGES, _


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise typer.BadParameter(_("An interactive terminal is required (or pass the options as flags)."))


def _is_int(value: str) -> bool | str:
    return value.isdigit() or _("Must be a number")


def _not_empty(value: str) -> bool | str:
    return bool(value.strip()) or _("Required field")


def ask_name(kind: str, taken: Iterable[str]) -> str:
    taken = set(taken)

    def validate(value: str) -> bool | str:
        try:
            actions.check_alias(value, taken)
        except actions.InvalidValue as invalid:
            return invalid.reason
        return True

    return questionary.text(_("Alias ({kind}):", kind=kind), validate=validate).unsafe_ask().strip()


def select_name(message: str, names: list[str], default: str = "") -> str:
    return questionary.select(message, choices=names, default=default if default in names else None).unsafe_ask()


def ask_language(current: str) -> str:
    return questionary.select(
        _("Language:"),
        choices=[questionary.Choice(name, code) for code, name in LANGUAGES.items()],
        default=current if current in LANGUAGES else None,
    ).unsafe_ask()


def ask_port(default: int, is_free: Callable[[int], bool]) -> int:
    def validate(value: str) -> bool | str:
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            return _("Must be a number between 1 and 65535")
        return is_free(int(value)) or _("Port {port} is in use", port=value)

    return int(questionary.text(_("Service port:"), default=str(default), validate=validate).unsafe_ask())


def ask_background() -> bool:
    return questionary.select(
        _("How should it run?"),
        choices=[
            questionary.Choice(_("Background (you can run several; logs with pdms logs)"), True),
            questionary.Choice(_("Foreground (in this terminal)"), False),
        ],
    ).unsafe_ask()


def ask_proxy_background() -> bool:
    return questionary.select(
        _("How should the proxy run?"),
        choices=[
            questionary.Choice(_("Foreground (in this terminal, with every request live)"), False),
            questionary.Choice(_("Background (keeps running; requests with pdms logs proxy)"), True),
        ],
    ).unsafe_ask()


def ask_database(current: Database | None = None) -> Database:
    c = current or Database(host="localhost")
    host = questionary.text(_("Host:"), default=c.host, validate=_not_empty).unsafe_ask()
    port = questionary.text(_("Port:"), default=str(c.port), validate=_is_int).unsafe_ask()
    database = questionary.text(_("Database name:"), default=c.database, validate=_not_empty).unsafe_ask()
    user = questionary.text(_("User:"), default=c.user, validate=_not_empty).unsafe_ask()
    hint = _(" (empty = keep the current one)") if current and current.password else ""
    password = questionary.password(_("Password{hint}:", hint=hint)).unsafe_ask()
    if not password and current:
        password = current.password
    protected = questionary.confirm(
        _("Is it a shared/remote DB? (asks for confirmation before starting)"),
        default=c.protected if current else host not in ("localhost", "127.0.0.1"),
    ).unsafe_ask()
    return Database(
        host=host.strip(),
        port=int(port),
        database=database.strip(),
        user=user.strip(),
        password=password,
        driver=c.driver,
        protected=protected,
    )


def ask_user(current: DevUser | None = None) -> DevUser:
    c = current or DevUser(user_id="", username="")
    return DevUser(
        user_id=questionary.text("DEV_USER_ID (uuid):", default=c.user_id, validate=_not_empty).unsafe_ask().strip(),
        username=questionary.text("DEV_USERNAME (email):", default=c.username, validate=_not_empty).unsafe_ask().strip(),
        first_name=questionary.text("DEV_FIRST_NAME:", default=c.first_name).unsafe_ask().strip(),
        last_name=questionary.text("DEV_LAST_NAME:", default=c.last_name).unsafe_ask().strip(),
        roles=questionary.text(_("DEV_ROLES (comma separated):"), default=c.roles).unsafe_ask().strip(),
    )


def ask_defaults(current: Defaults) -> Defaults:
    language = ask_language(current.language)
    host = questionary.text(_("uvicorn host:"), default=current.host).unsafe_ask()
    port = questionary.text(_("Default port:"), default=str(current.port), validate=_is_int).unsafe_ask()
    level = questionary.select("LOGGING_LEVEL:", choices=list(LOG_LEVELS), default=current.logging_level).unsafe_ask()
    reload = questionary.confirm(_("Use --reload?"), default=current.reload).unsafe_ask()
    install = questionary.confirm(
        _("Run poetry lock && poetry install before starting?"), default=current.install
    ).unsafe_ask()
    smart_install = install and questionary.confirm(
        _("Skip the install when nothing changed since the last one (smart install)?"), default=current.smart_install
    ).unsafe_ask()
    events_mode = questionary.select(
        _("Where should services publish SQS events?"),
        choices=[
            questionary.Choice(_("auto: local broker when pdms events up is running"), "auto"),
            questionary.Choice(_("local: always the local broker"), "local"),
            questionary.Choice(_("aws: as configured by each service"), "aws"),
        ],
        default=current.events if current.events in EVENTS_MODES else "auto",
    ).unsafe_ask()
    banner = questionary.confirm(_("Show the PDMS banner when the menu opens?"), default=current.banner).unsafe_ask()
    update_check = questionary.confirm(
        _("Tell me when a new pdms version is available?"), default=current.update_check
    ).unsafe_ask()
    db_timeout = questionary.text(
        _("Connection test timeout (seconds):"), default=str(current.db_timeout), validate=_is_int
    ).unsafe_ask()
    env = dict(current.env)
    while questionary.confirm(
        _("Add/edit extra environment variables? (current: {current})", current=", ".join(env) or _("none")),
        default=False,
    ).unsafe_ask():
        key = questionary.text(_("Variable name:"), validate=_not_empty).unsafe_ask().strip()
        value = questionary.text(_("Value of {key} (empty = delete):", key=key), default=env.get(key, "")).unsafe_ask()
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return Defaults(
        language=language, host=host.strip(), port=int(port), logging_level=level, reload=reload, install=install,
        smart_install=smart_install if install else current.smart_install, update_check=update_check, events=events_mode, banner=banner,
        db_timeout=int(db_timeout), env=env,
    )
