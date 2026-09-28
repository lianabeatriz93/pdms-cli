"""Export/import of the configuration."""

from __future__ import annotations

import pytest
import tomlkit

from pdms_cli import transfer
from pdms_cli.config import Config, Database, Defaults, DevUser, Stack

ALL = list(transfer.SECTIONS)


def make_config() -> Config:
    return Config(
        defaults=Defaults(port=9000, backend_path="~/pdms/backend"),
        users={"supervisor": DevUser(user_id="u-1", username="sup@x.com", roles="TPR.Supervisor")},
        dbs={
            "local": Database(host="localhost", user="postgres", password="local-secret"),
            "web": Database(host="db.example.com", user="writer", password="web-secret", protected=True),
        },
        stacks={"tp": Stack(services=["lead/lead-tp-list"], user="supervisor")},
        last_user="supervisor",
        last_db="web",
    )


def roundtrip(cfg: Config, sections=ALL, secrets=False) -> transfer.Document:
    return transfer.read_document(transfer.export_document(cfg, sections, secrets))


def test_export_leaves_passwords_out_by_default():
    doc = roundtrip(make_config())
    assert all(db.password == "" for db in doc.config.dbs.values())
    assert doc.config.dbs["web"].host == "db.example.com" and doc.config.dbs["web"].protected
    assert doc.meta["secrets"] is False


def test_export_with_secrets_keeps_passwords():
    doc = roundtrip(make_config(), secrets=True)
    assert doc.config.dbs["web"].password == "web-secret"


def test_export_only_selected_sections_and_never_state():
    text = transfer.export_document(make_config(), ["users", "stacks"], secrets=False)
    data = tomlkit.parse(text).unwrap()
    assert set(data) == {"pdms", "users", "stacks"}
    assert transfer.read_document(text).sections == ["users", "stacks"]


def test_full_roundtrip_with_secrets_reproduces_the_config():
    cfg = make_config()
    doc = roundtrip(cfg, secrets=True)
    result = transfer.apply_import(Config(), doc.config, doc.sections, set(), replace=True)
    for section in ALL:
        assert getattr(result, section) == getattr(cfg, section)


def test_import_without_passwords_keeps_local_passwords():
    mine = make_config()
    doc = roundtrip(mine)  # no secrets
    plans = {p.section: p for p in transfer.plan_import(mine, doc.config, ALL)}
    assert plans["dbs"].same == ["local", "web"] and not plans["dbs"].changed
    result = transfer.apply_import(mine, doc.config, ALL, {("dbs", "web")}, replace=True)
    assert result.dbs["web"].password == "web-secret"


def test_merge_adds_new_entries_and_keeps_conflicts_unless_chosen():
    mine = make_config()
    theirs = make_config()
    theirs.users["agent"] = DevUser(user_id="u-2", username="agent@x.com", roles="TPR.Agent")
    theirs.users["supervisor"].roles = "TPR.Admin"
    theirs.dbs["web"].host = "other.example.com"
    doc = roundtrip(theirs)

    plans = {p.section: p for p in transfer.plan_import(mine, doc.config, ALL)}
    assert plans["users"].added == ["agent"]
    assert plans["users"].changed == ["supervisor"]
    assert plans["dbs"].changed == ["web"]

    kept = transfer.apply_import(mine, doc.config, ALL, set())
    assert "agent" in kept.users
    assert kept.users["supervisor"].roles == "TPR.Supervisor"
    assert kept.dbs["web"].host == "db.example.com"

    chosen = transfer.apply_import(mine, doc.config, ALL, {("users", "supervisor"), ("dbs", "web")})
    assert chosen.users["supervisor"].roles == "TPR.Admin"
    assert chosen.dbs["web"].host == "other.example.com"
    assert chosen.dbs["web"].password == "web-secret"  # file had no password: local one is kept


def test_defaults_only_change_when_chosen_or_replacing():
    mine = make_config()
    theirs = make_config()
    theirs.defaults.language = "es"
    doc = roundtrip(theirs)
    assert transfer.plan_import(mine, doc.config, ["defaults"])[0].changed == ["defaults"]
    assert transfer.apply_import(mine, doc.config, ["defaults"], set()).defaults.language == "en"
    chosen = transfer.apply_import(mine, doc.config, ["defaults"], {("defaults", "defaults")})
    assert chosen.defaults.language == "es"


def test_replace_removes_local_entries_missing_from_the_file():
    mine = make_config()
    theirs = make_config()
    del theirs.dbs["web"]
    doc = roundtrip(theirs)
    plan = {p.section: p for p in transfer.plan_import(mine, doc.config, ["dbs"])}["dbs"]
    assert plan.missing == ["web"]
    result = transfer.apply_import(mine, doc.config, ["dbs"], set(), replace=True)
    assert list(result.dbs) == ["local"]
    assert result.last_db == ""  # it pointed to the removed database
    assert result.users == mine.users  # sections not selected are untouched


@pytest.mark.parametrize(
    "text",
    [
        "this is = = not toml",
        "[users.a]\nuser_id = 'x'\nusername = 'y'\n",  # no [pdms] header
        "[pdms]\nformat = 99\n",  # newer format
        "[pdms]\nformat = 1\n[users]\na = 'not a table'\n",  # invalid structure
    ],
)
def test_invalid_documents_are_rejected(text):
    with pytest.raises(transfer.TransferError):
        transfer.read_document(text)
