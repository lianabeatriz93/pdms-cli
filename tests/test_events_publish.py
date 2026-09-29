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
    from pdms_cli import cli
    from pdms_cli.config import Config

    monkeypatch.setattr(events, "running", lambda port: running)
    monkeypatch.setattr(events, "load_event_map", lambda root, env="dev": events.EventMap(broker_queue="b.fifo"))
    (tmp_path / "backend" / "snakesdk").mkdir(parents=True)  # make it look like a PDMS checkout
    cfg = Config()
    cfg.defaults.events = mode
    env, _label, kind = cli.events_for(cfg, tmp_path, None)
    assert kind == expected
    assert ("SQS_EVENT_BROKER_URL" in env) == (expected == "local")
