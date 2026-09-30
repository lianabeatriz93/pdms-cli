"""Commands called directly from Python (menus, other commands) must pass every Typer parameter.

A parameter left out receives its ``typer.Option``/``typer.Argument`` default, an ``OptionInfo`` object instead of a
real value (e.g. "Unknown events mode '<typer.models.OptionInfo object ...>'").
"""

from __future__ import annotations

import ast
from pathlib import Path

import pdms_cli

SOURCES = sorted(Path(pdms_cli.__file__).parent.glob("*.py"))


def _typer_params(function: ast.FunctionDef) -> tuple[list[str], set[str]]:
    args = function.args.args
    defaults = [None] * (len(args) - len(function.args.defaults)) + function.args.defaults
    typer = {a.arg for a, d in zip(args, defaults)
             if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("Option", "Argument")}
    return [a.arg for a in args], typer


def test_direct_calls_pass_every_typer_parameter() -> None:
    problems = []
    for source in SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        commands = {n.name: _typer_params(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        commands = {name: params for name, params in commands.items() if params[1]}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in commands:
                params, typer = commands[node.func.id]
                given = set(params[:len(node.args)]) | {k.arg for k in node.keywords}
                missing = sorted(typer - given)
                if missing:
                    problems.append(f"{source.name}:{node.lineno} {node.func.id}() without {', '.join(missing)}")
    assert not problems, "\n".join(problems)
