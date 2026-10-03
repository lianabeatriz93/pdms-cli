"""Commands called directly from Python (menus, other commands) must pass every Typer parameter.

A parameter left out receives its ``typer.Option``/``typer.Argument`` default, an ``OptionInfo`` object instead of a
real value (e.g. "Unknown events mode '<typer.models.OptionInfo object ...>'").
"""

from __future__ import annotations

import ast
from pathlib import Path

import pdms_cli

PACKAGE = Path(pdms_cli.__file__).parent
SOURCES = sorted(PACKAGE.rglob("*.py"))


def _typer_params(function: ast.FunctionDef) -> tuple[list[str], set[str]]:
    args = function.args.args
    defaults = [None] * (len(args) - len(function.args.defaults)) + function.args.defaults
    typer = {a.arg for a, d in zip(args, defaults)
             if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("Option", "Argument")}
    return [a.arg for a in args], typer


def test_direct_calls_pass_every_typer_parameter() -> None:
    # Commands are called across modules (the menus call the commands of every group), so collect them all first.
    trees = {source: ast.parse(source.read_text(encoding="utf-8")) for source in SOURCES}
    commands = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and _typer_params(node)[1]:
                assert node.name not in commands, f"two commands named {node.name}(): the check needs unique names"
                commands[node.name] = _typer_params(node)
    problems = []
    for source, tree in trees.items():
        # Only the commands this file defines or imports (another module may have a plain function of the same name).
        visible = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and _typer_params(n)[1]}
        visible |= {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
                    if a.name in commands}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in visible:
                params, typer = commands[node.func.id]
                given = set(params[:len(node.args)]) | {k.arg for k in node.keywords}
                missing = sorted(typer - given)
                if missing:
                    problems.append(f"{source.relative_to(PACKAGE)}:{node.lineno} {node.func.id}() without {', '.join(missing)}")
    assert not problems, "\n".join(problems)
