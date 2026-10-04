"""Restarting several services at once: pdms ui's set restart (one after the other, checked before anything stops),
a stack's services with another user or database, and the exceptions a stack remembers for them."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
import tomlkit

from pdms_cli import actions, instances
from pdms_cli.commands import instances as instance_commands
from pdms_cli.config import Config, Stack
from pdms_cli.instances import Instance

from test_actions import _live, backend, cfg, ports  # noqa: F401 - fixtures
from test_ui_server import machine, post, ui, wait_until  # noqa: F401 - fixtures


# --------------------------------------------------------------------------- what a stack remembers


def test_a_stack_starts_each_service_with_its_exception(cfg, ports, backend, monkeypatch) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b"], user="supervisor", db="local",
                                      overrides={"lead/b": {"user": "agent"}})
    _live(monkeypatch)
    monkeypatch.setattr(actions.events, "running", lambda port: False)
    plan = actions.plan_stack(cfg, "prospecting", backend, user_name="supervisor", db_name="local")
    assert [(s.service.name, s.user_name, s.db_name) for s in plan.services] == [
        ("a", "supervisor", "local"), ("b", "agent", "local")]


def test_remember_keeps_only_what_differs(cfg, backend) -> None:
    cfg.stacks["prospecting"] = Stack(services=["lead/a", "lead/b"], user="supervisor", db="local")
    actions.remember_in_stack(cfg, "prospecting", backend, [backend / "lead/b"], "agent", "local")
    assert cfg.stacks["prospecting"].overrides == {"lead/b": {"user": "agent"}}
    actions.remember_in_stack(cfg, "prospecting", backend, [backend / "lead/b"], "agent", "shared")
    assert cfg.stacks["prospecting"].overrides == {"lead/b": {"user": "agent", "db": "shared"}}
    actions.remember_in_stack(cfg, "prospecting", backend, [backend / "lead/b"], "supervisor", "local")
    assert cfg.stacks["prospecting"].overrides == {}  # like the rest again: no exception left
    with pytest.raises(actions.ActionError, match="not in the stack"):
        actions.remember_in_stack(cfg, "prospecting", backend, [backend / "lead/zzz"], "agent", "local")
    with pytest.raises(actions.ActionError, match="not a service of the current repo"):
        actions.remember_in_stack(cfg, "prospecting", backend, [Path("/elsewhere/lead/a")], "agent", "local")


def test_exceptions_follow_users_and_databases(cfg) -> None:
    cfg.stacks["s"] = Stack(services=["lead/a", "lead/b"], overrides={"lead/a": {"user": "agent", "db": "shared"},
                                                                     "lead/b": {"db": "shared"}})
    actions.rename_user(cfg, "agent", "agent2")
    assert cfg.stacks["s"].overrides["lead/a"]["user"] == "agent2"
    actions.remove_db(cfg, "shared")
    assert cfg.stacks["s"].overrides == {"lead/a": {"user": "agent2"}}
    actions.remove_user(cfg, "agent2")
    assert cfg.stacks["s"].overrides == {}


def test_saving_a_stack_drops_the_exceptions_of_removed_services(cfg) -> None:
    stack = Stack(services=["lead/a"], overrides={"lead/a": {"user": "agent"}, "lead/gone": {"user": "agent"}})
    actions.save_stack(cfg, "s", stack)
    assert cfg.stacks["s"].overrides == {"lead/a": {"user": "agent"}}
    with pytest.raises(actions.ActionError):
        actions.save_stack(cfg, "s", Stack(services=["lead/a"], overrides={"lead/a": {"user": "ghost"}}))


def test_exceptions_survive_the_config_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PDMS_CONFIG", str(tmp_path / "config.toml"))
    Config(stacks={"s": Stack(services=["lead/a"], overrides={"lead/a": {"user": "agent"}})}).save()
    assert Config.load().stacks["s"].overrides == {"lead/a": {"user": "agent"}}
    text = (tmp_path / "config.toml").read_text()
    doc = tomlkit.parse(text)
    doc["stacks"]["s"]["overrides"] = {"lead\\a": {"user": "x", "nonsense": "y"}}  # written on Windows, by hand
    (tmp_path / "config.toml").write_text(tomlkit.dumps(doc))
    assert Config.load().stacks["s"].overrides == {"lead/a": {"user": "x"}}


# --------------------------------------------------------------------------- pdms ui: a set, one after the other


@pytest.fixture
def three(machine, tmp_path):
    """Three live instances (this test process) besides the fixture's own."""
    me = os.getpid()
    registry = instances.load()
    for key in ("a@8091", "b@8092"):
        registry[key] = Instance(key=key, pid=me, created=instances.creation_time(me), service=str(tmp_path / key[0]),
                                 host="0.0.0.0", port=int(key[2:]), user="agent", db="local", reload=True,
                                 log=str(tmp_path / f"{key}.log"), started_at="2026-10-01T10:00:00")
    instances.save(registry)
    return ["svc@8081", "a@8091", "b@8092"]


def test_a_set_restarts_one_after_the_other(ui, three, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    spans, lock = [], threading.Lock()

    def restart_service(cfg, inst, *, user_name, db_name, confirmed, install):
        started = time.monotonic()
        time.sleep(0.1)
        with lock:
            spans.append((inst.key, user_name, db_name, started, time.monotonic()))
        return inst

    monkeypatch.setattr(actions, "restart_service", restart_service)
    status, data = post(port, "/api/instances/restart", {"keys": three})
    assert status == 202 and sorted(data["jobs"]) == sorted(three)
    wait_until(lambda: len(spans) == 3 and not jobs.snapshot())
    spans.sort(key=lambda span: span[3])
    assert all(earlier[4] <= later[3] for earlier, later in zip(spans, spans[1:]))  # never two at once
    assert {(key, user, db) for key, user, db, *_ in spans} == {(k, "agent", "local") for k in three}


def test_a_set_is_checked_before_anything_stops(ui, three, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    calls = []
    monkeypatch.setattr(actions, "restart_service", lambda cfg, inst, **kw: calls.append(inst.key))
    assert post(port, "/api/instances/restart", {"keys": []}) == (400, {"error": "Pick the services to restart."})
    assert post(port, "/api/instances/restart", {"keys": [*three, "ghost@1"]})[0] == 400
    assert post(port, "/api/instances/restart", {"keys": three, "db": "shared"}) == (
        409, {"decision": "protected_database", "name": "shared"})
    assert post(port, "/api/instances/restart", {"keys": three, "remember": True})[0] == 400  # no stack, no user
    assert not calls and not jobs.snapshot()

    assert post(port, "/api/instances/restart", {"keys": three, "user": "boss", "db": "shared", "confirmed": True})[0] == 202
    wait_until(lambda: len(calls) == 3 and not jobs.snapshot())


def test_a_partial_stack_restart_can_be_remembered(ui, three, machine, tmp_path, monkeypatch) -> None:
    port, _hub, _states, jobs = ui
    machine.stacks["s"] = Stack(services=["a", "b"], user="agent", db="local")
    monkeypatch.setattr(actions.repos, "active_backend", lambda cfg: tmp_path)
    from pdms_cli.ui import jobs as ui_jobs

    monkeypatch.setattr(ui_jobs.repos, "active_backend", lambda cfg: tmp_path)
    done = []
    monkeypatch.setattr(actions, "restart_service", lambda cfg, inst, **kw: done.append((inst.key, kw["user_name"])))
    body = {"keys": ["b@8092"], "user": "boss", "stack": "s", "remember": True}
    assert post(port, "/api/instances/restart", body)[0] == 202
    wait_until(lambda: done and not jobs.snapshot())
    assert done == [("b@8092", "boss")] and machine.stacks["s"].overrides == {"b": {"user": "boss"}}


def test_the_stack_editor_keeps_the_exceptions_unless_it_sends_them(ui, machine, monkeypatch) -> None:
    from pdms_cli.ui import jobs as ui_jobs

    monkeypatch.setattr(ui_jobs, "repo_services", lambda cfg: (Path("/r"), ["a", "b"]))
    machine.stacks["s"] = Stack(services=["a", "b"], overrides={"b": {"user": "boss"}})
    port = ui[0]
    assert post(port, "/api/stacks/s/save", {"services": ["a", "b"]}) == (200, {"name": "s"})
    assert machine.stacks["s"].overrides == {"b": {"user": "boss"}}
    assert post(port, "/api/stacks/s/save", {"services": ["a", "b"], "overrides": {}}) == (200, {"name": "s"})
    assert machine.stacks["s"].overrides == {}
    assert post(port, "/api/stacks/s/save", {"services": ["a"], "overrides": {"a": {"user": 3}}})[0] == 400


# --------------------------------------------------------------------------- pdms restart


def test_pdms_restart_picks_a_stack_s_running_services(cfg, backend, monkeypatch) -> None:
    cfg.stacks["s"] = Stack(services=["lead/a", "lead/b", "lead/c"])
    _live(monkeypatch, (backend / "lead/a", 8081), (backend / "lead/b", 8082))
    monkeypatch.setattr(instance_commands, "backend_root", lambda cfg: backend)
    keys = lambda found: [inst.key for inst in found]  # noqa: E731
    assert keys(instance_commands.restart_targets(cfg, [], "s")) == ["a@8081", "b@8082"]
    assert keys(instance_commands.restart_targets(cfg, ["b"], "s")) == ["b@8082"]
    import typer

    with pytest.raises(typer.Exit):
        instance_commands.restart_targets(cfg, ["c"], "s")  # in the stack, but not running
