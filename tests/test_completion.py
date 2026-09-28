"""Shell completion callbacks."""

from __future__ import annotations

from pdms_cli import completion
from pdms_cli.config import Config, Database, DevUser, Stack


def test_prefix_matches_win_over_substring_matches():
    assert completion._matching(["lead-tp-list", "lead-sp-list", "tp-tools"], "tp") == ["tp-tools"]
    assert completion._matching(["lead-tp-list", "lead-sp-list"], "tp") == ["lead-tp-list"]


def test_completes_config_entries_and_services(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    for rel in ("lead/lead-tp-list", "lead/lead-tp-details"):
        (backend / rel).mkdir(parents=True)
        (backend / rel / "pyproject.toml").write_text("")
        (backend / rel / "main.py").write_text("")
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    cfg = Config(
        users={"supervisor": DevUser("u", "s@x.com"), "agent": DevUser("a", "a@x.com")},
        dbs={"local": Database("localhost")},
        stacks={"tp": Stack(services=["lead/lead-tp-list"])},
    )
    cfg.defaults.backend_path = str(backend)
    cfg.save()

    assert completion.users("a") == ["agent"]
    assert completion.dbs("") == ["local"]
    assert completion.stacks("t") == ["tp"]
    assert completion.services("lead-tp-d") == ["lead-tp-details"]
    assert "lead/lead-tp-list" in completion.services("lead/")


def test_never_fails_without_backend_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "missing.toml"))
    assert completion.services("x") == []
