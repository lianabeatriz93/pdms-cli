"""Finding the services of the current repo: ``pdms services`` and the paths of a stack."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, prompts, repos, runner
from ..config import Config, Stack
from ..i18n import _
from .common import app, console, fail, settle


def services_root(cfg: Config) -> Optional[Path]:
    backend = repos.active_backend(cfg)
    if backend is None:
        return None
    if backend.is_dir():
        return backend.resolve()
    console.print(f"[yellow]{_('⚠ The configured backend folder does not exist: {root}', root=backend)}[/]")
    return None


def list_services(cfg: Config) -> tuple[Path, list[Path]]:
    root = services_root(cfg) or Path.cwd().resolve()
    candidates = runner.find_services_below(root)
    if not candidates:
        hint = "" if cfg.repos else _(" Register a repo with [bold]pdms repo add <path>[/].")
        fail(_("No services (pyproject.toml + main.py) found in {root}.{hint}", root=root, hint=hint))
    return root, candidates


def label(service: Path, root: Path) -> str:
    try:
        return service.relative_to(root).as_posix()
    except ValueError:
        return str(service)


def running_by_service() -> dict[str, list[int]]:
    return actions.running_by_service()


def choose_service(candidates: list[Path], root: Path, message: Optional[str] = None) -> Path:
    prompts.require_tty()
    running = running_by_service()
    labels = {label(c, root): c for c in candidates}
    meta = {
        text: _("running on :{ports}", ports=", :".join(map(str, running[str(path)])))
        for text, path in labels.items() if str(path) in running
    }

    def matching(text: str) -> list[str]:
        text = text.strip()
        return [text] if text in labels else [t for t in labels if text and text in t]

    def validate(text: str) -> bool | str:
        found = matching(text)
        if len(found) == 1:
            return True
        return _("{count} services match, narrow it down", count=len(found)) if found else _("No service matches")

    choice = questionary.autocomplete(
        _("{message} (type to filter, Tab to see the list):", message=message or _("Service to run")),
        choices=list(labels),
        meta_information=meta,
        validate=validate,
        match_middle=True,
    ).unsafe_ask()
    return labels[matching(choice)[0]]


def resolve_service(cfg: Config, name: Optional[str], path: Optional[Path]) -> Path:
    if path:
        if service := runner.find_service_upwards(path.expanduser().resolve()):
            return service
        fail(_("{path} is not (and is not inside) a service.", path=path))
    if name:
        root, candidates = list_services(cfg)
        exact = [c for c in candidates if name in (c.name, label(c, root))]
        matches = exact or [c for c in candidates if name in label(c, root)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            fail(_("No service matches '{name}' in {root}.", name=name, root=root))
        return choose_service(matches, root)
    if service := runner.find_service_upwards(Path.cwd().resolve()):
        return service
    root, candidates = list_services(cfg)
    return choose_service(candidates, root)


@app.command("services", help=_("List the services available in the backend folder."))
def services_cmd(filter: Optional[str] = typer.Argument(None, help=_("Filter by text."))) -> None:
    cfg = Config.load()
    root, candidates = list_services(cfg)
    running = running_by_service()
    table = Table(_("Service"), _("Running"), title=str(root), title_justify="left")
    for c in candidates:
        text = label(c, root)
        if filter and filter not in text:
            continue
        ports = running.get(str(c), [])
        table.add_row(text, "[green]" + ", ".join(f":{p}" for p in ports) + "[/]" if ports else "")
    console.print(table)


def backend_root(cfg: Config) -> Path:
    return services_root(cfg) or fail(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))


def stack_paths(cfg: Config, stack: Stack) -> list[Path]:
    root = backend_root(cfg)
    return settle(lambda: actions.stack_paths(stack, root))
