"""Test copies of the materialized views: try a change of a view against a shared database without touching the real one.

The view's SQL in the pdms-db-migrations checkout (``migrations/repeatable/materialized_views/R__MV_<view>.sql``)
creates ``<view>_new``, its indexes, and swaps it in for ``<view>``. pdms copies it with a suffix from the ticket of the
checkout's branch (``feature/PDMP-627-…`` → ``_pdmp627``, one per developer and ticket) on the names it creates, and
only on those: the view, its ``_new`` and its ``idx_…`` indexes (``pdms_report_document_view_expression``, a table the
SQL reads, stays). Run against the database (web-dev), it makes ``<view>_pdmp627`` beside the real view.

Services pdms starts then read the copy instead (``defaults.test_views``, given to them as ``PDMS_VIEW_MAP``, see
sqs_patch/pdms_views.py), until they stop using it; dropping it takes the copy away. A user that cannot create in the
schema (the usual one of web-dev) gets the SQL to run in a database client instead.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Database
from .i18n import _

FOLDER = Path("migrations") / "repeatable" / "materialized_views"
PREFIX = "R__MV_"
SCHEMA = "public"
MAX_NAME = 63  # Postgres cuts longer names
IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
TICKET = re.compile(r"(?:^|[/_-])([A-Za-z]{2,10})-(\d{1,6})(?:[^0-9]|$)")
SUFFIX = re.compile(r"^_[a-z0-9_]{1,20}$")


class TestViewError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class TestView:
    view: str  # pdms_report_lead_view
    name: str  # pdms_report_lead_view_pdmp627
    path: Path  # its SQL in the checkout


def files(repo: Path) -> dict[str, Path]:
    """``{view: its SQL}`` of the checkout's materialized views."""
    folder = repo / FOLDER
    return {path.stem[len(PREFIX):]: path for path in sorted(folder.glob(f"{PREFIX}*.sql"))} if folder.is_dir() else {}


def branch(repo: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def ticket_suffix(branch_name: str) -> str:
    """``feature/PDMP-627-report-row`` → ``_pdmp627`` ("" without a ticket in the branch's name)."""
    match = TICKET.search(branch_name)
    return f"_{match.group(1).lower()}{match.group(2)}" if match else ""


def check_suffix(suffix: str) -> str:
    suffix = suffix.strip().lower()
    suffix = suffix if suffix.startswith("_") else f"_{suffix}"
    if not SUFFIX.match(suffix):
        raise TestViewError(_("'{suffix}' is not a suffix: lowercase letters, digits and _ (e.g. _pdmp627).",
                              suffix=suffix))
    return suffix


def renames(view: str, sql: str, suffix: str) -> dict[str, str]:
    """``{name: test name}`` for the names the view's SQL creates: the view, ``<view>_new`` and its ``idx_…`` indexes."""
    found = {}
    for token in sorted(set(IDENTIFIER.findall(sql))):
        if token in (view, f"{view}_new"):
            found[token] = view + suffix + token[len(view):]
        elif token.startswith("idx_") and view in token:
            found[token] = token.replace(view, view + suffix, 1)
    too_long = [name for name in found.values() if len(name) > MAX_NAME]
    if too_long:
        raise TestViewError(_("{name} is longer than Postgres allows ({max} characters): use a shorter suffix.",
                              name=too_long[0], max=MAX_NAME))
    return found


def test_sql(repo: Path, view: str, suffix: str) -> tuple[TestView, str]:
    """The test view and the SQL that creates it (the checkout's, with the names it creates suffixed)."""
    path = files(repo).get(view)
    if path is None:
        raise TestViewError(_("No materialized view {view} in {folder}.", view=view, folder=repo / FOLDER))
    sql = path.read_text(encoding="utf-8")
    mapping = renames(view, sql, suffix)
    if view not in mapping:
        raise TestViewError(_("{path} does not create {view}: pdms cannot tell which names to copy.", path=path,
                              view=view))
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, sorted(mapping, key=len, reverse=True)))
                         + r")(?![A-Za-z0-9_])")
    # Always with data: a copy left empty (WITH NO DATA) cannot be read until refreshed.
    sql = re.sub(r"\bWITH\s+NO\s+DATA\b", "WITH DATA", sql, flags=re.IGNORECASE)
    header = (f"-- {_('Test copy of {view} made by pdms from {file}', view=view, file=path.name)}\n"
              f"-- {_('Drop it when you are done: {sql}', sql=drop_sql(mapping[view]))}\n\n")
    return TestView(view, mapping[view], path), header + pattern.sub(lambda match: mapping[match.group(1)], sql)


def drop_sql(name: str) -> str:
    return f"DROP MATERIALIZED VIEW IF EXISTS {SCHEMA}.{name};  -- {_('its indexes go with it')}"


# --------------------------------------------------------------------------- which services read which view

TABLES_FILE = Path("orm") / "tables.py"
SKIP_DIRS = {".venv", "venv", "tests", "__pycache__", "node_modules", ".git"}
_names_cache: dict[Path, tuple[float, dict[str, set[str]]]] = {}
_reads_cache: dict[Path, tuple[float, set[str]]] = {}  # folder → (time.monotonic(), views)


def tables_file(backend: Path) -> Path | None:
    """The ORM's tables.py of the repo, where the views are declared (common/core/…/orm/tables.py)."""
    for path in sorted(backend.glob("common/*/*/infrastructure/orm/tables.py")):
        if "class TableName" in path.read_text(encoding="utf-8", errors="replace"):
            return path
    return None


def view_names(backend: Path) -> dict[str, set[str]]:
    """``{view: the names code uses for it}``: its name, its ``TableName`` constant and its ORM class
    (``pdms_report_lead_view`` → ``REPORT_LEAD_VIEW``, ``ReportLeadView``)."""
    path = tables_file(backend)
    if path is None:
        return {}
    stamp = path.stat().st_mtime
    if (cached := _names_cache.get(path)) and cached[0] == stamp:
        return cached[1]
    text = path.read_text(encoding="utf-8", errors="replace")
    constants = {name: value for name, value in re.findall(r'^\s+([A-Z][A-Z0-9_]*)\s*=\s*"([a-z0-9_]+_view)"', text, re.M)}
    found: dict[str, set[str]] = {value: {value, constant} for constant, value in constants.items()}
    # Its __tablename__ within a few lines of the class, without crossing into the next class.
    for cls, constant in re.findall(r"^class (\w+)\(.*?\):\n(?:(?!class ).*\n){0,8}?\s+__tablename__ = "
                                    r"TableName\.(\w+)\.value", text, re.M):
        if constant in constants:
            found[constants[constant]].add(cls)
    _names_cache[path] = (stamp, found)
    return found


def _python_files(folder: Path):
    for path in folder.rglob("*.py"):
        if not SKIP_DIRS.intersection(path.relative_to(folder).parts):
            yield path


READS_FOR = 120  # seconds the views a folder's code names are kept (packages are shared by many services)


def _folder_reads(folder: Path, pattern: re.Pattern, owner: dict[str, str]) -> set[str]:
    import time

    if (cached := _reads_cache.get(folder)) and time.monotonic() - cached[0] < READS_FOR:
        return cached[1]
    found = set()
    for path in _python_files(folder):
        found.update(owner[match] for match in pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
    _reads_cache[folder] = (time.monotonic(), found)
    return found


def reads(service: Path, backend: Path) -> set[str]:
    """The views ``service`` reads: those its code, or its local packages', names (the package with the ORM's
    tables.py, where all are declared, does not count)."""
    from .testruns import dependencies

    names = view_names(backend)
    core = tables_file(backend)
    if not names or core is None:
        return set()
    pattern = re.compile(r"\b(" + "|".join(sorted({n for group in names.values() for n in group}, key=len, reverse=True))
                         + r")\b")
    owner = {name: view for view, group in names.items() for name in group}
    folders = [folder for folder in [service, *dependencies(service)] if not core.is_relative_to(folder)]
    return set().union(*(_folder_reads(folder, pattern, owner) for folder in folders))


def subject(view: str) -> str:
    """What a view is about, as services name it: ``pdms_report_lead_view`` → ``lead``, ``pdms_service_capacity_view``
    → ``service-capacity``."""
    core = view.removeprefix("pdms_").removeprefix("report_").removesuffix("_view")
    return core.replace("_", "-")


def narrowed(service_name: str, views: set[str]) -> set[str]:
    """The views a service is about (``report-lead-run`` → the lead view) when its name says so; all of them for a
    general one (``report-download``), since report-common names every view."""
    own = {view for view in views if re.search(rf"(^|-){re.escape(subject(view))}(-|$)", service_name)}
    return own or views


def modified(repo: Path, db: Database, timeout: int = 20) -> dict[str, bool]:
    """``{view: uncommitted}`` of the views whose SQL in the checkout differs from what ``db`` ran (Flyway's
    repeatable checksum): your change, committed or not."""
    from . import migrations

    known = migrations.status(repo, db, timeout)
    found = {}
    for item in known["migrations"]:
        script = Path(item.get("script") or "").name
        if item["kind"] == "repeatable" and item["state"] in ("outdated", "pending") and script.startswith(PREFIX):
            found[script[len(PREFIX):-len(".sql")]] = bool(item.get("uncommitted"))
    return found


# --------------------------------------------------------------------------- copies made, and with which SQL


def made_path() -> Path:
    from .instances import state_dir

    return state_dir() / "test-views.json"


def made() -> dict[str, dict]:
    """``{copy: {"db", "checksum"}}``: the copies pdms made, or the user ran the SQL of, and with which SQL."""
    import json

    try:
        data = json.loads(made_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(name: str, db_alias: str, sql: str) -> None:
    import json
    import zlib

    data = made()
    data[name] = {"db": db_alias, "checksum": zlib.crc32(sql.encode("utf-8"))}
    made_path().parent.mkdir(parents=True, exist_ok=True)
    made_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def forget(name: str) -> None:
    import json

    data = made()
    if data.pop(name, None) is not None:
        made_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def made_with(name: str, sql: str) -> bool:
    """Whether copy ``name`` was made with this very SQL (your file did not change since)."""
    import zlib

    return made().get(name, {}).get("checksum") == zlib.crc32(sql.encode("utf-8"))


# --------------------------------------------------------------------------- the database


def _connect(db: Database, timeout: int, read_only: bool):
    import psycopg

    conn = psycopg.connect(host=db.host, port=db.port, dbname=db.database, user=db.user, password=db.password,
                           connect_timeout=timeout, autocommit=not read_only)
    if read_only:
        conn.read_only = True
    return conn


def can_create(db: Database, timeout: int = 20) -> bool:
    """Whether the user of ``db`` may create in the schema (web-dev's usual one may not)."""
    with _connect(db, timeout, read_only=True) as conn:
        return bool(conn.execute("select has_schema_privilege(%s, 'CREATE')", [SCHEMA]).fetchone()[0])


def existing(db: Database, names: list[str], timeout: int = 20) -> dict[str, str]:
    """``{name: owner}`` of the materialized views among ``names`` that ``db`` has."""
    if not names:
        return {}
    with _connect(db, timeout, read_only=True) as conn:
        rows = conn.execute("select matviewname, matviewowner from pg_matviews where schemaname = %s "
                            "and matviewname = any(%s)", [SCHEMA, names]).fetchall()
    return {name: owner for name, owner in rows}


def run(db: Database, sql: str, timeout: int = 20) -> None:
    """Run ``sql`` on ``db`` (creating or dropping a test view); the database's error otherwise."""
    try:
        with _connect(db, timeout, read_only=False) as conn:
            conn.execute(sql)
    except Exception as exc:  # noqa: BLE001 - the driver's errors, said as they are
        raise TestViewError(str(exc).strip()[:500]) from exc


def sql_file(view: TestView, sql: str) -> Path:
    """Where pdms leaves the SQL for a database client, in its state folder."""
    from .instances import state_dir

    path = state_dir() / "test-views" / f"{view.name}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- what services read


def in_use(cfg: Config) -> dict[str, str]:
    """``{view: test view}`` the services on :func:`database_of` read (from their next start)."""
    return dict(cfg.defaults.test_views)


def database_of(cfg: Config) -> str:
    """The database alias the test views are in (web-dev, by default)."""
    return cfg.defaults.test_views_db or ("web-dev" if "web-dev" in cfg.dbs else cfg.last_db)


def env(cfg: Config, db_alias: str) -> dict[str, str]:
    """``PDMS_VIEW_MAP`` for a service on ``db_alias``: only where the test copies exist."""
    mapping = in_use(cfg) if db_alias == database_of(cfg) else {}
    return {"PDMS_VIEW_MAP": ",".join(f"{view}={test}" for view, test in sorted(mapping.items()))} if mapping else {}


def use(cfg: Config, view: str, name: str, db_alias: str) -> None:
    if cfg.defaults.test_views and db_alias != database_of(cfg):
        raise TestViewError(_("Services already read test views of {db}: stop using them first (one database at a "
                              "time).", db=database_of(cfg)))
    cfg.defaults.test_views = {**cfg.defaults.test_views, view: name}
    cfg.defaults.test_views_db = db_alias
    cfg.save()


def stop_using(cfg: Config, view: str) -> bool:
    if view not in cfg.defaults.test_views:
        return False
    cfg.defaults.test_views = {k: v for k, v in cfg.defaults.test_views.items() if k != view}
    cfg.save()
    return True


# --------------------------------------------------------------------------- what pdms ui and the CLI show and do


def overview(cfg: Config, repo: Path, db_alias: str, suffix: str = "", timeout: int = 20) -> dict:
    """The views your checkout changed against ``db_alias``, and the test copies there (one left behind once your
    change is in the real view, or dropped, says so)."""
    db = cfg.dbs[db_alias]
    suffix = check_suffix(suffix) if suffix else ticket_suffix(branch(repo))
    changed = modified(repo, db, timeout)
    used = in_use(cfg) if db_alias == database_of(cfg) else {}
    ours = {view: view + suffix for view in files(repo)} if suffix else {}
    names = {view: used.get(view) or ours.get(view, "") for view in files(repo)}
    there = existing(db, sorted(name for name in names.values() if name), timeout)
    rows = []
    for view, name in names.items():
        if view not in changed and name not in there:
            continue
        rows.append({"view": view, "name": name, "exists": name in there, "owner": there.get(name, ""),
                     "in_use": view in used, "changed": view in changed, "uncommitted": changed.get(view, False),
                     "current": made_with(name, test_sql(repo, view, suffix)[1]) if suffix and name in there else False})
    return {"db": db_alias, "repo": str(repo), "branch": branch(repo), "suffix": suffix,
            "can_create": can_create(db, timeout), "views": rows}
