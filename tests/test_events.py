"""SQS event map from a repo, ElasticMQ configuration and queue counts."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from pdms_cli import events

TERRAFORM = {
    "sqs_queue_email.tf": """
resource "aws_sqs_queue" "email_notify" {
  name                        = var.sqs_queue_email_notify_name
  fifo_queue                  = true
  visibility_timeout_seconds  = var.sqs_queue_email_notify_visibility_timeout_seconds
}
variable "sqs_queue_email_notify_name" {
  type    = string
  default = "email-send-sqs-queue.fifo"
}
variable "sqs_queue_email_notify_visibility_timeout_seconds" {
  default = 900
}
""",
    "sqs_queue_broker.tf": """
resource "aws_sqs_queue" "broker" {
  name       = var.sqs_queue_broker_name
  fifo_queue = true
}
variable "sqs_queue_broker_name" {
  default = "broker-sqs-queue.fifo"
}
""",
    "lambda_email_notify.tf": """
module "lambda_email_notify" {
  function_name = "email-notify"
  handler       = "email_notify.presentation.entry.lambda_handler"
  lambda_path   = "../../../../backend/notification/email-notify/"
}
resource "aws_lambda_event_source_mapping" "email" {
  function_name    = module.lambda_email_notify.lambda_function_arn
  event_source_arn = aws_sqs_queue.email_notify.arn
}
""",
    "lambda_broker_sqs_event.tf": """
module "lambda_broker_sqs_event" {
  handler     = "main.lambda_handler"
  lambda_path = "../../../../backend/broker/broker-sqs-event/"
  environment_variables = {
    LOGGING_LEVEL    = "DEBUG"
    SQS_EMAIL_NOTIFY = "https://sqs.us-east-1.amazonaws.com/${data.aws_caller_identity.current.account_id}/${aws_sqs_queue.email_notify.name}"
  }
}
resource "aws_lambda_event_source_mapping" "broker" {
  function_name    = module.lambda_broker_sqs_event.lambda_function_arn
  event_source_arn = aws_sqs_queue.broker.arn
}
""",
}

SETTINGS = """
from decouple import config
from event.domain.models import EventType

SQS_EMAIL_NOTIFY = config("SQS_EMAIL_NOTIFY", "")
SQS_NOT_DEPLOYED = config("SQS_NOT_DEPLOYED", "")
EVENT_ROUTE_DEST = {
    EventType.EMAIL_NOTIFICATION.value: SQS_EMAIL_NOTIFY,
    EventType.NOT_DEPLOYED.value: SQS_NOT_DEPLOYED,
}
"""

MODELS = """
from enum import Enum


class EventType(str, Enum):
    EMAIL_NOTIFICATION = "email-notify"
    NOT_DEPLOYED = "not-deployed"
"""

LOCAL_CONF = """
queues {
  "email-notify-sqs-queue.fifo" {
    fifo = true
    defaultVisibilityTimeout = 30 seconds
  }
  # "commented-sqs-queue.fifo" {
  #   fifo = true
  # }
}
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "pdms"
    tf = root / "infra/infra_auto/environments/dev"
    tf.mkdir(parents=True)
    for name, text in TERRAFORM.items():
        (tf / name).write_text(text)
    event = root / "backend/common/event/event"
    (event / "domain").mkdir(parents=True)
    (event / "settings.py").write_text(SETTINGS)
    (event / "domain/models.py").write_text(MODELS)
    (root / "infra/local_sqs").mkdir(parents=True)
    (root / "infra/local_sqs/elasticmq.conf").write_text(LOCAL_CONF)
    for service in ("notification/email-notify", "broker/broker-sqs-event"):
        (root / "backend" / service).mkdir(parents=True)
    return root


def test_event_map_comes_from_terraform_and_code(repo):
    event_map = events.load_event_map(repo)
    email = event_map.queues["email-send-sqs-queue.fifo"]
    assert email.fifo and email.visibility_timeout == 900 and email.source == "terraform"
    assert event_map.consumers["email-send-sqs-queue.fifo"] == events.Consumer(
        "notification/email-notify", "email_notify.presentation.entry.lambda_handler"
    )
    assert event_map.broker_queue == "broker-sqs-queue.fifo"
    assert event_map.broker_destinations == {"SQS_EMAIL_NOTIFY": "email-send-sqs-queue.fifo"}
    assert event_map.routes == {"email-notify": "email-send-sqs-queue.fifo"}  # not-deployed has no queue
    assert event_map.consumer_of_type("email-notify").service == "notification/email-notify"


def test_extra_queues_from_local_sqs_conf(repo):
    queues = events.load_event_map(repo).queues
    assert queues["email-notify-sqs-queue.fifo"].source == "elasticmq.conf"
    assert queues["email-notify-sqs-queue.fifo"].visibility_timeout == 30
    assert "commented-sqs-queue.fifo" not in queues


def test_map_is_cached_until_a_source_file_changes(repo):
    first = events.load_event_map(repo)
    (repo / "infra/infra_auto/environments/dev/sqs_queue_broker.tf").write_text(
        TERRAFORM["sqs_queue_broker.tf"].replace("broker-sqs-queue.fifo", "broker-v2-sqs-queue.fifo") + "\n"
    )
    second = events.load_event_map(repo)
    assert "broker-sqs-queue.fifo" in first.queues and "broker-v2-sqs-queue.fifo" in second.queues


def test_elasticmq_conf_lists_every_queue():
    conf = events.elasticmq_conf({
        "a.fifo": events.Queue("a.fifo", fifo=True, visibility_timeout=900),
        "b": events.Queue("b", fifo=False),
    }, port=9400)
    assert '"a.fifo" {' in conf and "defaultVisibilityTimeout = 900 seconds" in conf
    assert conf.count("fifo = true") == 1
    assert "port = 9400" in conf and "sqs-limits = relaxed" in conf
    assert events.queue_url("a.fifo", 9400) == "http://localhost:9400/000000000000/a.fifo"


NS = 'xmlns="http://queue.amazonaws.com/doc/2012-11-05/"'


class FakeElasticMQ(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        action = parse_qs(url.query)["Action"][0]
        port = self.server.server_address[1]
        if action == "ListQueues":
            body = (f"<ListQueuesResponse {NS}><ListQueuesResult>"
                    f"<QueueUrl>http://localhost:{port}/000000000000/a.fifo</QueueUrl>"
                    f"<QueueUrl>http://localhost:{port}/000000000000/b.fifo</QueueUrl>"
                    "</ListQueuesResult></ListQueuesResponse>")
        else:
            visible = "3" if url.path.rstrip("/").endswith("a.fifo") else "0"
            body = (f"<GetQueueAttributesResponse {NS}><GetQueueAttributesResult>"
                    f"<Attribute><Name>ApproximateNumberOfMessages</Name><Value>{visible}</Value></Attribute>"
                    "<Attribute><Name>ApproximateNumberOfMessagesNotVisible</Name><Value>1</Value></Attribute>"
                    "</GetQueueAttributesResult></GetQueueAttributesResponse>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_queue_counts_parse_the_sqs_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeElasticMQ)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert events.is_up(port)
        assert events.queue_counts(port) == {
            "a.fifo": {"visible": 3, "in_flight": 1}, "b.fifo": {"visible": 0, "in_flight": 1},
        }
    finally:
        server.shutdown()
    assert not events.is_up(port)
