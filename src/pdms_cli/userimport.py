"""Create development user profiles from the PDMS ``pdms_user`` table.

The table stores internal role names (``TRANSPORTATION_PR_SUPERVISOR``) while ``DEV_ROLES`` expects the external
ones (``TPR.Supervisor``): restapi-fastapi maps them with ``MAP_EXTERNAL_ROLES`` and silently drops unknown values.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import Database, DevUser

MODELS = Path("backend/common/core/core/domain/models.py")
SETTINGS = Path("backend/common/core/core/settings.py")

# Fallback copy of MAP_INTERNAL_ROLES (backend/common/core/core/settings.py), used when the repo cannot be read.
INTERNAL_TO_EXTERNAL_ROLES = {
    "SPECIALTY_PR_SUPERVISOR": "SPR.Supervisor",
    "SPECIALTY_PR_AGENT": "SPR.Agent",
    "TRANSPORTATION_PR_SUPERVISOR": "TPR.Supervisor",
    "TRANSPORTATION_PR_AGENT": "TPR.Agent",
    "CREDENTIALS_SUPERVISOR": "Credentialing.Supervisor",
    "CREDENTIALS_AGENT": "Credentialing.Agent",
    "CREDENTIALS_TP_PROVIDER": "TP.Provider",
    "CREDENTIALS_SP_PROVIDER": "SP.Provider",
}
EXTERNAL_TO_INTERNAL_ROLES = {v: k for k, v in INTERNAL_TO_EXTERNAL_ROLES.items()}


def _enum_values(tree: ast.Module, name: str) -> dict[str, str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {
                t.targets[0].id: t.value.value
                for t in node.body
                if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name)
                and isinstance(t.value, ast.Constant) and isinstance(t.value.value, str)
            }
    return {}


def _map_internal_roles(tree: ast.Module) -> list[tuple[str, str]] | None:
    """Member names paired in ``MAP_INTERNAL_ROLES = {UserRoleEnum.X: UserRolePPEnum.Y, ...}``."""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MAP_INTERNAL_ROLES"
                                                for t in node.targets) and isinstance(node.value, ast.Dict)):
            return [(k.attr, v.attr) for k, v in zip(node.value.keys, node.value.values)
                    if isinstance(k, ast.Attribute) and isinstance(v, ast.Attribute)]
    return None


@lru_cache(maxsize=8)
def repo_role_mapping(root: Path) -> dict[str, str] | None:
    """Internal -> external role names read (not imported) from a PDMS checkout, or None if unreadable."""
    try:
        models = ast.parse((root / MODELS).read_text())
    except (OSError, SyntaxError):
        return None
    internal, external = _enum_values(models, "UserRoleEnum"), _enum_values(models, "UserRolePPEnum")
    if not internal or not external:
        return None
    try:
        pairs = _map_internal_roles(ast.parse((root / SETTINGS).read_text()))
    except (OSError, SyntaxError):
        pairs = None
    if pairs is None:  # no explicit map: members with the same name correspond (as they do today)
        pairs = [(name, name) for name in internal if name in external]
    mapping = {internal[i]: external[e] for i, e in pairs if i in internal and e in external}
    return mapping or None


def role_mapping(root: Path | None) -> tuple[dict[str, str], str]:
    """``(internal -> external mapping, where it came from)`` for the given repo root."""
    mapping = repo_role_mapping(root.resolve()) if root else None
    if mapping:
        return mapping, str(root / SETTINGS)
    return INTERNAL_TO_EXTERNAL_ROLES, "built-in"

QUERY = """
SELECT entity_id::text, username, first_name, last_name, coalesce(roles, '{}'::varchar[]), is_active
FROM public.pdms_user
WHERE (%(inactive)s OR is_active IS TRUE)
  AND (%(search)s = ''
       OR username ILIKE %(like)s OR first_name ILIKE %(like)s OR last_name ILIKE %(like)s
       OR (first_name || ' ' || last_name) ILIKE %(like)s)
  AND (%(role)s = '' OR %(role)s = ANY(roles))
ORDER BY last_name, first_name, username
LIMIT %(limit)s
"""


@dataclass
class DbUser:
    user_id: str
    username: str
    first_name: str
    last_name: str
    roles: list[str]  # internal names, as stored
    is_active: bool | None

    def dev_roles(self, mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES) -> str:
        return ",".join(mapping.get(r, r) for r in self.roles)

    def unknown_roles(self, mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES) -> list[str]:
        return [r for r in self.roles if r not in mapping]

    def to_dev_user(self, mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES) -> DevUser:
        return DevUser(
            user_id=self.user_id, username=self.username, first_name=self.first_name, last_name=self.last_name,
            roles=self.dev_roles(mapping),
        )


def internal_role(role: str, mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES) -> str:
    """Accept a role filter in either form (TPR.Supervisor or TRANSPORTATION_PR_SUPERVISOR)."""
    return {v: k for k, v in mapping.items()}.get(role, role)


def fetch_users(
    db: Database, *, search: str = "", role: str = "", include_inactive: bool = False, limit: int = 200,
    timeout: int = 15, mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES,
) -> list[DbUser]:
    import psycopg

    params = {
        "inactive": include_inactive, "search": search, "like": f"%{search}%", "role": internal_role(role, mapping) if role else "",
        "limit": limit,
    }
    with psycopg.connect(
        host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password, connect_timeout=timeout
    ) as conn:
        conn.read_only = True
        rows = conn.execute(QUERY, params).fetchall()
    return [DbUser(r[0], r[1], r[2] or "", r[3] or "", list(r[4] or []), r[5]) for r in rows]


def alias_for(username: str) -> str:
    """A config alias from the email's local part: supervisor.nemt1@gmail.com -> supervisor-nemt1."""
    local = username.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", local).strip("-") or "user"


def merge_users(
    existing: dict[str, DevUser], picked: list[DbUser], mapping: dict[str, str] = INTERNAL_TO_EXTERNAL_ROLES,
) -> tuple[dict[str, DevUser], list[str], list[str]]:
    """Add the picked users, updating the ones already imported (same DEV_USER_ID) in place.

    Returns (users, added aliases, updated aliases).
    """
    users = dict(existing)
    by_id = {u.user_id: alias for alias, u in users.items()}
    added, updated = [], []
    for db_user in picked:
        dev_user = db_user.to_dev_user(mapping)
        if db_user.user_id in by_id:
            alias = by_id[db_user.user_id]
            if users[alias] != dev_user:
                updated.append(alias)
            users[alias] = dev_user
            continue
        base = alias = alias_for(db_user.username)
        n = 2
        while alias in users:
            alias, n = f"{base}-{n}", n + 1
        users[alias] = dev_user
        by_id[db_user.user_id] = alias
        added.append(alias)
    return users, added, updated
