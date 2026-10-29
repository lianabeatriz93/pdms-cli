"""The local SQS events: ElasticMQ, its queues and messages."""

from __future__ import annotations

from ... import actions, events, instances
from ...config import Config
from ...i18n import _
from .core import EVENTS_KEY, Job
from .services import ServiceJobs

__all__ = [
    "EventJobs", "events_map", "events_peek", "events_purge", "events_queues", "events_ready", "events_send",
    "events_template",
]


def events_queues(cfg: Config) -> dict:
    """Every queue of the repo (and any other ElasticMQ has) with its messages, consumer and the event types the
    broker routes to it, like ``pdms events status --all``."""
    root, event_map = actions.load_events(cfg)
    port = cfg.defaults.events_port
    up = events.is_up(port)
    counts = events.queue_counts(port) if up else {}
    running = {i.queue: i.key for i in actions.event_consumers()}
    types: dict[str, int] = {}
    for queue in event_map.routes.values():
        types[queue] = types.get(queue, 0) + 1
    names = sorted(set(event_map.queues) | set(counts))
    queues = []
    for name in names:
        queue = event_map.queues.get(name) or events.Queue(name, fifo=name.endswith(".fifo"), source="elasticmq")
        if name == events.SNS_QUEUE:
            queue = events.Queue(name, fifo=False, source="pdms")
        consumer = event_map.consumers.get(name)
        count = counts.get(name)
        queues.append({
            "name": name, "fifo": queue.fifo, "source": queue.source, "types": types.get(name, 0),
            "visible": count["visible"] if count else None, "in_flight": count["in_flight"] if count else None,
            "consumer": consumer.service if consumer else "", "running": running.get(name, ""),
            "broker": name == event_map.broker_queue, "sns": name == events.SNS_QUEUE,
        })
    publishers = [i.key for i in instances.load().values() if i.alive() and i.events == "local"]
    return {"up": up, "endpoint": events.endpoint(port), "queues": queues, "publishers": publishers,
            "broker": event_map.broker_queue, "broker_service": bool(actions.broker_service(root, event_map))}


def events_ready(cfg: Config) -> dict:
    """What starting the local events needs (Docker running, its port free) and a real route of the repo, for the
    Events screen while they are off."""
    docker_ok, docker_version = events.docker_available()
    port = cfg.defaults.events_port
    example = None
    try:
        _root, event_map = actions.load_events(cfg)
    except actions.ActionError:
        event_map = None
    if event_map:
        for event_type, queue in sorted(event_map.routes.items()):
            consumer = event_map.consumers.get(queue)
            if consumer and queue != event_map.broker_queue:
                example = {"type": event_type, "queue": queue, "consumer": consumer.service.rsplit("/", 1)[-1],
                           "broker": event_map.broker_queue}
                break
    return {
        "docker": {"ok": docker_ok, "version": docker_version},
        "port": {"port": port, "free": events.is_up(port) or actions.port_available("127.0.0.1", port)},
        "example": example,
        "queues": len(event_map.queues) if event_map else 0,
    }


def events_map(cfg: Config) -> dict:
    """Every event type, the queue the broker sends it to and its consumer, like ``pdms events map``."""
    _root, event_map = actions.load_events(cfg)
    return {
        "broker": event_map.broker_queue,
        "types": [
            {"type": event_type, "queue": queue,
             "consumer": consumer.service if (consumer := event_map.consumers.get(queue)) else ""}
            for event_type, queue in sorted(event_map.routes.items())
        ],
    }


def events_template(cfg: Config, event_type: str) -> dict:
    root, event_map = actions.load_events(cfg)
    if event_type not in event_map.routes:
        raise actions.ActionError(_("'{target}' is not an event type nor a queue.", target=event_type))
    fields = events.event_template(root, event_type)
    if fields is None:
        raise actions.ActionError(_("No event class found for '{target}' in backend/common/event.", target=event_type))
    return fields


def events_peek(cfg: Config, queue: str, limit: int) -> list[dict]:
    """The messages waiting in ``queue``, without consuming them (``pdms events peek``)."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    if queue not in events.queue_counts(port):
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=queue))
    found = []
    for message in events.peek(port, queue, limit):
        attributes = message.get("Attributes", {})
        sent = attributes.get("SentTimestamp", "")
        found.append({
            "id": message.get("MessageId", ""), "body": message.get("Body", ""),
            "receives": int(attributes.get("ApproximateReceiveCount", 0) or 0),
            "sent": int(sent) if sent.isdigit() else None, "group": attributes.get("MessageGroupId", ""),
        })
    return found


def events_send(cfg: Config, target: str, body: str | None, direct: bool) -> dict:
    """Send an event (through the broker unless ``direct``) or a message to a queue, like ``pdms events send``."""
    _root, event_map = actions.load_events(cfg)
    queue, message = actions.plan_send(event_map, target, body, direct)
    message_id = actions.send_message(cfg, event_map, queue, message)
    routed = event_map.routes.get(target, "") if queue == event_map.broker_queue and target != queue else ""
    consumer = event_map.consumers.get(routed or queue)
    return {"id": message_id, "queue": queue, "routed_to": routed, "consumer": consumer.service if consumer else "",
            "consumed": bool(actions.event_consumers(queue))}


def events_purge(cfg: Config, queues: list[str]) -> list[str]:
    """Delete every message of ``queues`` (all of them that have messages when empty), like ``pdms events purge``."""
    port = cfg.defaults.events_port
    if not events.is_up(port):
        raise actions.ActionError(_("ElasticMQ is not running. Start it with [bold]pdms events up[/]."))
    counts = events.queue_counts(port)
    unknown = [name for name in queues if name not in counts]
    if unknown:
        raise actions.ActionError(_("Unknown queue '{queue}'. See pdms events status --all.", queue=unknown[0]))
    targets = queues or [name for name, count in counts.items() if count["visible"] or count["in_flight"]]
    for name in targets:
        events.purge(port, name)
    return targets


class EventJobs(ServiceJobs):
    def events_up(
        self, *, broker: bool = True, user: str | None = None, db: str | None = None, install: bool | None = None,
        confirmed: bool = False, python: bool | None = None, migrate: bool | None = None, views: bool | None = None,
    ) -> Job:
        """Start the local ElasticMQ with every queue of the repo and, with ``broker``, the broker, like
        ``pdms events up``; a protected database for the broker is asked before anything starts."""
        cfg = Config.load()
        root, event_map = actions.load_events(cfg)
        service = actions.broker_service(root, event_map) if broker else None
        if service and actions.event_consumers(event_map.broker_queue):
            service = None  # already running
        if service:
            user, db = user or cfg.last_user, db or cfg.last_db
            if not user or not db:
                raise actions.ActionError(_("Configure a user and a database to run the broker (pdms user add, "
                                            "pdms db add), or start the events without it."))
            actions.require(cfg.users, _("user"), user)
            actions.require(cfg.dbs, _("database"), db)
            actions.check_database(cfg, db, confirmed)
        recreate = actions.check_pythons(cfg, [service] if service else [], install, python)
        behind = actions.check_migrations(cfg, [db] if service and db else [], migrate)
        needs = actions.check_test_views(cfg, [(service, db)] if service and db else [], views)
        if service:
            actions.remember_profile(cfg, user, db)

        def work(job: Job) -> None:
            self.migrate_first(job, behind)
            self.views_first(cfg, job, needs)
            actions.start_events(cfg, event_map.queues)
            if service and user and db:
                launch = actions.plan_service(cfg, service, user_name=user, db_name=db, events_mode="local")
                key = instances.make_key(launch.service, launch.port)
                self.install(cfg, job, service, key, install, service.name, recreate)
                self.phase(job, f"starting {service.name}")
                actions.start_service(cfg, launch)

        return self.run(EVENTS_KEY, "up", "starting ElasticMQ", work)

    def events_down(self) -> Job:
        """Stop the SQS consumers and the local ElasticMQ (its messages are lost), like ``pdms events down``."""

        def work(job: Job) -> None:
            for inst in actions.event_consumers():
                self.phase(job, f"stopping {inst.name}")
                actions.stop_service(inst)
            self.phase(job, "stopping ElasticMQ")
            events.stop()

        return self.run(EVENTS_KEY, "down", "stopping", work)
