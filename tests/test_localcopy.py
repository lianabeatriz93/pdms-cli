"""The local copy: what is copied from the source and how, the schemas built from the repo's migrations, its alias,
snapshots, and Flyway's lock with CREATE INDEX CONCURRENTLY."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from pdms_cli import actions, localcopy, localdb, migrations
from pdms_cli.config import Config, Database

WEB = Database(host="pdm-cluster.cluster-x.us-east-1.rds.amazonaws.com", database="pdm", user="dev", password="s3cret",
               protected=True)


def test_the_source_is_the_template_on_the_server_of_web_dev() -> None:
    cfg = Config(dbs={"web-dev": WEB})
    src = localcopy.source(cfg)
    assert (src.host, src.database, src.user) == (WEB.host, "pdm_template_dev", "dev")
    with pytest.raises(actions.ActionError):
        localcopy.source(cfg, database="pdm; drop table x")
    with pytest.raises(actions.ActionError):
        localcopy.source(cfg, alias="nope")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the host network is how Linux reaches a tunnel")
def test_pg_dump_copies_public_with_its_extensions_through_the_hosts_tunnel(monkeypatch) -> None:
    monkeypatch.setattr(localcopy.socket, "gethostbyname", lambda host: "127.0.0.6")  # devo-cli's /etc/hosts
    cmd = localcopy.dump_command(localcopy.source(Config(dbs={"web-dev": WEB})))
    assert cmd[:5] == ["docker", "run", "--rm", "--network", "host"]
    assert f"{WEB.host}:127.0.0.6" in cmd and localdb.IMAGE in cmd
    assert "--schema=public" in cmd and "--extension=*" in cmd and "--format=custom" in cmd
    assert "PGPASSWORD" in cmd and "s3cret" not in " ".join(cmd)  # the password goes in the environment


def test_configuration_is_copied_when_the_source_user_may_read_all_of_it(monkeypatch) -> None:
    monkeypatch.setattr(localcopy, "unreadable", lambda db, schema: [])
    log = io.StringIO()
    copied, rebuilt = localcopy.schemas_to_copy(WEB, log)
    assert (copied, rebuilt, log.getvalue()) == (["public", "configuration"], [], "")
    cmd = localcopy.dump_command(WEB, copied)
    assert "--schema=public" in cmd and "--schema=configuration" in cmd and "--schema=backup_data" not in cmd


def test_configuration_is_built_from_migrations_when_something_in_it_is_not_readable(monkeypatch) -> None:
    monkeypatch.setattr(localcopy, "unreadable", lambda db, schema: ["configuration.nom_new"])
    log = io.StringIO()
    assert localcopy.schemas_to_copy(WEB, log) == (["public"], ["configuration"])
    assert "configuration.nom_new" in log.getvalue() and "dev" in log.getvalue()

    def unreachable(db, schema):
        raise OSError("connection timed out")

    monkeypatch.setattr(localcopy, "unreadable", unreachable)
    log = io.StringIO()
    assert localcopy.schemas_to_copy(WEB, log) == (["public"], ["configuration"])
    assert "connection timed out" in log.getvalue()


def test_the_schemas_the_source_does_not_give_are_built_from_their_migrations(tmp_path) -> None:
    folder = tmp_path / "migrations" / "versioned" / "next_release"
    folder.mkdir(parents=True)
    for name in ("V2026.08.31.510.02__create_configuration_nom_status_table.sql",
                 "V2026.08.31.510.10__create_configuration_schema_extra.sql",
                 "V2026.08.31.510.01__create_configuration_schema.sql",
                 "V2026.09.21.443.01__add_is_all_state.sql"):
        (folder / name).write_text("select 1;")
    found = [path.name for path in localcopy.schema_migrations(tmp_path, "configuration")]
    assert found == ["V2026.08.31.510.01__create_configuration_schema.sql",
                     "V2026.08.31.510.02__create_configuration_nom_status_table.sql",
                     "V2026.08.31.510.10__create_configuration_schema_extra.sql"]  # by version, 10 after 02


def test_the_copy_gets_its_alias_once(monkeypatch) -> None:
    monkeypatch.setattr(localdb, "state", lambda: {"exists": True, "running": True, "port": localdb.PORT})
    cfg = Config(dbs={"web-dev": WEB})
    monkeypatch.setattr(cfg, "save", lambda: None)
    assert localcopy.register_alias(cfg) == "pdms-local"
    assert (cfg.dbs["pdms-local"].port, cfg.dbs["pdms-local"].database) == (localdb.PORT, "pdms")
    assert localcopy.register_alias(cfg) == "pdms-local" and len(cfg.dbs) == 2


def test_snapshot_names_are_checked(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for name in ("Fresh", "a b", "x;drop", "", "a" * 41):
        with pytest.raises(actions.ActionError, match="snapshot name"):
            localcopy.save_snapshot(name)


def test_migrate_holds_a_session_lock_so_concurrent_indexes_do_not_wait_forever(tmp_path) -> None:
    (tmp_path / "flyway.toml").write_text("[flyway]\n")
    db = localdb.database("pdms")
    assert "-postgresql.transactional.lock=false" in migrations.docker_command(tmp_path, db, "migrate")
    assert "-postgresql.transactional.lock=false" not in migrations.docker_command(tmp_path, db, "info")


def test_refresh_needs_the_source_alias(tmp_path) -> None:
    with pytest.raises(actions.ActionError):
        localcopy.refresh(Config(), open(tmp_path / "log", "w"), alias="web-dev")
    assert not Path(tmp_path / "local-copy.json").exists()


def test_the_data_screen_shows_the_copy_and_only_touches_test_databases(monkeypatch, tmp_path) -> None:
    from pdms_cli.ui import jobs as ui_jobs

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(localdb, "state", lambda: {"exists": True, "running": True, "port": localdb.PORT})
    monkeypatch.setattr(localdb, "databases", lambda: {"pdms": 14 << 20, "pdms_test_1": 7 << 20, "pdms_snap_fresh": 13 << 20,
                                                       "postgres": 7 << 20})
    cfg = Config(dbs={"web-dev": WEB, "pdms-local": localdb.database("pdms")})
    info = ui_jobs.data_info(cfg)
    assert info["copy"]["alias"] == "pdms-local" and info["copy"]["size"] == 14 << 20
    assert [s["name"] for s in info["snapshots"]] == ["fresh"]
    assert info["tests"] == [{"name": "pdms_test_1", "size": 7 << 20}]
    assert [d["name"] for d in info["dbs"] if d["copy"]] == ["pdms-local"]
    dropped = []
    monkeypatch.setattr(localdb, "psql", lambda sql, db="postgres", timeout=120: dropped.append(sql) or "")
    for name in ("pdms", "postgres", "pdms_snap_fresh"):
        with pytest.raises(actions.ActionError):
            ui_jobs.data_action("tests-recreate", {"name": name})
    assert not dropped
    with pytest.raises(actions.ActionError):
        ui_jobs.data_action("tests-count", {"count": 99})
