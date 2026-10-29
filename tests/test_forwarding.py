"""actions and ui.jobs keep working as one module each: ``package.<name>`` reads and replaces each name where it
lives, so a test that replaces a function replaces it for every caller (see forwarding.py)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pdms_cli import actions
from pdms_cli.ui import jobs

PACKAGES = pytest.mark.parametrize("package", [actions, jobs], ids=["actions", "ui.jobs"])
# Modules imported by name from: they hold classes and constants, and import no other module of their package.
BY_NAME = {"errors", "core"}


def module_tree(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def defined(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names - {"__all__"}


@PACKAGES
def test_each_module_lists_what_it_holds_and_every_name_has_one_home(package) -> None:
    names = {m.__name__.rsplit(".", 1)[1] for m in package.MODULES}
    assert {path.stem for path in Path(package.__file__).parent.glob("*.py")} - {"__init__"} == names
    for module in package.MODULES:
        assert set(module.__all__) == defined(module_tree(module)), module.__name__
    assert sum(len(module.__all__) for module in package.MODULES) == len(package.HOME)  # no name in two modules
    assert not names & set(package.HOME)  # package.<module> would hide the name (jobs.settings, the function)


@PACKAGES
def test_modules_call_each_others_functions_through_the_module(package) -> None:
    """``from .services import plan_service`` would keep a copy that a replaced function never reaches: functions
    are never imported by name; classes are (a base class is needed while the module loads), and only errors and
    core, which import no other module of theirs, give anything else by name."""
    for module in package.MODULES:
        for node in ast.walk(module_tree(module)):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                values = {alias.name: package.HOME[alias.name].__dict__[alias.name] for alias in node.names}
                functions = {name for name, value in values.items() if callable(value) and not isinstance(value, type)}
                others = {name for name, value in values.items() if not callable(value)}
                assert not functions, (module.__name__, node.module, functions)
                assert node.module in BY_NAME or not others, (module.__name__, node.module, others)
    for module in package.MODULES:  # errors and core load no other module (only for type annotations)
        if module.__name__.rsplit(".", 1)[1] in BY_NAME:
            tree = module_tree(module)
            typing_only = {id(n) for block in ast.walk(tree) if isinstance(block, ast.If)
                           and isinstance(block.test, ast.Name) and block.test.id == "TYPE_CHECKING"
                           for n in ast.walk(block)}
            assert not [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1
                        and id(n) not in typing_only], module.__name__


def test_replacing_a_name_through_actions_replaces_it_where_it_lives(monkeypatch) -> None:
    original = actions.local_events.consumer_of

    def fake(cfg, service):
        return None

    with monkeypatch.context() as patch:
        patch.setattr(actions, "consumer_of", fake)
        assert actions.consumer_of is fake and actions.local_events.consumer_of is fake
        assert "consumer_of" not in vars(actions)  # never a copy in the package
    assert actions.consumer_of is original and actions.local_events.consumer_of is original


def test_a_replaced_function_reaches_callers_in_other_modules(monkeypatch, tmp_path) -> None:
    """services.plan_service calls local_events.events_setup: replacing it through actions reaches that call."""
    from pdms_cli.config import Config, Database, DevUser

    seen = []

    class Stop(Exception):
        pass

    def events_setup(*args, **kwargs):
        seen.append(args)
        raise Stop

    service = tmp_path / "lead-a"
    service.mkdir()
    monkeypatch.setattr(actions, "events_setup", events_setup)
    monkeypatch.setattr(actions, "consumer_of", lambda cfg, path: None)
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set())
    cfg = Config(users={"a": DevUser("u", "a@x.com")}, dbs={"local": Database("localhost")})
    try:
        actions.plan_service(cfg, service, user_name="a", db_name="local")
    except Stop:
        pass
    assert seen, "plan_service did not reach the replaced events_setup"


def test_the_modules_they_use_answer_as_before() -> None:
    from pdms_cli import events, instances, proxy, runner
    from pdms_cli.config import Config

    assert actions.events is events and actions.instances is instances and actions.runner is runner
    assert jobs.events is events and jobs.proxy is proxy and jobs.actions is actions and jobs.Config is Config


def test_replacing_a_function_of_ui_jobs_reaches_the_jobs_that_call_it(monkeypatch) -> None:
    """Jobs.stop (services) finds the instance with services.find: replacing jobs.find reaches it."""
    def find(key):
        raise actions.ActionError(f"looked for {key}")

    monkeypatch.setattr(jobs, "find", find)
    try:
        jobs.Jobs().stop("lead-a@28100")
    except actions.ActionError as exc:
        assert exc.message == "looked for lead-a@28100"
    else:
        raise AssertionError("Jobs.stop did not reach the replaced find")


def test_replacing_a_function_of_ui_jobs_reaches_another_module(monkeypatch) -> None:
    """home.save_setup reads the body with settings_screen._text: replacing jobs._text reaches it."""
    def text(body, key, default=""):
        raise actions.ActionError(f"read {key}")

    monkeypatch.setattr(jobs, "_text", text)
    try:
        jobs.save_setup({})
    except actions.ActionError as exc:
        assert exc.message == "read stack"
    else:
        raise AssertionError("save_setup did not reach the replaced _text")
