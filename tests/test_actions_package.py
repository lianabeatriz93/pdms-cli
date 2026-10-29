"""The actions package keeps working as one module: ``actions.<name>`` reads and replaces each name where it lives,
so a test that replaces a function replaces it for every caller (see actions/__init__.py)."""

from __future__ import annotations

import ast
from pathlib import Path

from pdms_cli import actions

PACKAGE = Path(actions.__file__).parent


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


def test_each_module_lists_what_it_holds_and_every_name_has_one_home() -> None:
    assert {path.stem for path in PACKAGE.glob("*.py")} - {"__init__"} == {m.__name__.rsplit(".", 1)[1]
                                                                          for m in actions.MODULES}
    for module in actions.MODULES:
        assert set(module.__all__) == defined(module_tree(module)), module.__name__
    assert sum(len(module.__all__) for module in actions.MODULES) == len(actions.HOME)  # no name in two modules
    assert not {m.__name__.rsplit(".", 1)[1] for m in actions.MODULES} & set(actions.HOME)


def test_modules_call_each_others_functions_through_the_module() -> None:
    """``from .services import plan_service`` would keep a copy that a replaced function never reaches; only the
    errors (classes nothing replaces, in a module that depends on no other) are imported by name."""
    for module in actions.MODULES:
        for node in ast.walk(module_tree(module)):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                imported = {alias.name for alias in node.names}
                functions = {name for name in imported if callable(actions.HOME[name].__dict__.get(name))
                             and not isinstance(actions.HOME[name].__dict__.get(name), type)}
                assert node.module == "errors" or not functions, (module.__name__, node.module, functions)


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


def test_the_modules_it_uses_answer_as_before() -> None:
    from pdms_cli import events, instances, runner

    assert actions.events is events and actions.instances is instances and actions.runner is runner
