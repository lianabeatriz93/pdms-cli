"""The Settings screen: repos, databases, users, defaults, export and import."""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime

from ... import actions, i18n, instances, repos, runner, transfer, userimport
from ...config import EVENTS_MODES, LOG_LEVELS, THEMES, Config, Database, Defaults, DevUser, config_path
from ...i18n import _

__all__ = [
    "_bool", "_limit", "_plans", "_sections", "_strings", "_text", "add_repo", "apply_import", "connect_db",
    "database_from", "db_password", "db_users", "defaults_from", "export_config", "import_db_users", "look_at_repo",
    "plan_import", "remove_db", "remove_repo", "remove_user", "rename_user", "repo_settings", "save_db",
    "save_defaults", "save_repo", "save_user", "settings", "user_from",
]


def settings(cfg: Config) -> dict:
    """What the Settings tab shows: every database without its password, the users, the defaults and the stacks
    that use each database or user (they ask again when it is deleted)."""
    def used_by(kind: str, name: str) -> list[str]:
        return [stack_name for stack_name, stack in cfg.stacks.items() if getattr(stack, kind) == name]

    return {
        "path": str(config_path()),
        "dbs": [
            {"name": name, **{k: v for k, v in asdict(db).items() if k != "password"},
             "has_password": bool(db.password), "stacks": used_by("db", name)}
            for name, db in cfg.dbs.items()
        ],
        "users": [{"name": name, **asdict(user), "stacks": used_by("user", name)} for name, user in cfg.users.items()],
        "repos": repo_settings(cfg),
        "defaults": asdict(cfg.defaults),
        "roles": actions.known_roles(cfg),
        "sections": list(transfer.SECTIONS),
        "choices": {"language": i18n.LANGUAGES, "logging_level": list(LOG_LEVELS), "events": list(EVENTS_MODES), "theme": list(THEMES),
                    "update_channel": {"stable": _("Stable"), "beta": _("Beta: new versions before everyone else")}},
    }


def repo_settings(cfg: Config) -> list[dict]:
    """The Repos tab: each repo with its folders, the remote API (saved or read from frontend/.env) and how many
    instances run from it."""
    live = [inst for inst in instances.load().values() if inst.alive()]
    rows = []
    for alias, repo in cfg.repos.items():
        root = repo.root
        rows.append({
            "name": alias, "path": str(root), "exists": root.is_dir(), "backend": repo.backend,
            "migrations": repo.migrations, "remote": repo.remote,
            "remote_found": "" if repo.remote or not root.is_dir() else (repos.remote_from_frontend(root) or ""),
            "current": alias == cfg.current_repo,
            "running": sum(repos.repo_of(cfg, inst.service) == alias for inst in live),
        })
    return rows


def look_at_repo(body: dict) -> dict:
    """What the Add repo dialog says about a folder: its PDMS checkout, how many services and a name for it."""
    cfg = Config.load()
    root = actions.repo_root(_text(body, "path"))
    backend = root / "backend"
    return {"root": str(root), "services": len(runner.find_services_below(backend)) if backend.is_dir() else 0,
            "name": repos.alias_of(cfg, root) or repos.suggest_alias(cfg, root),
            "registered": repos.alias_of(cfg, root) or ""}


def add_repo(body: dict) -> str:
    """Register a repo with its migrations and remote API (each checked before anything is saved)."""
    cfg = Config.load()
    migrations_path = actions.migrations_repo(_text(body, "migrations"))
    remote = actions.remote_api(_text(body, "remote"))
    alias = actions.add_repo(cfg, _text(body, "path"), _text(body, "name"))
    return actions.edit_repo(Config.load(), alias, migrations_path=migrations_path, remote=remote)


def save_repo(name: str, body: dict) -> str:
    return actions.edit_repo(Config.load(), name, new_alias=_text(body, "name", name),
                             migrations_path=_text(body, "migrations"), remote=_text(body, "remote"))


def remove_repo(name: str) -> str:
    return actions.remove_repo(Config.load(), name)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _text(body: dict, key: str, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise actions.ActionError(f"{key} must be a text")
    return value


def database_from(body: dict, current: Database | None) -> Database:
    """The database a form sends; a ``password`` left out or null keeps the current one."""
    password = body.get("password")
    if password is None:
        password = current.password if current else ""
    elif not isinstance(password, str):
        raise actions.ActionError("password must be a text, or null to keep the current one")
    return Database(
        host=_text(body, "host"), port=body.get("port", 5432), database=_text(body, "database"),
        user=_text(body, "user"), password=password, driver=current.driver if current else Database.driver,
        protected=body.get("protected") is True,
    )


def save_db(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    current = None if new else cfg.dbs[actions.require(cfg.dbs, _("database"), name)]
    return actions.save_db(cfg, name, database_from(body, current), new=new)


def remove_db(name: str) -> list[str]:
    return actions.remove_db(Config.load(), name)


def db_password(name: str) -> str:
    cfg = Config.load()
    return cfg.dbs[actions.require(cfg.dbs, _("database"), name)].password


def connect_db(body: dict) -> str:
    """Connect to a saved database (only its ``name``) or to the one in a form (its fields; the password of ``name``
    when the form leaves it out); returns the server version."""
    cfg = Config.load()
    name = _text(body, "name")
    if "host" not in body:
        return actions.check_connection(cfg.dbs[actions.require(cfg.dbs, _("database"), name)], cfg.defaults.db_timeout)
    current = cfg.dbs[actions.require(cfg.dbs, _("database"), name)] if name else None
    return actions.check_connection(actions.valid_db(database_from(body, current)), cfg.defaults.db_timeout)


def user_from(body: dict) -> DevUser:
    return DevUser(**{key: _text(body, key) for key in ("user_id", "username", "first_name", "last_name", "roles")})


def save_user(name: str, body: dict, new: bool) -> str:
    cfg = Config.load()
    return actions.save_user(cfg, name, user_from(body), new=new, roles=actions.known_roles(cfg))


def remove_user(name: str) -> list[str]:
    return actions.remove_user(Config.load(), name)


def rename_user(name: str, body: dict) -> str:
    return actions.rename_user(Config.load(), name, _text(body, "new_name"))


def defaults_from(body: dict, current: Defaults) -> Defaults:
    """The current defaults with the values the form sends, each of the right kind (the action checks the rest)."""
    values = asdict(current)
    for item in fields(Defaults):
        if item.name not in body:
            continue
        value, kind = body[item.name], type(values[item.name])
        if kind is bool:
            ok, expected = isinstance(value, bool), "true or false"
        elif kind is int:
            ok, expected = isinstance(value, (int, str)) and not isinstance(value, bool), "a number"
        elif kind is dict:
            ok, expected = isinstance(value, dict) and all(isinstance(v, str) for v in value.values()), "texts by name"
        elif kind is list:
            ok, expected = isinstance(value, list) and all(isinstance(v, str) for v in value), "a list of texts"
        else:
            ok, expected = isinstance(value, str), "a text"
        if not ok:
            raise actions.InvalidValue(item.name, f"must be {expected}")
        values[item.name] = value
    return Defaults(**values)


def save_defaults(body: dict) -> None:
    """Save the defaults; the language also applies to this server's messages from now on."""
    cfg = Config.load()
    actions.save_defaults(cfg, defaults_from(body, cfg.defaults))
    i18n.set_language(cfg.defaults.language)


def _bool(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise actions.ActionError(f"{key} must be true or false")
    return value


def _sections(body: dict) -> list[str]:
    sections = body.get("sections")
    if not isinstance(sections, list) or not all(isinstance(section, str) for section in sections):
        raise actions.ActionError("sections must be a list of section names")
    return sections


def export_config(body: dict) -> dict:
    """The file of ``pdms config export`` for the page to download."""
    text = actions.export_config(Config.load(), _sections(body), _bool(body, "secrets"))
    return {"filename": f"pdms-config-{datetime.now():%Y-%m-%d}.toml", "text": text}


def _plans(current: Config, doc: transfer.Document, sections: list[str]) -> list[dict]:
    return [asdict(plan) for plan in transfer.plan_import(current, doc.config, sections)]


def plan_import(body: dict) -> dict:
    """What importing the file ``text`` would do to each of its sections (``pdms config import``'s table)."""
    doc = actions.read_export(_text(body, "text"))
    if not doc.sections:
        raise actions.ActionError(_("Nothing to import."))
    meta = {key: doc.meta.get(key) for key in ("exported_at", "cli_version", "secrets")}
    return {"meta": meta, "sections": doc.sections, "plans": _plans(Config.load(), doc, doc.sections),
            "first_setup": actions.first_setup()}


def apply_import(body: dict) -> dict:
    """Import the chosen sections of ``text``: new entries always, the ``overwrite`` ones (``[section, name]``) over
    the user's own, or exactly the file's content with ``replace``. Keeps a copy of the previous configuration."""
    doc = actions.read_export(_text(body, "text"))
    sections = [section for section in _sections(body) if section in doc.sections]
    if not sections:
        raise actions.ActionError(_("Nothing to import."))
    overwrite = body.get("overwrite", [])
    if not isinstance(overwrite, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in overwrite
    ):
        raise actions.ActionError("overwrite must be a list of [section, name] pairs")
    current = Config.load()
    chosen = {tuple(pair) for pair in overwrite}
    if actions.first_setup():
        chosen = {(plan["section"], name) for plan in _plans(current, doc, sections) for name in plan["changed"]}
    result = transfer.apply_import(current, doc.config, sections, chosen, replace=_bool(body, "replace"))
    if result.to_dict() == current.to_dict():
        return {"changed": False, "backup": None, "no_password": []}
    backup = actions.import_config(result)
    i18n.set_language(result.defaults.language)
    return {"changed": True, "backup": str(backup) if backup else None,
            "no_password": [name for name, db in result.dbs.items() if not db.password]}


def _limit(body: dict) -> int:
    limit = body.get("limit", 200)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise actions.ActionError("limit must be a number between 1 and 1000")
    return limit


def db_users(body: dict) -> dict:
    """The users of a database's ``pdms_user`` table, with the ``DEV_ROLES`` each would get (``pdms user import``)."""
    cfg = Config.load()
    mapping, source = userimport.role_mapping(repos.active_root(cfg))
    limit = _limit(body)
    found = actions.read_db_users(
        cfg, _text(body, "db"), mapping, search=_text(body, "search").strip(),
        role=userimport.internal_role(_text(body, "role"), mapping), inactive=_bool(body, "inactive"), limit=limit,
    )
    known = {user.user_id: name for name, user in cfg.users.items()}
    return {
        "source": "built-in" if source == "built-in" else "repo", "limited": len(found) == limit,
        "users": [
            {**asdict(user), "dev_roles": user.dev_roles(mapping), "unknown_roles": user.unknown_roles(mapping),
             "imported_as": known.get(user.user_id, "")}
            for user in found
        ],
    }


def import_db_users(body: dict) -> dict:
    """Add the picked users (as :func:`db_users` listed them), updating the ones already imported."""
    picked = body.get("users")
    if not isinstance(picked, list) or not all(isinstance(user, dict) for user in picked):
        raise actions.ActionError("users must be a list of users")
    users = []
    for user in picked:
        roles = user.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise actions.ActionError("roles must be a list of role names")
        users.append(userimport.DbUser(
            user_id=_text(user, "user_id"), username=_text(user, "username"), first_name=_text(user, "first_name"),
            last_name=_text(user, "last_name"), roles=roles, is_active=user.get("is_active") is True,
        ))
    if not users or not all(user.user_id and user.username for user in users):
        raise actions.ActionError(_("Nothing selected."))
    cfg = Config.load()
    mapping, _source = userimport.role_mapping(repos.active_root(cfg))
    added, updated = actions.import_users(cfg, users, mapping)
    return {"added": added, "updated": updated}
