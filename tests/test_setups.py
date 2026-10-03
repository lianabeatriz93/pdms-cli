"""Named setups of pdms ui's Home: saving the current one under a name, switching (what stops), editing the current
one keeps its saved copy in step, and pdms up --setup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, cli
from pdms_cli.config import Config, Setup, Stack
from pdms_cli.ui import jobs as ui_jobs


@pytest.fixture
def cfg(monkeypatch) -> Config:
    cfg = Config(stacks={"leads": Stack(services=["lead/a"]), "creds": Stack(services=["cred/b"])},
                 setup=Setup(stack="leads", proxy=True, frontend=True))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(Config, "save", lambda self: None)
    return cfg


def test_save_use_and_remove(cfg) -> None:
    assert actions.save_setup_as(cfg, "Prospecting") == "Prospecting"
    assert cfg.setups["Prospecting"] == cfg.setup and cfg.setups["Prospecting"] is not cfg.setup
    cfg.setups["Credentials"] = Setup(stack="creds", proxy=True, frontend=False, events=True)
    actions.use_setup(cfg, "Credentials")
    assert (cfg.setup.stack, cfg.setup.frontend, cfg.setup_name) == ("creds", False, "Credentials")
    with pytest.raises(actions.InvalidValue):
        actions.save_setup_as(cfg, "no spaces please")
    actions.save_setup_as(cfg, "Credentials")  # the same name replaces it
    actions.remove_setup(cfg, "Credentials")
    assert "Credentials" not in cfg.setups and cfg.setup_name == "" and cfg.setup.stack == "creds"


def test_editing_the_current_setup_updates_its_saved_copy(cfg, monkeypatch) -> None:
    actions.save_setup_as(cfg, "Prospecting")
    ui_jobs.save_setup({"stack": "leads", "proxy": True, "frontend": False, "frontend_mode": "dev", "events": True})
    assert cfg.setups["Prospecting"].events is True and cfg.setups["Prospecting"].frontend is False


def test_deleting_a_stack_leaves_the_saved_setups_without_it(cfg) -> None:
    cfg.setups["Prospecting"] = Setup(stack="leads")
    actions.remove_stack(cfg, "leads")
    assert cfg.setups["Prospecting"].stack == "" and cfg.setup.stack == ""


def test_setups_survive_the_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    Config(setups={"Prospecting": Setup(stack="leads", events=True)}, setup_name="Prospecting").save()
    loaded = Config.load()
    assert loaded.setups["Prospecting"].events and loaded.setup_name == "Prospecting"


def test_switching_stops_what_the_new_setup_does_not_use(cfg, monkeypatch) -> None:
    cfg.setups["Credentials"] = Setup(stack="creds", proxy=True, frontend=False)
    jobs = ui_jobs.Jobs()
    downs, stops = [], []
    monkeypatch.setattr(jobs, "down", lambda name: downs.append(name) or SimpleNamespace(key=f"stack:{name}"))
    monkeypatch.setattr(jobs, "stop", lambda key: stops.append(key) or SimpleNamespace(key=key))
    monkeypatch.setattr(ui_jobs.proxy, "running_proxy", lambda: {"port": 28800, "pid": 1})
    monkeypatch.setattr(ui_jobs.proxy, "display_key", lambda running: "proxy@28800")
    monkeypatch.setattr(ui_jobs.frontend, "running", lambda: {"port": 3000})
    assert jobs.switch_setup("Credentials") == ["stack:leads", "frontend"]
    assert downs == ["leads"] and stops == ["frontend"]  # the proxy stays: the new setup wants it too
    assert cfg.setup.stack == "creds" and cfg.setup_name == "Credentials"


def test_pdms_up_with_a_setup_starts_its_stack(cfg, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    cfg.setups["Credentials"] = Setup(stack="creds")
    seen = []
    monkeypatch.setattr("pdms_cli.commands.stacks.pick", lambda items, kind, name, *a: seen.append(name) or name)
    monkeypatch.setattr("pdms_cli.commands.stacks.backend_root", lambda cfg: (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr("pdms_cli.cli.check_repo", lambda cfg: None)
    CliRunner().invoke(cli.app, ["up", "--setup", "Credentials"])
    assert seen[-1] == "creds" and cfg.setup_name == "Credentials"
    result = CliRunner().invoke(cli.app, ["up", "leads", "--setup", "Credentials"])
    assert result.exit_code == 1 and "not both" in result.output
