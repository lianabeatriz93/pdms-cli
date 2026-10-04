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

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Where services publish SQS events (see ``Defaults.events``).
EVENTS_MODES = ("auto", "local", "aws")
THEMES = ("system", "light", "dark")  # pdms ui: like the system, or always light or dark


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


OLD_SERVICE_PORT = 8080  # the default up to 0.2.5


@dataclass
class Defaults:
    # CLI language: "en" (default) or "es".
    language: str = "en"
    host: str = "0.0.0.0"
    # The first port tried for a service (the next free one when it is busy). Far from 8080 and the like, which many
    # programs use, and below the ports the system hands out to outgoing connections (32768 and up).
    port: int = 28100
    logging_level: str = "DEBUG"
    reload: bool = True
    install: bool = True
    # With install enabled, skip it when nothing that affects the install changed since the last one.
    smart_install: bool = True
    # Where services publish SQS events: "auto" (the local broker when `pdms events up` is running, else as
    # configured by the service), "local" (always the local broker) or "aws" (never touch it).
    events: str = "auto"
    # Host port of the local ElasticMQ (SQS) started by `pdms events up`.
    events_port: int = 9324
    # Show the big PDMS banner when the interactive menu opens.
    banner: bool = True
    # Tell when a new pdms version is published (checked at most once a day).
    update_check: bool = True
    # Open pdms ui (in the tray, without its window) when the user logs in to the computer.
    ui_at_login: bool = False
    # Seconds to wait when testing a database connection.
    db_timeout: int = 15
    # Seconds the proxy waits for a service (or the remote API) to answer before replying 502.
    proxy_timeout: int = 300
    # Port of pdms proxy (pdms proxy -p, pdms ui). Far from 8000, for the same reason.
    proxy_port: int = 28800
    # Look of pdms ui: "system" (light or dark like the computer), "light" or "dark".
    theme: str = "system"
    # pdms ui tells the desktop when a service fails to load or stops by itself (while it runs, also in the tray).
    notify: bool = True
    # Each request of a service runs in its own thread, so a slow database query only holds up its own request
    # (PDMS services query the database synchronously inside async endpoints). See sqs_patch/pdms_parallel.py.
    parallel_requests: bool = True
    # Database connections each service opens as soon as it starts, so its first requests don't wait for them
    # (0: open them on demand, as the service would on its own).
    warm_connections: int = 2
    # Each request of a service says how long it spent in the database and which queries it ran (pdms proxy keeps
    # them for pdms ui; the service's log warns about the same query run many times). See sqs_patch/pdms_queries.py.
    query_stats: bool = True
    # Profile of the user's AWS config the services use (AWS_PROFILE), with the buckets pdms reads from that
    # account's Lambdas (see awsenv.py). Empty: pdms leaves AWS as the terminal has it.
    aws_profile: str = ""
    # Where the emails services send through SES go (see emails.py): empty, nowhere (pdms ui shows them); an
    # address, to that one only, whoever they were for.
    email_to: str = ""
    # Extra environment variables injected on every run.
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Stack:
    # Service paths relative to the backend folder, e.g. "lead/lead-tp-list".
    services: list[str] = field(default_factory=list)
    # Optional fixed user/db aliases; empty means "ask when starting".
    user: str = ""
    db: str = ""
    # Services that run with another user or database than the rest: path -> {"user": ..., "db": ...} (either may
    # be missing). Set from a partial restart with "Remember in the stack", or in the stack editor.
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)

    def profile_of(self, service: str, user: str, db: str) -> tuple[str, str]:
        """The user and database ``service`` starts with, given the stack's own (``user``, ``db``)."""
        own = self.overrides.get(service, {})
        return own.get("user") or user, own.get("db") or db


def _stack(data: dict[str, Any]) -> Stack:
    stack = _from_dict(Stack, data)
    # Service paths are stored with "/" on every OS (a config written on Windows may still use "\\").
    stack.services = [str(svc).replace("\\", "/") for svc in stack.services]
    stack.overrides = {
        str(svc).replace("\\", "/"): {k: str(v) for k, v in own.items() if k in ("user", "db") and v}
        for svc, own in (stack.overrides or {}).items() if isinstance(own, dict)
    }
    return stack


@dataclass
class Repo:
    # Root of a PDMS checkout (the folder that contains backend/ and infra/).
    path: str
    # Folder with the services, relative to the root.
    backend: str = "backend"
    # Remote API the proxy falls back to, e.g. https://<id>.execute-api.us-east-1.amazonaws.com/dev
    remote: str = ""
    # Flyway migrations checkout (pdms-db-migrations) that goes with this repo.
    migrations: str = ""

    @property
    def root(self) -> Path:
        return Path(self.path).expanduser()

    @property
    def backend_dir(self) -> Path:
        return self.root / self.backend


@dataclass
class Setup:
    """What "Start everything" starts (pdms ui's Home), in this order: events, the stack, the proxy, the frontend."""

    stack: str = ""
    proxy: bool = True
    frontend: bool = True
    # How the frontend runs: "dev" (yarn dev) or "build" (a production build, served by vite preview).
    frontend_mode: str = "dev"
    events: bool = False


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
    # The current setup (what Start everything starts); setup_name is the saved one it comes from ("" when none).
    setup: Setup = field(default_factory=Setup)
    setups: dict[str, Setup] = field(default_factory=dict)
    setup_name: str = ""

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
            stacks={k: _stack(v) for k, v in data.get("stacks", {}).items()},
            repos={k: _from_dict(Repo, v) for k, v in data.get("repos", {}).items()},
            current_repo=state.get("current_repo", ""),
            ignored_repos=list(state.get("ignored_repos", [])),
            last_user=state.get("last_user", ""),
            last_db=state.get("last_db", ""),
            setup=_from_dict(Setup, data.get("setup", {})),
            setups={k: _from_dict(Setup, v) for k, v in data.get("setups", {}).items()},
            setup_name=state.get("setup_name", ""),
        )
        # Before proxy_port existed the defaults were 8080 (services) and 8000 (proxy), ports many other programs use:
        # a config still on the old service default moves to the new one, once (saving it adds proxy_port).
        if "proxy_port" not in data.get("defaults", {}) and cfg.defaults.port == OLD_SERVICE_PORT:
            cfg.defaults.port = Defaults.port
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
                "ignored_repos": list(self.ignored_repos), "setup_name": self.setup_name,
            },
            "users": {k: asdict(v) for k, v in self.users.items()},
            "dbs": {k: asdict(v) for k, v in self.dbs.items()},
            "stacks": {k: asdict(v) for k, v in self.stacks.items()},
            "repos": {k: asdict(v) for k, v in self.repos.items()},
            "setup": asdict(self.setup),
            "setups": {k: asdict(v) for k, v in self.setups.items()},
        }

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        if not path.exists():
            return cls()
        return cls.from_dict(tomlkit.parse(path.read_text(encoding="utf-8")).unwrap())

    def save(self) -> None:
        write_private(config_path(), tomlkit.dumps(self.to_dict()))


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable only by the current user (it may contain passwords)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
