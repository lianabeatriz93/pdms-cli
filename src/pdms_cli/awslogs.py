"""What the dev Lambdas wrote to CloudWatch, and the events they sent: ``pdms aws logs`` (read-only).

The Lambdas log at DEBUG, so botocore writes every call before making it: ``Making request for
OperationModel(name=SendMessage) with params: {... 'body': b'{"QueueUrl": ..., "MessageBody": ...}' ...}`` holds the
exact queue and message of an SQS send, and ``OperationModel(name=Publish)`` the topic and message of an SNS publish.
The broker also logs what it receives (``Process events: ... body={...}``). Those parameters are Python reprs: only
the piece that is a literal (the ``'body'`` value, each ``body={...}``) is read, with :func:`ast.literal_eval`, never
run.

Lines are read with ``aws logs filter-log-events`` (the AWS CLI pdms already uses, with the profile of the services),
always within a time range and, unless every line is asked for, with a filter pattern so that AWS only sends back
event lines. There is no Live Tail (it is billed by the minute): following means asking again for what came after
the newest line seen (:func:`follow`), once a minute by default.
"""

from __future__ import annotations

import ast
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import awsenv, events
from .i18n import _

GROUP_PREFIX = "/aws/lambda/"
LIMIT = 500  # lines read per Lambda and call; the rest waits for "load more"
FOLLOW_EVERY = 60  # seconds between two reads while following
OVERLAP = 120_000  # ms read again when following: CloudWatch may store a line a while after its time
READERS = 3  # Lambdas read at once (FilterLogEvents has a small rate limit, shared by the whole account)
TIMEOUT = 180

# Event lines and errors, for the filter pattern AWS applies (any of them; SendMessage and Publish also match their
# Batch). Errors come along: a send that failed (the broker's "Failed event broker") is what one looks for.
EVENT_TERMS = ("OperationModel(name=SendMessage", "OperationModel(name=Publish", "Process events", "ERROR")

# `<logger> 2026-10-19 15:53:37,368 <pid> <thread> <LEVEL> <module>:<line> <message>`: the services' log format.
PYTHON_LINE = re.compile(
    r"(?P<logger>\S+) (?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \d+ \S+ "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<where>\S+) (?P<text>.*)", re.DOTALL)
# What the Lambda runtime writes itself: `[ERROR]\t<time>\t<request id>\t<message>`.
RUNTIME_LINE = re.compile(r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+\S+\s+(?P<request>[0-9a-f-]{36})\s+"
                          r"(?P<text>.*)", re.DOTALL)
PLATFORM = re.compile(r"(?:START|END|REPORT) RequestId: (?P<request>[0-9a-f-]{36})")
PLATFORM_OTHER = re.compile(r"(?:INIT_START|INIT_REPORT|LOGS|EXTENSION|TELEMETRY)\b")
BOTOCORE_CALL = re.compile(r"Making request for OperationModel\(name=(?P<operation>\w+)\) with params: ")
SENDS = ("SendMessage", "SendMessageBatch", "Publish", "PublishBatch")
RECEIVED = re.compile(r"\bbody=(?=\{)")
LEVELS = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARN", "ERROR": "ERROR", "CRITICAL": "ERROR"}
CONTINUATION_MS = 2000  # a line with no header this soon after another of its stream goes on that one (tracebacks)


class LogsError(awsenv.AwsError):
    """A Lambda whose lines could not be read: ``kind`` is denied, missing, throttled or error."""

    def __init__(self, message: str, kind: str = "error", expired: bool = False) -> None:
        super().__init__(message, expired=expired)
        self.kind = kind


@dataclass
class Message:
    """An event a line shows: sent (an SQS send or SNS publish) or received (by the broker)."""

    direction: str  # sent | received
    operation: str  # SendMessage, SendMessageBatch, Publish, PublishBatch or received
    destination: str = ""  # queue or topic name (with .fifo); "" for what the broker received
    kind: str = ""  # sqs | sns
    type: str = ""  # the event's "type" (SQS) or "event" (SNS)
    body: object = None  # the message, decoded when it is JSON (an SNS "data" string too)


@dataclass
class Line:
    at: int  # ms since the epoch (CloudWatch's timestamp)
    function: str
    stream: str
    id: str  # CloudWatch's eventId
    text: str
    level: str = ""  # DEBUG, INFO, WARN, ERROR; "" for the runtime's own lines and unknown ones
    logger: str = ""
    where: str = ""  # module:line
    request: str = ""  # the invocation (Lambda request id), when its START line was read
    platform: bool = False  # START / END / REPORT / INIT_START... of the Lambda runtime
    more: list[str] = field(default_factory=list)  # lines that go on this one (a traceback)
    messages: list[Message] = field(default_factory=list)

    @property
    def raw(self) -> str:
        return "\n".join([self.text, *self.more])

    def to_dict(self) -> dict:
        data = asdict(self)
        data["time"] = local_time(self.at)
        return data


@dataclass
class Read:
    lines: list[Line] = field(default_factory=list)
    errors: dict[str, LogsError] = field(default_factory=dict)  # Lambda → why its lines could not be read
    more: dict[str, str] = field(default_factory=dict)  # Lambda → the token to read its next lines


# --------------------------------------------------------------------------- reading a line


def _literal_at(text: str, start: int) -> object:
    """The Python literal (dict, list, str or bytes) that starts at ``text[start]``, or None when it is not one."""
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
                if depth == 0:
                    return _evaluate(text[start:i + 1])
        elif ch in "'\"":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return _evaluate(text[start:i + 1])
            if depth < 0:
                return None
        elif depth == 0 and ch not in "bB":
            return None  # neither a container nor a (bytes) string
        i += 1
    return None  # cut: CloudWatch keeps at most 256 KB of a line


def _evaluate(source: str) -> object:
    try:
        return ast.literal_eval(source)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def decode(text: object) -> object:
    """A message as JSON when it is (and an SNS ``data`` that is JSON inside it), else as it came."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict) and isinstance(value.get("data"), str):
        try:
            value = {**value, "data": json.loads(value["data"])}
        except ValueError:
            pass
    return value


def _event_type(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    value = body.get("type") or body.get("event") or ""
    return value if isinstance(value, str) else ""


def _name(target: str) -> str:
    """A queue's name from its URL, a topic's from its ARN."""
    return target.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def sent(operation: str, body: object) -> list[Message]:
    """The messages of a botocore call's ``body``: JSON bytes (SQS) or the form's fields (SNS, older SQS)."""
    if isinstance(body, bytes):
        try:
            body = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return [Message("sent", operation, body=body.decode("utf-8", errors="replace"))]
    if not isinstance(body, dict):
        return []
    target = str(body.get("QueueUrl") or body.get("TopicArn") or body.get("TargetArn") or "")
    kind = "sns" if operation.startswith("Publish") else "sqs"
    payloads = [body[key] for key in ("MessageBody", "Message") if key in body]
    payloads += [entry.get("MessageBody", entry.get("Message")) for entry in body.get("Entries") or []
                 if isinstance(entry, dict)]
    payloads += [value for key, value in body.items() if re.search(r"\.(MessageBody|Message)$", str(key))]
    found = []
    for payload in payloads:
        value = decode(payload)
        found.append(Message("sent", operation, _name(target), kind, _event_type(value), value))
    return found


def received(text: str) -> list[Message]:
    """What the broker logs it received: each ``body={...}`` of ``Process events: ... items=[MessageSQS(...)]``."""
    found = []
    for match in RECEIVED.finditer(text):
        body = _literal_at(text, match.end())
        if isinstance(body, dict):
            found.append(Message("received", "received", kind="sqs", type=_event_type(body), body=body))
    return found


def messages_of(text: str) -> list[Message]:
    call = BOTOCORE_CALL.search(text)
    if call:
        if call.group("operation") not in SENDS:
            return []
        start = text.find("'body': ", call.end())
        return sent(call.group("operation"), _literal_at(text, start + 8)) if start >= 0 else []
    if "Process events" in text:
        return received(text)
    return []


def parse(event: dict, function: str) -> Line:
    """One CloudWatch event (``timestamp``, ``message``, ``logStreamName``, ``eventId``) as a :class:`Line`."""
    text = str(event.get("message", "")).rstrip("\n")
    line = Line(int(event.get("timestamp") or 0), function, str(event.get("logStreamName", "")),
                str(event.get("eventId", "")), text)
    if match := PYTHON_LINE.match(text):
        line.level, line.logger, line.where = LEVELS[match.group("level")], match.group("logger"), match.group("where")
        line.messages = messages_of(match.group("text"))
    elif match := RUNTIME_LINE.match(text):
        line.level, line.request = LEVELS[match.group("level")], match.group("request")
    elif match := PLATFORM.match(text):
        line.platform, line.request = True, match.group("request")
    elif PLATFORM_OTHER.match(text):
        line.platform = True
    return line


def has_header(line: Line) -> bool:
    return bool(line.level or line.platform)


def assemble(found: list[Line]) -> list[Line]:
    """Lines in time order, each with its invocation (from its stream's START) and with the header-less lines that
    go on it (a traceback is one CloudWatch event per line) put in its ``more``."""
    found = sorted(found, key=lambda line: (line.at, line.id))
    last: dict[tuple[str, str], Line] = {}
    request: dict[tuple[str, str], str] = {}
    kept = []
    for line in found:
        stream = (line.function, line.stream)
        if line.platform and line.text.startswith("START ") and line.request:
            request[stream] = line.request
        previous = last.get(stream)
        if not has_header(line) and previous and line.at - previous.at <= CONTINUATION_MS:
            previous.more.append(line.text)
            continue
        line.request = line.request or request.get(stream, "")
        if line.platform and line.text.startswith("END "):
            request.pop(stream, None)
        last[stream] = line
        kept.append(line)
    return kept


# --------------------------------------------------------------------------- what to read


def log_group(function: str) -> str:
    return GROUP_PREFIX + function


def pattern(grep: str = "", event_type: str = "", events_only: bool = True) -> str:
    """The filter pattern AWS applies: the text and the event type (both must be there), else any event line."""
    terms = [term.replace('"', "").strip() for term in (grep, event_type)]
    terms = [f'"{term}"' for term in terms if term]
    if terms:
        return " ".join(terms)
    return " ".join(f'?"{term}"' for term in EVENT_TERMS) if events_only else ""


def keep(line: Line, events_only: bool = True, event_type: str = "", level: str = "", grep: str = "") -> bool:
    """What AWS's filter cannot say: only event lines (and errors), the event type exactly, a level, the text in any
    case."""
    if events_only and not line.messages and line.level != "ERROR":
        return False
    if event_type and not any(m.type == event_type for m in line.messages):
        return False
    if level and line.level != LEVELS.get(level.upper(), level.upper()):
        return False
    return not grep or grep.lower() in line.raw.lower()


DURATION = re.compile(r"(\d+)\s*(s|m|min|h|d)", re.IGNORECASE)
UNITS = {"s": "seconds", "m": "minutes", "min": "minutes", "h": "hours", "d": "days"}


def since(text: str) -> timedelta:
    """``15m``, ``1h``, ``2d``, ``30s`` as a span of time."""
    match = DURATION.fullmatch(text.strip())
    if not match:
        raise ValueError(_("{text} is not a span of time: use 30s, 15m, 1h or 2d.", text=text))
    return timedelta(**{UNITS[match.group(2).lower()]: int(match.group(1))})


def moment(text: str, today: datetime | None = None) -> datetime:
    """``2026-11-02 09:00`` (or with seconds, or ``T``) or ``09:00`` (today), in local time."""
    text = text.strip().replace("T", " ")
    today = today or datetime.now().astimezone()
    for layout in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, layout).replace(tzinfo=today.tzinfo)
        except ValueError:
            pass
    for layout in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(text, layout)
        except ValueError:
            continue
        return today.replace(hour=clock.hour, minute=clock.minute, second=clock.second, microsecond=0)
    raise ValueError(_("{text} is not a date: use 2026-11-02 09:00, 2026-11-02 or 09:00 (today).", text=text))


def millis(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def local_time(ms: int) -> str:
    """The moment in local time; in UTC where the system cannot say it (Windows, for times near 1970)."""
    at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    try:
        return at.astimezone().isoformat(timespec="milliseconds")
    except (OSError, OverflowError, ValueError):
        return at.isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- asking AWS


def _error(function: str, exc: awsenv.AwsError) -> LogsError:
    text = exc.message
    if exc.expired:
        return LogsError(text, "expired", expired=True)
    if "AccessDenied" in text or "not authorized" in text:
        return LogsError(_("Your AWS role cannot read the logs of {function} (logs:FilterLogEvents).",
                           function=function), "denied")
    if "ResourceNotFound" in text:
        return LogsError(_("{function} has no logs: it does not exist or never ran.", function=function), "missing")
    if "Throttling" in text or "Rate exceeded" in text:
        return LogsError(_("AWS asked to wait before reading {function} again (too many reads in the account).",
                           function=function), "throttled")
    return LogsError(text)


def read_one(profile: str, function: str, start: int, end: int | None = None, filter_pattern: str = "",
             limit: int = LIMIT, token: str = "") -> tuple[list[Line], str]:
    """The first ``limit`` lines of ``function`` from ``start`` (ms) to ``end``, and the token to read the next."""
    args = ["logs", "filter-log-events", "--log-group-name", log_group(function), "--start-time", str(start),
            "--max-items", str(limit)]
    if end is not None:
        args += ["--end-time", str(end)]
    if filter_pattern:
        args += ["--filter-pattern", filter_pattern]
    if token:
        args += ["--starting-token", token]
    try:
        out = awsenv._aws(profile, args, TIMEOUT)
    except awsenv.AwsError as exc:
        raise _error(function, exc) from exc
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError as exc:
        raise LogsError(_("AWS answered something that is not JSON.")) from exc
    return [parse(event, function) for event in data.get("events") or []], str(data.get("NextToken") or "")


def read(profile: str, functions: list[str], start: int, end: int | None = None, filter_pattern: str = "",
         limit: int = LIMIT, tokens: dict[str, str] | None = None) -> Read:
    """The lines of several Lambdas (a few at a time), in time order. A session that is over stops everything
    (raised); a Lambda that cannot be read only goes to ``errors``."""
    tokens = tokens or {}
    result = Read()

    def one(function: str) -> tuple[str, list[Line], str, LogsError | None]:
        try:
            lines, token = read_one(profile, function, start, end, filter_pattern, limit, tokens.get(function, ""))
        except LogsError as exc:
            return function, [], "", exc
        return function, lines, token, None

    with ThreadPoolExecutor(max_workers=READERS) as pool:
        answers = list(pool.map(one, functions))
    for function, lines, token, error in answers:
        if error and error.expired:
            raise error
        if error:
            result.errors[function] = error
        result.lines += lines
        if token:
            result.more[function] = token
    result.lines = assemble(result.lines)
    return result


def follow(profile: str, functions: list[str], after: int, seen: dict[str, int], filter_pattern: str = "",
           limit: int = LIMIT) -> Read:
    """One round of following: the lines from ``after`` (ms: when the previous round asked) with :data:`OVERLAP`,
    without those already shown. ``seen`` (line id → its time) is updated here and forgets what the overlap can no
    longer bring back. The caller waits :data:`FOLLOW_EVERY` between two rounds."""
    start = max(0, after - OVERLAP)
    result = read(profile, functions, start, None, filter_pattern, limit)
    result.lines = [line for line in result.lines if line.id not in seen]
    for old in [key for key, at in seen.items() if at < start]:
        del seen[old]
    seen.update((line.id, line.at) for line in result.lines)
    return result


def lambdas(profile: str) -> list[str]:
    """The Lambdas of the account that have logs (their log groups), by name."""
    try:
        out = awsenv._aws(profile, ["logs", "describe-log-groups", "--log-group-name-prefix", GROUP_PREFIX,
                                    "--query", "logGroups[].logGroupName"], TIMEOUT)
    except awsenv.AwsError as exc:
        raise _error(GROUP_PREFIX, exc) from exc
    try:
        names = json.loads(out) or []
    except ValueError as exc:
        raise LogsError(_("AWS answered something that is not JSON.")) from exc
    return sorted(name[len(GROUP_PREFIX):] for name in names if isinstance(name, str) and name.startswith(GROUP_PREFIX))


# --------------------------------------------------------------------------- which Lambdas take part in events


@dataclass
class Suggestion:
    function: str
    role: str  # broker | sns | consumer
    detail: str = ""  # the topics it publishes to, or the queue it consumes


def suggestions(root: Path) -> list[Suggestion]:
    """The Lambdas of the repo's dev Terraform that take part in events: the broker, those that publish to SNS and
    each queue's consumer (a Lambda only once, in that order)."""
    names = awsenv.terraform_names(root)
    event_map = events.load_event_map(root)
    found: dict[str, Suggestion] = {}
    for function in names.get(events.BROKER_SERVICE, []):
        found.setdefault(function, Suggestion(function, "broker", event_map.broker_queue))
    for service, variables in sorted(event_map.topic_variables.items()):
        for function in names.get(service, []):
            found.setdefault(function, Suggestion(function, "sns", ", ".join(sorted(set(variables.values())))))
    for queue, consumer in sorted(event_map.consumers.items()):
        for function in names.get(consumer.service, []):
            found.setdefault(function, Suggestion(function, "consumer", queue))
    return list(found.values())


def event_flow(root: Path) -> list[str]:
    """What ``pdms aws logs`` reads when no Lambda is named: the broker and the SNS publishers."""
    return [s.function for s in suggestions(root) if s.role in ("broker", "sns")]
