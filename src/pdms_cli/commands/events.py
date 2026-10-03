"""Local SQS events: ``pdms events ...``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import questionary
import typer
from rich.table import Table

from .. import actions, completion, events, instances, prompts, repos
from ..config import Config
from ..i18n import _
from .common import console, current_repo_root, events_app, fail, interactive_terminal, settle, show_menu
from .run import do_run


def load_events(env: str = "dev") -> tuple[Path, events.EventMap]:
    cfg = Config.load()
    current_repo_root(cfg)
    with console.status(_("Reading the event map (Terraform and backend/common/event)...")):
        return settle(lambda: actions.load_events(cfg, env))


@events_app.callback()
def events_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        events_status(False)


@events_app.command("map", help=_("Show every event type, the queue the broker sends it to and its consumer."))
def events_map(
    contains: str = typer.Option("", "--filter", "-f", help=_("Only rows containing this text.")),
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
) -> None:
    _root, event_map = load_events(env)
    table = Table(_("Event type"), _("Queue"), _("Consumer"))
    routed = set()
    for event_type, queue in sorted(event_map.routes.items()):
        consumer = event_map.consumers.get(queue)
        row = (event_type, queue, consumer.service if consumer else f"[red]{_('none')}[/]")
        routed.add(queue)
        if not contains or any(contains in str(v) for v in row):
            table.add_row(*row)
    console.print(table)
    others = Table(_("Queue"), _("Consumer"), _("Source"), title=_("Queues not routed by the broker"), title_justify="left")
    for name, queue in sorted(event_map.queues.items()):
        if name in routed:
            continue
        consumer = event_map.consumers.get(name)
        row = (name, consumer.service if consumer else "-", queue.source)
        if not contains or any(contains in str(v) for v in row):
            others.add_row(*row)
    if others.row_count:
        console.print(others)
    console.print(_("{types} event types · {queues} queues · {consumers} consumers · broker: {broker}",
                    types=len(event_map.routes), queues=len(event_map.queues), consumers=len(event_map.consumers),
                    broker=event_map.broker_queue or _("not found")))


@events_app.command("up", help=_("Start a local ElasticMQ (Docker) with every queue of the repo, and the broker."))
def events_up(
    env: str = typer.Option("dev", "--env", "-e", help=_("Terraform environment to read the routes from.")),
    broker: bool = typer.Option(True, "--broker/--no-broker", help=_("Also run the broker (broker-sqs-event) locally.")),
) -> None:
    cfg = Config.load()
    _root, event_map = load_events(env)
    port = cfg.defaults.events_port
    with console.status(_("Starting ElasticMQ...")):
        result = settle(lambda: actions.start_events(cfg, event_map.queues))
    extra = sum(q.source != "terraform" for q in event_map.queues.values())
    message = {
        "created": _("ElasticMQ started"), "restarted": _("ElasticMQ restarted with the updated queues"),
        "unchanged": _("ElasticMQ was already running with these queues"),
    }[result]
    console.print("[green]✓[/] " + message + f" · {events.endpoint(port)}")
    console.print("  " + _("{count} queues ({extra} only in infra/local_sqs/elasticmq.conf) · broker: {broker}",
                           count=len(event_map.queues), extra=extra, broker=event_map.broker_queue or "-"))
    if broker:
        start_broker(cfg, _root, event_map)


def start_broker(cfg: Config, root: Path, event_map: events.EventMap) -> None:
    """Run broker-sqs-event locally, so published events are routed to their queues as in AWS."""
    service = actions.broker_service(root, event_map)
    if service is None:
        console.print("[yellow]" + _("⚠ The broker ({service}) was not found in the repo; events stay in the broker "
                                     "queue.", service=events.BROKER_SERVICE) + "[/]")
        return
    if actions.event_consumers(event_map.broker_queue):
        console.print(_("[dim]The broker is already running.[/]"))
        return
    if not cfg.users or not cfg.dbs:
        console.print("[yellow]" + _("⚠ Configure a user and a database to run the broker (pdms user add, pdms db "
                                     "add), then: pdms run {service} -b", service=events.BROKER_SERVICE) + "[/]")
        return
    console.rule(_("Broker"))
    try:
        do_run(user=cfg.last_user or None, db=cfg.last_db or None, path=service, background=True, yes=True,
               events_mode="local")
    except typer.Exit:
        console.print("[yellow]" + _("⚠ The broker did not start; see pdms logs {name}", name=service.name) + "[/]")


@events_app.command("down", help=_("Stop the local ElasticMQ (its messages are lost)."))
def events_down() -> None:
    for inst in actions.event_consumers():
        with console.status(_("Stopping {key}...", key=inst.key)):
            actions.stop_service(inst)
        console.print("[green]✓[/] " + _("{key} stopped.", key=inst.key))
    if events.stop():
        console.print("[green]✓[/] " + _("ElasticMQ stopped."))
    else:
        console.print(_("ElasticMQ is not running."))


@events_app.command("status", help=_("Show whether ElasticMQ is running and the messages waiting in each queue."))
def events_status(
    all_: bool = typer.Option(False, "--all", "-a", help=_("Also list empty queues.")),
) -> None:
    cfg = Config.load()
    port = cfg.defaults.events_port
    state = events.container_state()
    if not state or not state["running"] or not events.is_up(port):
        console.print(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
        return
    root = repos.active_root(cfg)
    event_map = events.load_event_map(root) if root and root.is_dir() else events.EventMap()
    counts = events.queue_counts(port)
    table = Table(_("Queue"), _("Waiting"), _("In flight"), _("Consumer"))
    for name, count in sorted(counts.items()):
        if name == events.SNS_QUEUE:
            continue  # shown last, always: it is the local SNS, not a queue of the repo
        if not all_ and not count["visible"] and not count["in_flight"]:
            continue
        consumer = event_map.consumers.get(name)
        running = next((i.key for i in instances.load().values() if i.is_consumer and i.queue == name and i.alive()), "")
        table.add_row(name, str(count["visible"]), str(count["in_flight"]),
                      f"[green]● {running}[/]" if running else (f"[dim]{consumer.service}[/]" if consumer else "-"))
    repo_rows = table.row_count
    sns = counts.get(events.SNS_QUEUE, {"visible": 0, "in_flight": 0})
    table.add_row(f"{events.SNS_QUEUE} [dim]({_('local SNS')})[/]", str(sns["visible"]), str(sns["in_flight"]),
                  f"[dim]{_('every SNS publish · pdms events peek {queue}', queue=events.SNS_QUEUE)}[/]")
    console.print("[green]●[/] " + _("ElasticMQ running at {url} · {count} queues", url=events.endpoint(port),
                                     count=len(counts)))
    publishers = [i.key for i in instances.load().values() if i.alive() and i.events == "local"]
    console.print("  " + _("Publishing to the local broker: {names}", names=", ".join(publishers) or _("none")))
    console.print(table)
    if not repo_rows:
        console.print("  [dim]" + _("All queues are empty (--all to list them).") + "[/]")


def require_elasticmq(cfg: Config) -> int:
    port = cfg.defaults.events_port
    if not events.running(port):
        fail(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    return port


@events_app.command("send", help=_("Send an event (through the broker, or --direct to its queue) or a message to a queue."))
def events_send(
    target: str = typer.Argument(..., help=_("Event type (e.g. email-notify) or queue name."),
                                 autocompletion=completion.event_targets),
    body: Optional[str] = typer.Option(None, "--body", "-b", help=_("Event fields / message body as JSON.")),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help=_("Read the JSON from a file ('-' = stdin).")),
    direct: bool = typer.Option(False, "--direct", help=_("Skip the broker: send the event straight to its queue.")),
    template: bool = typer.Option(False, "--template", "-t", help=_("Print the fields of the event type and exit.")),
) -> None:
    cfg = Config.load()
    root, event_map = load_events()
    is_event = target in event_map.routes
    if template:
        settle(lambda: actions.plan_send(event_map, target, None))  # an unknown target says so first
        fields = events.event_template(root, target) if is_event else None
        if fields is None:
            fail(_("No event class found for '{target}' in backend/common/event.", target=target))
        print(json.dumps(fields, indent=2))
        return
    if file is not None:
        raw = sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
    else:
        raw = body
    queue, message = settle(lambda: actions.plan_send(event_map, target, raw, direct))
    require_elasticmq(cfg)
    message_id = settle(lambda: actions.send_message(cfg, event_map, queue, message))
    console.print("[green]✓[/] " + _("Sent {id} to {queue}", id=message_id[:8], queue=queue))
    if is_event and queue == event_map.broker_queue:
        consumer = event_map.consumer_of_type(target)
        console.print("  [dim]" + _("The broker routes it to {queue} (consumer: {consumer}).",
                                    queue=event_map.routes[target], consumer=consumer.service if consumer else "-") + "[/]")
    if not actions.event_consumers(queue):
        console.print("  [yellow]" + _("Nothing is consuming {queue} right now; it waits there (pdms events peek {queue}).",
                                       queue=queue) + "[/]")


@events_app.command("peek", help=_("Show the messages waiting in a queue, without consuming them."))
def events_peek(
    queue: str = typer.Argument(..., help=_("Queue name."), autocompletion=completion.queues),
    limit: int = typer.Option(10, "--limit", "-n", help=_("Maximum number of messages.")),
    full: bool = typer.Option(False, "--full", help=_("Print the whole body of each message.")),
) -> None:
    cfg = Config.load()
    port = require_elasticmq(cfg)
    try:
        messages = events.peek(port, queue, limit)
    except Exception as exc:  # noqa: BLE001 - unknown queue, ElasticMQ error
        fail(_("Could not read {queue}: {error}", queue=queue, error=exc))
    if not messages:
        console.print(_("{queue} is empty.", queue=queue))
        return
    for message in messages:
        body = message.get("Body", "")
        try:
            parsed = json.loads(body)
            kind = parsed.get("type", "-") if isinstance(parsed, dict) else "-"
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            kind, pretty = "-", body
        receives = message["Attributes"].get("ApproximateReceiveCount", "?")
        console.rule(f"{message['MessageId'][:8]} · type={kind} · " + _("received {n} times", n=receives), align="left")
        console.print(pretty if full or len(pretty) < 1500 else pretty[:1500] + " …", markup=False, highlight=False)


@events_app.command("purge", help=_("Delete every message of a queue (or of all of them)."))
def events_purge(
    queue: Optional[str] = typer.Argument(None, help=_("Queue name."), autocompletion=completion.queues),
    all_: bool = typer.Option(False, "--all", "-a", help=_("Purge every queue.")),
    yes: bool = typer.Option(False, "--yes", "-y", help=_("Do not ask for confirmation.")),
) -> None:
    cfg = Config.load()
    port = require_elasticmq(cfg)
    counts = events.queue_counts(port)
    if all_:
        targets = [q for q, c in counts.items() if c["visible"] or c["in_flight"]]
    elif queue:
        if queue not in counts:
            fail(_("Unknown queue '{queue}'. See pdms events status --all.", queue=queue))
        targets = [queue]
    else:
        prompts.require_tty()
        targets = [prompts.select_name(_("Queue:"), sorted(counts))]
    if not targets:
        console.print(_("All queues are empty (--all to list them)."))
        return
    total = sum(counts[q]["visible"] + counts[q]["in_flight"] for q in targets)
    if not yes and not (interactive_terminal() and questionary.confirm(
        _("Delete {count} messages from {queues}?", count=total, queues=", ".join(targets)), default=False
    ).unsafe_ask()):
        raise typer.Exit(1)
    for name in targets:
        events.purge(port, name)
    console.print("[green]✓[/] " + _("Purged {queues}.", queues=", ".join(targets)))


def events_menu() -> None:
    prompts.require_tty()
    show_menu(_("Events (local SQS):"), {
        _("Status"): lambda: events_status(False),
        _("Start ElasticMQ and the broker"): lambda: events_up("dev", True),
        _("Event map"): lambda: events_map("", "dev"),
        _("Peek a queue"): lambda: events_peek(prompts.select_name(_("Queue:"), sorted(load_events()[1].queues)), 10, False),
        _("Purge a queue"): lambda: events_purge(None, False, False),
        _("Stop everything"): events_down,
    })
