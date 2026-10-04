"""A stack made of the services running now, and giving a stack another name (CLI, actions and pdms ui)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, cli
from pdms_cli.config import Config, Database, Defaults, DevUser, Repo, Setup, Stack
from pdms_cli.instances import Instance
from pdms_cli.ui import jobs as ui_jobs


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Config:
    backend = tmp_path / "pdms" / "backend"
    for svc in ("lead/a", "lead/b", "lead/c", "auth/login"):
        (backend / svc).mkdir(parents=True)
    cfg = Config(
        users={"supervisor": DevUser("u1", "s@x.com"), "agent": DevUser("u2", "a@x.com")},
        dbs={"local": Database("localhost"), "copy": Database("localhost")},
        defaults=Defaults(), repos={"pdms": Repo(path=str(tmp_path / "pdms"))}, current_repo="pdms",
    )
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(ui_jobs, "backend", lambda cfg: backend.resolve())
    monkeypatch.setattr(ui_jobs, "repo_services",
                        lambda cfg: (backend, ["auth/login", "lead/a", "lead/b", "lead/c"]))
    return cfg


def backend_of(cfg: Config) -> Path:
    return Path(cfg.repos["pdms"].path) / "backend"


def running(monkeypatch, cfg: Config, *items: tuple[str, str, str], dead: tuple[str, ...] = ()) -> None:
    """``(service, user, db)`` running now, in the order they started; ``dead`` ones stopped."""
    root = backend_of(cfg)
    live = {}
    for at, (svc, user, db) in enumerate(items):
        path = Path(svc) if svc.startswith("/") else root / svc
        key = f"{path.name}@{8000 + at}"
        live[key] = Instance(key=key, pid=at + 1, service=str(path), host="0.0.0.0", port=8000 + at, user=user, db=db,
                             reload=True, log="x.log", started_at=f"2026-10-04T10:0{at}:00")
    monkeypatch.setattr(actions.instances, "load", lambda: live)
    monkeypatch.setattr(Instance, "alive", lambda self: self.service.rsplit("/", 1)[-1] not in dead)


def test_the_running_services_become_a_stack_with_the_user_and_db_most_use(cfg, monkeypatch) -> None:
    running(monkeypatch, cfg, ("lead/b", "supervisor", "local"), ("lead/a", "supervisor", "copy"),
            ("auth/login", "agent", "local"), ("lead/b", "agent", "copy"))  # lead/b twice: the first one counts
    stack = actions.stack_from_running(cfg, backend_of(cfg))
    assert stack.services == ["auth/login", "lead/a", "lead/b"]
    assert (stack.user, stack.db) == ("supervisor", "local")
    assert stack.overrides == {"lead/a": {"db": "copy"}, "auth/login": {"user": "agent"}}


def test_only_live_services_of_the_current_repo_and_known_profiles(cfg, monkeypatch, tmp_path) -> None:
    running(monkeypatch, cfg, ("lead/a", "gone-user", "local"), ("lead/c", "supervisor", "local"),
            (str(tmp_path / "other" / "backend" / "x"), "supervisor", "local"), dead=("c",))
    stack = actions.stack_from_running(cfg, backend_of(cfg))
    assert stack.services == ["lead/a"] and stack.user == "" and stack.db == "local" and stack.overrides == {}


def test_nothing_running_is_an_error(cfg, monkeypatch) -> None:
    running(monkeypatch, cfg)
    with pytest.raises(actions.ActionError, match="running"):
        actions.stack_from_running(cfg, backend_of(cfg))


def test_a_renamed_stack_keeps_its_place_and_the_setups_follow(cfg) -> None:
    cfg.stacks = {"one": Stack(["lead/a"]), "tp": Stack(["lead/b"], user="agent"), "three": Stack(["lead/c"])}
    cfg.setup = Setup(stack="tp")
    cfg.setups = {"morning": Setup(stack="tp"), "other": Setup(stack="one")}
    assert actions.rename_stack(cfg, "tp", " tp-prospecting ") == "tp-prospecting"
    assert list(cfg.stacks) == ["one", "tp-prospecting", "three"]
    assert cfg.stacks["tp-prospecting"].user == "agent"
    assert cfg.setup.stack == "tp-prospecting" and cfg.setups["morning"].stack == "tp-prospecting"
    assert cfg.setups["other"].stack == "one"


@pytest.mark.parametrize("new_name", ["one", "", "bad name"])
def test_a_stack_cannot_take_a_used_or_invalid_name(cfg, new_name) -> None:
    cfg.stacks = {"one": Stack(["lead/a"]), "tp": Stack(["lead/b"])}
    with pytest.raises(actions.InvalidValue):
        actions.rename_stack(cfg, "tp", new_name)
    assert list(cfg.stacks) == ["one", "tp"]


def test_the_editor_saves_and_renames_and_checks_the_name_before_saving(cfg) -> None:
    cfg.stacks = {"one": Stack(["lead/a"]), "tp": Stack(["lead/b"])}
    with pytest.raises(actions.InvalidValue):
        ui_jobs.save_stack("tp", ["lead/c"], "", "", new=False, rename="one")
    assert cfg.stacks["tp"].services == ["lead/b"]  # nothing saved
    assert ui_jobs.save_stack("tp", ["lead/c"], "agent", "", new=False, rename="tp2") == "tp2"
    assert list(cfg.stacks) == ["one", "tp2"] and cfg.stacks["tp2"].services == ["lead/c"]


def test_pdms_ui_hands_the_editor_the_running_services(cfg, monkeypatch) -> None:
    running(monkeypatch, cfg, ("lead/a", "supervisor", "local"))
    assert ui_jobs.stack_from_running() == {"services": ["lead/a"], "user": "supervisor", "db": "local",
                                            "overrides": {}}


def test_cli_saves_the_running_services_as_a_stack_and_renames_it(cfg, monkeypatch) -> None:
    running(monkeypatch, cfg, ("lead/a", "supervisor", "local"), ("lead/b", "agent", "local"))
    monkeypatch.setattr("pdms_cli.commands.services.services_root", lambda cfg: backend_of(cfg))
    result = CliRunner().invoke(cli.app, ["stack", "add", "fix-1234", "--running"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "lead/a  (supervisor)" in result.output  # a tie: agent, the first in alphabetical order
    assert cfg.stacks["fix-1234"].services == ["lead/a", "lead/b"]
    result = CliRunner().invoke(cli.app, ["stack", "rename", "fix-1234", "tp-fix"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert list(cfg.stacks) == ["tp-fix"]
