"""Test copies of materialized views: named after the ticket, made from the checkout's SQL, read by pdms's services."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pdms_cli import actions, events, testviews
from pdms_cli.config import Config, Database, Defaults

VIEW_SQL = """-- Create temporary materialized view with new structure
DROP MATERIALIZED VIEW IF EXISTS public.pdms_report_document_view_new;
CREATE MATERIALIZED VIEW IF NOT EXISTS public.pdms_report_document_view_new AS
SELECT d.id FROM pdms_document d
  INNER JOIN pdms_report_document_view_expression dve ON (dve.id = d.id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdms_report_document_view_new_entity_id
ON public.pdms_report_document_view_new (entity_id);
DO $$
BEGIN
    DROP MATERIALIZED VIEW IF EXISTS public.pdms_report_document_view;
    ALTER MATERIALIZED VIEW public.pdms_report_document_view_new RENAME TO pdms_report_document_view;
    ALTER INDEX IF EXISTS public.idx_pdms_report_document_view_new_entity_id
    RENAME TO idx_pdms_report_document_view_entity_id;
END $$;
COMMENT ON MATERIALIZED VIEW public.pdms_report_document_view IS 'Documents';
"""


@pytest.fixture
def repo(tmp_path) -> Path:
    folder = tmp_path / "pdms-db-migrations" / testviews.FOLDER
    folder.mkdir(parents=True)
    (folder / "R__MV_pdms_report_document_view.sql").write_text(VIEW_SQL, encoding="utf-8")
    return tmp_path / "pdms-db-migrations"


@pytest.mark.parametrize(("branch", "suffix"), [
    ("feature/PDMP-627-report-row-navigation", "_pdmp627"), ("PDMP-12", "_pdmp12"), ("bugfix/pdmp-3_fix", "_pdmp3"),
    ("main", ""), ("feature/no-ticket", ""),
])
def test_the_suffix_comes_from_the_branchs_ticket(branch, suffix) -> None:
    assert testviews.ticket_suffix(branch) == suffix


def test_only_the_names_the_sql_creates_get_the_suffix(repo) -> None:
    made, sql = testviews.test_sql(repo, "pdms_report_document_view", "_pdmp627")
    assert made.name == "pdms_report_document_view_pdmp627"
    assert "public.pdms_report_document_view_pdmp627_new" in sql
    assert "RENAME TO pdms_report_document_view_pdmp627;" in sql
    assert "idx_pdms_report_document_view_pdmp627_new_entity_id" in sql
    assert "RENAME TO idx_pdms_report_document_view_pdmp627_entity_id;" in sql
    assert "COMMENT ON MATERIALIZED VIEW public.pdms_report_document_view_pdmp627 IS" in sql
    assert "INNER JOIN pdms_report_document_view_expression dve" in sql  # a table it reads: untouched
    assert " public.pdms_report_document_view;" not in sql  # never the real view
    assert sql.startswith("-- Test copy of pdms_report_document_view")


def test_names_postgres_would_cut_and_bad_suffixes_are_refused(repo) -> None:
    with pytest.raises(testviews.TestViewError, match="longer than Postgres allows"):
        testviews.test_sql(repo, "pdms_report_document_view", "_" + "x" * 20)
    with pytest.raises(testviews.TestViewError):
        testviews.check_suffix("PDMP-627; drop table x")
    assert testviews.check_suffix("PDMP627") == "_pdmp627"
    with pytest.raises(testviews.TestViewError, match="No materialized view"):
        testviews.test_sql(repo, "pdms_report_nothing_view", "_pdmp627")


def test_the_services_rewrite_their_queries(monkeypatch) -> None:
    sys.path.insert(0, str(events.PATCH_DIR))
    try:
        import pdms_views
    finally:
        sys.path.remove(str(events.PATCH_DIR))
    monkeypatch.setenv("PDMS_VIEW_MAP", "pdms_report_document_view=pdms_report_document_view_pdmp627,bad name=x")
    mapping = pdms_views.view_map()
    assert mapping == {"pdms_report_document_view": "pdms_report_document_view_pdmp627"}
    rewrite = pdms_views.rewriter(mapping)
    assert rewrite('SELECT * FROM pdms_report_document_view JOIN pdms_report_document_view_expression e '
                   'WHERE "pdms_report_document_view".id = 1') == (
        'SELECT * FROM pdms_report_document_view_pdmp627 JOIN pdms_report_document_view_expression e '
        'WHERE "pdms_report_document_view_pdmp627".id = 1')
    assert pdms_views.rewriter({})("SELECT 1") == "SELECT 1"


def test_only_services_on_that_database_read_the_copy() -> None:
    cfg = Config(dbs={"web-dev": Database("db.example.com"), "pdms-local": Database("localhost")},
                 defaults=Defaults(test_views={"pdms_report_lead_view": "pdms_report_lead_view_pdmp627"},
                                   test_views_db="web-dev"))
    assert testviews.env(cfg, "web-dev") == {"PDMS_VIEW_MAP": "pdms_report_lead_view=pdms_report_lead_view_pdmp627"}
    assert testviews.env(cfg, "pdms-local") == {}  # the copy is not there


@pytest.fixture
def cfg(monkeypatch, repo) -> Config:
    config = Config(dbs={"web-dev": Database("db.example.com", protected=True)})
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(actions, "current_migrations_repo", lambda cfg: repo)
    monkeypatch.setattr(testviews, "branch", lambda repo: "feature/PDMP-627-x")
    monkeypatch.setattr(testviews, "sql_file", lambda view, sql: Path(f"/state/{view.name}.sql"))
    monkeypatch.setattr(testviews, "readable", lambda db, name, timeout=20: True)
    return config


def test_without_permission_you_get_the_sql(cfg, monkeypatch) -> None:
    monkeypatch.setattr(testviews, "can_create", lambda db, timeout=20: False)
    result = actions.create_test_view(cfg, "pdms_report_document_view")
    assert result["created"] is False and result["name"] == "pdms_report_document_view_pdmp627"
    assert "pdms_report_document_view_pdmp627_new" in result["sql"] and result["file"].endswith("_pdmp627.sql")


def test_with_permission_it_is_made_after_confirming(cfg, monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(testviews, "can_create", lambda db, timeout=20: True)
    monkeypatch.setattr(testviews, "run", lambda db, sql, timeout=20: ran.append(sql))
    with pytest.raises(actions.ProtectedDatabase):
        actions.create_test_view(cfg, "pdms_report_document_view")
    assert actions.create_test_view(cfg, "pdms_report_document_view", confirmed=True)["created"] and len(ran) == 1


def test_services_read_a_copy_only_once_it_exists_and_drop_takes_it_away(cfg, monkeypatch) -> None:
    there = {}
    monkeypatch.setattr(testviews, "existing", lambda db, names, timeout=20: {n: "root" for n in names if n in there})
    with pytest.raises(actions.ActionError, match="not in web-dev yet"):
        actions.use_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
    there["pdms_report_document_view_pdmp627"] = True
    actions.use_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
    assert testviews.in_use(cfg) == {"pdms_report_document_view": "pdms_report_document_view_pdmp627"}
    assert cfg.defaults.test_views_db == "web-dev"
    monkeypatch.setattr(testviews, "can_create", lambda db, timeout=20: False)
    result = actions.drop_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
    assert result == {"dropped": False, "name": "pdms_report_document_view_pdmp627",
                      "sql": testviews.drop_sql("pdms_report_document_view_pdmp627")}
    assert testviews.in_use(cfg) == {}  # the services read the real one again, whatever happens to the copy
    with pytest.raises(actions.ActionError, match="not a test copy"):
        actions.drop_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view")


# --------------------------------------------------------------------------- which services, which views, and when

TABLES = '''
class TableName(str, Enum):
    REPORT_LEAD_VIEW = "pdms_report_lead_view"
    REPORT_DOCUMENT_VIEW = "pdms_report_document_view"


class ReportLeadView(BaseReportView, BaseTable):
    """The lead report."""

    __tablename__ = TableName.REPORT_LEAD_VIEW.value


class ReportDocumentView(BaseReportView, BaseTable):
    __tablename__ = TableName.REPORT_DOCUMENT_VIEW.value
'''


@pytest.fixture
def backend(tmp_path) -> Path:
    """A repo: core declares the views, report-common names them, report services and another one use it."""
    root = tmp_path / "backend"

    def package(path: str, deps: list[str], code: str = "") -> None:
        folder = root / path
        folder.mkdir(parents=True)
        lines = "\n".join(f'{name} = {{path = "../../{dep}", develop = true}}' for name, dep in
                          ((dep.rsplit("/", 1)[-1], dep) for dep in deps))
        (folder / "pyproject.toml").write_text(f"[tool.poetry.dependencies]\n{lines}\n", encoding="utf-8")
        (folder / "code.py").write_text(code, encoding="utf-8")

    package("common/core", [])
    (root / "common/core/core/infrastructure/orm").mkdir(parents=True)
    (root / "common/core/core/infrastructure/orm/tables.py").write_text(TABLES, encoding="utf-8")
    package("report/report-common", ["common/core"], "table_class = tables.ReportLeadView\nother = tables.ReportDocumentView\n")
    package("report/report-lead-run", ["report/report-common", "common/core"])
    package("report/report-download", ["report/report-common", "common/core"])
    package("lead/lead-place-get", ["common/core"], "from core.infrastructure.orm import tables\n")
    return root


def test_which_views_a_service_reads(backend) -> None:
    assert testviews.view_names(backend)["pdms_report_lead_view"] == {
        "pdms_report_lead_view", "REPORT_LEAD_VIEW", "ReportLeadView"}
    both = {"pdms_report_lead_view", "pdms_report_document_view"}
    assert testviews.reads(backend / "report/report-lead-run", backend) == both  # through report-common
    assert testviews.reads(backend / "lead/lead-place-get", backend) == set()  # core declares them: not a reader
    assert testviews.narrowed("report-lead-run", both) == {"pdms_report_lead_view"}
    assert testviews.narrowed("report-document-run-download", both) == {"pdms_report_document_view"}
    assert testviews.narrowed("report-download", both) == both  # a general one reads them all
    assert testviews.subject("pdms_service_capacity_view") == "service-capacity"


def test_a_view_is_changed_when_your_sql_differs_from_what_the_database_ran(repo, monkeypatch) -> None:
    from pdms_cli import migrations

    monkeypatch.setattr(migrations, "status", lambda repo, db, timeout: {"flyway": True, "migrations": [
        {"kind": "repeatable", "version": "", "description": "MV a", "state": "outdated", "uncommitted": True,
         "script": "materialized_views/R__MV_pdms_report_lead_view.sql"},
        {"kind": "repeatable", "version": "", "description": "MV b", "state": "applied", "uncommitted": False,
         "script": "materialized_views/R__MV_pdms_report_document_view.sql"},
        {"kind": "versioned", "version": "1", "description": "x", "state": "pending", "script": "V1__x.sql"},
    ]})
    assert testviews.modified(repo, Database("db")) == {"pdms_report_lead_view": True}


def test_a_copy_remembers_the_sql_it_was_made_with(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    testviews.remember("pdms_report_lead_view_pdmp627", "web-dev", "select 1")
    assert testviews.made_with("pdms_report_lead_view_pdmp627", "select 1")
    assert not testviews.made_with("pdms_report_lead_view_pdmp627", "select 2")  # your SQL changed since
    testviews.forget("pdms_report_lead_view_pdmp627")
    assert testviews.made() == {}


@pytest.fixture
def starting(cfg, backend, monkeypatch, tmp_path):
    """report-document-run on web-dev, with the document view changed in the checkout."""
    from pdms_cli import repos

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (backend / "report/report-document-run").mkdir(parents=True)
    (backend / "report/report-document-run/pyproject.toml").write_text(
        '[tool.poetry.dependencies]\nreport-common = {path = "../report-common", develop = true}\n', encoding="utf-8")
    monkeypatch.setattr(repos, "active_backend", lambda cfg: backend)
    monkeypatch.setattr(testviews, "modified", lambda repo, db, timeout=20: {"pdms_report_document_view": True})
    monkeypatch.setattr(testviews, "existing", lambda db, names, timeout=20: {})
    monkeypatch.setattr(testviews, "can_create", lambda db, timeout=20: False)
    return [(backend / "report/report-document-run", "web-dev")]


def test_starting_a_reader_of_a_changed_view_asks(cfg, starting) -> None:
    with pytest.raises(actions.TestViewsPending) as asked:
        actions.check_test_views(cfg, starting, None)
    item = asked.value.items[0]
    assert (item.view, item.name, item.services, item.exists, item.uncommitted) == (
        "pdms_report_document_view", "pdms_report_document_view_pdmp627", ["report-document-run"], False, True)
    assert "pdms_report_document_view_pdmp627_new" in item.describe()["sql"]  # it cannot create: the SQL to run
    assert actions.check_test_views(cfg, [(starting[0][0], "pdms-local")], None) == []  # a local one: migrated instead


def test_the_real_views_or_yours_after_running_the_sql(cfg, starting, monkeypatch) -> None:
    assert actions.check_test_views(cfg, starting, False) == []
    with pytest.raises(actions.ActionError, match="not in the database yet"):
        actions.check_test_views(cfg, starting, True)  # "I ran it", but it is not there
    monkeypatch.setattr(testviews, "existing", lambda db, names, timeout=20: {n: "root" for n in names})
    needs = actions.check_test_views(cfg, starting, True)
    actions.prepare_test_views(cfg, needs)
    assert testviews.in_use(cfg) == {"pdms_report_document_view": "pdms_report_document_view_pdmp627"}
    assert testviews.made_with("pdms_report_document_view_pdmp627", needs[0].sql)


def test_a_user_who_can_create_gets_the_copy_made(cfg, starting, monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(testviews, "can_create", lambda db, timeout=20: True)
    monkeypatch.setattr(testviews, "run", lambda db, sql, timeout=20: ran.append(sql))
    actions.prepare_test_views(cfg, actions.check_test_views(cfg, starting, True))
    assert len(ran) == 1 and "pdms_report_document_view_pdmp627" in ran[0]


def test_the_page_gets_a_decision(cfg, starting) -> None:
    from pdms_cli.ui import api as ui_api

    with pytest.raises(actions.TestViewsPending) as asked:
        actions.check_test_views(cfg, starting, None)
    body = ui_api.decision_body(asked.value)
    assert body["decision"] == "test_views_pending" and body["views"][0]["name"] == "pdms_report_document_view_pdmp627"
    assert ui_api.launch_options({"views": True})["views"] is True


def test_a_copy_is_always_made_with_its_data(repo) -> None:
    path = repo / testviews.FOLDER / "R__MV_pdms_report_document_view.sql"
    path.write_text(VIEW_SQL.replace("SELECT d.id FROM pdms_document d", "SELECT d.id FROM pdms_document d\nWITH NO DATA"),
                    encoding="utf-8")
    _made, sql = testviews.test_sql(repo, "pdms_report_document_view", "_pdmp627")
    assert "WITH NO DATA" not in sql and "WITH DATA" in sql


def test_refreshing_the_data_is_for_its_owner_or_you_get_the_sql(cfg, monkeypatch) -> None:
    ran = []
    owner = {"pdms_report_document_view_pdmp627": "root"}
    monkeypatch.setattr(testviews, "existing", lambda db, names, timeout=20: {n: owner[n] for n in names if n in owner})
    monkeypatch.setattr(testviews, "run", lambda db, sql, timeout=20: ran.append(sql))
    result = actions.refresh_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
    assert result == {"refreshed": False, "name": "pdms_report_document_view_pdmp627",
                      "sql": "REFRESH MATERIALIZED VIEW public.pdms_report_document_view_pdmp627;"} and ran == []
    cfg.dbs["web-dev"].user = "root"  # the copy is the user's own
    with pytest.raises(actions.ProtectedDatabase):
        actions.refresh_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
    assert actions.refresh_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627",
                                     confirmed=True)["refreshed"]
    assert ran == ["REFRESH MATERIALIZED VIEW public.pdms_report_document_view_pdmp627;"]
    owner.clear()
    with pytest.raises(actions.ActionError, match="is not in web-dev"):
        actions.refresh_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")


def test_the_copy_is_granted_to_the_services_user(repo) -> None:
    _made, sql = testviews.test_sql(repo, "pdms_report_document_view", "_pdmp627", "pdm-writer")
    assert sql.rstrip().splitlines()[-1].startswith(
        'GRANT SELECT ON public.pdms_report_document_view_pdmp627 TO "pdm-writer";')
    assert "GRANT" not in testviews.test_sql(repo, "pdms_report_document_view", "_pdmp627")[1]


def test_a_copy_the_services_user_cannot_read_says_how_to_grant_it(cfg, starting, monkeypatch) -> None:
    cfg.dbs["web-dev"].user = "pdm-writer"
    monkeypatch.setattr(testviews, "existing", lambda db, names, timeout=20: {n: "admin" for n in names})
    monkeypatch.setattr(testviews, "readable", lambda db, name, timeout=20: False)
    with pytest.raises(actions.ActionError, match='GRANT SELECT ON public.pdms_report_document_view_pdmp627 TO "pdm-writer"'):
        actions.check_test_views(cfg, starting, True)
    with pytest.raises(actions.ActionError, match="pdm-writer cannot read"):
        actions.use_test_view(cfg, "pdms_report_document_view", "pdms_report_document_view_pdmp627")
