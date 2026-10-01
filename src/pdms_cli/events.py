"""SQS event map of a PDMS repo, and a local ElasticMQ to run it.

Where things come from (nothing has to be registered by hand):

* **Queues** — ``aws_sqs_queue`` resources in the repo's Terraform (name, FIFO, visibility timeout, resolving
  ``var.*`` to their defaults), plus any extra queue declared in ``infra/local_sqs/elasticmq.conf`` for events that
  have no Terraform yet.
* **Consumers** — ``aws_lambda_event_source_mapping`` → the Lambda module's ``lambda_path`` and ``handler``.
* **Routing** — the broker publishes each event to the queue of its ``type``: ``EVENT_ROUTE_DEST`` and the
  ``EventType`` enum in ``backend/common/event`` (parsed, not imported) plus the broker Lambda's environment in
  Terraform (``SQS_EMAIL_NOTIFY = …aws_sqs_queue.email_notify.name``).
* **SNS topics** — each Lambda's environment variables that name an ``aws_sns_topic`` (``SNS_…_ARN =
  aws_sns_topic.sns_account_topic.arn``). Locally there is one SNS for all of them: what any service publishes ends
  up in the ``pdms-sns`` queue (see ``sqs_patch/sitecustomize.py``).

The map is cached until one of those files changes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .instances import state_dir
from .routes import terraform_dir

CACHE_VERSION = 2
ACCOUNT_ID = "000000000000"
REGION = "us-east-1"
SNS_QUEUE = "pdms-sns"  # every SNS publish of every topic, locally
BROKER_SERVICE = "broker/broker-sqs-event"
BROKER_URL_VARIABLE = "SQS_EVENT_BROKER_URL"
EVENT_SETTINGS = Path("backend/common/event/event/settings.py")
EVENT_MODELS = Path("backend/common/event/event/domain/models.py")
LOCAL_SQS_CONF = Path("infra/local_sqs/elasticmq.conf")
CONTAINER = "pdms-elasticmq"
IMAGE = "softwaremill/elasticmq-native:latest"

VAR = re.compile(r"var\.([A-Za-z0-9_]+)")
QUEUE_REF = re.compile(r"aws_sqs_queue\.([A-Za-z0-9_]+)\.(?:name|arn|url|id)")
TOPIC_REF = re.compile(r"aws_sns_topic\.([A-Za-z0-9_]+)\.(?:name|arn|id)")
MODULE_REF = re.compile(r"module\.([A-Za-z0-9_-]+)(?:\[\d+\])?\.")


@dataclass
class Queue:
    name: str
    fifo: bool = True
    visibility_timeout: int = 60
    source: str = "terraform"  # terraform | elasticmq.conf


@dataclass
class Consumer:
    service: str  # relative to the backend folder, e.g. notification/email-notify
    handler: str  # module.function, e.g. main.lambda_handler


@dataclass
class EventMap:
    queues: dict[str, Queue] = field(default_factory=dict)
    consumers: dict[str, Consumer] = field(default_factory=dict)  # queue name -> consumer
    routes: dict[str, str] = field(default_factory=dict)  # event type -> queue name
    broker_queue: str = ""
    # Broker environment variable -> queue name (what the broker needs to route locally).
    broker_destinations: dict[str, str] = field(default_factory=dict)
    # Service (relative to the backend folder) -> its environment variable -> the SNS topic name it names.
    topic_variables: dict[str, dict[str, str]] = field(default_factory=dict)

    def consumer_of_type(self, event_type: str) -> Consumer | None:
        queue = self.routes.get(event_type)
        return self.consumers.get(queue) if queue else None

    def queue_of_service(self, service: str) -> tuple[str, Consumer] | None:
        """``(queue, consumer)`` when the service (relative to the backend folder) is an SQS consumer."""
        for queue, consumer in sorted(self.consumers.items()):
            if consumer.service == service:
                return queue, consumer
        return None


# ---------------------------------------------------------------------- parsing


def _unquote(value: object) -> str:
    text = str(value).strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def _resolve(value: object, variables: dict[str, str]) -> str:
    text = _unquote(value)
    match = VAR.fullmatch(text.strip("${} ")) or VAR.search(text)
    if match and match.group(1) in variables:
        return variables[match.group(1)]
    return text


def _parse_tf_file(path: str) -> tuple[dict, dict, list, dict, dict]:
    import hcl2

    variables, queues, mappings, modules, topics = {}, {}, [], {}, {}
    try:
        data = hcl2.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken file only loses its own definitions
        return variables, queues, mappings, modules, topics
    for block in data.get("variable", []):
        for name, body in block.items():
            if "default" in body:
                variables[_unquote(name)] = _unquote(body["default"])
    for block in data.get("resource", []):
        for kind, items in block.items():
            for name, body in items.items():
                kind, name = _unquote(kind), _unquote(name)
                if kind == "aws_sqs_queue":
                    queues[name] = body
                elif kind == "aws_sns_topic":
                    topics[name] = body
                elif kind == "aws_lambda_event_source_mapping":
                    mappings.append(body)
    for block in data.get("module", []):
        for name, body in block.items():
            modules[_unquote(name)] = (path, body)
    return variables, queues, mappings, modules, topics


def _terraform_blocks(directory: Path) -> tuple[dict, dict, list, dict, dict]:
    files = [str(p) for p in sorted(directory.glob("*.tf"))]
    if len(files) < 40:
        parsed = [_parse_tf_file(f) for f in files]
    else:
        try:
            with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
                parsed = list(pool.map(_parse_tf_file, files, chunksize=8))
        except Exception:  # noqa: BLE001 - worker processes not available: parse here
            parsed = [_parse_tf_file(f) for f in files]
    variables, queues, mappings, modules, topics = {}, {}, [], {}, {}
    for file_variables, file_queues, file_mappings, file_modules, file_topics in parsed:
        variables.update(file_variables)
        queues.update(file_queues)
        mappings.extend(file_mappings)
        modules.update({name: (Path(path), body) for name, (path, body) in file_modules.items()})
        topics.update(file_topics)
    return variables, queues, mappings, modules, topics


def _event_routes(root: Path) -> dict[str, str]:
    """Event type value -> settings variable name, from EVENT_ROUTE_DEST and the EventType enum."""
    try:
        settings = ast.parse((root / EVENT_SETTINGS).read_text(encoding="utf-8"))
        models = ast.parse((root / EVENT_MODELS).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    types = {}
    for node in ast.walk(models):
        if isinstance(node, ast.ClassDef) and node.name == "EventType":
            types = {
                t.targets[0].id: t.value.value for t in node.body
                if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name) and isinstance(t.value, ast.Constant)
            }
    routes = {}
    for node in settings.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "EVENT_ROUTE_DEST" for t in node.targets):
            if isinstance(node.value, ast.Dict):
                for key, value in zip(node.value.keys, node.value.values):
                    member = key.value.attr if isinstance(key, ast.Attribute) and isinstance(key.value, ast.Attribute) \
                        else getattr(key, "attr", None)
                    if member and isinstance(value, ast.Name):
                        routes[types.get(member, member)] = value.id
    return routes


def _local_conf_queues(root: Path) -> dict[str, Queue]:
    try:
        text = (root / LOCAL_SQS_CONF).read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for match in re.finditer(r'^\s*"([^"]+)"\s*\{([^}]*)\}', text, re.M):
        name, body = match.groups()  # commented-out blocks start with '#' and do not match
        timeout = re.search(r"defaultVisibilityTimeout\s*=\s*(\d+)", body)
        found[name] = Queue(name, fifo=name.endswith(".fifo"), visibility_timeout=int(timeout.group(1)) if timeout else 60,
                            source="elasticmq.conf")
    return found


def discover(root: Path, env: str = "dev") -> EventMap:
    directory = terraform_dir(root, env)
    variables, raw_queues, mappings, modules, raw_topics = (
        _terraform_blocks(directory) if directory.is_dir() else ({}, {}, [], {}, {})
    )
    event_map = EventMap()
    resource_names = {}
    for resource, body in raw_queues.items():
        name = _resolve(body.get("name", resource), variables)
        timeout = _resolve(body.get("visibility_timeout_seconds", 60), variables)
        fifo = str(_resolve(body.get("fifo_queue", "false"), variables)).lower() == "true" or name.endswith(".fifo")
        event_map.queues[name] = Queue(name, fifo=fifo, visibility_timeout=int(timeout) if str(timeout).isdigit() else 60)
        resource_names[resource] = name

    backend = (root / "backend").resolve()
    for mapping in mappings:
        queue_ref = QUEUE_REF.search(str(mapping.get("event_source_arn", "")))
        module_ref = MODULE_REF.search(str(mapping.get("function_name", "")))
        if not queue_ref or not module_ref or queue_ref.group(1) not in resource_names:
            continue
        module = modules.get(module_ref.group(1))
        if not module or "lambda_path" not in module[1]:
            continue
        path, body = module
        service_dir = (path.parent / _unquote(body["lambda_path"])).resolve()
        try:
            service = service_dir.relative_to(backend).as_posix()
        except ValueError:
            continue
        queue = resource_names[queue_ref.group(1)]
        event_map.consumers[queue] = Consumer(service, _unquote(body.get("handler", "main.lambda_handler")))
        if service == BROKER_SERVICE:
            event_map.broker_queue = queue

    for name, (_path, body) in modules.items():
        service_dir = _unquote(body.get("lambda_path", "")).rstrip("/")
        if service_dir.endswith(BROKER_SERVICE):
            for key, value in (body.get("environment_variables") or {}).items():
                ref = QUEUE_REF.search(str(value))
                if ref and ref.group(1) in resource_names:
                    event_map.broker_destinations[_unquote(key)] = resource_names[ref.group(1)]

    topic_names = {resource: _resolve(body.get("name", resource), variables) for resource, body in raw_topics.items()}
    for path, body in modules.values():
        if "lambda_path" not in body:
            continue
        try:
            service = (path.parent / _unquote(body["lambda_path"])).resolve().relative_to(backend).as_posix()
        except ValueError:
            continue
        for key, value in (body.get("environment_variables") or {}).items():
            ref = TOPIC_REF.search(str(value))
            if ref and ref.group(1) in topic_names:
                event_map.topic_variables.setdefault(service, {})[_unquote(key)] = topic_names[ref.group(1)]

    for event_type, variable in _event_routes(root).items():
        if variable in event_map.broker_destinations:
            event_map.routes[event_type] = event_map.broker_destinations[variable]

    for name, queue in _local_conf_queues(root).items():
        event_map.queues.setdefault(name, queue)
    return event_map


def _fingerprint(root: Path, env: str) -> str:
    digest = hashlib.sha256()
    files = sorted(terraform_dir(root, env).glob("*.tf")) + [root / EVENT_SETTINGS, root / EVENT_MODELS, root / LOCAL_SQS_CONF]
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def load_event_map(root: Path, env: str = "dev") -> EventMap:
    root = root.resolve()
    cache = state_dir() / "events" / (hashlib.sha1(f"{root}|{env}".encode()).hexdigest() + ".json")
    fingerprint = _fingerprint(root, env)
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION and data.get("fingerprint") == fingerprint:
            m = data["map"]
            return EventMap(
                queues={k: Queue(**v) for k, v in m["queues"].items()},
                consumers={k: Consumer(**v) for k, v in m["consumers"].items()},
                routes=m["routes"], broker_queue=m["broker_queue"], broker_destinations=m["broker_destinations"],
                topic_variables=m["topic_variables"],
            )
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        pass
    event_map = discover(root, env)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": CACHE_VERSION, "fingerprint": fingerprint, "map": asdict(event_map)}),
                     encoding="utf-8")
    return event_map


# ---------------------------------------------------------------------- ElasticMQ


PATCH_DIR = Path(__file__).resolve().parent / "sqs_patch"
POLLER = PATCH_DIR / "pdms_sqs_poller.py"
EVENT_MODES = ("auto", "local", "aws")


def local_env(event_map: EventMap, port: int) -> dict[str, str]:
    """Variables that make a service publish to (and, for the broker, route through) the local ElasticMQ."""
    env = {name: queue_url(queue, port) for name, queue in event_map.broker_destinations.items()}
    if event_map.broker_queue:
        env[BROKER_URL_VARIABLE] = queue_url(event_map.broker_queue, port)
    env["PDMS_SQS_ENDPOINT"] = endpoint(port)
    env["PDMS_SNS_QUEUE_URL"] = queue_url(SNS_QUEUE, port)
    env["PYTHONPATH"] = str(PATCH_DIR)  # its sitecustomize.py redirects the SQS and SNS clients (see sqs_patch/)
    return env


def topic_arn(name: str) -> str:
    return f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:{name}"


def topic_env(event_map: EventMap, service: str) -> dict[str, str]:
    """The SNS topic ARNs the service's Lambda gets from Terraform, as local ARNs (they show in ``pdms-sns``)."""
    return {name: topic_arn(topic) for name, topic in event_map.topic_variables.get(service, {}).items()}


def poller_command(poetry: str, queue: Queue, consumer: Consumer, port: int) -> list[str]:
    return [
        poetry, "run", "python", str(POLLER), "--queue-url", queue_url(queue.name, port),
        "--handler", consumer.handler, "--function-name", Path(consumer.service).name,
        "--timeout", str(queue.visibility_timeout),
    ]


def running(port: int) -> bool:
    state = container_state()
    return bool(state and state["running"]) and is_up(port)


def endpoint(port: int) -> str:
    return f"http://localhost:{port}"


def queue_url(name: str, port: int) -> str:
    return f"{endpoint(port)}/{ACCOUNT_ID}/{name}"


def elasticmq_conf(queues: dict[str, Queue], port: int) -> str:
    blocks = []
    for queue in sorted(queues.values(), key=lambda q: q.name):
        lines = [f'  "{queue.name}" {{', f"    defaultVisibilityTimeout = {queue.visibility_timeout} seconds"]
        if queue.fifo:
            lines += ["    fifo = true", "    contentBasedDeduplication = true"]
        blocks.append("\n".join(lines + ["  }"]))
    return f"""# Generated by pdms from the repo's Terraform and infra/local_sqs/elasticmq.conf. Do not edit.
include classpath("application.conf")

node-address {{
  protocol = http
  host = localhost
  port = {port}
  context-path = ""
}}

rest-sqs {{
  enabled = true
  bind-port = 9324
  bind-hostname = "0.0.0.0"
  sqs-limits = relaxed
}}

generate-node-address = false

aws {{
  region = us-east-1
  accountId = {ACCOUNT_ID}
}}

queues {{
{chr(10).join(blocks)}
}}
"""


def conf_path() -> Path:
    return state_dir() / "events" / "elasticmq.conf"


def _docker(*args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def docker_available() -> tuple[bool, str]:
    code, out = _docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    return code == 0, out.splitlines()[-1] if out else ""


def container_state() -> dict | None:
    """``{"running": bool, "conf_hash": str, "port": int}`` of the pdms ElasticMQ container, or None."""
    code, out = _docker("inspect", CONTAINER, "--format",
                        '{{.State.Running}}|{{index .Config.Labels "pdms.conf-hash"}}|{{index .Config.Labels "pdms.port"}}')
    if code != 0:
        return None
    running, conf_hash, port = (out.splitlines()[-1].split("|") + ["", "", ""])[:3]
    return {"running": running == "true", "conf_hash": conf_hash, "port": int(port) if port.isdigit() else 0}


def start(queues: dict[str, Queue], port: int) -> str:
    """Start (or recreate, if the queues changed) the ElasticMQ container. Returns created | restarted | unchanged."""
    conf = elasticmq_conf({**queues, SNS_QUEUE: Queue(SNS_QUEUE, fifo=False, source="pdms")}, port)
    conf_hash = hashlib.sha1(conf.encode()).hexdigest()
    state = container_state()
    if state and state["running"] and state["conf_hash"] == conf_hash and state["port"] == port:
        return "unchanged"
    if state:
        _docker("rm", "-f", CONTAINER)
    path = conf_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(conf, encoding="utf-8")
    code, out = _docker(
        "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{port}:9324",
        "-v", f"{path.resolve()}:/opt/elasticmq.conf:ro",
        "--label", f"pdms.conf-hash={conf_hash}", "--label", f"pdms.port={port}", IMAGE,
        timeout=300,
    )
    if code != 0:
        raise RuntimeError(out.splitlines()[-1] if out else "docker run failed")
    return "restarted" if state else "created"


def stop() -> bool:
    if container_state() is None:
        return False
    _docker("rm", "-f", CONTAINER)
    return True


def _sqs(port: int, params: dict[str, str], url: str | None = None, timeout: float = 5) -> ET.Element:
    query = urllib.parse.urlencode({**params, "Version": "2012-11-05"})
    with urllib.request.urlopen(f"{url or endpoint(port)}/?{query}", timeout=timeout) as response:
        return ET.fromstring(response.read())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def queue_counts(port: int) -> dict[str, dict[str, int]]:
    """Messages per queue: ``{name: {"visible": n, "in_flight": n}}``."""
    root = _sqs(port, {"Action": "ListQueues"})
    urls = [el.text for el in root.iter() if _local(el.tag) == "QueueUrl" and el.text]
    counts = {}
    for url in urls:
        attrs = _sqs(port, {"Action": "GetQueueAttributes", "AttributeName.1": "All"}, url=url)
        values, current = {}, None
        for el in attrs.iter():
            if _local(el.tag) == "Name":
                current = el.text
            elif _local(el.tag) == "Value" and current:
                values[current] = el.text
        counts[url.rsplit("/", 1)[-1]] = {
            "visible": int(values.get("ApproximateNumberOfMessages", 0) or 0),
            "in_flight": int(values.get("ApproximateNumberOfMessagesNotVisible", 0) or 0),
        }
    return counts


def is_up(port: int) -> bool:
    try:
        _sqs(port, {"Action": "ListQueues"}, timeout=2)
        return True
    except Exception:  # noqa: BLE001 - not listening / not ElasticMQ yet
        return False


# ---------------------------------------------------------------------- sending and inspecting messages


def send(port: int, queue: str, body: str, fifo: bool = True) -> str:
    """Send ``body`` to a local queue; returns the message id.

    FIFO messages get a random group (as common/event does) and an explicit deduplication id, so sending the same
    test body twice is not silently dropped by content-based deduplication.
    """
    params = {"Action": "SendMessage", "MessageBody": body}
    if fifo:
        params["MessageGroupId"] = str(uuid.uuid4())
        params["MessageDeduplicationId"] = str(uuid.uuid4())
    root = _sqs(port, params, url=queue_url(queue, port))
    ids = [el.text for el in root.iter() if _local(el.tag) == "MessageId"]
    return ids[0] if ids else ""


def peek(port: int, queue: str, limit: int = 10) -> list[dict]:
    """Messages waiting in a queue, without consuming them (they stay visible for consumers)."""
    found: dict[str, dict] = {}
    for _attempt in range(max(1, (limit + 9) // 10) * 3):
        root = _sqs(port, {
            "Action": "ReceiveMessage", "MaxNumberOfMessages": str(min(10, limit)), "VisibilityTimeout": "0",
            "AttributeName.1": "All", "WaitTimeSeconds": "0",
        }, url=queue_url(queue, port))
        for message in (el for el in root.iter() if _local(el.tag) == "Message"):
            data, attrs, name = {}, {}, None
            for child in message.iter():
                tag = _local(child.tag)
                if tag in ("MessageId", "Body"):
                    data[tag] = child.text or ""
                elif tag == "Name":
                    name = child.text
                elif tag == "Value" and name:
                    attrs[name] = child.text
            if data.get("MessageId"):
                found.setdefault(data["MessageId"], {**data, "Attributes": attrs})
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


def purge(port: int, queue: str) -> None:
    _sqs(port, {"Action": "PurgeQueue"}, url=queue_url(queue, port))


PLACEHOLDERS = {"str": "", "int": 0, "float": 0.0, "bool": False, "list": [], "List": [], "dict": {}, "Dict": {}}


def event_template(root: Path, event_type: str) -> dict | None:
    """A JSON skeleton of the event class whose ``type`` defaults to ``event_type`` (from common/event models)."""
    try:
        models = ast.parse((root / EVENT_MODELS).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    members = {}
    for node in ast.walk(models):
        if isinstance(node, ast.ClassDef) and node.name == "EventType":
            members = {t.targets[0].id: t.value.value for t in node.body
                       if isinstance(t, ast.Assign) and isinstance(t.value, ast.Constant)}
    for node in models.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields, matches = {}, False
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            name = item.target.id
            if name == "type":
                default = item.value
                member = default.attr if isinstance(default, ast.Attribute) else None
                matches = members.get(member) == event_type
                continue
            if item.value is not None:
                try:
                    fields[name] = ast.literal_eval(item.value)
                    continue
                except (ValueError, SyntaxError):
                    pass
            annotation = ast.unparse(item.annotation)
            base = annotation.split("[", 1)[0].split("|", 1)[0].strip()
            fields[name] = None if "None" in annotation else PLACEHOLDERS.get(base)
        if matches:
            return fields
    return None


def event_body(event_type: str, data: dict) -> str:
    """The message a service would publish: common/event's Event serialized as JSON."""
    return json.dumps({"event_id": str(uuid.uuid4()), "app_context": None, **data, "type": event_type})
