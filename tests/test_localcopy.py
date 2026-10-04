"""The local copy: what is copied from the source and how, the schemas built from the repo's migrations, its alias,
snapshots, and Flyway's lock with CREATE INDEX CONCURRENTLY."""

from __future__ import annotations

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
