"""pdms events send/peek and consumer debugging."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from pdms_cli import events, vscode

MODELS = """
from enum import Enum
from typing import List


class EventType(str, Enum):
    REPORT_GENERATE_EVENT = "report-generate"
    EMAIL_NOTIFICATION = "email-notify"


class Event(BaseEntity):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    type: EventType


class ReportExportEvent(Event):
    type: EventType = EventType.REPORT_GENERATE_EVENT
    tree_node_id: str
    retry: int = 0
    tags: List[str]
    owner: str | None
"""


def test_event_template_reads_the_event_class(tmp_path):
    path = tmp_path / events.EVENT_MODELS
    path.parent.mkdir(parents=True)
    path.write_text(MODELS, encoding="utf-8")
    assert events.event_template(tmp_path, "report-generate") == {
        "tree_node_id": "", "retry": 0, "tags": [], "owner": None,
    }
    assert events.event_template(tmp_path, "email-notify") is None  # no class for it


def test_event_body_is_what_common_event_publishes():
    body = json.loads(events.event_body("report-generate", {"tree_node_id": "x", "type": "ignored"}))
    assert body["type"] == "report-generate" and body["tree_node_id"] == "x"
    assert body["app_context"] is None and len(body["event_id"]) == 36


class FakeSQS(BaseHTTPRequestHandler):
    sent: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        ns = 'xmlns="http://queue.amazonaws.com/doc/2012-11-05/"'
        if query["Action"] == "SendMessage":
            FakeSQS.sent.append(query)
            body = f"<SendMessageResponse {ns}><SendMessageResult><MessageId>abc123</MessageId></SendMessageResult></SendMessageResponse>"
        else:
            body = (f"<ReceiveMessageResponse {ns}><ReceiveMessageResult><Message><MessageId>m1</MessageId>"
                    "<Body>{\"type\": \"email-notify\"}</Body><Attribute><Name>ApproximateReceiveCount</Name>"
                    "<Value>2</Value></Attribute></Message></ReceiveMessageResult></ReceiveMessageResponse>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_send_and_peek_speak_the_sqs_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSQS)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        assert events.send(port, "q.fifo", '{"a": 1}') == "abc123"
        events.send(port, "q.fifo", '{"a": 1}')
        first, second = FakeSQS.sent[-2:]
        assert first["MessageBody"] == '{"a": 1}' and first["MessageGroupId"]
        assert first["MessageDeduplicationId"] != second["MessageDeduplicationId"]  # identical bodies both arrive
        assert "MessageGroupId" not in (events.send(port, "std", "x", fifo=False) and FakeSQS.sent[-1])
        [message] = events.peek(port, "q.fifo", limit=1)
        assert message["MessageId"] == "m1" and message["Attributes"]["ApproximateReceiveCount"] == "2"
    finally:
        server.shutdown()


def test_consumer_debug_configuration_runs_the_poller(tmp_path):
    service = tmp_path / "backend" / "notification" / "email-notify"
    service.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    program = [str(events.POLLER), "--queue-url", "http://localhost:9324/000000000000/q.fifo"]
    launch, name, _ = vscode.upsert_configuration(
        service, python=tmp_path / "python", env_file=tmp_path / "e.env", host="0.0.0.0", port=0,
        description="sup @ local sqs q.fifo", program=program,
    )
    config = json.loads(launch.read_text(encoding="utf-8"))["configurations"][0]
    assert config["program"] == str(events.POLLER) and config["args"] == program[1:]
    assert "module" not in config and config["cwd"].endswith("backend/notification/email-notify")
