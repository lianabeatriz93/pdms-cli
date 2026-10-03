"""General settings: ``pdms config ...``."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, i18n, prompts, transfer
from ..config import Config, config_path, write_private
from ..i18n import _
from .common import config_app, console, fail, settle


@config_app.callback()
def config_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        config_defaults()


@config_app.command("defaults", help=_("Edit the defaults (language, port, log, reload, extra env...)."))
def config_defaults() -> None:
    prompts.require_tty()
    cfg = Config.load()
    defaults = prompts.ask_defaults(cfg.defaults)
    settle(lambda: actions.save_defaults(cfg, defaults))
    i18n.set_language(cfg.defaults.language)
    console.print("[green]✓[/] " + _("Defaults saved."))


@config_app.command("language", help=_("Change the CLI language."))
def config_language(
    lang: Optional[str] = typer.Argument(
        None, help=_("Language code: {codes}. Empty = ask.", codes=", ".join(i18n.LANGUAGES)), autocompletion=completion.languages
    ),
) -> None:
    cfg = Config.load()
    if lang is None:
        prompts.require_tty()
        lang = prompts.ask_language(cfg.defaults.language)
    settle(lambda: actions.set_language(cfg, lang))
    i18n.set_language(lang)
    console.print("[green]✓[/] " + _("Language set to {name}.", name=i18n.LANGUAGES[lang]))


def section_label(section: str) -> str:
    return {
        "defaults": _("defaults"), "users": _("users"), "dbs": _("databases"), "stacks": _("stacks"),
    }[section]


def parse_sections(only: Optional[str], available: list[str]) -> Optional[list[str]]:
    if not only:
        return None
    sections = [s.strip() for s in only.split(",") if s.strip()]
    unknown = [s for s in sections if s not in transfer.SECTIONS]
    if unknown:
        fail(_("Unknown sections: {unknown}. Available: {codes}",
               unknown=", ".join(unknown), codes=", ".join(transfer.SECTIONS)))
    return [s for s in sections if s in available]


def ask_sections(message: str, available: list[str]) -> list[str]:
    selected = questionary.checkbox(
        message, choices=[questionary.Choice(section_label(s), s, checked=True) for s in available]
    ).unsafe_ask()
    return [s for s in transfer.SECTIONS if s in selected]


SECTIONS_HELP = _("Comma-separated sections: defaults, users, dbs, stacks. Default: all.")


@config_app.command("export", help=_("Export the configuration (users, databases, stacks, defaults) to a TOML file."))
def config_export(
    file: Optional[Path] = typer.Argument(None, help=_("Output file ('-' = stdout). Default: pdms-config-<date>.toml.")),
    only: Optional[str] = typer.Option(None, "--only", help=SECTIONS_HELP),
    secrets: Optional[bool] = typer.Option(
        None, "--secrets/--no-secrets", help=_("Include database passwords (asked if omitted; no by default).")
    ),
    force: bool = typer.Option(False, "--force", help=_("Overwrite the file if it exists.")),
) -> None:
    cfg = Config.load()
    interactive = sys.stdin.isatty() and file != Path("-")
    sections = parse_sections(only, list(transfer.SECTIONS))
    if sections is None:
        sections = ask_sections(_("What do you want to export?"), list(transfer.SECTIONS)) if interactive \
            else list(transfer.SECTIONS)
    if not sections:
        fail(_("Nothing selected."))
    has_passwords = "dbs" in sections and any(db.password for db in cfg.dbs.values())
    if secrets is None:
        secrets = has_passwords and interactive and questionary.confirm(
            _("Include database passwords? (only if the file stays private)"), default=False
        ).unsafe_ask()

    text = settle(lambda: actions.export_config(cfg, sections, secrets))
    if file == Path("-"):
        sys.stdout.write(text)
        return
    file = file or Path(f"pdms-config-{datetime.now():%Y-%m-%d}.toml")
    if file.exists() and not force:
        if not interactive or not questionary.confirm(_("{file} already exists. Overwrite it?", file=file),
                                                      default=False).unsafe_ask():
            fail(_("{file} already exists (use --force).", file=file))
    write_private(file, text)

    counts = {"users": len(cfg.users), "dbs": len(cfg.dbs), "stacks": len(cfg.stacks)}
    parts = [f"{section_label(s)}" if s == "defaults" else f"{counts[s]} {section_label(s)}" for s in sections]
    console.print("[green]✓[/] " + _("Exported {parts} to {file}.", parts=", ".join(parts), file=file))
    if "dbs" in sections:
        console.print(_("  [yellow]The file includes database passwords: do not share it or commit it.[/]") if secrets
                      else _("  [dim]Database passwords were left out.[/]"))


@config_app.command("import", help=_("Import a configuration exported with pdms config export."))
def config_import(
    file: Optional[Path] = typer.Argument(None, help=_("File to import.")),
    only: Optional[str] = typer.Option(None, "--only", help=SECTIONS_HELP),
    replace: bool = typer.Option(
        False, "--replace", help=_("Replace the selected sections entirely instead of merging.")
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help=_("Overwrite existing entries without asking.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Apply without asking for confirmation.")),
) -> list[str]:
    """Returns the imported sections (used by the guided setup)."""
    interactive = sys.stdin.isatty()
    if file is None:
        prompts.require_tty()
        file = Path(questionary.path(_("File to import:"), validate=lambda v: Path(v).expanduser().is_file()
                                     or _("File not found")).unsafe_ask()).expanduser()
    if not file.is_file():
        fail(_("File not found: {file}", file=file))
    try:
        doc = transfer.read_document(file.read_text(encoding="utf-8"))
    except transfer.TransferError as exc:
        fail(str(exc))

    current = Config.load()
    sections = parse_sections(only, doc.sections)
    if sections is None:
        sections = ask_sections(_("What do you want to import?"), doc.sections) if interactive else doc.sections
    if not sections:
        fail(_("Nothing to import."))
    plans = transfer.plan_import(current, doc.config, sections)

    exported = doc.meta.get("exported_at", "?")
    console.print(_("File exported on {date} (pdms {version}).", date=exported, version=doc.meta.get("cli_version", "?")))
    table = Table(_("Section"), _("New"), _("Changed"), _("Unchanged"), _("Only local") if not replace else _("Removed"))
    for plan in plans:
        table.add_row(
            section_label(plan.section), ", ".join(plan.added) or "-", ", ".join(plan.changed) or "-",
            ", ".join(plan.same) or "-", ", ".join(plan.missing) or "-",
        )
    console.print(table)
    if not doc.meta.get("secrets") and "dbs" in sections:
        console.print(_("[dim]The file has no passwords: databases you already have keep their password.[/]"))

    conflicts = [(p.section, name) for p in plans for name in p.changed]
    chosen: set[tuple[str, str]] = set()
    if actions.first_setup():
        chosen = set(conflicts)  # there is no own configuration to keep
    elif replace:
        removed = sum(len(p.missing) for p in plans)
        if removed:
            console.print("[yellow]" + _("⚠ --replace will delete {count} local entries not in the file.",
                                         count=removed) + "[/]")
    elif overwrite:
        chosen = set(conflicts)
    elif conflicts and interactive:
        chosen = set(questionary.checkbox(
            _("These entries differ from yours. Which ones do you want to overwrite? (unchecked = keep yours)"),
            choices=[
                questionary.Choice(section_label(sec) if sec == "defaults" else f"{section_label(sec)}: {name}", (sec, name))
                for sec, name in conflicts
            ],
        ).unsafe_ask())
    elif conflicts:
        console.print(_("[dim]Existing entries are kept (use --overwrite to replace them).[/]"))

    result = transfer.apply_import(current, doc.config, sections, chosen, replace=replace)
    if result.to_dict() == current.to_dict():
        console.print(_("Nothing changes."))
        return sections
    if not yes:
        if not interactive:
            fail(_("Use --yes to import without an interactive terminal."))
        if not questionary.confirm(_("Apply the import?"), default=True).unsafe_ask():
            raise typer.Exit(1)

    backup = actions.import_config(result)
    i18n.set_language(result.defaults.language)
    console.print("[green]✓[/] " + _("Configuration imported."))
    if backup:
        console.print(_("  [dim]Previous configuration saved to {backup}[/]", backup=backup))
    no_password = [name for name, db in result.dbs.items() if not db.password]
    if no_password:
        console.print("[yellow]" + _("⚠ Databases without password: {names}. Set it with pdms db edit <name>.",
                                     names=", ".join(no_password)) + "[/]")
    return sections


@config_app.command("path", help=_("Show the path of the configuration file."))
def config_show_path() -> None:
    console.print(str(config_path()))


@config_app.command("edit", help=_("Open the configuration file in $EDITOR."))
def config_edit() -> None:
    cfg = Config.load()
    if not config_path().exists():
        cfg.save()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(config_path())])
    else:
        typer.launch(str(config_path()))
