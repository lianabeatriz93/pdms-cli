"""Export and import of the pdms configuration (defaults, users, databases and stacks).

Exports are TOML files with a ``[pdms]`` header. Database passwords are left out unless explicitly requested, and
importing a database without password keeps the password already configured locally under the same alias.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import tomlkit

from . import __version__
from .config import Config
from .i18n import _

FORMAT_VERSION = 1
SECTIONS = ("defaults", "users", "dbs", "stacks")
ITEM_SECTIONS = ("users", "dbs", "stacks")


class TransferError(Exception):
    pass


def export_document(cfg: Config, sections: list[str], secrets: bool) -> str:
    data = cfg.to_dict()
    doc: dict[str, Any] = {
        "pdms": {
            "format": FORMAT_VERSION,
            "cli_version": __version__,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "secrets": secrets,
            "sections": list(sections),
        }
    }
    for section in SECTIONS:
        if section in sections:
            doc[section] = data[section]
    if "dbs" in doc and not secrets:
        for db in doc["dbs"].values():
            db["password"] = ""
    return tomlkit.dumps(doc)


@dataclass
class Document:
    meta: dict[str, Any]
    config: Config
    sections: list[str]


def read_document(text: str) -> Document:
    try:
        data = tomlkit.parse(text).unwrap()
    except Exception as exc:  # noqa: BLE001 - tomlkit raises several error types
        raise TransferError(_("The file is not valid TOML: {error}", error=exc)) from exc
    meta = data.get("pdms")
    if not isinstance(meta, dict):
        raise TransferError(_("This is not a pdms export (the \\[pdms] header is missing)."))
    if int(meta.get("format", 0)) > FORMAT_VERSION:
        raise TransferError(_("The file was exported by a newer pdms version; update pdms first."))
    sections = [s for s in SECTIONS if s in data]
    try:
        incoming = Config.from_dict({s: data[s] for s in sections})
    except (TypeError, AttributeError, ValueError) as exc:
        raise TransferError(_("The file has an invalid structure: {error}", error=exc)) from exc
    return Document(meta=meta, config=incoming, sections=sections)


@dataclass
class SectionPlan:
    section: str
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    # Entries that exist locally but not in the file (only removed with replace=True).
    missing: list[str] = field(default_factory=list)


def _comparable(section: str, item: Any, current: Any = None) -> dict[str, Any]:
    data = asdict(item)
    if section == "dbs" and not data["password"] and current is not None:
        data["password"] = current.password  # an empty password means "keep mine"
    return data


def plan_import(current: Config, incoming: Config, sections: list[str]) -> list[SectionPlan]:
    plans = []
    if "defaults" in sections:
        plan = SectionPlan("defaults")
        (plan.same if current.defaults == incoming.defaults else plan.changed).append("defaults")
        plans.append(plan)
    for section in ITEM_SECTIONS:
        if section not in sections:
            continue
        mine, theirs = getattr(current, section), getattr(incoming, section)
        plan = SectionPlan(section)
        for name, item in theirs.items():
            if name not in mine:
                plan.added.append(name)
            elif _comparable(section, item, mine[name]) == asdict(mine[name]):
                plan.same.append(name)
            else:
                plan.changed.append(name)
        plan.missing = [name for name in mine if name not in theirs]
        plans.append(plan)
    return plans


def apply_import(
    current: Config,
    incoming: Config,
    sections: list[str],
    overwrite: set[tuple[str, str]],
    replace: bool = False,
) -> Config:
    """Return a new config with the import applied.

    Merge (default): new entries are added and existing ones are only overwritten when listed in ``overwrite``.
    Replace: the selected sections become exactly the file's content.
    Either way, imported databases without password keep the local password of the same alias.
    """
    result = copy.deepcopy(current)
    if "defaults" in sections and (replace or ("defaults", "defaults") in overwrite):
        result.defaults = copy.deepcopy(incoming.defaults)
    for section in ITEM_SECTIONS:
        if section not in sections:
            continue
        mine, theirs = getattr(current, section), getattr(incoming, section)
        merged = {} if replace else dict(copy.deepcopy(mine))
        for name, item in theirs.items():
            if name in mine and not replace and (section, name) not in overwrite:
                continue
            item = copy.deepcopy(item)
            if section == "dbs" and not item.password and name in mine:
                item.password = mine[name].password
            merged[name] = item
        setattr(result, section, merged)
    if result.last_user not in result.users:
        result.last_user = ""
    if result.last_db not in result.dbs:
        result.last_db = ""
    return result
