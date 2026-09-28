"""Persistent configuration: dev users, databases and run defaults.

Stored as TOML in ``~/.config/pdms/config.toml`` (override with ``PDMS_CONFIG``).
The file holds database passwords, so it is always written with ``0600`` permissions.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import quote

import tomlkit


def config_path() -> Path:
    if custom := os.environ.get("PDMS_CONFIG"):
        return Path(custom).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pdms" / "config.toml"


def _from_dict(cls, data: dict[str, Any]):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class DevUser:
    user_id: str
    username: str
    first_name: str = ""
    last_name: str = ""
    roles: str = ""

    def env(self) -> dict[str, str]:
        return {
            "DEV_USER_ID": self.user_id,
            "DEV_USERNAME": self.username,
            "DEV_FIRST_NAME": self.first_name,
            "DEV_LAST_NAME": self.last_name,
            "DEV_ROLES": self.roles,
        }


@dataclass
class Database:
    host: str
    port: int = 5432
    database: str = "pdm"
    user: str = ""
    password: str = ""
    driver: str = "postgresql+psycopg2"
    # Protected databases (shared/remote ones) ask for confirmation before running.
    protected: bool = False

    def url(self, mask: bool = False) -> str:
        auth = quote(self.user, safe="")
        if self.password:
            auth += ":" + ("****" if mask else quote(self.password, safe=""))
        return f"{self.driver}://{auth}@{self.host}:{self.port}/{self.database}"


@dataclass
class Defaults:
    # CLI language: "en" (default) or "es".
    language: str = "en"
    host: str = "0.0.0.0"
    port: int = 8080
    logging_level: str = "DEBUG"
    reload: bool = True
    install: bool = True
    # With install enabled, skip it when nothing that affects the install changed since the last one.
    smart_install: bool = True
    # Seconds to wait when testing a database connection.
    db_timeout: int = 15
    # Extra environment variables injected on every run.
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Stack:
    # Service paths relative to the backend folder, e.g. "lead/lead-tp-list".
    services: list[str] = field(default_factory=list)
    # Optional fixed user/db aliases; empty means "ask when starting".
    user: str = ""
    db: str = ""


@dataclass
class Repo:
    # Root of a PDMS checkout (the folder that contains backend/ and infra/).
    path: str
    # Folder with the services, relative to the root.
    backend: str = "backend"

    @property
    def root(self) -> Path:
        return Path(self.path).expanduser()

    @property
    def backend_dir(self) -> Path:
        return self.root / self.backend


SEED_USERS = {
    "supervisor": DevUser(
        user_id="f3d55402-2dce-476f-aa93-890b2f4d61c4",
        username="supervisor.nemt1@gmail.com",
        first_name="Transportation",
        last_name="Supervisor",
        roles="TPR.Supervisor",
    ),
}


@dataclass
class Config:
    defaults: Defaults = field(default_factory=Defaults)
    users: dict[str, DevUser] = field(default_factory=dict)
    dbs: dict[str, Database] = field(default_factory=dict)
    stacks: dict[str, Stack] = field(default_factory=dict)
    repos: dict[str, Repo] = field(default_factory=dict)
    current_repo: str = ""
    # Repo roots where pdms must not offer to switch the current repo.
    ignored_repos: list[str] = field(default_factory=list)
    last_user: str = ""
    last_db: str = ""

    @property
    def repo(self) -> Repo | None:
        return self.repos.get(self.current_repo)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        state = data.get("state", {})
        cfg = cls(
            defaults=_from_dict(Defaults, data.get("defaults", {})),
            users={k: _from_dict(DevUser, v) for k, v in data.get("users", {}).items()},
            dbs={k: _from_dict(Database, v) for k, v in data.get("dbs", {}).items()},
            stacks={k: _from_dict(Stack, v) for k, v in data.get("stacks", {}).items()},
            repos={k: _from_dict(Repo, v) for k, v in data.get("repos", {}).items()},
            current_repo=state.get("current_repo", ""),
            ignored_repos=list(state.get("ignored_repos", [])),
            last_user=state.get("last_user", ""),
            last_db=state.get("last_db", ""),
        )
        # Before repos existed, the services folder was configured as defaults.backend_path.
        legacy = data.get("defaults", {}).get("backend_path")
        if legacy and not cfg.repos:
            backend = Path(legacy).expanduser()
            root = backend.parent
            cfg.repos[root.name or "pdms"] = Repo(path=str(root), backend=backend.name)
            cfg.current_repo = root.name or "pdms"
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaults": asdict(self.defaults),
            "state": {
                "last_user": self.last_user, "last_db": self.last_db, "current_repo": self.current_repo,
                "ignored_repos": list(self.ignored_repos),
            },
            "users": {k: asdict(v) for k, v in self.users.items()},
            "dbs": {k: asdict(v) for k, v in self.dbs.items()},
            "stacks": {k: asdict(v) for k, v in self.stacks.items()},
            "repos": {k: asdict(v) for k, v in self.repos.items()},
        }

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        if not path.exists():
            return cls(users=dict(SEED_USERS))
        return cls.from_dict(tomlkit.parse(path.read_text()).unwrap())

    def save(self) -> None:
        write_private(config_path(), tomlkit.dumps(self.to_dict()))


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable only by the current user (it may contain passwords)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
