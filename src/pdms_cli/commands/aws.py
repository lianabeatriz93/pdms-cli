"""The AWS of dev for the services: ``pdms aws ...`` (see awsenv.py)."""

from __future__ import annotations

import subprocess
from typing import Optional

import questionary
import typer

from .. import actions, awsenv, prompts
from ..config import Config
from ..i18n import _
from .common import app, console, interactive_terminal, settle, show_menu

aws_app = typer.Typer(help=_(
    "The AWS profile the services use, and the buckets pdms reads from that account's Lambdas."
), invoke_without_command=True)
app.add_typer(aws_app, name="aws")


@aws_app.callback()
def aws_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        if interactive_terminal():
            aws_menu()
        else:
            aws_status()


def ask_profile(current: str) -> Optional[str]:
    """The profile chosen among the AWS config's ("" = none), or None when there is none to choose."""
    found = awsenv.profiles()
    if not found:
        console.print("  " + _("No profiles in {path}: set one up with aws configure sso.", path=awsenv.config_file()))
        return None
    none = _("None: leave AWS as the terminal has it")
    choices = [questionary.Choice(f"{name}  ·  {awsenv.region_of(name)}", name) for name in found]
    choices.append(questionary.Choice(none, ""))
    return questionary.select(_("AWS profile for the services:"), choices=choices,
                              default=current if current in found else None).unsafe_ask()


def print_changes(changes: list[awsenv.Change]) -> None:
    for change in changes:
        console.print(f"  [yellow]~[/] {change.function} · {change.variable}: "
                      f"{change.old or _('(none)')} → {change.new or _('(none)')}", highlight=False)


def read_now(cfg: Config) -> None:
    profile = cfg.defaults.aws_profile
    if profile and interactive_terminal():
        with console.status(_("Asking AWS who {profile} is...", profile=profile)):
            who = awsenv.session(profile)
        if who.state == "expired" and awsenv.cli() and questionary.confirm(
            _("The AWS session of {profile} is over. Log in now (opens the browser)?", profile=profile), default=True
        ).unsafe_ask():
            subprocess.run(awsenv.login_command(profile))
    with console.status(_("Reading the Lambdas of {profile}...", profile=cfg.defaults.aws_profile)):
        changes = settle(lambda: actions.read_aws(cfg))
    found = awsenv.summary(cfg)
    console.print("[green]✓[/] " + _("{functions} Lambdas read; {services} services of the repo get their buckets.",
                                      functions=found["functions"], services=found["with_buckets"]))
    if changes:
        console.print(_("Changed since the last read:"))
        print_changes(changes)
        if found["restart"]:
            console.print("  " + _("Restart to use them: {keys}", keys=", ".join(found["restart"])))


@aws_app.command("status", help=_("The profile, its session, and what was read from its Lambdas."))
def aws_status() -> None:
    cfg = Config.load()
    found = awsenv.summary(cfg)
    if not found["cli"]:
        console.print("[yellow]![/] " + _("The AWS CLI (aws) is not installed."))
    if not found["profile"]:
        console.print(_("No AWS profile chosen: services use AWS as the terminal has it. pdms aws profile chooses one."))
        return
    with console.status(_("Asking AWS who {profile} is...", profile=found["profile"])):
        who = awsenv.session(found["profile"])
    mark = {"ok": "[green]✓[/]", "expired": "[yellow]![/]"}.get(who.state, "[red]✗[/]")
    detail = _("account {account}", account=who.account) if who.state == "ok" else \
        _("session over: pdms aws login") if who.state == "expired" else who.detail
    console.print(f"{mark} {found['profile']} · {found['region']} · {detail}", highlight=False)
    if not found["read_at"]:
        console.print("  " + _("Its Lambdas were not read yet: pdms aws read."))
        return
    console.print("  " + _("Lambdas read {when}: {functions}; {services} of {total} services get buckets.",
                           when=found["read_at"][:16].replace("T", " "), functions=found["functions"],
                           services=found["with_buckets"], total=found["services"]), highlight=False)
    for service, function in found["named"].items():
        console.print("  [dim]" + _("{service} is the Lambda {function} (dev Terraform)", service=service,
                                    function=function) + "[/]", highlight=False)
    if found["changes"]:
        console.print("  " + _("Changed on {when}:", when=found["changed_at"][:16].replace("T", " ")), highlight=False)
        print_changes([awsenv.Change(**c) for c in found["changes"]])
    if found["restart"]:
        console.print("  [yellow]![/] " + _("Restart to use them: {keys}", keys=", ".join(found["restart"])))


@aws_app.command("profile", help=_("Choose the AWS profile the services use (empty = ask; --none = no profile)."))
def aws_profile(
    name: Optional[str] = typer.Argument(None, help=_("A profile of your AWS config.")),
    none: bool = typer.Option(False, "--none", help=_("No profile: leave AWS as the terminal has it.")),
) -> None:
    cfg = Config.load()
    if none:
        name = ""
    elif name is None:
        prompts.require_tty()
        name = ask_profile(cfg.defaults.aws_profile)
        if name is None:
            raise typer.Exit(1)
    settle(lambda: actions.set_aws_profile(cfg, name))
    if not name:
        console.print("[green]✓[/] " + _("No AWS profile: services use AWS as the terminal has it."))
        return
    console.print("[green]✓[/] " + _("Services use the AWS profile {profile}.", profile=name))
    if awsenv.summary(cfg)["read_at"]:
        return
    if interactive_terminal() and not questionary.confirm(
        _("Read the buckets of its Lambdas now?"), default=True
    ).unsafe_ask():
        console.print("  [dim]" + _("Later with pdms aws read.") + "[/]")
        return
    read_now(cfg)


@aws_app.command("read", help=_("Read the buckets of the Lambdas now (read-only; only bucket variables are kept)."))
def aws_read() -> None:
    read_now(Config.load())


@aws_app.command("login", help=_("Log in to the profile's SSO session (aws sso login opens the browser)."))
def aws_login() -> None:
    cfg = Config.load()
    if not cfg.defaults.aws_profile:
        console.print("[red]✗[/] " + _("Choose an AWS profile first (pdms aws profile)."))
        raise typer.Exit(1)
    if not awsenv.cli():
        console.print("[red]✗[/] " + _("The AWS CLI (aws) is not installed."))
        raise typer.Exit(1)
    raise typer.Exit(subprocess.run(awsenv.login_command(cfg.defaults.aws_profile)).returncode)


def aws_menu() -> None:
    def login() -> None:
        try:
            aws_login()
        except typer.Exit:
            pass

    show_menu(_("AWS:"), {
        _("Status"): aws_status,
        _("Choose the profile"): lambda: aws_profile(None, False),
        _("Read the Lambdas' buckets now"): aws_read,
        _("Log in (aws sso login)"): login,
    })
