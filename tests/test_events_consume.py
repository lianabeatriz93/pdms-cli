"""Event consumers (SQS-triggered Lambdas) run locally by pdms."""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path

from pdms_cli import events, instances

spec = importlib.util.spec_from_file_location("pdms_sqs_poller", events.POLLER)
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

MESSAGE = {
    "MessageId": "0123456789abcdef", "ReceiptHandle": "rh", "Body": '{"type": "email-notify", "x": 1}',
    "Attributes": {"ApproximateReceiveCount": "1"}, "MD5OfBody": "md5",
}


def test_records_look_like_the_aws_sqs_trigger():
    record = poller.to_record(MESSAGE, "email-send-sqs-queue.fifo")
    assert record["messageId"] == "0123456789abcdef" and record["body"] == MESSAGE["Body"]
    assert record["eventSource"] == "aws:sqs"
    assert record["eventSourceARN"].endswith(":email-send-sqs-queue.fifo")
    assert poller.describe(MESSAGE) == "01234567 type=email-notify"
    assert poller.describe({**MESSAGE, "Body": "not json"}) == "01234567 type=?"


def test_partial_batch_failures_are_honoured():
    assert poller.failed_ids({"batchItemFailures": [{"itemIdentifier": "a"}, {"itemIdentifier": "b"}]}) == {"a", "b"}
    assert poller.failed_ids({"batchItemFailures": []}) == set()
    assert poller.failed_ids(None) == set() and poller.failed_ids("ok") == set()


def test_context_counts_down():
    context = poller.Context("email-notify", timeout=60)
    assert context.function_name == "email-notify"
    assert 0 < context.get_remaining_time_in_millis() <= 60_000


def test_consumers_are_keyed_by_queue_and_report_ready_from_their_log(tmp_path):
    assert instances.make_key(Path("/x/email-notify"), 0) == "email-notify@sqs"
    assert instances.make_key(Path("/x/lead-tp-list"), 8080) == "lead-tp-list@8080"
    log = tmp_path / "email-notify@sqs.log"
    log.write_text("# pdms start\n", encoding="utf-8")
    inst = instances.Instance(
        key="email-notify@sqs", pid=os.getpid(), service="/x/email-notify", host="0.0.0.0", port=0, user="u",
        db="d", reload=False, log=str(log), started_at=datetime.now().isoformat(), queue="email-send-sqs-queue.fifo",
    )
    assert inst.is_consumer
    assert instances.health(inst).state == "starting"
    log.write_text("# pdms start\n10:00:00 [pdms-poller] Polling email-send-sqs-queue.fifo -> main.lambda_handler\n",
                   encoding="utf-8")
    assert instances.health(inst).state == "ok"
    with log.open("a", encoding="utf-8") as fh:  # the service's DEBUG output pushes the line far up the log
        fh.write("DEBUG botocore.endpoint Making request for OperationModel(name=ReceiveMessage)\n" * 5000)
    assert instances.health(inst).state == "ok"


def test_queue_of_service_and_poller_command():
    event_map = events.EventMap(
        queues={"q.fifo": events.Queue("q.fifo", visibility_timeout=900)},
        consumers={"q.fifo": events.Consumer("notification/email-notify", "pkg.entry.lambda_handler")},
    )
    queue_name, consumer = event_map.queue_of_service("notification/email-notify")
    assert queue_name == "q.fifo" and event_map.queue_of_service("lead/lead-tp-list") is None
    cmd = events.poller_command("poetry", event_map.queues[queue_name], consumer, 9324)
    assert cmd[:3] == ["poetry", "run", "python"] and cmd[3] == str(events.POLLER)
    assert cmd[cmd.index("--queue-url") + 1] == "http://localhost:9324/000000000000/q.fifo"
    assert cmd[cmd.index("--handler") + 1] == "pkg.entry.lambda_handler"
    assert cmd[cmd.index("--timeout") + 1] == "900"
