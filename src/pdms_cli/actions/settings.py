"""Databases, users, defaults, and exporting and importing the configuration."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .. import browser, desktop, emails, health, repos, transfer, userimport
from ..config import EVENTS_MODES, LOG_LEVELS, THEMES, UPDATE_CHANNELS, Config, Database, Defaults, DevUser, config_path
from ..i18n import LANGUAGES, _
from . import checks, local_proxy, services
from .errors import ActionError, InvalidValue

__all__ = [
    "ENV_NAME", "_alias", "_forget_in_overrides", "_number", "_one_of", "_required", "check_alias",
    "check_connection", "check_email_to", "check_front_hosts", "export_config", "first_setup", "import_config",
    "import_users", "install_extension", "known_roles", "read_db_users", "read_export", "remove_db", "remove_user",
    "rename_user", "save_db", "save_defaults", "save_user", "set_email_to", "set_front_hosts", "set_front_redirect",
    "set_language", "set_ui_at_login", "split_roles", "valid_db",
]


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_alias(name: str, taken: Iterable[str]) -> str:
    """``name`` without surrounding spaces, if it can be the alias of a new database, user or stack."""
    name = name.strip()
    if not name:
        raise InvalidValue("name", _("Required field"))
    if name in set(taken):
        raise InvalidValue("name", _("That name already exists"))
    if not all(c.isalnum() or c in "-_" for c in name):
        raise InvalidValue("name", _("Use only letters, numbers, '-' or '_'"))
    return name


def _alias(items: dict, kind: str, name: str, new: bool) -> str:
    return check_alias(name, items) if new else checks.require(items, kind, name)


def _required(field: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise InvalidValue(field, _("Required field"))
    return value


def _number(field: str, value: int | str, low: int, high: int) -> int:
    try:
        number = None if isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        number = None
    if number is None or not low <= number <= high:
        raise InvalidValue(field, _("Must be a number between {low} and {high}", low=low, high=high))
    return number


def check_email_to(value: str) -> str:
    """The address emails go to (empty: nowhere), trimmed, or :class:`InvalidValue`."""
    value = value.strip()
    if value and not emails.valid_address(value):
        raise InvalidValue("email_to", _("'{value}' is not an email address", value=value))
    return value


def check_front_hosts(hosts: Iterable[str]) -> list[str]:
    """The known fronts the browser extension redirects (``https://dev-pdm.alivi.com/`` → ``dev-pdm.alivi.com``)."""
    found: list[str] = []
    for given in hosts:
        host = given.strip().lower()
        host = (urlsplit(host).hostname or "") if "://" in host else host.split("/", 1)[0]
        if not browser.HOST.fullmatch(host):
            raise InvalidValue("front_redirect_hosts", _(
                "'{host}' is not a host of alivi.com (the extension only redirects those)", host=given.strip()))
        if host not in found:
            found.append(host)
    if not found:
        raise InvalidValue("front_redirect_hosts", _("Give at least one host"))
    return found


def set_front_redirect(cfg: Config, on: bool) -> None:
    """Turn on or off sending the known front to the frontend pdms runs (the browser extension follows in 30 s)."""
    cfg.defaults.front_redirect = on
    cfg.save()


def set_front_hosts(cfg: Config, hosts: Iterable[str]) -> list[str]:
    cfg.defaults.front_redirect_hosts = check_front_hosts(hosts)
    cfg.save()
    return cfg.defaults.front_redirect_hosts


def install_extension(key: str) -> tuple[browser.Browser, Path, bool]:
    """Write the extension's folder and open the extensions page of browser ``key`` (Firefox: open the signed file,
    and it asks whether to add it): the browser, the folder or file, and whether the browser opened."""
    found = browser.BY_KEY.get(key)
    if not found:
        raise ActionError(_("Unknown browser '{key}'. Available: {keys}", key=key, keys=", ".join(browser.BY_KEY)))
    if key not in browser.installed() and not browser.executable(found):
        raise ActionError(_("{name} is not installed on this computer.", name=found.name))
    if key == "firefox":
        try:
            xpi = browser.firefox_xpi()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ActionError(_("Could not get the signed extension for Firefox: {error}", error=exc)) from exc
        return found, xpi, browser.open_page(found, str(xpi))
    folder = browser.write_extension()
    return found, folder, browser.open_page(found)


def _one_of(field: str, value: str, choices: Iterable[str]) -> str:
    if value not in choices:
        raise InvalidValue(field, _("Must be one of: {choices}", choices=", ".join(choices)))
    return value


def valid_db(db: Database) -> Database:
    """``db`` with its fields trimmed, or :class:`InvalidValue` for the first wrong one. The password stays as given."""
    return replace(
        db, host=_required("host", db.host), port=_number("port", db.port, 1, 65535),
        database=_required("database", db.database), user=_required("user", db.user),
    )


def save_db(cfg: Config, name: str, db: Database, new: bool = False) -> str:
    """Create (``new``) or replace the database ``name``; returns its alias."""
    name = _alias(cfg.dbs, _("database"), name, new)
    cfg.dbs[name] = valid_db(db)
    cfg.save()
    return name


def _forget_in_overrides(cfg: Config, kind: str, name: str) -> None:
    """A deleted user or database leaves the stacks' exceptions: those services start like the rest again."""
    for stack in cfg.stacks.values():
        for svc, own in list(stack.overrides.items()):
            if own.get(kind) == name:
                own.pop(kind)
                if not own:
                    del stack.overrides[svc]


def remove_db(cfg: Config, name: str) -> list[str]:
    """Delete a database; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.dbs[checks.require(cfg.dbs, _("database"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.db == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].db = ""
    _forget_in_overrides(cfg, "db", name)
    if cfg.last_db == name:
        cfg.last_db = ""
    cfg.save()
    return stacks


def check_connection(db: Database, timeout: int) -> str:
    """The server version and what a round trip costs, e.g. ``PostgreSQL 16.4 · 370 ms per round trip``;
    :class:`ActionError` with the driver's message otherwise."""
    try:
        version, ms = health.probe(db, timeout)
    except Exception as exc:  # noqa: BLE001 - any driver error is the answer
        raise ActionError(str(exc).strip() or type(exc).__name__) from exc
    return _("{version} · {time} per round trip", version=version, time=health.describe_ms(ms))


def known_roles(cfg: Config) -> list[str]:
    """The ``DEV_ROLES`` the services of the current repo understand (its ``MAP_INTERNAL_ROLES``)."""
    mapping, _source = userimport.role_mapping(repos.active_root(cfg))
    return list(dict.fromkeys(mapping.values()))


def split_roles(roles: str) -> list[str]:
    return list(dict.fromkeys(role.strip() for role in roles.split(",") if role.strip()))


def save_user(cfg: Config, name: str, user: DevUser, new: bool = False, roles: Iterable[str] | None = None) -> str:
    """Create (``new``) or replace the development user ``name``; returns its alias.

    With ``roles``, every role must be one of them, except the ones the user already had (an import keeps the roles
    the repo does not know, and editing the user must not fail because of them)."""
    name = _alias(cfg.users, _("user"), name, new)
    picked = split_roles(user.roles)
    if roles is not None:
        allowed = set(roles) | set(split_roles(cfg.users[name].roles) if name in cfg.users else [])
        if unknown := [role for role in picked if role not in allowed]:
            raise InvalidValue("roles", _("Unknown roles: {roles}. Available: {available}",
                                          roles=", ".join(unknown), available=", ".join(roles)))
    cfg.users[name] = DevUser(
        user_id=_required("user_id", user.user_id), username=_required("username", user.username),
        first_name=user.first_name.strip(), last_name=user.last_name.strip(), roles=",".join(picked),
    )
    cfg.save()
    return name


def rename_user(cfg: Config, name: str, new_name: str) -> str:
    """Give a user another alias, also in the stacks that use it and as the last one used; returns the new alias."""
    name = checks.require(cfg.users, _("user"), name)
    if new_name.strip() == name:
        return name
    renamed = check_alias(new_name, cfg.users)
    cfg.users = {renamed if alias == name else alias: user for alias, user in cfg.users.items()}
    for stack in cfg.stacks.values():
        if stack.user == name:
            stack.user = renamed
        for own in stack.overrides.values():
            if own.get("user") == name:
                own["user"] = renamed
    if cfg.last_user == name:
        cfg.last_user = renamed
    cfg.save()
    return renamed


def remove_user(cfg: Config, name: str) -> list[str]:
    """Delete a user; the stacks that used it ask for one again when they start (returns their names)."""
    del cfg.users[checks.require(cfg.users, _("user"), name)]
    stacks = [stack_name for stack_name, stack in cfg.stacks.items() if stack.user == name]
    for stack_name in stacks:
        cfg.stacks[stack_name].user = ""
    _forget_in_overrides(cfg, "user", name)
    if cfg.last_user == name:
        cfg.last_user = ""
    cfg.save()
    return stacks


def read_db_users(
    cfg: Config, db_name: str, mapping: dict[str, str], search: str = "", role: str = "", inactive: bool = False,
    limit: int = 200,
) -> list[userimport.DbUser]:
    """The users of the ``pdms_user`` table of a database, to choose which ones to import."""
    database = cfg.dbs[checks.require(cfg.dbs, _("database"), db_name)]
    try:
        return userimport.fetch_users(
            database, search=search, role=role, include_inactive=inactive, limit=limit,
            timeout=cfg.defaults.db_timeout, mapping=mapping,
        )
    except Exception as exc:  # noqa: BLE001 - show any driver error to the user
        raise ActionError(_("Could not read the users from {name}: {error}", name=db_name,
                            error=str(exc).strip())) from exc


def import_users(
    cfg: Config, picked: list[userimport.DbUser], mapping: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Add the picked users (updating the ones already imported); returns the added and the updated aliases."""
    cfg.users, added, updated = userimport.merge_users(cfg.users, picked, mapping)
    cfg.save()
    return added, updated


def export_config(cfg: Config, sections: list[str], secrets: bool) -> str:
    """The TOML of ``pdms config export`` with those sections; database passwords only with ``secrets``."""
    if unknown := [section for section in sections if section not in transfer.SECTIONS]:
        raise ActionError(_("Unknown sections: {unknown}. Available: {codes}",
                            unknown=", ".join(unknown), codes=", ".join(transfer.SECTIONS)))
    if not sections:
        raise ActionError(_("Nothing selected."))
    return transfer.export_document(cfg, [section for section in transfer.SECTIONS if section in sections], secrets)


def read_export(text: str) -> transfer.Document:
    try:
        return transfer.read_document(text)
    except transfer.TransferError as exc:
        raise ActionError(str(exc)) from exc


def first_setup() -> bool:
    """No configuration file yet: an import has nothing of the user's own to keep."""
    return not config_path().exists()


def import_config(result: Config) -> Path | None:
    """Save an imported configuration (``transfer.apply_import``), keeping a copy of the previous file; returns it."""
    backup = None
    if config_path().exists():
        backup = config_path().with_name(f"{config_path().name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path(), backup)
    result.save()
    return backup


def save_defaults(cfg: Config, defaults: Defaults) -> None:
    """Replace the defaults, after checking every value. Applying the language is up to the front end; opening
    pdms ui at login is set up (or removed) here."""
    for key in defaults.env:
        if not ENV_NAME.fullmatch(key):
            raise InvalidValue("env", _("'{name}' is not a valid variable name", name=key))
    if defaults.ui_at_login != cfg.defaults.ui_at_login:
        set_ui_at_login(defaults.ui_at_login)
    cfg.defaults = replace(
        defaults,
        language=_one_of("language", defaults.language, LANGUAGES),
        host=_required("host", defaults.host),
        port=_number("port", defaults.port, 1, 65535),
        logging_level=_one_of("logging_level", defaults.logging_level, LOG_LEVELS),
        events=_one_of("events", defaults.events, EVENTS_MODES),
        events_port=_number("events_port", defaults.events_port, 1, 65535),
        db_timeout=_number("db_timeout", defaults.db_timeout, 1, 600),
        proxy_timeout=_number("proxy_timeout", defaults.proxy_timeout, 1, local_proxy.MAX_PROXY_TIMEOUT),
        warm_connections=_number("warm_connections", defaults.warm_connections, 0, services.MAX_WARM_CONNECTIONS),
        proxy_port=_number("proxy_port", defaults.proxy_port, 1, 65535),
        idle_minutes=_number("idle_minutes", defaults.idle_minutes, 1, services.MAX_IDLE_MINUTES),
        theme=_one_of("theme", defaults.theme, THEMES),
        update_channel=_one_of("update_channel", defaults.update_channel, UPDATE_CHANNELS),
        email_to=check_email_to(defaults.email_to),
        test_views=dict(defaults.test_views), test_views_db=defaults.test_views_db,
        front_redirect_hosts=check_front_hosts(defaults.front_redirect_hosts),
        env=dict(defaults.env),
    )
    cfg.save()


def set_ui_at_login(enabled: bool) -> None:
    """Add or remove the system's entry that opens pdms ui at login; :class:`InvalidValue` if the system refuses."""
    try:
        desktop.set_autostart(enabled)
    except OSError as exc:
        raise InvalidValue("ui_at_login", _("Could not change the login items: {error}", error=exc)) from exc


def set_language(cfg: Config, lang: str) -> None:
    if lang not in LANGUAGES:
        raise ActionError(_("Unknown language '{lang}'. Available: {codes}", lang=lang, codes=", ".join(LANGUAGES)))
    cfg.defaults.language = lang
    cfg.save()


def set_email_to(cfg: Config, address: str) -> None:
    """Where the emails of the services go from their next start (empty: nowhere, pdms ui shows them)."""
    cfg.defaults.email_to = check_email_to(address)
    cfg.save()
