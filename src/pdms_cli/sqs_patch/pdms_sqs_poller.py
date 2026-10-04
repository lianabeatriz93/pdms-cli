"""SQS trigger for a consumer Lambda running locally (started by ``pdms run <consumer>``).

pdms runs this script with the consumer service's own Python (``poetry run python …``) from the service folder, with
the local-events environment (this folder on PYTHONPATH, so its sitecustomize.py sends the SQS client to the local
ElasticMQ). It behaves like the AWS SQS -> Lambda trigger:

* long-polls the queue and calls the handler with ``{"Records": [...]}`` and a Lambda-like context;
* deletes the messages that were processed; honours ``batchItemFailures`` (partial batch responses);
* a failed message comes back after ``RETRY_DELAY`` seconds (instead of the queue's visibility timeout, up to 15
  minutes) and is dropped, with a warning, after ``--max-receives`` attempts, since there is no dead-letter queue
  locally.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime

RETRY_DELAY = 5

try:
    import pdms_trace  # this folder (on PYTHONPATH): the request a message was sent for, see that module
except ImportError:  # pragma: no cover - run without pdms's folder
    pdms_trace = None


def request_of(message: dict) -> str:
    return pdms_trace.from_attributes(message.get("MessageAttributes")) if pdms_trace else ""


def log(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [pdms-poller] {message}", flush=True)


class Context:
    """The attributes handlers usually read from the Lambda context."""

    def __init__(self, function_name: str, timeout: int) -> None:
        self.function_name = function_name
        self.function_version = "$LATEST"
        self.invoked_function_arn = f"arn:aws:lambda:us-east-1:000000000000:function:{function_name}"
        self.memory_limit_in_mb = 512
        self.aws_request_id = str(uuid.uuid4())
        self.log_group_name = f"/aws/lambda/{function_name}"
        self.log_stream_name = "local"
        self._deadline = time.monotonic() + timeout

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int((self._deadline - time.monotonic()) * 1000))


def to_record(message: dict, queue_name: str) -> dict:
    return {
        "messageId": message["MessageId"],
        "receiptHandle": message["ReceiptHandle"],
        "body": message["Body"],
        "attributes": message.get("Attributes", {}),
        "messageAttributes": message.get("MessageAttributes", {}),
        "md5OfBody": message.get("MD5OfBody", ""),
        "eventSource": "aws:sqs",
        "eventSourceARN": f"arn:aws:sqs:us-east-1:000000000000:{queue_name}",
        "awsRegion": "us-east-1",
    }


def describe(message: dict) -> str:
    try:
        body = json.loads(message["Body"])
        kind = body.get("type") or body.get("action_type") or "?"
    except (ValueError, AttributeError):
        kind = "?"
    request = request_of(message)
    return f"{message['MessageId'][:8]} type={kind}" + (f" #{request}" if request else "")


def failed_ids(result: object) -> set[str]:
    if isinstance(result, dict):
        return {f.get("itemIdentifier", "") for f in result.get("batchItemFailures") or []}
    return set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--handler", default="main.lambda_handler")
    parser.add_argument("--function-name", default=os.path.basename(os.getcwd()))
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900, help="Lambda timeout for the context, in seconds")
    parser.add_argument("--max-receives", type=int, default=3)
    args = parser.parse_args()
    queue_name = args.queue_url.rstrip("/").rsplit("/", 1)[-1]

    for stream in (sys.stdout, sys.stderr):  # the log is a file: on Windows it would default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.path.insert(0, os.getcwd())
    module_name, function_name = args.handler.rsplit(".", 1)
    try:
        handler = getattr(importlib.import_module(module_name), function_name)
    except Exception:  # noqa: BLE001 - report and stop: the instance shows as stopped with this traceback
        log(f"Could not load the handler {args.handler}:")
        traceback.print_exc()
        sys.exit(1)

    import boto3

    client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION") or "us-east-1")
    log(f"Polling {queue_name} -> {args.handler} (batch size {args.batch_size})")
    # Our own attempt count: ApproximateReceiveCount also counts `pdms events peek` and other readers.
    attempts: dict[str, int] = {}
    while True:
        try:
            response = client.receive_message(
                QueueUrl=args.queue_url, MaxNumberOfMessages=args.batch_size, WaitTimeSeconds=10,
                AttributeNames=["All"], MessageAttributeNames=["All"],
            )
        except Exception as exc:  # noqa: BLE001 - ElasticMQ restarting, etc.: keep trying
            log(f"Could not read the queue ({exc.__class__.__name__}: {exc}); retrying in {RETRY_DELAY}s")
            time.sleep(RETRY_DELAY)
            continue
        messages = response.get("Messages", [])
        if not messages:
            continue
        for message in messages:
            log(f"Received {describe(message)}")
        started, started_at = time.monotonic(), time.time()
        # The handler works for the request its message was sent for: its logs and what it sends carry the id.
        request = next((r for r in map(request_of, messages) if r), "")
        token = pdms_trace.current.set(request) if pdms_trace and request else None
        try:
            try:
                result = handler({"Records": [to_record(m, queue_name) for m in messages]},
                                 Context(args.function_name, args.timeout))
                failed = failed_ids(result)
            except Exception:  # noqa: BLE001 - a failing invocation fails every message of the batch, like in AWS
                traceback.print_exc()
                failed = {m["MessageId"] for m in messages}
            if token is not None:
                pdms_trace.record("consumer", queue_name, started_at, time.monotonic() - started,
                                  function=args.function_name, messages=len(messages), failed=len(failed))
        finally:
            if token is not None:
                pdms_trace.current.reset(token)
        took = f"{(time.monotonic() - started) * 1000:.0f}ms"
        for message in messages:
            receives = attempts[message["MessageId"]] = attempts.get(message["MessageId"], 0) + 1
            if message["MessageId"] not in failed:
                attempts.pop(message["MessageId"], None)
                client.delete_message(QueueUrl=args.queue_url, ReceiptHandle=message["ReceiptHandle"])
                log(f"Processed {describe(message)} in {took}")
            elif receives >= args.max_receives:
                attempts.pop(message["MessageId"], None)
                client.delete_message(QueueUrl=args.queue_url, ReceiptHandle=message["ReceiptHandle"])
                log(f"FAILED {describe(message)} {receives} times: dropped (there is no dead-letter queue locally)")
            else:
                client.change_message_visibility(QueueUrl=args.queue_url, ReceiptHandle=message["ReceiptHandle"],
                                                  VisibilityTimeout=RETRY_DELAY)
                log(f"FAILED {describe(message)} (attempt {receives}/{args.max_receives}); retrying in {RETRY_DELAY}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
