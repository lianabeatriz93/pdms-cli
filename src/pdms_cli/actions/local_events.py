"""The local SQS events: the event map of the repo, ElasticMQ and where services publish."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .. import events, instances, repos, runner
from ..config import Config
from ..i18n import _
from .errors import ActionError, LocalEventsDown

__all__ = [
    "EventsSetup", "broker_service", "consumer_of", "event_consumers", "events_setup", "load_events", "plan_send",
    "repo_event_map", "send_message", "start_events",
]


@dataclass
class EventsSetup:
    env: dict[str, str]
    label: str
    kind: str  # "local" or "aws"
    warning: str = ""


def events_setup(cfg: Config, service: Path, mode: str | None) -> EventsSetup:
    """Where the service publishes SQS events: auto, local (pdms events broker) or aws."""
    mode = mode or cfg.defaults.events
    if mode not in events.EVENT_MODES:
        raise ActionError(_("Unknown events mode '{mode}'. Available: {codes}",
                            mode=mode, codes=", ".join(events.EVENT_MODES)))
    if mode == "aws":
        return EventsSetup({}, _("AWS (the service's own configuration)"), "aws")
    port = cfg.defaults.events_port
    if not events.running(port):
        if mode == "auto":
            return EventsSetup({}, _("AWS (run pdms events up to publish locally)"), "aws")
        raise LocalEventsDown(port)
    root = repos.find_repo_root(service) or repos.active_root(cfg)
    event_map = events.load_event_map(root) if root else events.EventMap()
    warning = "" if event_map.broker_queue else _(
        "⚠ No broker queue found in the repo's Terraform; SQS_EVENT_BROKER_URL is left as configured.")
    label = _("local broker · {url}", url=events.queue_url(event_map.broker_queue, port)) \
        if event_map.broker_queue else _("local ElasticMQ · {url}", url=events.endpoint(port))
    return EventsSetup(events.local_env(event_map, port), label, "local", warning)


def repo_event_map(cfg: Config, service: Path) -> tuple[events.EventMap, str] | None:
    """The event map of the service's repo and the service's path in it (``lead/lead-tp-list``)."""
    root = repos.find_repo_root(service) or repos.active_root(cfg)
    if not root:
        return None
    try:
        relative = service.resolve().relative_to((root / "backend").resolve()).as_posix()
    except ValueError:
        return None
    return events.load_event_map(root), relative


def consumer_of(cfg: Config, service: Path) -> tuple[events.Queue, events.Consumer] | None:
    """The queue and handler when ``service`` is an SQS consumer (event Lambda) of its repo."""
    found_map = repo_event_map(cfg, service)
    if not found_map:
        return None
    event_map, relative = found_map
    found = event_map.queue_of_service(relative)
    if not found:
        return None
    queue_name, consumer = found
    return event_map.queues.get(queue_name) or events.Queue(queue_name), consumer


def load_events(cfg: Config, env: str = "dev") -> tuple[Path, events.EventMap]:
    """The current repo and its event map (Terraform and backend/common/event); fails when it has no queues."""
    root = repos.active_root(cfg)
    if root is None or not root.is_dir():
        raise ActionError(_("No current repo. Register one with [bold]pdms repo add <path>[/]."))
    event_map = events.load_event_map(root, env)
    if not event_map.queues:
        raise ActionError(_("No SQS queues found in {path}.", path=root))
    return root, event_map


def start_events(cfg: Config, queues: dict[str, events.Queue], wait: float = 30) -> str:
    """Start (or recreate) the local ElasticMQ with ``queues`` and wait until it answers: created | restarted |
    unchanged, as :func:`events.start`."""
    ok, detail = events.docker_available()
    if not ok:
        raise ActionError(_("Docker is not available: {detail}", detail=detail or _("docker not found")))
    port = cfg.defaults.events_port
    state = events.container_state()
    if not (state and state["running"] and state["port"] == port) and not runner.port_is_free("127.0.0.1", port):
        raise ActionError(_("Port {port} is in use by something else (maybe infra/local_sqs's docker compose). Stop "
                            "it or change events_port in pdms config.", port=port))
    try:
        result = events.start(queues, port)
    except RuntimeError as exc:
        raise ActionError(_("Could not start ElasticMQ: {error}", error=exc)) from exc
    deadline = time.monotonic() + wait
    while not events.is_up(port):
        if time.monotonic() > deadline:
            raise ActionError(_("ElasticMQ did not answer on {url}; see: docker logs {name}",
                                url=events.endpoint(port), name=events.CONTAINER))
        time.sleep(0.5)
    return result


def broker_service(root: Path, event_map: events.EventMap) -> Path | None:
    """broker-sqs-event of the repo, which routes published events to their queues as in AWS, if it has one."""
    service = root / "backend" / events.BROKER_SERVICE
    return service if event_map.broker_queue and runner.is_service(service) else None


def event_consumers(queue: str | None = None) -> list[instances.Instance]:
    """The SQS consumers running now (of ``queue`` only, if given)."""
    return [i for i in instances.load().values() if i.is_consumer and i.alive() and queue in (None, i.queue)]


def plan_send(event_map: events.EventMap, target: str, raw: str | None, direct: bool = False) -> tuple[str, str]:
    """``(queue, message)`` to send ``target``: an event type (``raw`` holds its fields; it goes through the broker
    unless ``direct``) or a queue (``raw`` is the message body, JSON)."""
    if target not in event_map.routes and target not in event_map.queues:
        close = [t for t in sorted(event_map.routes) + sorted(event_map.queues) if target in t][:8]
        raise ActionError(_("'{target}' is not an event type nor a queue.", target=target)
                          + (" " + _("Did you mean: {names}", names=", ".join(close)) if close else " pdms events map"))
    raw = "{}" if raw is None else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionError(_("Invalid JSON: {error}", error=exc)) from exc
    if target not in event_map.routes:
        return target, raw
    if not isinstance(data, dict):
        raise ActionError(_("Event fields must be a JSON object."))
    queue = event_map.routes[target] if direct or not event_map.broker_queue else event_map.broker_queue
    return queue, events.event_body(target, data)


def send_message(cfg: Config, event_map: events.EventMap, queue: str, message: str) -> str:
    """Send ``message`` to a local queue; the message id. Fails when the local ElasticMQ is not running."""
    port = cfg.defaults.events_port
    if not events.running(port):
        raise ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    return events.send(port, queue, message, fifo=event_map.queues.get(queue, events.Queue(queue)).fifo)
