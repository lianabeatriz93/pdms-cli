"""Registered PDMS checkouts."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdms_cli import repos
from pdms_cli.config import Config


def make_repo(root: Path, services=("lead/lead-tp-list",)) -> Path:
    (root / "backend" / "snakesdk").mkdir(parents=True)
    for rel in services:
        svc = root / "backend" / rel
        svc.mkdir(parents=True)
        (svc / "pyproject.toml").write_text("")
        (svc / "main.py").write_text("")
    return root


@pytest.fixture(autouse=True)
def reset_session():
    repos.use_for_this_command(None)
    yield
    repos.use_for_this_command(None)


def test_legacy_backend_path_becomes_the_current_repo(tmp_path):
    cfg = Config.from_dict({"defaults": {"backend_path": str(tmp_path / "pdms" / "backend"), "port": 9000}})
    assert cfg.current_repo == "pdms"
    assert cfg.repo.root == tmp_path / "pdms"
    assert cfg.repo.backend_dir == tmp_path / "pdms" / "backend"
    assert cfg.defaults.port == 9000
    again = Config.from_dict(cfg.to_dict())  # the migration survives a save/load round trip
    assert again.repos == cfg.repos and again.current_repo == "pdms"


def test_find_repo_root_from_a_nested_folder(tmp_path):
    root = make_repo(tmp_path / "pdms_v2")
    assert repos.find_repo_root(root / "backend" / "lead" / "lead-tp-list") == root.resolve()
    assert repos.find_repo_root(tmp_path) is None


def test_register_is_idempotent_and_aliases_are_unique(tmp_path):
    cfg = Config()
    first = repos.register(cfg, make_repo(tmp_path / "a" / "pdms"))
    second = repos.register(cfg, make_repo(tmp_path / "b" / "pdms"))
    assert (first, second) == ("pdms", "pdms-2")
    assert repos.register(cfg, tmp_path / "a" / "pdms") == "pdms"
    assert cfg.current_repo == "pdms"  # the first registered repo becomes current


def test_repo_of_picks_the_innermost_registered_repo(tmp_path):
    cfg = Config()
    outer = repos.register(cfg, make_repo(tmp_path / "pdms"))
    inner = repos.register(cfg, make_repo(tmp_path / "pdms" / "nested"))
    assert repos.repo_of(cfg, tmp_path / "pdms" / "backend" / "lead" / "lead-tp-list") == outer
    assert repos.repo_of(cfg, tmp_path / "pdms" / "nested" / "backend" / "lead" / "lead-tp-list") == inner
    assert repos.repo_of(cfg, tmp_path / "elsewhere") is None


def test_session_override_changes_the_active_backend_only(tmp_path):
    cfg = Config()
    repos.register(cfg, make_repo(tmp_path / "pdms"))
    other = make_repo(tmp_path / "pdms_v2")
    assert repos.active_backend(cfg) == tmp_path / "pdms" / "backend"
    repos.use_for_this_command(other)
    assert repos.active_backend(cfg) == other.resolve() / "backend"
    assert cfg.current_repo == "pdms"


def test_translate_finds_the_same_service_in_another_checkout(tmp_path):
    old = make_repo(tmp_path / "pdms", ("lead/lead-tp-list", "lead/only-old"))
    new = make_repo(tmp_path / "pdms_v2", ("lead/lead-tp-list",))
    assert repos.translate(old / "backend/lead/lead-tp-list", old, new) == new / "backend/lead/lead-tp-list"
    assert repos.translate(old / "backend/lead/only-old", old, new) is None


def test_stack_paths_written_on_windows_are_normalized():
    cfg = Config.from_dict({"stacks": {"tp": {"services": ["lead\\lead-tp-list", "lead/lead-tp-create"]}}})
    assert cfg.stacks["tp"].services == ["lead/lead-tp-list", "lead/lead-tp-create"]


def test_an_old_config_moves_off_the_old_service_port_once(tmp_path, monkeypatch) -> None:
    from pdms_cli.config import Config, Defaults

    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[defaults]\nport = 8080\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.defaults.port == Defaults.port == 28100 and cfg.defaults.proxy_port == 28800
    cfg.save()
    (tmp_path / "config.toml").write_text(
        (tmp_path / "config.toml").read_text(encoding="utf-8").replace("port = 28100", "port = 8080"), encoding="utf-8")
    assert Config.load().defaults.port == 8080  # saved after the change (it has proxy_port): the user's choice
    (tmp_path / "config.toml").write_text("[defaults]\nport = 9000\n", encoding="utf-8")
    assert Config.load().defaults.port == 9000  # an old config with its own port keeps it
