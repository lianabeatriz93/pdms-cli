"""The AWS of dev for the services: ``pdms aws ...`` (see awsenv.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Optional

import questionary
import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from .. import actions, awsenv, awslogs, codeartifact, completion, frontend, prompts, repos
from ..config import Config
from ..i18n import _
from .common import app, console, current_repo_root, fail, interactive_terminal, settle, show_menu

errors_console = Console(stderr=True)  # with --json, what is not a line goes to stderr
BODY_CAP = 1500  # characters of a message body printed without --full

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


@aws_app.command("codeartifact", help=_(
    "Log in to AWS CodeArtifact for the web app's @alivi packages, as frontend/codeartifact-login.sh does "
    "(pdms front does it by itself when the token is over)."))
def aws_codeartifact() -> None:
    from .frontend import sso_login

    cfg = Config.load()
    folder = frontend.folder(current_repo_root(cfg))
    if not codeartifact.domains(folder):
        fail(_("{path} not found: this repo's web app does not use AWS CodeArtifact.", path=folder / codeartifact.SCRIPT))
    used = settle(lambda: actions.codeartifact_ready(folder, sys.stdout, sso_login if interactive_terminal() else None,
                                                     force=True))
    console.print("[green]✓[/] " + _("Logged in to AWS CodeArtifact with {profiles}: the token lasts 12 hours.",
                                     profiles=", ".join(dict.fromkeys(used))), highlight=False)


def logs_profile(cfg: Config) -> str:
    if not awsenv.cli():
        fail(_("The AWS CLI (aws) is not installed."))
    if not cfg.defaults.aws_profile:
        fail(_("Choose an AWS profile first (pdms aws profile)."))
    return cfg.defaults.aws_profile


def print_line(line: awslogs.Line, as_json: bool, short: bool, full: bool) -> None:
    if as_json:
        print(json.dumps(line.to_dict(), ensure_ascii=False, default=str), flush=True)
        return
    when = datetime.fromtimestamp(line.at / 1000).strftime("%Y-%m-%d %H:%M:%S")
    if not line.messages:
        style = {"ERROR": "red", "WARN": "yellow"}.get(line.level, "dim")
        text = line.raw.split("\n")[0] if short else line.raw
        if not full and len(text) > BODY_CAP:
            text = text[:BODY_CAP] + " …"
        console.print(Text.assemble((f"{when} {line.function} {line.level or '-'} ", style), text))
        return
    for message in line.messages:
        arrow, where = ("→", message.destination) if message.direction == "sent" else ("←", _("received"))
        title = f"{when} · {line.function} {arrow} {where or '?'} · {message.type or '-'}"
        if short:
            console.print(title, markup=False, highlight=False, soft_wrap=True)
            continue
        console.rule(escape(title), align="left")
        body = message.body if isinstance(message.body, str) else json.dumps(message.body, indent=2,
                                                                                ensure_ascii=False, default=str)
        console.print(body if full or len(body) < BODY_CAP else body[:BODY_CAP] + " …", markup=False, highlight=False)


def show_logs(cfg: Config, functions: list[str], since: str = "1h", start: str = "", end: str = "",
              event_type: str = "", level: str = "", grep: str = "", every_line: bool = False, follow: bool = False,
              every: int = awslogs.FOLLOW_EVERY, limit: int = awslogs.LIMIT, as_json: bool = False,
              short: bool = False, full: bool = False) -> None:
    profile = logs_profile(cfg)
    note = errors_console if as_json else console
    if not functions:
        functions = awslogs.event_flow(current_repo_root(cfg))
        if not functions:
            fail(_("No Lambda named, and the repo's dev Terraform has no broker nor SNS publisher: pdms aws lambdas "
                   "lists them."))
        note.print("[dim]" + _("Reading {functions} (the broker and the Lambdas that publish to SNS).",
                               functions=", ".join(functions)) + "[/]", highlight=False)
    if follow and end:
        fail(_("--follow reads up to now: leave --to out."))
    try:
        end_at = awslogs.moment(end) if end else None
        start_at = awslogs.moment(start) if start else (end_at or datetime.now().astimezone()) - awslogs.since(since)
    except ValueError as exc:
        fail(str(exc))
    filter_pattern = awslogs.pattern(grep, event_type, not every_line)

    def ask(round_: Callable[[], awslogs.Read]) -> awslogs.Read:
        try:
            if as_json:
                return round_()
            with console.status(_("Asking CloudWatch about {count} Lambdas...", count=len(functions))):
                return round_()
        except awslogs.LogsError as exc:
            if exc.expired:
                fail(_("The AWS session of {profile} is over: pdms aws login.", profile=profile))
            fail(exc.message)

    def show(result: awslogs.Read) -> int:
        for error in result.errors.values():
            note.print("[yellow]![/] " + escape(error.message), highlight=False)
        shown = [line for line in result.lines if awslogs.keep(line, not every_line, event_type, level, grep)]
        for line in shown:
            print_line(line, as_json, short, full)
        return len(shown)

    asked = awslogs.millis(datetime.now().astimezone())
    result = ask(lambda: awslogs.read(profile, functions, awslogs.millis(start_at),
                                      awslogs.millis(end_at) if end_at else None, filter_pattern, limit))
    count = show(result)
    if result.more:
        note.print("[yellow]![/] " + _("More than {limit} lines in {functions}: these are the first ones; narrow the time or raise --limit.",
                                       limit=limit, functions=", ".join(sorted(result.more))), highlight=False)
    if not follow:
        if not count:
            note.print(_("Nothing found from {start} to {end}.", start=start_at.strftime("%Y-%m-%d %H:%M"),
                         end=(end_at or datetime.now()).strftime("%Y-%m-%d %H:%M")))
        return
    seen = {line.id: line.at for line in result.lines}
    note.print("[dim]" + _("Following: a new read every {seconds} s (Ctrl+C stops).", seconds=every) + "[/]")
    try:
        while True:
            time.sleep(every)
            now = awslogs.millis(datetime.now().astimezone())
            show(ask(lambda after=asked: awslogs.follow(profile, functions, after, seen, filter_pattern, limit)))
            asked = now
    except KeyboardInterrupt:
        pass


@aws_app.command("logs", help=_(
    "What the dev Lambdas wrote to CloudWatch and the events they sent (read-only). By default the events and errors "
    "of the last hour of the broker and the Lambdas that publish to SNS."))
def aws_logs(
    functions: Optional[list[str]] = typer.Argument(
        None, help=_("Lambdas to read (pdms aws lambdas lists them)."), autocompletion=completion.lambdas),
    since: str = typer.Option("1h", "--since", "-s", help=_("How far back: 30s, 15m, 1h, 2d.")),
    start: str = typer.Option("", "--from", help=_("From this moment instead: 2026-11-02 09:00, or 09:00 today.")),
    end: str = typer.Option("", "--to", help=_("Up to this moment (default: now).")),
    event_type: str = typer.Option("", "--type", "-t", help=_("Only this event type (SQS type or SNS event)."),
                                   autocompletion=completion.event_targets),
    level: str = typer.Option("", "--level", "-l", help=_("Only this level: debug, info, warn, error.")),
    grep: str = typer.Option("", "--grep", "-g", help=_("Only lines with this text (an id, a file name...).")),
    every_line: bool = typer.Option(False, "--all", "-a", help=_("Every line, not only events and errors.")),
    follow: bool = typer.Option(False, "--follow", "-f", help=_("Keep reading what comes (Ctrl+C stops).")),
    every: int = typer.Option(awslogs.FOLLOW_EVERY, "--every", min=15, help=_("Seconds between two reads with --follow.")),
    limit: int = typer.Option(awslogs.LIMIT, "--limit", "-n", min=1, max=10000, help=_("Lines read per Lambda.")),
    as_json: bool = typer.Option(False, "--json", help=_("One JSON object per line (for jq).")),
    short: bool = typer.Option(False, "--short", help=_("One line per event, without its body.")),
    full: bool = typer.Option(False, "--full", help=_("Print the whole body of each message.")),
) -> None:
    show_logs(Config.load(), list(functions or []), since, start, end, event_type, level, grep, every_line, follow,
              every, limit, as_json, short, full)


@aws_app.command("lambdas", help=_("The Lambdas of the account that have logs, and the part each one takes in events."))
def aws_lambdas(
    text: str = typer.Argument("", help=_("Only names with this text.")),
    events_only: bool = typer.Option(False, "--events", "-e", help=_("Only the broker, SNS publishers and consumers.")),
) -> None:
    cfg = Config.load()
    profile = logs_profile(cfg)
    root = repos.active_root(cfg)
    roles = {s.function: s for s in awslogs.suggestions(root)} if root and root.is_dir() else {}
    try:
        with console.status(_("Asking CloudWatch for the log groups of {profile}...", profile=profile)):
            names = awslogs.lambdas(profile)
    except awslogs.LogsError as exc:
        fail(_("The AWS session of {profile} is over: pdms aws login.", profile=profile) if exc.expired else exc.message)
    names = [name for name in names if text.lower() in name.lower() and (not events_only or name in roles)]
    if not names:
        console.print(_("No Lambda found."))
        return
    width = max(len(name) for name in names)
    labels = {"broker": _("broker"), "sns": _("publishes to SNS"), "consumer": _("consumes a queue")}
    for name in names:
        role = roles.get(name)
        extra = f"  [cyan]{labels[role.role]}[/] [dim]{escape(role.detail)}[/]" if role else ""
        console.print(f"  {name.ljust(width)}{extra}", highlight=False, soft_wrap=True)


def aws_menu() -> None:
    def login() -> None:
        try:
            aws_login()
        except typer.Exit:
            pass

    def logs() -> None:
        try:
            show_logs(Config.load(), [])
        except typer.Exit:
            pass

    show_menu(_("AWS:"), {
        _("Status"): aws_status,
        _("Choose the profile"): lambda: aws_profile(None, False),
        _("Read the Lambdas' buckets now"): aws_read,
        _("Events of the last hour (CloudWatch)"): logs,
        _("Log in (aws sso login)"): login,
    })
