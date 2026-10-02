"""Publishing SQS events to the local broker."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from pdms_cli import events, runner
from pdms_cli.config import Database, Defaults, DevUser

PROBE = """
import json, botocore.session
session = botocore.session.get_session()
sqs, s3 = session.create_client("sqs", region_name="us-east-1"), session.create_client("s3", region_name="us-east-1")
credentials = sqs._request_signer._credentials
print(json.dumps({"sqs": sqs.meta.endpoint_url, "s3": s3.meta.endpoint_url,
                  "key": credentials.access_key if credentials else None}))
"""


def probe(env: dict[str, str]) -> dict:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PDMS_SQS", "PYTHONPATH"))}
    result = subprocess.run([sys.executable, "-c", PROBE], env={**clean, **env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_patch_sends_only_sqs_clients_to_the_local_endpoint():
    out = probe({"PYTHONPATH": str(events.PATCH_DIR), "PDMS_SQS_ENDPOINT": "http://localhost:9999"})
    assert out["sqs"] == "http://localhost:9999"
    assert out["key"] == "local"  # dummy credentials: no AWS profile or token is used
    assert "amazonaws.com" in out["s3"]


def test_patch_does_nothing_without_the_endpoint_variable():
    out = probe({"PYTHONPATH": str(events.PATCH_DIR), "AWS_ACCESS_KEY_ID": "real", "AWS_SECRET_ACCESS_KEY": "x"})
    assert "amazonaws.com" in out["sqs"] and out["key"] == "real"


def test_local_env_points_the_broker_and_its_destinations_to_elasticmq():
    event_map = events.EventMap(
        broker_queue="broker-sqs-queue.fifo", broker_destinations={"SQS_EMAIL_NOTIFY": "email-send-sqs-queue.fifo"}
    )
    env = events.local_env(event_map, 9324)
    assert env["SQS_EVENT_BROKER_URL"] == "http://localhost:9324/000000000000/broker-sqs-queue.fifo"
    assert env["SQS_EMAIL_NOTIFY"] == "http://localhost:9324/000000000000/email-send-sqs-queue.fifo"
    assert env["PDMS_SQS_ENDPOINT"] == "http://localhost:9324"
    assert env["PYTHONPATH"] == str(events.PATCH_DIR)


def test_build_env_keeps_the_users_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/my/libs")
    env = runner.build_env(Defaults(), DevUser("u", "a@x.com"), Database("localhost"), {"PYTHONPATH": "/patch"})
    assert env["PYTHONPATH"] == f"/patch{os.pathsep}/my/libs"
    assert runner.build_env(Defaults(), DevUser("u", "a@x.com"), Database("localhost"))["PYTHONPATH"] == "/my/libs"


@pytest.mark.parametrize("mode,running,expected", [
    ("auto", False, "aws"), ("auto", True, "local"), ("aws", True, "aws"), ("local", True, "local"),
])
def test_events_mode_decision(tmp_path, monkeypatch, mode, running, expected):
    from pdms_cli import actions
    from pdms_cli.config import Config

    monkeypatch.setattr(events, "running", lambda port: running)
    monkeypatch.setattr(events, "load_event_map", lambda root, env="dev": events.EventMap(broker_queue="b.fifo"))
    (tmp_path / "backend" / "snakesdk").mkdir(parents=True)  # make it look like a PDMS checkout
    cfg = Config()
    cfg.defaults.events = mode
    setup = actions.events_setup(cfg, tmp_path, None)
    assert setup.kind == expected
    assert ("SQS_EVENT_BROKER_URL" in setup.env) == (expected == "local")


# --------------------------------------------------------------------------- SNS

SNS_PROBE = """
import json, botocore.session
from botocore.exceptions import ClientError
sns = botocore.session.get_session().create_client("sns", region_name="us-east-1")
out = {"endpoint": sns.meta.endpoint_url}
arn = "arn:aws:sns:us-east-1:000000000000:sns-account-publish.fifo"
try:
    out["publish"] = sns.publish(
        TopicArn=arn, Message='{"lead_id": 7}', MessageGroupId="7", Subject="terms",
        MessageAttributes={"type": {"DataType": "String", "StringValue": "term-cond"},
                           "raw": {"DataType": "Binary", "BinaryValue": b"ok"}},
    )
    out["empty"] = sns.publish(TopicArn="", Message="no topic")
    out["batch"] = sns.publish_batch(TopicArn=arn, PublishBatchRequestEntries=[
        {"Id": "a", "Message": "one", "MessageGroupId": "g"}, {"Id": "b", "Message": "two", "MessageGroupId": "g"}])
except ClientError as error:
    out["error"] = error.response["Error"]
print(json.dumps(out))
"""


class FakeElasticMQ:
    """Records the query-API calls; answers the first SendMessage as if the queue did not exist yet."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs

        self.calls: list[tuple[str, dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                fields = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                fake.calls.append((self.path, fields))
                missing = fields["Action"] == "SendMessage" and not any(f["Action"] == "CreateQueue" for _p, f in fake.calls)
                self.send_response(400 if missing else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def sns_probe(env: dict[str, str]) -> dict:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PDMS_", "PYTHONPATH"))}
    result = subprocess.run([sys.executable, "-c", SNS_PROBE], env={**clean, **env}, capture_output=True, text=True,
                            cwd=str(events.PATCH_DIR))
    assert result.returncode == 0, result.stderr
    return {**json.loads(result.stdout), "stderr": result.stderr}


def test_every_sns_publish_lands_in_the_one_local_queue(tmp_path):
    elasticmq = FakeElasticMQ()
    queue = f"http://127.0.0.1:{elasticmq.port}/000000000000/{events.SNS_QUEUE}"
    log = tmp_path / "logs" / "sns.log"
    out = sns_probe({"PYTHONPATH": str(events.PATCH_DIR), "PDMS_SQS_ENDPOINT": f"http://127.0.0.1:{elasticmq.port}",
                     "PDMS_SNS_QUEUE_URL": queue, "PDMS_SNS_LOG": str(log)})
    elasticmq.server.shutdown()
    assert "error" not in out, out
    assert out["endpoint"] == f"http://127.0.0.1:{elasticmq.port}"  # never the real AWS
    assert out["publish"]["MessageId"] and out["publish"]["SequenceNumber"]  # a FIFO topic answers like AWS
    assert [entry["Id"] for entry in out["batch"]["Successful"]] == ["a", "b"] and out["batch"]["Failed"] == []

    actions = [fields["Action"] for _path, fields in elasticmq.calls]
    assert actions == ["SendMessage", "CreateQueue", "SendMessage", "SendMessage", "SendMessage", "SendMessage"]
    assert elasticmq.calls[1] == ("/", {"Action": "CreateQueue", "Version": "2012-11-05", "QueueName": "pdms-sns"})
    kept = [json.loads(fields["MessageBody"]) for _path, fields in elasticmq.calls[2:]]
    assert {k: kept[0][k] for k in ("Topic", "TopicArn", "Message", "Subject", "MessageGroupId", "Service")} == {
        "Topic": "sns-account-publish.fifo", "TopicArn": "arn:aws:sns:us-east-1:000000000000:sns-account-publish.fifo",
        "Message": '{"lead_id": 7}', "Subject": "terms", "MessageGroupId": "7", "Service": "sqs_patch",
    }
    assert kept[0]["MessageAttributes"] == {"type": {"DataType": "String", "StringValue": "term-cond"},
                                            "raw": {"DataType": "Binary", "BinaryValue": "b2s="}}
    assert kept[1]["Topic"] == "(no TopicArn)" and kept[1]["Message"] == "no topic"
    assert [k["Message"] for k in kept[2:]] == ["one", "two"]

    # The readable log: a header per publish, then the message (JSON pretty-printed).
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(' sqs_patch → sns-account-publish.fifo group=7 subject=terms attributes='
                             '{"raw": {"BinaryValue": "b2s=", "DataType": "Binary"}, '
                             '"type": {"DataType": "String", "StringValue": "term-cond"}}')
    assert lines[1:4] == ["  {", '    "lead_id": 7', "  }"]
    assert lines[4].endswith(" sqs_patch → (no TopicArn)") and lines[5] == "  no topic"
    assert lines[6].endswith(" sqs_patch → sns-account-publish.fifo group=g") and lines[7] == "  one"


def test_sns_fails_like_aws_when_the_local_queue_is_unreachable():
    out = sns_probe({"PYTHONPATH": str(events.PATCH_DIR), "PDMS_SQS_ENDPOINT": "http://127.0.0.1:9",
                     "PDMS_SNS_QUEUE_URL": "http://127.0.0.1:9/000000000000/pdms-sns"})
    assert out["error"]["Code"] == "ServiceUnavailable" and "pdms local SNS" in out["error"]["Message"]
    assert "[pdms] could not keep the SNS message" in out["stderr"]


def test_sns_is_untouched_without_local_events():
    out = sns_probe({"PYTHONPATH": str(events.PATCH_DIR), "AWS_ACCESS_KEY_ID": "real", "AWS_SECRET_ACCESS_KEY": "x"})
    assert "amazonaws.com" in out["endpoint"]


def test_local_env_names_the_sns_queue_and_each_services_topics():
    event_map = events.EventMap(topic_variables={
        "credential/credential-term-cond-publish-ev": {"SNS_CONTRACT_TERM_AND_COND_PUBLISH_ARN": "sns-account-publish.fifo"},
    })
    env = events.local_env(event_map, 9324)
    assert env["PDMS_SNS_QUEUE_URL"] == "http://localhost:9324/000000000000/pdms-sns"
    assert env["PDMS_SNS_LOG"] == str(events.sns_log_path()) and events.sns_log_path().name == "sns.log"
    assert events.topic_env(event_map, "credential/credential-term-cond-publish-ev") == {
        "SNS_CONTRACT_TERM_AND_COND_PUBLISH_ARN": "arn:aws:sns:us-east-1:000000000000:sns-account-publish.fifo",
    }
    assert events.topic_env(event_map, "lead/lead-tp-list") == {}


def test_pdms_logs_sns_shows_what_was_published(tmp_path, monkeypatch):
    from pdms_cli import cli

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(cli.typer.Exit), cli.console.capture() as captured:
        cli.logs(["sns"], False, None, False, None, False)
    assert "Nothing was published to the local SNS yet." in captured.get()

    events.sns_log_path().parent.mkdir(parents=True)
    events.sns_log_path().write_text("2026-10-01T10:00:00+00:00 lead → sns-account-publish.fifo\n  hi\n", encoding="utf-8")
    with cli.console.capture() as captured:
        cli.logs(["sns"], False, None, False, 5, False)
    assert "lead → sns-account-publish.fifo" in captured.get() and "  hi" in captured.get()


def test_sns_log_opens_json_sent_as_a_string(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pdms_sitecustomize", events.PATCH_DIR / "sitecustomize.py")
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)  # without PDMS_SQS_ENDPOINT it patches nothing
    data = json.dumps({"account": {"name": "Ñandú"}, "items": [json.dumps({"id": 1}), "[not json"], "n": "7"})
    log = tmp_path / "sns.log"
    patch.write_log(str(log), {"Timestamp": "t", "Service": "s", "Topic": "topic", "MessageGroupId": None,
                               "Subject": None, "MessageAttributes": {},
                               "Message": json.dumps({"event": "UPDATED", "data": data})})
    body = "\n".join(line[2:] for line in log.read_text(encoding="utf-8").splitlines()[1:])
    assert json.loads(body) == {"event": "UPDATED", "data": {"account": {"name": "Ñandú"},
                                                             "items": [{"id": 1}, "[not json"], "n": "7"}}
    assert '      "name": "Ñandú"' in body  # pretty-printed at every depth
