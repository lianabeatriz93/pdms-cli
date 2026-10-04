"""Following one request: the proxy's X-Request-Id, the service's logs and SQS messages, the consumer, the trace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pdms_cli import actions, captures, events, trace

ID = "1a2b3c4d"

SERVICE = """
import asyncio, json, logging, sys
import botocore.session
from botocore.stub import Stubber
import starlette.applications as app

lines = []
class Keep(logging.Handler):
    def emit(self, record):
        lines.append(self.format(record))
log = logging.getLogger("svc")
handler = Keep()
handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)

session = botocore.session.get_session()
sqs = session.create_client("sqs", region_name="us-east-1")
sent = []
sqs.meta.events.register_last("before-parameter-build.sqs.SendMessage", lambda params, **kw: sent.append(dict(params)))
stub = Stubber(sqs)
stub.add_response("send_message", {"MessageId": "m1"})
stub.add_response("send_message", {"MessageId": "m2"})
stub.activate()

class Starlette:
    async def __call__(self, scope, receive, send):
        log.info("handling")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
        sqs.send_message(QueueUrl="http://localhost:9/000000000000/broker-sqs-queue.fifo", MessageBody="{}")
app.Starlette = Starlette  # what sitecustomize patched is the class this module had: patch the new one too
import pdms_trace
pdms_trace.follow_requests()

asyncio.run(app.Starlette()({"type": "http", "headers": [(b"x-request-id", b"__ID__")]}, None, None))
log.info("outside")
sqs.send_message(QueueUrl="http://localhost:9/000000000000/other", MessageBody="{}")
print(json.dumps({"lines": lines, "attributes": [s.get("MessageAttributes") for s in sent]}))
""".replace("__ID__", ID)

FAKE_BOTO3 = """
import json
class Client:
    calls = 0
    def receive_message(self, **kwargs):
        Client.calls += 1
        if Client.calls > 1:
            raise KeyboardInterrupt
        return {"Messages": [{"MessageId": "abcdef0123", "ReceiptHandle": "r", "Body": json.dumps({"type": "EMAIL"}),
                "MessageAttributes": {"pdms-request-id": {"DataType": "String", "StringValue": "%s"}}}]}
    def delete_message(self, **kwargs):
        pass
def client(*args, **kwargs):
    return Client()
""" % ID

HANDLER = """
import logging, pdms_trace
logging.basicConfig(level=logging.INFO, format="%(message)s")
def handler(event, context):
    logging.getLogger("consumer").info("got %s", event["Records"][0]["messageAttributes"]["pdms-request-id"]["StringValue"])
    return {"current": pdms_trace.current.get()}
"""


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PDMS_", "PYTHONPATH"))}
    return {**env, "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", **extra}


def test_a_service_logs_the_request_and_passes_it_on_in_sqs(tmp_path) -> None:
    (tmp_path / "starlette").mkdir()
    (tmp_path / "starlette" / "__init__.py").write_text("")
    (tmp_path / "starlette" / "applications.py").write_text("class Starlette:\n    async def __call__(self, *a):\n        pass\n")
    trace_log = tmp_path / "trace.jsonl"
    env = clean_env(PYTHONPATH=os.pathsep.join([str(events.PATCH_DIR), str(tmp_path)]),
                    PDMS_SQS_ENDPOINT="http://localhost:9", PDMS_TRACE_LOG=str(trace_log))
    out = subprocess.run([sys.executable, "-c", SERVICE], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.splitlines()[-1])
    handling, failed, outside = result["lines"][0], result["lines"][1], result["lines"][-1]
    assert handling == f"INFO handling #{ID}"
    assert failed.startswith(f"ERROR failed #{ID}\nTraceback") and failed.endswith("ValueError: boom")
    assert outside == "INFO outside"  # no request: nothing added
    inside, after = result["attributes"]
    assert inside == {"pdms-request-id": {"DataType": "String", "StringValue": ID}}
    assert not after  # sent outside a request: nothing added
    steps = trace.steps(ID, trace_log)
    assert [(s["kind"], s["name"], s["service"]) for s in steps] == [("sqs", "broker-sqs-queue.fifo", tmp_path.name)]
    assert steps[0]["operation"] == "SendMessage" and steps[0]["at"] <= time.time()


def test_without_local_events_the_id_is_not_added_to_real_messages(tmp_path) -> None:
    (tmp_path / "starlette").mkdir()
    (tmp_path / "starlette" / "__init__.py").write_text("")
    (tmp_path / "starlette" / "applications.py").write_text("class Starlette:\n    pass\n")
    env = clean_env(PYTHONPATH=os.pathsep.join([str(events.PATCH_DIR), str(tmp_path)]),
                    PDMS_TRACE_LOG=str(tmp_path / "trace.jsonl"))
    out = subprocess.run([sys.executable, "-c", SERVICE], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.splitlines()[-1])
    assert result["attributes"] == [None, None]  # AWS queues get the message as the service sent it
    assert [s["kind"] for s in trace.steps(ID, tmp_path / "trace.jsonl")] == ["sqs"]  # but the send is timed


def test_the_consumer_works_for_the_request_its_message_was_sent_for(tmp_path) -> None:
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "boto3.py").write_text(FAKE_BOTO3)
    service = tmp_path / "email-notify"
    service.mkdir()
    (service / "main.py").write_text(HANDLER)
    trace_log = tmp_path / "trace.jsonl"
    env = clean_env(PYTHONPATH=os.pathsep.join([str(events.PATCH_DIR), str(fake)]), PDMS_TRACE_LOG=str(trace_log))
    out = subprocess.run([sys.executable, str(events.POLLER), "--queue-url", "http://x/000000000000/email.fifo",
                          "--handler", "main.handler"], env=env, capture_output=True, text=True, cwd=service,
                         timeout=60)
    assert f"Received abcdef01 type=EMAIL #{ID}" in out.stdout, out.stdout + out.stderr
    assert f"Processed abcdef01 type=EMAIL #{ID}" in out.stdout
    assert f"got {ID} #{ID}" in out.stdout + out.stderr  # the handler's own log line
    (step,) = trace.steps(ID, trace_log)
    assert (step["kind"], step["name"], step["function"], step["failed"]) == ("consumer", "email.fifo", "email-notify", 0)


def test_the_proxy_sends_its_id_and_the_detail_has_the_steps(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(trace, "path", lambda: tmp_path / "trace.jsonl")
    lines = [{"id": ID, "kind": "consumer", "service": "email-notify", "name": "q", "at": 20.0, "ms": 5},
             {"id": ID, "kind": "sqs", "service": "lead-tp-update", "name": "broker", "at": 10.0, "ms": 2},
             {"id": "ffffffff", "kind": "sqs", "service": "x", "name": "y", "at": 1.0, "ms": 1},
             {"id": ID, "kind": "weird", "service": "x", "name": "y", "at": 1.0, "ms": 1}]
    (tmp_path / "trace.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n")
    assert [s["service"] for s in trace.steps(ID)] == ["lead-tp-update", "email-notify"]  # by time, known kinds
    assert trace.steps("../etc") == []
    recorder = captures.Recorder(tmp_path / "requests.jsonl")
    recorder.record(captures.entry(ID, "GET", "/api/v1/x", 200, "remote", 0.1, [], None, [], None, started_at=9.5))
    monkeypatch.setattr(captures, "path", lambda: recorder.file)
    monkeypatch.setattr(actions.proxy, "running_proxy", lambda: None)
    detail = actions.proxy_request(ID)
    assert detail["request"]["started_at"] == 9.5 and [s["kind"] for s in detail["steps"]] == ["sqs", "consumer"]


def test_services_started_by_pdms_always_get_the_trace_log(monkeypatch, tmp_path) -> None:
    from pdms_cli.config import Config, Database, Defaults, DevUser

    monkeypatch.setattr(actions, "consumer_of", lambda cfg, service: None)
    monkeypatch.setattr(actions.runner, "port_is_free", lambda host, port: True)
    monkeypatch.setattr(actions.instances, "running_ports", lambda: set())
    cfg = Config(users={"a": DevUser("u", "a@x.com")}, dbs={"local": Database("localhost")},
                 defaults=Defaults(query_stats=False, warm_connections=0))
    launch = actions.plan_service(cfg, Path(tmp_path), user_name="a", db_name="local", parallel=False)
    env = actions.service_env(cfg, launch)
    assert env["PDMS_TRACE_LOG"] == str(trace.path()) and str(events.PATCH_DIR) in env["PYTHONPATH"]
