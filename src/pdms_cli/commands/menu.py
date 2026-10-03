"""The interactive menu that ``pdms`` opens without arguments."""

from __future__ import annotations

import os

import questionary
import typer

from .. import __version__, banner, instances, prompts
from ..config import Config, config_path
from ..i18n import _
from .common import console, show_menu
from .dbs import db_menu
from .doctor import doctor_cmd
from .events import events_menu
from .instances import instances_menu, ps
from .proxy import proxy_main
from .repos import check_repo, repo_menu
from .run import do_run
from .settings import config_defaults, config_export, config_import, config_language, config_show_path
from .setup import setup_cmd
from .stacks import stack_menu
from .users import user_menu


def settings_menu() -> None:
    prompts.require_tty()
    show_menu(_("Settings:"), {
        _("Guided setup"): setup_cmd,
        _("Defaults"): config_defaults,
        _("Repos"): repo_menu,
        _("Check the environment (doctor)"): lambda: doctor_cmd(False, 5),
        _("Language"): lambda: config_language(None),
        _("Export configuration"): lambda: config_export(None, None, None, False),
        _("Import configuration"): lambda: config_import(None, None, False, False, False),
        _("Show configuration file path"): config_show_path,
    })


def main_menu(first_run: bool = False) -> None:
    prompts.require_tty()
    cfg = Config.load()
    if cfg.defaults.banner and not os.environ.get("PDMS_NO_BANNER"):
        banner.render(console, __version__, _("Local PDMS services, made easy"))
    if first_run:
        if questionary.confirm(
            _("Welcome! There is no pdms configuration yet. Run the guided setup now?"), default=True
        ).unsafe_ask():
            setup_cmd()
            console.print()
        else:
            check_repo(Config.load())
    if not config_path().exists():
        Config.load().save()
        console.print(_("[dim]Configuration created at {path}[/]", path=config_path()))
    if instances.running_ports():
        ps(False)
        console.print()
    while True:
        running = len(instances.running_ports())
        choice = questionary.select(
            _("What do you want to do?"),
            choices=[
                questionary.Choice(_("▶  Run a service"), "run"),
                questionary.Choice(_("📋 Background services ({count} running)", count=running), "ps"),
                questionary.Choice(_("🧩 Stacks (groups of services)"), "stack"),
                questionary.Choice(_("🌐 Proxy (one port for every service)"), "proxy"),
                questionary.Choice(_("📨 Events (local SQS)"), "events"),
                questionary.Choice(_("🗄  Databases"), "db"),
                questionary.Choice(_("👤 Users"), "user"),
                questionary.Choice(_("⚙  Settings"), "defaults"),
                questionary.Choice(_("✕  Exit"), "exit"),
            ],
        ).unsafe_ask()
        if choice == "exit":
            return
        actions = {
            "run": do_run, "ps": instances_menu, "stack": stack_menu,
            "proxy": lambda: proxy_main(None, None, None, None, False, "dev", None, None, None),
            "events": events_menu, "db": db_menu, "user": user_menu,
            "defaults": settings_menu,
        }
        try:
            actions[choice]()  # a foreground do_run replaces the process
        except typer.Exit:
            pass
