"""The request a service works for, carried from the proxy through events, so pdms ui can follow one request.

* The background proxy sends ``X-Request-Id`` (the id of its capture). While the service answers that request, the
  id is current (a context variable: it follows the request into threads and tasks) and every line the service
  logs ends with `` #<id>``.
* With local events, every SQS message sent while an id is current carries it as the message attribute
  ``pdms-request-id``; pdms_sqs_poller.py makes it current again while the consumer handles the message, so the
  broker passes it on to the queue it routes to, and so on.
* Each SQS send and SNS publish, and each consumer run (pdms_sqs_poller.py), is written with its exact time to
  ``PDMS_TRACE_LOG`` (one JSON line each), which pdms ui adds to the request's waterfall.
"""

import contextvars
import functools
import json
import logging
import os
import time

HEADER = b"x-request-id"
ATTRIBUTE = "pdms-request-id"
LOG_LIMIT = 5 * 1024 * 1024  # then the file starts again, keeping the previous one as <file>.1
MAX_ATTRIBUTES = 10  # SQS refuses more message attributes

current = contextvars.ContextVar("pdms_request_id", default="")


def trace_log():
    return os.environ.get("PDMS_TRACE_LOG", "")


def record(kind, name, started, seconds, **extra):
    """One line of the trace: what ``kind`` of step (sqs, sns, consumer), on what, when (epoch) and how long."""
    path, request = trace_log(), current.get()
    if not path or not request:
        return
    entry = {"id": request, "kind": kind, "service": os.path.basename(os.getcwd()), "name": name,
             "at": round(started, 4), "ms": round(seconds * 1000, 1), **extra}
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, f"{path}.1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:  # one write per line: several services append at once
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def from_attributes(attributes):
    """The id an SQS message carries (``messageAttributes`` as boto or a Lambda record has them), or ""."""
    value = (attributes or {}).get(ATTRIBUTE) or {}
    return value.get("StringValue") or value.get("stringValue") or ""


def _with_id(attributes):
    request = current.get()
    attributes = dict(attributes or {})
    if request and ATTRIBUTE not in attributes and len(attributes) < MAX_ATTRIBUTES:
        attributes[ATTRIBUTE] = {"DataType": "String", "StringValue": request}
    return attributes


def carry_in_sqs(params, **kwargs):
    """``before-parameter-build`` of SendMessage / SendMessageBatch: the current id goes with the message."""
    if not current.get():
        return
    if "Entries" in params:
        params["Entries"] = [{**entry, "MessageAttributes": _with_id(entry.get("MessageAttributes"))}
                             for entry in params["Entries"]]
    else:
        params["MessageAttributes"] = _with_id(params.get("MessageAttributes"))


def _target(params, context, **kwargs):
    """``before-parameter-build``: what the call goes to (after-call does not get the parameters)."""
    target = params.get("QueueUrl") or params.get("TopicArn") or params.get("TargetArn") or ""
    context["pdms_trace_target"] = target.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    context["pdms_trace_messages"] = len(params.get("Entries") or params.get("PublishBatchRequestEntries") or []) or 1
    # Timed from here: a before-call handler may answer the call itself (a local SNS, a test's stub) and stop the rest
    context["pdms_trace_started"] = time.time()


def _sent(kind):
    def after(model, context, **kwargs):
        started = context.get("pdms_trace_started")
        if started:
            record(kind, context.get("pdms_trace_target", ""), started, time.time() - started, operation=model.name,
                   messages=context.get("pdms_trace_messages", 1))
    return after


def watch_client(client, service_name, local_events):
    """Time the SQS sends and SNS publishes of a boto client (and, with local events, pass the id on in SQS)."""
    operations = {"sqs": ("SendMessage", "SendMessageBatch"), "sns": ("Publish", "PublishBatch")}.get(service_name)
    if not operations:
        return
    events = client.meta.events
    for operation in operations:
        if service_name == "sqs" and local_events:
            events.register(f"before-parameter-build.sqs.{operation}", carry_in_sqs)
        events.register(f"before-parameter-build.{service_name}.{operation}", _target)
        events.register(f"after-call.{service_name}.{operation}", _sent(service_name))


def tag_log_records():
    """A record logged while an id is current ends its first line with `` #<id>`` (whatever the formatter)."""
    factory = logging.getLogRecordFactory()
    if getattr(factory, "_pdms_trace", False):
        return

    def make_record(*args, **kwargs):
        made = factory(*args, **kwargs)
        made.pdms_request_id = current.get()
        return made

    original_format = logging.Formatter.format

    def format(self, record):
        text = original_format(self, record)
        request = getattr(record, "pdms_request_id", "")
        if not request:
            return text
        first, sep, rest = text.partition("\n")
        return f"{first} #{request}{sep}{rest}"

    setattr(make_record, "_pdms_trace", True)
    logging.setLogRecordFactory(make_record)
    logging.Formatter.format = format


def follow_requests():
    import starlette.applications

    original = starlette.applications.Starlette.__call__
    if getattr(original, "_pdms_trace", False):
        return

    @functools.wraps(original)  # keeps the marks of the wrappers inside (pdms_queries, pdms_parallel)
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await original(self, scope, receive, send)
        request = next((value.decode("latin-1") for key, value in scope.get("headers") or [] if key == HEADER), "")
        if not request or current.get():
            return await original(self, scope, receive, send)
        token = current.set(request[:64])
        try:
            return await original(self, scope, receive, send)
        finally:
            current.reset(token)

    setattr(__call__, "_pdms_trace", True)
    starlette.applications.Starlette.__call__ = __call__


def install():
    tag_log_records()
    try:
        follow_requests()
    except ImportError:  # not a web service (a consumer: pdms_sqs_poller.py sets the id itself)
        pass
