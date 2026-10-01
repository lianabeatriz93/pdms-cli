"""Loaded by Python at startup in services run by pdms with local events (pdms puts this folder on PYTHONPATH).

It sends every SQS client created through botocore to the local ElasticMQ given in ``PDMS_SQS_ENDPOINT``, with
dummy credentials, without touching the service code or other AWS clients (S3 & co. keep their normal endpoint).
PDMS services pin botocore 1.29, which predates ``AWS_ENDPOINT_URL_SQS``, hence the patch. Being a
``sitecustomize``, it also applies in every process uvicorn ``--reload`` spawns.

SNS has no local server: with ``PDMS_SNS_QUEUE_URL`` every ``Publish`` and ``PublishBatch`` of any topic is kept as
one message in that local queue (``pdms events peek pdms-sns``) and answered like AWS would, so nothing leaves the
machine; with ``PDMS_SNS_LOG`` it is also written there, readable (``pdms logs sns`` and the sns row of ``pdms ui``).
Other SNS calls go to ElasticMQ, which rejects them, instead of the real AWS.
"""

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

CAPTURED = "pdms_sns_params"
LOG_LIMIT = 5 * 1024 * 1024  # then the log starts again, keeping the previous one as <log>.1


def _plain(value):
    """Message attributes as JSON (a ``BinaryValue`` becomes base64)."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode()
    return value


def _sqs(queue_url, action, **fields):
    data = urllib.parse.urlencode({"Action": action, "Version": "2012-11-05", **fields}).encode()
    with urllib.request.urlopen(urllib.request.Request(queue_url, data=data), timeout=5) as response:  # nosec B310
        return response.read()


def keep(queue_url, notification):
    """Send ``notification`` to the local SNS queue, creating the queue if ElasticMQ started without it."""
    body = json.dumps(notification, sort_keys=True)
    try:
        _sqs(queue_url, "SendMessage", MessageBody=body)
    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise
        endpoint, name = queue_url.rsplit("/", 2)[0], queue_url.rsplit("/", 1)[1]
        _sqs(endpoint, "CreateQueue", QueueName=name)
        _sqs(queue_url, "SendMessage", MessageBody=body)


def write_log(path, kept):
    """One readable entry per publish: when, who, to which topic, then the message (JSON pretty-printed)."""
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, f"{path}.1")
    except OSError:
        pass
    extras = [f"{name}={kept[key]}" for name, key in (("group", "MessageGroupId"), ("subject", "Subject")) if kept[key]]
    if kept["MessageAttributes"]:
        extras.append("attributes=" + json.dumps(kept["MessageAttributes"], sort_keys=True))
    try:
        message = json.dumps(json.loads(kept["Message"]), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        message = str(kept["Message"])
    header = " ".join([kept["Timestamp"], kept["Service"], "→", kept["Topic"], *extras])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:  # one write per entry: several services may append at once
        fh.write(header + "\n" + "".join(f"  {line}\n" for line in message.splitlines()))


def notification(params, entry=None):
    """What a ``Publish`` (or one ``PublishBatch`` entry) carried, with where and when it was published."""
    source = entry or params
    arn = params.get("TopicArn") or params.get("TargetArn") or params.get("PhoneNumber") or ""
    return {
        "TopicArn": arn,
        "Topic": arn.rsplit(":", 1)[-1] if arn else "(no TopicArn)",
        "Message": source.get("Message", ""),
        "Subject": source.get("Subject", ""),
        "MessageAttributes": _plain(source.get("MessageAttributes", {})),
        "MessageGroupId": source.get("MessageGroupId", ""),
        "MessageDeduplicationId": source.get("MessageDeduplicationId", ""),
        "Service": os.path.basename(os.getcwd()),
        "Timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),  # local, like the other logs
    }


def _capture_params(params, context, **kwargs):
    context[CAPTURED] = dict(params)


def _publish_locally(queue_url, log):
    from botocore.awsrequest import AWSResponse

    def handler(model, context, **kwargs):
        params = context.get(CAPTURED, {})
        is_fifo = (params.get("TopicArn") or "").endswith(".fifo")

        def answer(entry=None):
            kept = notification(params, entry)
            keep(queue_url, kept)
            if log:
                write_log(log, kept)
            message = {"MessageId": str(uuid.uuid4())}
            if is_fifo:
                message["SequenceNumber"] = str(uuid.uuid4().int)[:20]
            return message

        try:
            if model.name == "PublishBatch":
                entries = params.get("PublishBatchRequestEntries", [])
                parsed = {"Successful": [{"Id": e.get("Id"), **answer(e)} for e in entries], "Failed": []}
            else:
                parsed = answer()
        except Exception as error:  # noqa: BLE001 - say why in the service's log, then fail like AWS would
            print(f"[pdms] could not keep the SNS message in {queue_url}: {error}", file=sys.stderr, flush=True)
            return AWSResponse(queue_url, 503, {}, None), {
                "Error": {"Code": "ServiceUnavailable", "Message": f"pdms local SNS: {error}"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            }
        parsed["ResponseMetadata"] = {"HTTPStatusCode": 200, "RequestId": str(uuid.uuid4())}
        return AWSResponse(queue_url, 200, {}, None), parsed

    return handler


def _local_credentials(kwargs, endpoint):
    if not kwargs.get("endpoint_url"):
        kwargs["endpoint_url"] = endpoint
    kwargs.setdefault("region_name", os.environ.get("AWS_REGION") or "us-east-1")
    kwargs["aws_access_key_id"] = "local"
    kwargs["aws_secret_access_key"] = "local"  # nosec B105 - local ElasticMQ, not a real credential
    kwargs.pop("aws_session_token", None)


def _patch() -> None:
    endpoint = os.environ.get("PDMS_SQS_ENDPOINT")
    if not endpoint:
        return
    try:
        import botocore.session
    except ImportError:  # not a boto service
        return
    original = botocore.session.Session.create_client
    if getattr(original, "_pdms_patched", False):
        return

    sns_queue = os.environ.get("PDMS_SNS_QUEUE_URL")
    sns_log = os.environ.get("PDMS_SNS_LOG")

    def create_client(self, service_name, *args, **kwargs):
        if service_name == "sqs" or (service_name == "sns" and sns_queue):
            _local_credentials(kwargs, endpoint)
        client = original(self, service_name, *args, **kwargs)
        if service_name == "sns" and sns_queue:
            for operation in ("Publish", "PublishBatch"):
                client.meta.events.register(f"before-parameter-build.sns.{operation}", _capture_params)
                client.meta.events.register(f"before-call.sns.{operation}", _publish_locally(sns_queue, sns_log))
        return client

    create_client._pdms_patched = True
    botocore.session.Session.create_client = create_client


try:
    _patch()
except Exception:  # noqa: BLE001 - a broken patch must never stop the service from starting
    pass
