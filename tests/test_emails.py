"""The emails services send through SES: kept for pdms ui, or sent to one address only (sqs_patch/pdms_email.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pdms_cli import actions, awsenv, emails, events, trace
from pdms_cli.config import Config

# Creates an SES client the way a service does, with a botocore hook in place of AWS: it keeps what would have gone
# to AWS (after pdms changed it) and answers like AWS, or fails when FAIL is set.
PROBE = """
import json, os, sys, botocore.session

sent = []

def fake_aws(model, params, **kwargs):
    sent.append({"operation": model.name, "body": {k: v.decode() if isinstance(v, bytes) else v
                                                   for k, v in params.get("body", {}).items()}})
    if os.environ.get("FAIL"):
        raise RuntimeError("Email address is not verified")
    return None

def answer(**kwargs):
    from botocore.awsrequest import AWSResponse
    return AWSResponse("https://email.us-east-1.amazonaws.com", 200, {}, None), {"MessageId": "from-aws"}

service, operation, params = json.loads(sys.argv[1])
client = botocore.session.get_session().create_client(service, region_name="us-east-1", aws_access_key_id="x",
                                                      aws_secret_access_key="y")
client.meta.events.register("before-call", fake_aws)
client.meta.events.register("before-call", answer)
for key in ("RawMessage",):
    if key in params:
        params[key]["Data"] = params[key]["Data"].encode()
if "Content" in params and "Raw" in params["Content"]:
    params["Content"]["Raw"]["Data"] = params["Content"]["Raw"]["Data"].encode()
try:
    result = getattr(client, operation)(**params)
    error = ""
except Exception as exc:
    result, error = {}, str(exc)
print(json.dumps({"result": {k: v for k, v in result.items() if k != "ResponseMetadata"}, "aws": sent, "error": error}))
"""

SIMPLE = {
    "Source": "no-reply@mail.dev-pdm.alivi.com",
    "Destination": {"ToAddresses": ["juan@cliente.com"], "CcAddresses": ["ana@alivi.com"], "BccAddresses": ["b@x.com"]},
    "Message": {"Subject": {"Data": "Acme has been assigned to you"},
                "Body": {"Text": {"Data": "Hi Juan,\n\nAcme is yours."}}},
}

RAW = (
    "From: no-reply@mail.dev-pdm.alivi.com\r\nTo: Juan <juan@cliente.com>\r\nSubject: Import done\r\n"
    "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n"
    "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nThe roster was imported.\r\n"
    "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p style=\"color:red\">The roster was imported.</p>\r\n--b--\r\n"
)


def send(tmp_path: Path, service: str, operation: str, params: dict, to: str = "", fail: bool = False,
         log: bool = True) -> dict:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PDMS_", "PYTHONPATH"))}
    env = {**clean, "PYTHONPATH": str(events.PATCH_DIR)}
    if log:
        env["PDMS_EMAIL_LOG"] = str(tmp_path / "emails.log")
    if to:
        env["PDMS_EMAIL_TO"] = to
    if fail:
        env["FAIL"] = "1"
    result = subprocess.run([sys.executable, "-c", PROBE, json.dumps([service, operation, params])], env=env,
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def log_of(tmp_path: Path) -> str:
    return (tmp_path / "emails.log").read_text(encoding="utf-8")


def test_without_an_address_the_email_never_reaches_aws_and_shows_in_the_log(tmp_path):
    out = send(tmp_path, "ses", "send_email", SIMPLE)
    assert out["aws"] == [] and out["error"] == ""
    assert out["result"]["MessageId"] and out["result"]["MessageId"] != "from-aws"  # answered like AWS would
    header, *lines = log_of(tmp_path).splitlines()
    assert emails.HEADER.match(header).group(3) == "kept"
    assert "  To: juan@cliente.com" in lines and "  Cc: ana@alivi.com" in lines and "  Bcc: b@x.com" in lines
    assert "  Subject: Acme has been assigned to you" in lines
    assert lines[-3:] == ["  Hi Juan,", "  ", "  Acme is yours."]


def test_with_an_address_it_goes_there_only_with_who_it_was_for_in_the_subject(tmp_path):
    out = send(tmp_path, "ses", "send_email", SIMPLE, to="liana.roget@alivi.com")
    assert out["result"]["MessageId"] == "from-aws"
    [call] = out["aws"]
    body = call["body"]
    assert body["Destination.ToAddresses.member.1"] == "liana.roget@alivi.com"
    assert not any(key.startswith(("Destination.CcAddresses", "Destination.BccAddresses")) for key in body)
    assert "Destination.ToAddresses.member.2" not in body
    assert body["Message.Subject.Data"] == \
        "[to: juan@cliente.com, ana@alivi.com, b@x.com] Acme has been assigned to you"
    text = log_of(tmp_path)
    assert emails.HEADER.match(text.splitlines()[0]).group(3) == "sent"
    assert "  Sent to: liana.roget@alivi.com" in text
    assert "  Subject: Acme has been assigned to you" in text  # the log keeps the email as the service wrote it


def test_a_send_aws_refuses_is_written_as_failed_and_the_service_gets_the_error(tmp_path):
    out = send(tmp_path, "ses", "send_email", SIMPLE, to="liana.roget@alivi.com", fail=True)
    assert "not verified" in out["error"]
    text = log_of(tmp_path)
    assert emails.HEADER.match(text.splitlines()[0]).group(3) == "failed"
    assert "  Error: RuntimeError: Email address is not verified" in text


def test_a_raw_email_shows_its_text_and_html_and_is_redirected_with_its_subject_tagged(tmp_path):
    out = send(tmp_path, "ses", "send_raw_email", {"RawMessage": {"Data": RAW}}, to="me@alivi.com")
    [call] = out["aws"]
    assert call["body"]["Destinations.member.1"] == "me@alivi.com"
    assert "Subject: [to: Juan <juan@cliente.com>] Import done" in __import__("base64").b64decode(
        call["body"]["RawMessage.Data"]).decode()
    text = log_of(tmp_path)
    assert "  The roster was imported." in text and f"  {emails.HTML_MARK}" in text
    ident = emails.HEADER.match(text.splitlines()[0]).group(4)
    assert ident


def test_sesv2_is_kept_too(tmp_path):
    params = {"FromEmailAddress": "a@b.com", "Destination": {"ToAddresses": ["x@y.com"]},
              "Content": {"Simple": {"Subject": {"Data": "Hello"}, "Body": {"Html": {"Data": "<b>Hi</b>"}}}}}
    out = send(tmp_path, "sesv2", "send_email", params)
    assert out["aws"] == [] and out["result"]["MessageId"]
    assert "  <b>Hi</b>" in log_of(tmp_path)


def test_a_bulk_send_never_leaves_even_with_an_address(tmp_path):
    params = {"Source": "a@b.com", "Template": "welcome", "DefaultTemplateData": "{}",
              "Destinations": [{"Destination": {"ToAddresses": ["x@y.com"]}}, {"Destination": {"ToAddresses": ["z@y.com"]}}]}
    out = send(tmp_path, "ses", "send_bulk_templated_email", params, to="me@alivi.com")
    assert out["aws"] == [] and len(out["result"]["Status"]) == 2
    assert "pdms never sends it" in log_of(tmp_path)


def test_without_the_log_variable_nothing_is_watched(tmp_path):
    out = send(tmp_path, "ses", "send_email", SIMPLE, log=False)
    assert len(out["aws"]) == 1 and out["result"]["MessageId"] == "from-aws"
    assert not (tmp_path / "emails.log").exists()


def test_html_of_finds_an_email_by_its_id(tmp_path, monkeypatch):
    log = tmp_path / "emails.log"
    monkeypatch.setattr(emails, "log_path", lambda: log)
    log.write_text(
        "2026-10-04T11:52:04+02:00 email-notify kept 0123456789ab #28106\n  To: a@b.com\n  Subject: Hi\n  \n  text\n"
        f"  \n  {emails.HTML_MARK}\n  <p>hi</p>\n  <p>there</p>\n"
        "2026-10-04T11:52:05+02:00 email-notify kept 0123456789ac\n  To: c@d.com\n  Subject: Hi\n  \n  only text\n",
        encoding="utf-8")
    assert emails.html_of("0123456789ab") == "<p>hi</p>\n<p>there</p>"
    assert emails.html_of("0123456789ac") is None  # no HTML
    assert emails.html_of("ffffffffffff") is None
    assert emails.html_of("../../etc") is None


@pytest.mark.parametrize("value,expected", [("", ""), ("  me@alivi.com ", "me@alivi.com")])
def test_check_email_to_accepts_nothing_or_an_address(value, expected):
    assert actions.check_email_to(value) == expected


@pytest.mark.parametrize("value", ["me", "me@alivi", "a@b.com, c@d.com", "<a@b.com>"])
def test_check_email_to_refuses_anything_else(value):
    with pytest.raises(actions.InvalidValue):
        actions.check_email_to(value)


def test_every_service_keeps_its_emails_and_gets_the_address_when_there_is_one():
    cfg = Config()
    assert emails.env(cfg) == {"PDMS_EMAIL_LOG": str(emails.log_path())}
    cfg.defaults.email_to = "me@alivi.com"
    assert emails.env(cfg)["PDMS_EMAIL_TO"] == "me@alivi.com"


def test_the_lambdas_email_sender_is_kept_but_is_not_a_bucket():
    assert awsenv.KEPT.search("EMAIL_SENDER") and not awsenv.KEPT.search("EMAIL_SENDER_NAME")
    assert awsenv.is_bucket("S3_BUCKET") and not awsenv.is_bucket("S3_BUCKET_REGION")
    assert not awsenv.is_bucket("EMAIL_SENDER")


def test_a_copy_made_by_an_older_pdms_is_read_again():
    saved = {"profile": "pdm-dev", "checked_at": awsenv.now()}
    assert awsenv.due(saved, "pdm-dev")
    assert not awsenv.due({**saved, "kept": awsenv.KEPT.pattern}, "pdm-dev")


def test_the_trace_takes_emails():
    assert "email" in trace.KINDS
