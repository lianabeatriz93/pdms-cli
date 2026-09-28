"""Create development user profiles from the PDMS ``pdms_user`` table.

The table stores internal role names (``TRANSPORTATION_PR_SUPERVISOR``) while ``DEV_ROLES`` expects the external
ones (``TPR.Supervisor``): restapi-fastapi maps them with ``MAP_EXTERNAL_ROLES`` and silently drops unknown values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Database, DevUser

# Mirrors MAP_INTERNAL_ROLES in backend/common/core/core/settings.py.
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

    @property
    def dev_roles(self) -> str:
        return ",".join(INTERNAL_TO_EXTERNAL_ROLES.get(r, r) for r in self.roles)

    @property
    def unknown_roles(self) -> list[str]:
        return [r for r in self.roles if r not in INTERNAL_TO_EXTERNAL_ROLES]

    def to_dev_user(self) -> DevUser:
        return DevUser(
            user_id=self.user_id, username=self.username, first_name=self.first_name, last_name=self.last_name,
            roles=self.dev_roles,
        )


def internal_role(role: str) -> str:
    """Accept a role filter in either form (TPR.Supervisor or TRANSPORTATION_PR_SUPERVISOR)."""
    return EXTERNAL_TO_INTERNAL_ROLES.get(role, role)


def fetch_users(
    db: Database, *, search: str = "", role: str = "", include_inactive: bool = False, limit: int = 200,
    timeout: int = 15,
) -> list[DbUser]:
    import psycopg

    params = {
        "inactive": include_inactive, "search": search, "like": f"%{search}%", "role": internal_role(role) if role else "",
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


def merge_users(existing: dict[str, DevUser], picked: list[DbUser]) -> tuple[dict[str, DevUser], list[str], list[str]]:
    """Add the picked users, updating the ones already imported (same DEV_USER_ID) in place.

    Returns (users, added aliases, updated aliases).
    """
    users = dict(existing)
    by_id = {u.user_id: alias for alias, u in users.items()}
    added, updated = [], []
    for db_user in picked:
        dev_user = db_user.to_dev_user()
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
